"""
src/tools/cbs_tool.py

CBS (Centraal Bureau voor de Statistiek) data source tool.
Uses the CBS OData API directly for fast metadata and sample fetching.
Uses cbsodata for full dataset downloads.

All configuration comes from the profile — no hardcoded values.
"""

import queue as queue_module
import threading

import requests

from tools.base import (
    DatasetResult,
    DataSourceTool,
    safe_table_name,
)


class CBSTool(DataSourceTool):
    """
    Tool for CBS Statistics Netherlands open data.

    Search: CBS OData Catalog API (fast, scored, filters archived tables)
    Fetch:  CBS OData TypedDataSet with $top (fast, timeout-safe)
    Download: cbsodata.get_data() in a daemon thread with timeout
    """

    @property
    def source_name(self) -> str:
        return "CBS Statistics Netherlands"

    @property
    def source_type(self) -> str:
        return "cbs"

    def is_available(self) -> bool:
        try:
            import cbsodata  # noqa

            return True
        except ImportError:
            return False

    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search CBS OData catalog by keyword.
        Scores results: title match = 2 pts, description match = 1 pt.
        Filters out archived/discontinued tables.
        """
        catalog = self.config.get("base_url", "https://opendata.cbs.nl/ODataCatalog")
        timeout = self.config.get("timeout_seconds", 30)

        try:
            resp = requests.get(f"{catalog}/Tables?$format=json", timeout=timeout)
            resp.raise_for_status()
            all_tables = resp.json().get("value", [])
        except Exception as e:
            return [
                self._error_result(
                    id=f"cbs_search_{keyword}", title=f"CBS search failed: {keyword}", error=str(e)
                )
            ]

        keyword_lower = keyword.lower()
        scored = []

        for table in all_tables:
            title = table.get("Title", "").lower()
            desc = table.get("ShortDescription", "").lower()
            status = table.get("OutputStatus", "").lower()

            # Skip archived/discontinued
            if any(s in status for s in ("archief", "gestopt", "discontinued")):
                continue

            score = 0
            if keyword_lower in title:
                score += 2
            if keyword_lower in desc:
                score += 1

            if score == 0:
                continue

            modified = table.get("Modified", "")
            modified_display = modified[:10] if modified else None

            scored.append(
                (
                    score,
                    DatasetResult(
                        id=table.get("Identifier", ""),
                        title=table.get("Title", ""),
                        description=table.get("ShortDescription", "")[:300],
                        source=self.source_type,
                        source_name=self.source_name,
                        url=f"https://opendata.cbs.nl/statline/#/CBS/nl/dataset/{table.get('Identifier')}",
                        download_url=None,
                        format="OData/CSV",
                        modified=modified_display,
                        frequency=table.get("Frequency"),
                        license="CC-BY",
                        license_url="https://creativecommons.org/licenses/by/4.0/",
                        row_count=None,
                        columns=None,
                        sample=None,
                        language=table.get("Language", "nl"),
                        tags=[],
                        status="found",
                    ),
                )
            )

        # Sort by score desc, then modified desc
        scored.sort(key=lambda x: (x[0], x[1].modified or ""), reverse=True)
        return [r for _, r in scored[:max_results]]

    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Fetch metadata and sample from CBS OData API.
        Uses direct HTTP with timeout — never blocks.
        """
        timeout = self.config.get("timeout_seconds", 30)
        base = "https://opendata.cbs.nl/ODataApi/odata"

        try:
            # Step 1: Table metadata
            meta_resp = requests.get(
                f"{base}/{dataset_id}/TableInfos?$format=json", timeout=timeout
            )
            meta_resp.raise_for_status()
            meta_values = meta_resp.json().get("value", [{}])
            meta = meta_values[0] if meta_values else {}
            title = meta.get("Title", dataset_id)
            modified = meta.get("Modified", "")
            modified_display = modified[:10] if modified else None
            summary = meta.get("Summary", "")[:300]

            # Step 2: Sample rows
            sample_resp = requests.get(
                f"{base}/{dataset_id}/TypedDataSet?$top={sample_rows}&$format=json", timeout=timeout
            )
            sample_resp.raise_for_status()
            sample_data = sample_resp.json().get("value", [])

            if not sample_data:
                return self._error_result(
                    id=dataset_id, title=title, error=f"No sample data returned for {dataset_id}"
                )

            columns = [{"name": k, "type": "string"} for k in sample_data[0].keys()]

            return DatasetResult(
                id=dataset_id,
                title=title,
                description=summary,
                source=self.source_type,
                source_name=self.source_name,
                url=f"https://opendata.cbs.nl/statline/#/CBS/nl/dataset/{dataset_id}",
                download_url=f"{base}/{dataset_id}/TypedDataSet",
                format="OData",
                modified=modified_display,
                frequency=meta.get("Frequency"),
                license="CC-BY",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                row_count=None,  # Unknown until full download
                columns=columns,
                sample=sample_data[:3],
                language="nl",
                tags=[],
                status="probed",
            )

        except requests.Timeout:
            return self._error_result(
                id=dataset_id,
                title=dataset_id,
                error=f"Timed out after {timeout}s — table may be unavailable",
            )
        except Exception as e:
            return self._error_result(id=dataset_id, title=dataset_id, error=str(e))

    def download(self, dataset: DatasetResult, db_path: str) -> DatasetResult:
        """
        Download full CBS table using cbsodata in a daemon thread with timeout.
        Saves to DuckDB via pandas.
        """
        import cbsodata
        import duckdb
        import pandas as pd

        timeout = self.config.get("download_timeout_seconds", 300)
        result_queue = queue_module.Queue()

        def _fetch():
            try:
                data = cbsodata.get_data(dataset.id)
                result_queue.put(("ok", data))
            except Exception as e:
                result_queue.put(("error", str(e)))

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            dataset.status = "failed"
            dataset.error = f"Download timed out after {timeout}s"
            return dataset

        status, payload = result_queue.get()
        if status == "error":
            dataset.status = "failed"
            dataset.error = payload
            return dataset

        data = payload
        if not data:
            dataset.status = "failed"
            dataset.error = "No data returned from CBS"
            return dataset

        table_name = safe_table_name(dataset.id, dataset.title)

        try:
            df = pd.DataFrame(data)  # noqa: F841 -- DuckDB reads 'df' from local scope in the SQL below
            con = duckdb.connect(db_path)
            con.execute(f'CREATE OR REPLACE TABLE "{table_name}" AS SELECT * FROM df')
            actual_rows = con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            con.close()

            dataset.row_count = actual_rows
            dataset.status = "downloaded"
            return dataset

        except Exception as e:
            dataset.status = "failed"
            dataset.error = str(e)
            return dataset

    def _error_result(self, id: str, title: str, error: str) -> DatasetResult:
        """Create a failed DatasetResult."""
        return DatasetResult.failed(
            id=id,
            title=title,
            source=self.source_type,
            source_name=self.source_name,
            error=error,
            language="nl",
        )
