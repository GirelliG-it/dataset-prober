"""
src/tools/tavily_tool.py

Direct-resource portion of the Tavily adapter.

Provider-side search and extraction are disabled for v0.1 because the
application cannot enforce DNS and redirect policy inside that opaque fetch.
"""

import re
from pathlib import Path

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    LoaderKind,
    claims_for_dataset,
    detect_resource_format,
    loader_for_resource,
)
from dataset_prober.tools.base import (
    DatasetResult,
    DataSourceTool,
    download_csv_dataset,
    probe_csv_url,
)
from dataset_prober.tools.guards import safe_download

_PROVIDER_DISABLED = (
    "Tavily provider-side search and extraction are disabled by the v0.1 "
    "URL-safety policy; use a direct supported public resource URL"
)


class TavilyTool(DataSourceTool):
    """
    Web search and page extraction tool using Tavily AI.

    Search/extraction: disabled by the v0.1 URL-safety policy
    Direct fetch: guarded temporary retrieval followed by local DuckDB probing
    Download: guarded temporary retrieval followed by authorized local loading

    It is not registered as an agentic fallback while provider-side fetching
    remains disabled. Exact direct CSV URLs can still use the guarded path.
    """

    @property
    def source_name(self) -> str:
        return "Web Search (Tavily)"

    @property
    def source_type(self) -> str:
        return "tavily"

    def is_available(self) -> bool:
        """Provider-side fetching is intentionally unavailable for v0.1."""
        return False

    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """Fail explicitly because provider-side Tavily search is disabled."""
        return [
            self._error_result(
                id=f"tavily_search_{keyword}",
                title=f"Web search disabled: {keyword}",
                error=_PROVIDER_DISABLED,
            )
        ]

    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Probe direct supported resources locally; reject provider extraction.
        """
        timeout = self.config.get("timeout_seconds", 30)
        blocked_sources = self.config.get("blocked_sources", [])

        # Check blocked
        if any(blocked in dataset_id for blocked in blocked_sources):
            return self._error_result(
                id=dataset_id, title=dataset_id, error="Source is blocked in current profile"
            )

        # If it's already a direct dataset URL, probe it
        if self._is_dataset_url(dataset_id):
            return self._probe_direct(dataset_id, sample_rows, timeout)

        return self._error_result(id=dataset_id, title=dataset_id, error=_PROVIDER_DISABLED)

    def _probe_direct(self, url: str, sample_rows: int, timeout: int) -> DatasetResult:
        """Safely retrieve a direct CSV and probe its temporary local copy."""
        resource_format = detect_resource_format(url)
        if loader_for_resource(self.source_type, resource_format, url) is not LoaderKind.DUCKDB_CSV:
            return self._unsupported_direct_result(url, resource_format)

        try:
            import duckdb

            with safe_download(url, timeout=timeout) as fetched:
                con = duckdb.connect()
                try:
                    probe = probe_csv_url(con, fetched.path, sample_rows)
                finally:
                    con.close()

            # Extract filename as title
            title = url.split("/")[-1].split("?")[0] or url

            return DatasetResult(
                id=url,
                title=title,
                description=f"Direct CSV file from {url}",
                source=self.source_type,
                source_name=self.source_name,
                url=url,
                download_url=url,
                format=resource_format,
                modified=None,
                frequency=None,
                license=None,
                license_url=None,
                row_count=probe["row_count"],
                columns=probe["columns"],
                sample=probe["sample"][:3],
                language=None,
                tags=[],
                status="probed",
            )

        except Exception as e:
            return self._error_result(id=url, title=url, error=f"Probe failed: {str(e)}")

    def download(
        self,
        dataset: DatasetResult,
        destination: str | Path,
        authorization: AuthorizedLoad,
    ) -> DatasetResult:
        """Safely retrieve and load one authorized CSV dataset into DuckDB."""
        if not isinstance(authorization, AuthorizedLoad):
            raise TypeError("Tavily persistent loading requires an AuthorizedLoad")
        actual_claims = claims_for_dataset(dataset, self.adapter_identity, destination)
        with authorization.activate(actual_claims) as permit:
            return download_csv_dataset(dataset, self.adapter_identity, destination, permit)

    def _is_dataset_url(self, url: str) -> bool:
        """Check if URL points directly to a dataset file."""
        return detect_resource_format(url) is not None

    def _detect_format(self, url: str) -> str | None:
        """Detect file format from URL extension."""
        return detect_resource_format(url)

    def _find_dataset_urls(self, content: str) -> list[str]:
        """Extract direct dataset file URLs from page content."""
        pattern = r'https?://[^\s<>"\']+?\.(?:csv|xlsx|xls|json|parquet|geojson)'
        found = re.findall(pattern, content, re.IGNORECASE)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for url in found:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def _error_result(self, id: str, title: str, error: str) -> DatasetResult:
        """Create a failed DatasetResult."""
        return DatasetResult.failed(
            id=id,
            title=title,
            source=self.source_type,
            source_name=self.source_name,
            error=error,
            url=id,
        )

    def _unsupported_direct_result(self, url: str, resource_format: str | None) -> DatasetResult:
        """Retain truthful metadata while refusing an unsupported CSV probe."""
        title = url.split("/")[-1].split("?")[0] or url
        return DatasetResult(
            id=url,
            title=title,
            description="",
            source=self.source_type,
            source_name=self.source_name,
            url=url,
            download_url=url,
            format=resource_format,
            modified=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
            status="failed",
            error=(
                f"Unsupported or unproven Tavily resource format: {resource_format or 'unknown'}"
            ),
        )
