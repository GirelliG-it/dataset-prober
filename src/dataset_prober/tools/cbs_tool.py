"""
src/tools/cbs_tool.py

CBS (Centraal Bureau voor de Statistiek) data source tool.
Uses the CBS OData API directly for fast metadata and sample fetching.
Uses cbsodata for full dataset downloads.

All configuration comes from the profile — no hardcoded values.
"""

from pathlib import Path

import requests

from dataset_prober.loading_policy import AuthorizedLoad, claims_for_dataset
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    DatasetResult,
    DataSourceTool,
    load_dataframe_to_table,
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

    def download(
        self,
        dataset: DatasetResult,
        destination: str | Path,
        authorization: AuthorizedLoad,
    ) -> DatasetResult:
        """
        Retrieve one full CBS table synchronously and persist it via pandas.

        Synchronous retrieval keeps the load attempt inside the authorization
        lifetime because cbsodata does not expose a cancellable timeout.
        """
        import cbsodata
        import pandas as pd

        if not isinstance(authorization, AuthorizedLoad):
            raise TypeError("CBS persistent loading requires an AuthorizedLoad")
        actual_claims = claims_for_dataset(dataset, self.adapter_identity, destination)
        with authorization.activate(actual_claims) as permit:
            try:
                # cbsodata exposes no cancellable timeout. Keeping this call
                # synchronous ensures retrieval cannot outlive its active permit.
                data = cbsodata.get_data(actual_claims.resource_id)
                if not data:
                    raise ValueError("No data returned from CBS")

                dataframe = pd.DataFrame(data)
                with AuthorizedDuckDBConnection(permit, destination) as connection:
                    actual_rows = load_dataframe_to_table(connection, dataframe)

                dataset.row_count = actual_rows
                dataset.status = "downloaded"
                return dataset

            except Exception as exc:
                dataset.status = "failed"
                dataset.error = str(exc)
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
