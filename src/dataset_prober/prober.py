import json
import time
from dataclasses import dataclass, field
from typing import Optional

import duckdb

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    claims_for_probe,
    detect_resource_format,
    is_supported_format,
    sanitize_for_presentation,
    sanitize_url_text,
)
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    csv_scan_expr,
    load_csv_to_table,
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
        )

    try:
        with safe_download(url) as fetched:
            con = duckdb.connect()
            try:
                # DuckDB receives only the guarded temporary path. The source
                # URL never reaches httpfs or another opaque network transport.
                expr = csv_scan_expr(con, fetched.path)
                count_result = con.execute(
                    f"SELECT COUNT(*) FROM {expr}", [fetched.path]
                ).fetchone()
                row_count = count_result[0]

                describe = con.execute(
                    f"DESCRIBE SELECT * FROM {expr} LIMIT 1", [fetched.path]
                ).fetchall()
                columns = [{"name": row[0], "type": row[1]} for row in describe]

                sample_rows = con.execute(
                    f"SELECT * FROM {expr} LIMIT 3", [fetched.path]
                ).fetchall()
                sample = [list(row) for row in sample_rows]
            finally:
                con.close()

        return ProbeResult(
            url=url,
            name=name,
            status="ok",
            row_count=row_count,
            columns=columns,
            sample=sample,
            format=resource_format,
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
    data = [sanitize_for_presentation(vars(r)) for r in results]
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
