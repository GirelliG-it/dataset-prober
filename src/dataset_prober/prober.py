import json
import time
from dataclasses import dataclass, field
from typing import Optional

import duckdb

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    canonical_candidate_identity,
    claims_for_probe,
    configured_adapter_identity,
    detect_resource_format,
    is_supported_format,
    sanitize_for_presentation,
    sanitize_url_text,
)
from dataset_prober.resource_classification import (
    InspectionOutcome,
    ResourceAssessment,
    inspection_error_assessment,
    unknown_assessment,
    unsupported_format_assessment,
)
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    inspect_csv_resource,
    load_csv_to_table,
    require_eligible_csv_payload,
)
from dataset_prober.tools.guards import safe_download


@dataclass
class ProbeResult:
    url: str
    name: str
    status: str  # "ok", "error", "redirect_trap"
    row_count: Optional[int] = None
    columns: list = field(default_factory=list)
    sample: list = field(default_factory=list)
    error: Optional[str] = None
    format: Optional[str] = None
    assessment: ResourceAssessment = field(default_factory=unknown_assessment)

    def to_dict(self) -> dict:
        """Serialize existing fields plus an additive deterministic assessment."""
        return {
            "url": self.url,
            "name": self.name,
            "status": self.status,
            "row_count": self.row_count,
            "columns": self.columns,
            "sample": self.sample,
            "error": self.error,
            "format": self.format,
            "assessment": self.assessment.to_dict(),
        }


def probe_url(name: str, url: str) -> ProbeResult:
    """Safely retrieve one URL and probe its local copy with DuckDB."""
    resource_format = detect_resource_format(url)
    if not is_supported_format("manual", resource_format):
        detail = resource_format or "unknown"
        return ProbeResult(
            url=url,
            name=name,
            status="error",
            error=f"Unsupported or unproven manual resource format: {detail}",
            format=resource_format,
            assessment=unsupported_format_assessment(str(detail)),
        )

    try:
        candidate_identity = canonical_candidate_identity(
            "manual",
            configured_adapter_identity("manual", {}),
            url,
            url,
        )
        with safe_download(url) as fetched:
            con = duckdb.connect()
            try:
                # DuckDB receives only the guarded temporary path. The source
                # URL never reaches httpfs or another opaque network transport.
                inspection = inspect_csv_resource(
                    con,
                    fetched,
                    sample_rows=3,
                    candidate_identity=candidate_identity,
                )
            finally:
                con.close()

        assessment = inspection["assessment"]
        return ProbeResult(
            url=url,
            name=name,
            status=(
                "ok" if assessment.inspection_outcome is InspectionOutcome.SUCCEEDED else "error"
            ),
            row_count=inspection["row_count"],
            columns=inspection["columns"],
            sample=inspection["sample"],
            format=resource_format,
            error=None if assessment.load_eligible else assessment.explanation,
            assessment=assessment,
        )

    except Exception as e:
        error_msg = sanitize_url_text(str(e))
        # Detect silent redirect traps (HTML returned instead of data)
        if "Expected" in error_msg and "but got" in error_msg:
            status = "redirect_trap"
        else:
            status = "error"
        return ProbeResult(
            url=url,
            name=name,
            status=status,
            error=error_msg,
            format=resource_format,
            assessment=inspection_error_assessment(e),
        )


def probe_all(sources: list[dict]) -> list[ProbeResult]:
    """Probe a list of sources. Each source is a dict with 'name' and 'url'."""
    results = []
    for source in sources:
        print(f"  Probing: {sanitize_url_text(str(source['name']))}...")
        result = probe_url(source["name"], source["url"])
        results.append(result)
        time.sleep(0.5)
    return results


def save_results(results: list[ProbeResult], path: str):
    """Save probe results to a JSON file."""
    data = [sanitize_for_presentation(r.to_dict()) for r in results]
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Results saved to {path}")


def download_to_duckdb(
    result: ProbeResult, destination: str, authorization: AuthorizedLoad
) -> ProbeResult:
    """Perform one exact manual CSV load through a one-shot authorization."""
    if not isinstance(authorization, AuthorizedLoad):
        raise TypeError("Manual persistent loading requires an AuthorizedLoad")

    actual_claims = claims_for_probe(result, destination)
    with authorization.activate(actual_claims) as permit:
        try:
            with safe_download(actual_claims.retrieval_url) as fetched:
                require_eligible_csv_payload(fetched, actual_claims.candidate_identity)
                print(
                    f"  Downloading: {sanitize_url_text(result.name)} → "
                    f"table '{actual_claims.planned_table_name}'..."
                )
                with AuthorizedDuckDBConnection(permit, destination) as connection:
                    rows = load_csv_to_table(connection, fetched)
            result.row_count = rows
            print(f"  Done — {rows} rows loaded.")
        except Exception as exc:
            print(f"  Failed: {sanitize_url_text(str(exc))}")
    return result
