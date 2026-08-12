"""
src/tools/ckan_tool.py

Generic CKAN data source tool.
Works with any CKAN-based catalog: data.gov, data.overheid.nl, EU Open Data Portal.
All catalog-specific settings come from the profile configuration.

CKAN API reference: https://docs.ckan.org/en/latest/api/
"""

from pathlib import Path

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    LoaderKind,
    canonical_candidate_identity,
    claims_for_dataset,
    detect_resource_format,
    loader_for_resource,
    sanitize_url_text,
)
from dataset_prober.resource_classification import (
    InspectionOutcome,
    error_response_assessment,
    inspection_error_assessment,
    inspection_failed_assessment,
    unsupported_format_assessment,
)
from dataset_prober.tools.base import (
    DatasetResult,
    DataSourceTool,
    download_csv_dataset,
    inspect_csv_resource,
)
from dataset_prober.tools.guards import safe_download, safe_http_get

# Known license mappings from CKAN license_id to standardized names
LICENSE_MAP = {
    "cc-zero": "CC0",
    "cc-by": "CC-BY",
    "cc-by-sa": "CC-BY-SA",
    "cc-by-nc": "CC-BY-NC",
    "odc-pddl": "CC0",
    "odc-odbl": "CC-BY-SA",
    "us-pd": "US-PD",
    "other-pd": "CC0",
    "notspecified": "unknown",
    "other-license-specified": "other",
}


class CKANTool(DataSourceTool):
    """
    Tool for any CKAN-based open data catalog.

    Configured via profile — base_url and api_key_env vary per catalog:
      - data.gov:        https://api.gsa.gov/technology/datagov/v3
      - data.overheid.nl: https://data.overheid.nl
      - EU portal:       https://data.europa.eu/api/hub/search

    Search: CKAN package_search API
    Fetch:  guarded CKAN package_show API + guarded local CSV probe
    Download: guarded temporary retrieval + authorized local DuckDB scan
    """

    @property
    def source_name(self) -> str:
        return self.config.get("name", "CKAN Catalog")

    @property
    def source_type(self) -> str:
        return "ckan"

    def _headers(self) -> dict:
        """Build request headers including API key if configured."""
        headers = {"Content-Type": "application/json"}
        api_key_env = self.config.get("api_key_env")
        if api_key_env:
            import os

            key = os.environ.get(api_key_env)
            if key:
                headers["x-api-key"] = key
        return headers

    def _base_url(self) -> str:
        return self.config.get("base_url", "").rstrip("/")

    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search CKAN catalog using package_search endpoint.
        Filters for CSV resources only.
        """
        timeout = self.config.get("timeout_seconds", 30)
        url = f"{self._base_url()}/action/package_search"

        try:
            resp = safe_http_get(
                url,
                params={
                    "q": keyword,
                    "rows": max_results * 2,  # Fetch extra, filter down
                    "fq": "res_format:CSV",
                    "sort": "metadata_modified desc",
                },
                headers=self._headers(),
                timeout=timeout,
            )
            data = resp.json()

            if not data.get("success"):
                return [
                    self._error_result(
                        id=f"ckan_search_{keyword}",
                        title=f"CKAN search failed: {keyword}",
                        error=data.get("error", {}).get("message", "Unknown error"),
                    )
                ]

            results = []
            for pkg in data["result"]["results"][:max_results]:
                result = self._package_to_result(pkg)
                if result:
                    results.append(result)

            return results

        except Exception as e:
            return [
                self._error_result(
                    id=f"ckan_search_{keyword}", title=f"CKAN search error: {keyword}", error=str(e)
                )
            ]

    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Fetch full metadata for a CKAN package and probe its CSV resource.
        """
        timeout = self.config.get("timeout_seconds", 30)
        url = f"{self._base_url()}/action/package_show"

        try:
            resp = safe_http_get(
                url, params={"id": dataset_id}, headers=self._headers(), timeout=timeout
            )
            data = resp.json()

            if not data.get("success"):
                result = self._error_result(
                    id=dataset_id, title=dataset_id, error="Package not found"
                )
                result.assessment = error_response_assessment(
                    "CKAN package response contains an explicit error envelope"
                )
                return result

            pkg = data["result"]
            result = self._package_to_result(pkg)
            if not result.download_url:
                result.error = "No supported CSV resources found in package"
                result.assessment = unsupported_format_assessment(None)
                return result

            # Probe the CSV if we have a direct URL
            if result.download_url:
                result = self._probe_csv(result, sample_rows, timeout)

            return result

        except Exception as e:
            result = self._error_result(id=dataset_id, title=dataset_id, error=str(e))
            result.assessment = inspection_failed_assessment(
                "CKAN package retrieval and inspection did not complete"
            )
            return result

    def _probe_csv(self, result: DatasetResult, sample_rows: int, timeout: int) -> DatasetResult:
        """Safely retrieve a CSV and probe its temporary local copy with DuckDB."""
        admission_error = self._csv_admission_error(result)
        if admission_error:
            result.status = "found"
            result.error = admission_error
            result.assessment = unsupported_format_assessment(result.format)
            return result

        try:
            import duckdb

            candidate_identity = canonical_candidate_identity(
                self.source_type,
                self.adapter_identity,
                result.id,
                result.download_url,
            )
            with safe_download(result.download_url, timeout=timeout) as fetched:
                con = duckdb.connect()
                try:
                    probe = inspect_csv_resource(
                        con,
                        fetched,
                        sample_rows,
                        candidate_identity=candidate_identity,
                    )
                finally:
                    con.close()

            result.columns = probe["columns"]
            result.sample = probe["sample"][:3]
            result.row_count = probe["row_count"]
            result.assessment = probe["assessment"]
            result.status = (
                "probed"
                if result.assessment.inspection_outcome is InspectionOutcome.SUCCEEDED
                else "failed"
            )
            result.error = (
                None if result.assessment.load_eligible else result.assessment.explanation
            )
            return result

        except Exception as e:
            result.status = "failed"
            result.row_count = None
            result.columns = None
            result.sample = None
            result.error = sanitize_url_text(f"Probe failed: {str(e)[:100]}")
            result.assessment = inspection_error_assessment(e)
            return result

    def download(
        self,
        dataset: DatasetResult,
        destination: str | Path,
        authorization: AuthorizedLoad,
    ) -> DatasetResult:
        """Safely retrieve and load one authorized CSV dataset into DuckDB."""
        if not isinstance(authorization, AuthorizedLoad):
            raise TypeError("CKAN persistent loading requires an AuthorizedLoad")
        actual_claims = claims_for_dataset(dataset, self.adapter_identity, destination)
        with authorization.activate(actual_claims) as permit:
            return download_csv_dataset(dataset, self.adapter_identity, destination, permit)

    def _package_to_result(self, pkg: dict) -> DatasetResult | None:
        """Convert a CKAN package dict to a DatasetResult."""
        # Find best CSV resource
        resources = pkg.get("resources", [])
        csv_resource = None
        for r in resources:
            fmt = r.get("format", "").upper()
            if fmt in ("CSV", "TEXT/CSV", "CSV/ZIP"):
                csv_resource = r
                break

        # Parse dates
        modified = pkg.get("metadata_modified", "")
        modified_display = modified[:10] if modified else None

        # Normalize license
        license_id = pkg.get("license_id", "notspecified")
        license_name = LICENSE_MAP.get(license_id.lower(), "other")
        license_url = pkg.get("license_url")

        # Organization
        org = pkg.get("organization", {})
        org_name = org.get("title", "") if org else ""

        # Tags
        tags = [t.get("name", "") for t in pkg.get("tags", [])]

        # Description
        notes = pkg.get("notes", "") or ""
        description = notes[:300]

        resource_format = None
        if csv_resource:
            catalog_format = csv_resource.get("format", "").upper()
            resource_format = "CSV" if catalog_format in ("CSV", "TEXT/CSV") else catalog_format

        return DatasetResult(
            id=pkg.get("name", pkg.get("id", "")),
            title=pkg.get("title", ""),
            description=description,
            source=self.source_type,
            source_name=f"{self.source_name} ({org_name})" if org_name else self.source_name,
            url=f"https://catalog.data.gov/dataset/{pkg.get('name', '')}",
            download_url=csv_resource.get("url") if csv_resource else None,
            format=resource_format,
            modified=modified_display,
            frequency=self._parse_frequency(pkg),
            license=license_name,
            license_url=license_url,
            row_count=None,
            columns=None,
            sample=None,
            language=pkg.get("language", "en"),
            tags=tags,
            status="found",
        )

    def _parse_frequency(self, pkg: dict) -> str | None:
        """Extract update frequency from CKAN extras."""
        extras = pkg.get("extras", [])
        for extra in extras:
            if extra.get("key") == "accrualPeriodicity":
                freq = extra.get("value", "")
                # Convert ISO 8601 duration to human-readable
                freq_map = {
                    "R/P1Y": "annual",
                    "R/P6M": "semi-annual",
                    "R/P3M": "quarterly",
                    "R/P1M": "monthly",
                    "R/P1W": "weekly",
                    "R/P1D": "daily",
                    "R/PT1H": "hourly",
                    "R/P2W": "bi-weekly",
                }
                return freq_map.get(freq, freq)
        return None

    def _csv_admission_error(self, dataset: DatasetResult) -> str | None:
        """Reject catalog labels that contradict or cannot prove the resource format."""
        loader = loader_for_resource(self.source_type, dataset.format, dataset.download_url or "")
        if loader is LoaderKind.DUCKDB_CSV:
            return None
        detected_format = detect_resource_format(dataset.download_url or "")
        declared_format = dataset.format.strip().upper() if dataset.format else None
        return (
            "Unsupported or unproven CKAN resource format: "
            f"{detected_format or declared_format or 'unknown'}"
        )

    def _error_result(self, id: str, title: str, error: str) -> DatasetResult:
        """Create a failed DatasetResult."""
        return DatasetResult.failed(
            id=id,
            title=title,
            source=self.source_type,
            source_name=self.source_name,
            error=error,
            language="en",
        )
