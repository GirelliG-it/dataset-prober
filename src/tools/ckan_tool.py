"""
src/tools/ckan_tool.py

Generic CKAN data source tool.
Works with any CKAN-based catalog: data.gov, data.overheid.nl, EU Open Data Portal.
All catalog-specific settings come from the profile configuration.

CKAN API reference: https://docs.ckan.org/en/latest/api/
"""

import requests
from datetime import datetime
from pathlib import Path

from tools.base import DataSourceTool, DatasetResult


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
    Fetch:  CKAN package_show API + direct CSV probe via DuckDB httpfs
    Download: DuckDB httpfs read_csv_auto
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
            resp = requests.get(
                url,
                params={
                    "q": keyword,
                    "rows": max_results * 2,  # Fetch extra, filter down
                    "fq": "res_format:CSV",
                    "sort": "metadata_modified desc"
                },
                headers=self._headers(),
                timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                return [self._error_result(
                    id=f"ckan_search_{keyword}",
                    title=f"CKAN search failed: {keyword}",
                    error=data.get("error", {}).get("message", "Unknown error")
                )]

            results = []
            for pkg in data["result"]["results"][:max_results]:
                result = self._package_to_result(pkg)
                if result:
                    results.append(result)

            return results

        except requests.Timeout:
            return [self._error_result(
                id=f"ckan_search_{keyword}",
                title=f"CKAN search timed out: {keyword}",
                error=f"Request timed out after {timeout}s"
            )]
        except Exception as e:
            return [self._error_result(
                id=f"ckan_search_{keyword}",
                title=f"CKAN search error: {keyword}",
                error=str(e)
            )]

    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Fetch full metadata for a CKAN package and probe its CSV resource.
        """
        timeout = self.config.get("timeout_seconds", 30)
        url = f"{self._base_url()}/action/package_show"

        try:
            resp = requests.get(
                url,
                params={"id": dataset_id},
                headers=self._headers(),
                timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                return self._error_result(
                    id=dataset_id,
                    title=dataset_id,
                    error="Package not found"
                )

            pkg = data["result"]
            result = self._package_to_result(pkg)
            if not result:
                return self._error_result(
                    id=dataset_id,
                    title=dataset_id,
                    error="No CSV resources found in package"
                )

            # Probe the CSV if we have a direct URL
            if result.download_url:
                result = self._probe_csv(result, sample_rows, timeout)

            return result

        except Exception as e:
            return self._error_result(id=dataset_id, title=dataset_id, error=str(e))

    def _probe_csv(
        self,
        result: DatasetResult,
        sample_rows: int,
        timeout: int
    ) -> DatasetResult:
        """Probe a CSV URL using DuckDB httpfs."""
        try:
            import duckdb
            con = duckdb.connect()
            con.execute("INSTALL httpfs; LOAD httpfs;")

            # Get column info
            describe = con.execute(
                f"DESCRIBE SELECT * FROM read_csv_auto('{result.download_url}') LIMIT 1"
            ).fetchall()
            columns = [{"name": row[0], "type": row[1]} for row in describe]

            # Get sample
            sample_rows_data = con.execute(
                f"SELECT * FROM read_csv_auto('{result.download_url}') LIMIT {sample_rows}"
            ).fetchall()

            # Get row count (may be slow for large files — use with care)
            try:
                count = con.execute(
                    f"SELECT COUNT(*) FROM read_csv_auto('{result.download_url}')"
                ).fetchone()[0]
                result.row_count = count
            except Exception:
                pass  # Row count is optional

            con.close()

            result.columns = columns
            result.sample = [list(row) for row in sample_rows_data[:3]]
            result.status = "probed"
            return result

        except Exception as e:
            # Probe failed — still return the result with found status
            result.status = "found"
            result.error = f"Probe failed: {str(e)[:100]}"
            return result

    def download(self, dataset: DatasetResult, db_path: str) -> DatasetResult:
        """
        Download a CSV dataset into DuckDB using httpfs.
        """
        if not dataset.download_url:
            dataset.status = "failed"
            dataset.error = "No download URL available"
            return dataset

        try:
            import duckdb

            # Create safe table name
            table_name = dataset.title.lower()
            table_name = "".join(c if c.isalnum() else "_" for c in table_name)
            table_name = table_name.strip("_")[:50]

            con = duckdb.connect(db_path)
            con.execute("INSTALL httpfs; LOAD httpfs;")
            con.execute(f"""
                CREATE OR REPLACE TABLE "{table_name}" AS
                SELECT * FROM read_csv_auto('{dataset.download_url}')
            """)
            actual_rows = con.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone()[0]
            con.close()

            dataset.row_count = actual_rows
            dataset.status = "downloaded"
            return dataset

        except Exception as e:
            dataset.status = "failed"
            dataset.error = str(e)
            return dataset

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

        return DatasetResult(
            id=pkg.get("name", pkg.get("id", "")),
            title=pkg.get("title", ""),
            description=description,
            source=self.source_type,
            source_name=f"{self.source_name} ({org_name})" if org_name else self.source_name,
            url=f"https://catalog.data.gov/dataset/{pkg.get('name', '')}",
            download_url=csv_resource.get("url") if csv_resource else None,
            format="CSV" if csv_resource else None,
            modified=modified_display,
            frequency=self._parse_frequency(pkg),
            license=license_name,
            license_url=license_url,
            row_count=None,
            columns=None,
            sample=None,
            language=pkg.get("language", "en"),
            tags=tags,
            status="found"
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

    def _error_result(self, id: str, title: str, error: str) -> DatasetResult:
        """Create a failed DatasetResult."""
        return DatasetResult(
            id=id,
            title=title,
            description="",
            source=self.source_type,
            source_name=self.source_name,
            url="",
            download_url=None,
            format=None,
            modified=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language="en",
            tags=[],
            status="failed",
            error=error
        )
