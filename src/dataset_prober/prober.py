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
)
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    csv_scan_expr,
    ensure_httpfs,
    load_csv_to_table,
    response_is_html,
)


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
    """Probe a single URL with DuckDB and return a structured result."""
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

    con = duckdb.connect()
    ensure_httpfs(con)

    try:
        # URLs are bound as parameters, never interpolated into SQL text.
        # DuckDB treats a bound value as a literal path, so an injection
        # payload fails as a bad filename rather than executing.
        expr = csv_scan_expr(con, url)
        count_result = con.execute(f"SELECT COUNT(*) FROM {expr}", [url]).fetchone()
        row_count = count_result[0]

        # Get column names and types
        describe = con.execute(f"DESCRIBE SELECT * FROM {expr} LIMIT 1", [url]).fetchall()
        columns = [{"name": row[0], "type": row[1]} for row in describe]

        # Get sample rows
        sample_rows = con.execute(f"SELECT * FROM {expr} LIMIT 3", [url]).fetchall()
        sample = [list(row) for row in sample_rows]

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
        error_msg = str(e)
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

    finally:
        con.close()


def probe_all(sources: list[dict]) -> list[ProbeResult]:
    """Probe a list of sources. Each source is a dict with 'name' and 'url'."""
    results = []
    for source in sources:
        print(f"  Probing: {source['name']}...")
        result = probe_url(source["name"], source["url"])
        results.append(result)
        time.sleep(0.5)
    return results


def save_results(results: list[ProbeResult], path: str):
    """Save probe results to a JSON file."""
    data = [vars(r) for r in results]
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
            if response_is_html(actual_claims.retrieval_url):
                raise ValueError(
                    f"{actual_claims.retrieval_url} serves HTML, not data (landing page?)"
                )

            print(f"  Downloading: {result.name} → table '{actual_claims.planned_table_name}'...")
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                rows = load_csv_to_table(connection)
            result.row_count = rows
            print(f"  Done — {rows} rows loaded.")
        except Exception as exc:
            print(f"  Failed: {exc}")
    return result
