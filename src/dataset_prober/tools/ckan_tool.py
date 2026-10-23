"""
src/tools/ckan_tool.py

CKAN data source tool for the explicitly declared closed route dialects.
Catalog-specific API and landing origins come from validated profile configuration.

CKAN API reference: https://docs.ckan.org/en/latest/api/
"""

from pathlib import Path
from urllib.parse import quote, urlencode

from dataset_prober.freshness_observation import (
    FreshnessObservation,
    TimestampClaim,
    TimestampSemantics,
    utc_now,
)
from dataset_prober.loading_policy import (
    AuthorizedLoad,
    LoaderKind,
    canonical_candidate_identity,
    claims_for_dataset,
    detect_resource_format,
    loader_for_resource,
    safe_url_identity,
    sanitize_url_text,
)
from dataset_prober.profile_contract import CKANDialect, CKANSearchMode
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
    RemainingTimeProvider,
    RunDeadlineExceeded,
    bounded_source_timeout,
    download_csv_dataset,
    inspect_csv_resource,
)
from dataset_prober.tools.guards import (
    UnsafeResourceError,
    UnsafeURLError,
    safe_download,
    safe_http_get,
)

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

_CKAN_DIALECT_ROUTES: dict[CKANDialect, tuple[str, str, str]] = {
    CKANDialect.CKAN_ACTION: (
        "action/package_search",
        "action/package_show",
        "dataset",
    ),
    CKANDialect.EU_HUB: (
        "ckan/package_search",
        "ckan/package_show",
        "data/datasets",
    ),
}

_EU_AUTHORITY_CSV_FORMAT = "http://publications.europa.eu/resource/authority/file-type/CSV"


class CKANTool(DataSourceTool):
    """
    Tool for CKAN catalogs using one of the declared closed route dialects.

    Configured via profile — base_url and api_key_env vary per catalog:
      - data.gov:        https://api.gsa.gov/technology/datagov/v3
      - data.overheid.nl: https://data.overheid.nl/data/api/3
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

    def _route_configuration(self) -> tuple[str, str, tuple[str, str, str]]:
        """Return one validated closed CKAN route shape without network access."""

        raw_dialect = self.config.get("ckan_dialect")
        try:
            dialect = CKANDialect(raw_dialect)
        except (TypeError, ValueError) as exc:
            raise ValueError("CKAN route configuration requires a supported ckan_dialect") from exc

        api_base = self.config.get("base_url")
        landing_base = self.config.get("landing_base_url")
        if (
            not isinstance(api_base, str)
            or not api_base.strip()
            or api_base != api_base.strip()
            or not isinstance(landing_base, str)
            or not landing_base.strip()
            or landing_base != landing_base.strip()
        ):
            raise ValueError("CKAN route configuration requires API and landing base URLs")

        return (
            api_base.rstrip("/"),
            landing_base.rstrip("/"),
            _CKAN_DIALECT_ROUTES[dialect],
        )

    def _search_mode(self) -> CKANSearchMode:
        """Return one validated closed package-search filtering strategy."""

        raw_mode = self.config.get("ckan_search_mode")
        try:
            return CKANSearchMode(raw_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "CKAN search configuration requires a supported ckan_search_mode"
            ) from exc

    def _resource_selection_mode(self) -> CKANSearchMode:
        """Return the mode governing package resource selection.

        Standalone fetch and conversion callers predate the required profile field,
        so an absent mode retains the established server-literal selection behavior.
        Explicit but invalid values still fail closed through ``_search_mode``.
        """

        if "ckan_search_mode" not in self.config:
            return CKANSearchMode.SERVER_LITERAL_CSV
        return self._search_mode()

    @staticmethod
    def _package_identifier(pkg: dict) -> str:
        """Select and validate one stable CKAN identifier for identity and landing URLs."""

        identifier = None
        for field in ("name", "id"):
            value = pkg.get(field)
            if isinstance(value, str) and value.strip():
                identifier = value
                break
        if identifier is None or identifier in {".", ".."}:
            raise ValueError("CKAN package requires a non-special stable identifier")
        return identifier

    def search(
        self,
        keyword: str,
        max_results: int,
        *,
        remaining_time: RemainingTimeProvider | None = None,
    ) -> list[DatasetResult]:
        """
        Search CKAN catalog using one declared bounded package-search strategy.
        """
        source_timeout = self.config.get("timeout_seconds", 30)
        search_mode = self._search_mode()
        api_base, _landing_base, (search_path, _show_path, _landing_path) = (
            self._route_configuration()
        )
        url = f"{api_base}/{search_path}"
        params = {
            "q": keyword,
            "rows": max_results * 2,  # Bounded over-fetch for local metadata filtering
            "sort": "metadata_modified desc",
        }
        if search_mode is CKANSearchMode.SERVER_LITERAL_CSV:
            params["fq"] = "res_format:CSV"

        try:
            resp = safe_http_get(
                url,
                params=params,
                headers=self._headers(),
                timeout=bounded_source_timeout(source_timeout, remaining_time),
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
            for pkg in data["result"]["results"][: max_results * 2]:
                result = self._package_to_result(pkg, search_mode=search_mode)
                if search_mode is CKANSearchMode.LOCAL_RESOURCE_METADATA and (
                    result.format != "CSV"
                    or not isinstance(result.download_url, str)
                    or not result.download_url.strip()
                ):
                    continue
                results.append(result)
                if len(results) == max_results:
                    break

            return results

        except RunDeadlineExceeded:
            raise
        except Exception as e:
            return [
                self._error_result(
                    id=f"ckan_search_{keyword}", title=f"CKAN search error: {keyword}", error=str(e)
                )
            ]

    def fetch(
        self,
        dataset_id: str,
        sample_rows: int,
        *,
        remaining_time: RemainingTimeProvider | None = None,
    ) -> DatasetResult:
        """
        Fetch full metadata for a CKAN package and probe its CSV resource.
        """
        source_timeout = self.config.get("timeout_seconds", 30)
        api_base, _landing_base, (_search_path, show_path, _landing_path) = (
            self._route_configuration()
        )
        url = f"{api_base}/{show_path}"
        observation = None
        completed_at = None

        def observe(status, reason=None, metadata=None):
            nonlocal completed_at
            if not isinstance(dataset_id, str) or not dataset_id.strip():
                return None
            if completed_at is None:
                completed_at = utc_now()
            endpoint = safe_url_identity(f"{url}?{urlencode({'id': dataset_id})}")
            resource = None
            identifier = dataset_id
            claims = ()
            if metadata is not None:
                identifier = self._package_identifier(metadata)
                resource, _format = self._select_resource(metadata, self._resource_selection_mode())
                claims = (
                    TimestampClaim.capture(
                        metadata,
                        "metadata_modified",
                        resource=endpoint,
                        field_path="/result/metadata_modified",
                        semantics=TimestampSemantics.CATALOG_METADATA_UPDATE,
                    ),
                )
            resource = resource or {}
            resource_id = resource.get("id")
            resource_url = resource.get("url")
            return FreshnessObservation(
                source=self.source_type,
                catalog_identity=sanitize_url_text(self.adapter_identity),
                dataset_id=sanitize_url_text(identifier),
                resource_id=sanitize_url_text(resource_id)
                if isinstance(resource_id, str)
                else None,
                resource_identity=safe_url_identity(resource_url)
                if isinstance(resource_url, str)
                else None,
                metadata_endpoint=endpoint,
                observed_at=completed_at,
                collection_status=status,
                collector_rule_version="ckan-package-freshness-v1",
                claims=claims,
                reason_code=reason,
            )

        try:
            resp = safe_http_get(
                url,
                params={"id": dataset_id},
                headers=self._headers(),
                timeout=bounded_source_timeout(source_timeout, remaining_time),
            )
            data = resp.json()

            if not data.get("success"):
                result = self._error_result(
                    id=dataset_id, title=dataset_id, error="Package not found"
                )
                result.assessment = error_response_assessment(
                    "CKAN package response contains an explicit error envelope"
                )
                result.freshness_observation = observe("FAILED", "metadata_error_response")
                return result

            pkg = data["result"]
            if isinstance(pkg, dict):
                observation = observe("SUCCEEDED", metadata=pkg)
            result = self._package_to_result(pkg)
            result.freshness_observation = observation
            if not result.download_url:
                result.error = "No supported CSV resources found in package"
                result.assessment = unsupported_format_assessment(None)
                return result

            # Probe the CSV if we have a direct URL
            if result.download_url:
                result = self._probe_csv(
                    result,
                    sample_rows,
                    timeout=source_timeout,
                    remaining_time=remaining_time,
                )

            return result

        except RunDeadlineExceeded as e:
            e.freshness_observation = observation or observe("FAILED", "metadata_deadline_exceeded")
            raise
        except Exception as e:
            result = self._error_result(id=dataset_id, title=dataset_id, error=str(e))
            result.assessment = inspection_failed_assessment(
                "CKAN package retrieval and inspection did not complete"
            )
            blocked = isinstance(e, (UnsafeURLError, UnsafeResourceError))
            result.freshness_observation = observation or observe(
                "BLOCKED" if blocked else "FAILED",
                "metadata_blocked" if blocked else "metadata_collection_failed",
            )
            return result

    def _probe_csv(
        self,
        result: DatasetResult,
        sample_rows: int,
        timeout: float,
        *,
        remaining_time: RemainingTimeProvider | None = None,
    ) -> DatasetResult:
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
            with safe_download(
                result.download_url,
                timeout=bounded_source_timeout(timeout, remaining_time),
            ) as fetched:
                # Retrieval and local inspection are sequential steps. Observe the
                # shared run deadline again before starting DuckDB work.
                bounded_source_timeout(timeout, remaining_time)
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

        except RunDeadlineExceeded:
            raise
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

    def _select_resource(self, pkg: dict, selection_mode: CKANSearchMode):
        """Share the existing selection rule with metadata evidence capture."""
        # Local metadata filtering admits only a plain CSV with a nonblank URL.
        # Server-filter mode retains its established first-recognized-resource shape.
        resources = pkg.get("resources", [])
        csv_resource = None
        resource_format = None
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            normalized_format = self._normalized_resource_format(resource)
            if selection_mode is CKANSearchMode.LOCAL_RESOURCE_METADATA:
                resource_url = resource.get("url")
                if (
                    normalized_format != "CSV"
                    or not isinstance(resource_url, str)
                    or not resource_url.strip()
                ):
                    continue
            elif normalized_format is None:
                continue

            csv_resource = resource
            resource_format = normalized_format
            break

        return csv_resource, resource_format

    def _package_to_result(
        self,
        pkg: dict,
        *,
        search_mode: CKANSearchMode | None = None,
    ) -> DatasetResult:
        """Convert a package using its mode's closed resource-selection contract."""
        identifier = self._package_identifier(pkg)
        _api_base, landing_base, (_search_path, _show_path, landing_path) = (
            self._route_configuration()
        )
        landing_url = f"{landing_base}/{landing_path}/{quote(identifier, safe='')}"
        selection_mode = search_mode or self._resource_selection_mode()
        csv_resource, resource_format = self._select_resource(pkg, selection_mode)

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
            id=identifier,
            title=pkg.get("title", ""),
            description=description,
            source=self.source_type,
            source_name=f"{self.source_name} ({org_name})" if org_name else self.source_name,
            url=landing_url,
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

    @staticmethod
    def _normalized_resource_format(resource: dict) -> str | None:
        """Normalize closed CSV-related metadata; reject contradictory evidence."""

        raw_format = resource.get("format")
        if isinstance(raw_format, str) and raw_format.upper() == "CSV/ZIP":
            # Preserve the established server-filter result shape. The local metadata
            # search mode admits only the normalized plain-CSV value below.
            return "CSV/ZIP"

        checks: list[bool] = []
        for field in (
            "format",
            "format_displayname",
            "mimetype",
            "mimetype_inner",
        ):
            value = resource.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                return None

            if field == "format":
                checks.append(
                    value.upper() in {"CSV", "TEXT/CSV"} or value == _EU_AUTHORITY_CSV_FORMAT
                )
            elif field == "format_displayname":
                checks.append(value.upper() == "CSV")
            else:
                checks.append(value.lower() == "text/csv")

        if checks and all(checks):
            return "CSV"
        return None

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
