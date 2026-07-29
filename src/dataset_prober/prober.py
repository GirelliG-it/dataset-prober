import json
import time
from dataclasses import dataclass, field
from typing import Optional

import duckdb

from dataset_prober.tools.base import (
    csv_scan_expr,
    ensure_httpfs,
    load_csv_to_table,
    response_is_html,
    safe_table_name,
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


def _url_identity(url: str) -> str:
    """
    Short stable identity for a URL -  used to key its DuckDB table.

    Returns the hash ONLY. Readability is safe_table_nam's job, via the
    title suffix; including the filename stem here duplicated it (a URL
    ending in 2026_05_N02 produced
    t_2026_05_N02.csv).

    8 hex chars = 4.3e9 values; ~1% collisiob risk around 9000 URLs by the
    birthday bound. Ample for local use -  widen if this ever ingests at scale.
    """
    import hashlib

    return hashlib.sha256(url.encode()).hexdigest()[:8]


def probe_url(name: str, url: str) -> ProbeResult:
    """Probe a single URL with DuckDB and return a structured result."""
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
        )

    except Exception as e:
        error_msg = str(e)
        # Detect silent redirect traps (HTML returned instead of data)
        if "Expected" in error_msg and "but got" in error_msg:
            status = "redirect_trap"
        else:
            status = "error"
        return ProbeResult(url=url, name=name, status=status, error=error_msg)

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


def download_to_duckdb(results: list[ProbeResult], db_path: str):
    """Download selected datasets into a shared DuckDB database file."""
    con = duckdb.connect(db_path)
    ensure_httpfs(con)

    for result in results:
        if result.status != "ok":
            print(f"  Skipping {result.name} — status: {result.status}")
            continue

        # Identity comes from the URL (unique), not the display name (not unique).
        # Two sources both called "population" would otherwise clobber each other.
        table_name = safe_table_name(_url_identity(result.url), result.name)

        if response_is_html(result.url):
            print(f" Skipped: {result.url} serves HTML, not data (landing page?)")
            continue

        print(f"  Downloading: {result.name} → table '{table_name}'...")
        try:
            rows = load_csv_to_table(con, table_name, result.url)
            print(f"  Done — {rows} rows loaded.")
        except Exception as e:
            print(f"  Failed: {e}")

    con.close()
    print(f"\n  Database saved to {db_path}")
