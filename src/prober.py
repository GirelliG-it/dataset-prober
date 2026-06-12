import duckdb
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProbeResult:
    url: str
    name: str
    status: str                    # "ok", "error", "redirect_trap"
    row_count: Optional[int] = None
    columns: list = field(default_factory=list)
    sample: list = field(default_factory=list)
    error: Optional[str] = None


def probe_url(name: str, url: str) -> ProbeResult:
    """Probe a single URL with DuckDB and return a structured result."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")

    try:
        # Get row count
        count_result = con.execute(
            f"SELECT COUNT(*) FROM read_csv_auto('{url}')"
        ).fetchone()
        row_count = count_result[0]

        # Get column names and types
        describe = con.execute(
            f"DESCRIBE SELECT * FROM read_csv_auto('{url}') LIMIT 1"
        ).fetchall()
        columns = [{"name": row[0], "type": row[1]} for row in describe]

        # Get sample rows
        sample_rows = con.execute(
            f"SELECT * FROM read_csv_auto('{url}') LIMIT 3"
        ).fetchall()
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
    con.execute("INSTALL httpfs; LOAD httpfs;")

    for result in results:
        if result.status != "ok":
            console_print(f"  Skipping {result.name} — status: {result.status}")
            continue

        # Create a safe table name from the dataset name
        table_name = result.name.lower()
        table_name = "".join(c if c.isalnum() else "_" for c in table_name)
        table_name = table_name.strip("_")

        print(f"  Downloading: {result.name} → table '{table_name}'...")
        try:
            con.execute(f"""
                CREATE OR REPLACE TABLE {table_name} AS
                SELECT * FROM read_csv_auto('{result.url}')
            """)
            print(f"  Done — {result.row_count} rows loaded.")
        except Exception as e:
            print(f"  Failed: {e}")

    con.close()
    print(f"\n  Database saved to {db_path}")
