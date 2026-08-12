"""
src/tools/cbs_tool.py

CBS (Centraal Bureau voor de Statistiek) data source tool.
Uses the CBS OData API through the application-owned guarded HTTP transport
for metadata, samples, pagination, and full dataset retrieval.

All configuration comes from the profile — no hardcoded values.
"""

from pathlib import Path
from urllib.parse import urljoin, urlsplit

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    canonical_candidate_identity,
    claims_for_dataset,
    sanitize_url_text,
)
from dataset_prober.resource_classification import (
    InspectionOutcome,
    ResourceClassificationError,
    classify_record_payload,
    error_response_assessment,
    inspection_error_assessment,
    is_error_envelope,
)
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    DatasetResult,
    DataSourceTool,
    load_dataframe_to_table,
)
from dataset_prober.tools.guards import (
    MAX_DATASET_DOWNLOAD_BYTES,
    UnsafeResourceError,
    UnsafeURLError,
    safe_http_get,
)

_CBS_ODATA_BASE = "https://opendata.cbs.nl/ODataApi/odata"
_MAX_ODATA_PAGES = 10_000
_MAX_ODATA_DOWNLOAD_BYTES = MAX_DATASET_DOWNLOAD_BYTES


class CBSTool(DataSourceTool):
    """
    Tool for CBS Statistics Netherlands open data.

    Search: CBS OData Catalog API (fast, scored, filters archived tables)
    Fetch:  CBS OData TypedDataSet with $top (fast, timeout-safe)
    Download: guarded CBS OData pagination followed by authorized DuckDB CTAS
    """

    @property
    def source_name(self) -> str:
        return "CBS Statistics Netherlands"

    @property
    def source_type(self) -> str:
        return "cbs"

    def is_available(self) -> bool:
        return True

    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search CBS OData catalog by keyword.
        Scores results: title match = 2 pts, description match = 1 pt.
        Filters out archived/discontinued tables.
        """
        catalog = self.config.get("base_url", "https://opendata.cbs.nl/ODataCatalog")
        timeout = self.config.get("timeout_seconds", 30)

        try:
            resp = safe_http_get(
                f"{catalog.rstrip('/')}/Tables",
                params={"$format": "json"},
                timeout=timeout,
            )
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
        try:
            self._validate_table_id(dataset_id)
            # Step 1: Table metadata
            meta_resp = safe_http_get(
                f"{_CBS_ODATA_BASE}/{dataset_id}/TableInfos",
                params={"$format": "json"},
                timeout=timeout,
            )
            meta_payload = meta_resp.json()
            if is_error_envelope(meta_payload):
                result = self._error_result(
                    id=dataset_id,
                    title=dataset_id,
                    error="CBS metadata response contains an error envelope",
                )
                result.assessment = error_response_assessment(
                    "CBS metadata response contains an explicit error envelope"
                )
                return result
            meta_values = meta_payload.get("value", [{}])
            meta = meta_values[0] if meta_values else {}
            title = meta.get("Title", dataset_id)
            modified = meta.get("Modified", "")
            modified_display = modified[:10] if modified else None
            summary = meta.get("Summary", "")[:300]

            # Step 2: Sample rows
            sample_resp = safe_http_get(
                f"{_CBS_ODATA_BASE}/{dataset_id}/TypedDataSet",
                params={"$top": int(sample_rows), "$format": "json"},
                timeout=timeout,
            )
            sample_payload = sample_resp.json()
            retrieval_url = f"{_CBS_ODATA_BASE}/{dataset_id}/TypedDataSet?$format=json"
            candidate_identity = canonical_candidate_identity(
                self.source_type,
                self.adapter_identity,
                dataset_id,
                retrieval_url,
            )
            assessment, sample_data = classify_record_payload(
                sample_payload,
                candidate_identity=candidate_identity,
            )
            columns = (
                [{"name": key, "type": "string"} for key in sample_data[0]] if sample_data else []
            )

            return DatasetResult(
                id=dataset_id,
                title=title,
                description=summary,
                source=self.source_type,
                source_name=self.source_name,
                url=f"https://opendata.cbs.nl/statline/#/CBS/nl/dataset/{dataset_id}",
                download_url=retrieval_url,
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
                status=(
                    "probed"
                    if assessment.inspection_outcome is InspectionOutcome.SUCCEEDED
                    else "failed"
                ),
                error=None if assessment.load_eligible else assessment.explanation,
                assessment=assessment,
            )

        except Exception as e:
            result = self._error_result(id=dataset_id, title=dataset_id, error=str(e))
            result.assessment = inspection_error_assessment(e)
            return result

    def download(
        self,
        dataset: DatasetResult,
        destination: str | Path,
        authorization: AuthorizedLoad,
    ) -> DatasetResult:
        """
        Retrieve one full CBS table synchronously through guarded OData pages
        and persist it via pandas inside the authorization lifetime.
        """
        import pandas as pd

        if not isinstance(authorization, AuthorizedLoad):
            raise TypeError("CBS persistent loading requires an AuthorizedLoad")
        actual_claims = claims_for_dataset(dataset, self.adapter_identity, destination)
        with authorization.activate(actual_claims) as permit:
            try:
                data = self._download_odata_rows(
                    actual_claims.retrieval_url,
                    timeout=self.config.get("download_timeout_seconds", 30),
                )
                payload_assessment, data = classify_record_payload(
                    {"value": data},
                    candidate_identity=actual_claims.candidate_identity,
                )
                if not payload_assessment.load_eligible:
                    raise ResourceClassificationError(
                        f"Actual CBS load payload is report-only: {payload_assessment.reason.value}"
                    )

                dataframe = pd.DataFrame(data)
                with AuthorizedDuckDBConnection(permit, destination) as connection:
                    actual_rows = load_dataframe_to_table(connection, dataframe)

                dataset.row_count = actual_rows
                dataset.status = "downloaded"
                return dataset

            except Exception as exc:
                dataset.status = "failed"
                dataset.error = sanitize_url_text(str(exc))
                return dataset

    @staticmethod
    def _validate_table_id(dataset_id: str) -> None:
        if not dataset_id or not all(
            character.isalnum() or character in "_-" for character in dataset_id
        ):
            raise ValueError("CBS table identifier contains unsupported characters")

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return scheme, (parsed.hostname or "").lower(), port

    def _download_odata_rows(self, initial_url: str, *, timeout: float) -> list[dict]:
        """Retrieve CBS OData pages through the guarded transport only."""
        expected_origin = self._origin(initial_url)
        current_url = initial_url
        seen = set()
        rows = []
        downloaded_bytes = 0

        for _page_number in range(_MAX_ODATA_PAGES):
            if current_url in seen:
                raise ValueError("CBS OData pagination loop detected")
            if self._origin(current_url) != expected_origin:
                raise UnsafeURLError("CBS OData pagination changed source origin")
            seen.add(current_url)

            response = safe_http_get(current_url, timeout=timeout)
            if self._origin(response.url) != expected_origin:
                raise UnsafeURLError("CBS OData redirect changed source origin")
            downloaded_bytes += len(response.content)
            if downloaded_bytes > _MAX_ODATA_DOWNLOAD_BYTES:
                raise UnsafeResourceError("CBS OData download exceeds the configured size limit")
            payload = response.json()
            page_rows = payload.get("value")
            if not isinstance(page_rows, list):
                raise ValueError("CBS OData response has no row collection")
            rows.extend(page_rows)

            next_link = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
            if not next_link:
                return rows
            current_url = urljoin(response.url, next_link)

        raise ValueError("CBS OData pagination limit exceeded")

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
