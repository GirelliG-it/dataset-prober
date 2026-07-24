"""
src/tools/base.py

Base interfaces, standardized data structures, and shared DuckDB loading
helpers for all data-source tools.

Every tool implementation must inherit from DataSourceTool and implement
all abstract methods. Tools return DatasetResult objects so tool-specific
structures do not leak into the agent layer.

Shared loading helpers generate collision-resistant DuckDB table names and
provide one consistent loading path across all tools.
"""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DatasetResult:
    """
    Standardized dataset representation across all data sources.
    Every tool returns this structure — CBS, CKAN, OpenDataSoft, Tavily.
    The agent works exclusively with DatasetResult objects.
    """

    # Identity
    id: str  # Source-specific identifier (CBS table ID, CKAN package name, URL)
    title: str  # Human-readable dataset name
    description: str  # What the dataset contains
    source: str  # Tool that found this: "cbs", "ckan", "opendatasoft", "tavily"
    source_name: str  # Human-readable source name: "CBS", "Data.gov", "Den Haag Open Data"

    # Access
    url: str  # Catalog or landing page URL
    download_url: Optional[str]  # Direct file URL (CSV, parquet etc.) if known
    format: Optional[str]  # "CSV", "JSON", "parquet", "OData" etc.

    # Freshness
    modified: Optional[str]  # Last update date (ISO format preferred)
    frequency: Optional[str]  # Update frequency: "monthly", "annual" etc.

    # License (CCREL/ODRL)
    license: Optional[str]  # "CC0", "CC-BY", "CC-BY-SA", "CC-BY-NC", "other", "unknown"
    license_url: Optional[str]  # Full license URL

    # Schema (populated after probe/fetch)
    row_count: Optional[int]  # Number of rows — None until probed
    columns: Optional[list]  # Column definitions — None until probed
    sample: Optional[list]  # Sample rows — None until probed

    # Discovery metadata
    language: Optional[str]  # Dataset language: "nl", "en" etc.
    tags: list = field(default_factory=list)  # Keywords/topics

    # Pipeline status
    status: str = "found"  # "found" → "probed" → "downloaded" → "failed" → "skipped"
    error: Optional[str] = None  # Error message if status is "failed"

    # Cost tracking
    tokens_used: int = 0  # Tokens consumed discovering/fetching this dataset
    cost_usd: float = 0.0  # Estimated cost in USD

    def freshness_days(self) -> Optional[int]:
        """Return how many days ago this dataset was last modified. None if unknown."""
        if not self.modified:
            return None
        from datetime import datetime

        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y"):
            try:
                dt = datetime.strptime(self.modified.strip()[:19], fmt)
                return (datetime.now() - dt).days
            except ValueError:
                continue
        return None

    def passes_freshness(self, max_days: int) -> Optional[bool]:
        """
        Check if dataset meets a freshness requirement.
        Returns None if freshness cannot be determined.
        """
        days = self.freshness_days()
        if days is None:
            return None
        return days <= max_days

    def license_grade(self) -> str:
        """
        Return a human-readable license grade.
        A = unrestricted, B = attribution required, C = restricted, ? = unknown
        """
        if not self.license:
            return "?"
        lic = self.license.upper()
        if "CC0" in lic or "PUBLIC DOMAIN" in lic or "PDDL" in lic:
            return "A"
        if "CC-BY" in lic and "NC" not in lic and "SA" not in lic:
            return "B"
        if "CC-BY-SA" in lic:
            return "B-"
        if "NC" in lic:
            return "C"
        return "?"

    @classmethod
    def failed(
        cls,
        id: str,
        title: str,
        source: str,
        source_name: str,
        error: str,
        language: Optional[str] = None,
        url: str = "",
    ) -> "DatasetResult":
        """
        Build a status='failed' result with every unknown field set to its
        neutral default. Every tool's fetch/search error path needs exactly
        this shape — this is the one place that spells it out.
        """
        return cls(
            id=id,
            title=title,
            description="",
            source=source,
            source_name=source_name,
            url=url,
            download_url=None,
            format=None,
            modified=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=language,
            tags=[],
            status="failed",
            error=error,
        )

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON output."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "source_name": self.source_name,
            "url": self.url,
            "download_url": self.download_url,
            "format": self.format,
            "modified": self.modified,
            "frequency": self.frequency,
            "license": self.license,
            "license_url": self.license_url,
            "license_grade": self.license_grade(),
            "row_count": self.row_count,
            "columns": self.columns,
            "language": self.language,
            "tags": self.tags,
            "status": self.status,
            "error": self.error,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6),
        }


class DataSourceTool(ABC):
    """
    Abstract base class for all data source tools.

    Every tool must implement:
      - search(keyword, max_results) → list[DatasetResult]
      - fetch(dataset_id, sample_rows) → DatasetResult
      - download(dataset, db_path) → DatasetResult

    Tools receive all configuration from the profile at instantiation.
    No default values are hardcoded — all limits come from config.
    """

    def __init__(self, config: dict):
        """
        Initialize tool with profile configuration.

        Args:
            config: Dictionary from profile YAML containing all tool settings.
                    No fallback defaults — if a key is missing, raise KeyError
                    so misconfigured profiles fail loudly.
        """
        self.config = config

    @abstractmethod
    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search the catalog for datasets matching a keyword.

        Args:
            keyword: Search term (may be in any language)
            max_results: Maximum number of results to return (from config)

        Returns:
            List of DatasetResult objects with status="found"
        """
        pass

    @abstractmethod
    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Fetch metadata and sample data for a specific dataset.

        Args:
            dataset_id: Source-specific identifier
            sample_rows: Number of sample rows to retrieve (from config)

        Returns:
            DatasetResult with status="probed", columns and sample populated
        """
        pass

    @abstractmethod
    def download(self, dataset: DatasetResult, db_path: str) -> DatasetResult:
        """
        Download the full dataset into a local DuckDB database.

        Args:
            dataset: A DatasetResult with status="probed"
            db_path: Path to the DuckDB file

        Returns:
            DatasetResult with status="downloaded" and row_count updated,
            or status="failed" with error populated
        """
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable name of this data source (e.g. 'CBS Statistics Netherlands')"""
        pass

    @property
    @abstractmethod
    def source_type(self) -> str:
        """
        Machine-readable source type identifier.
        Used by config_loader to instantiate the correct tool class.
        Values: 'cbs', 'ckan', 'opendatasoft', 'tavily'
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this tool's dependencies and credentials are available.
        Override in subclasses that require API keys or external services.
        Returns True by default.
        """
        return True


# ─── Shared DuckDB loading helpers ───────────────────────────────────────────
# These exist so that every download path — prober.download_to_duckdb,
# CBSTool, CKANTool, TavilyTool — goes through ONE implementation.
# Previously each path had its own copy of the CTAS statement, and a security
# fix applied to one copy silently missed the other three.


def safe_table_name(dataset_id: str, title: str = "") -> str:
    """
    Build a readable, collision-resistant DuckDB table name.

    Identity never rests on the human title, because two distinct
    titles can sanitize to the same string ("Bevolking per gemeente, 2024"
    and "Bevolking per gemeente (2024)" sanitize identically).

    Therefore uniqueness rests on the source's stable ID; the title, when
    present, only makes the name legible to a human reading SHOW TABLES.

    The resulting name is at most 63 characters (consciously chosen as a
    limit). The digest prevents distinct long IDs that share the same
    truncated prefix from producing the same table name.
    """

    def _clean(s: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")

    ident = _clean(dataset_id)
    if not ident:
        raise ValueError("dataset_id must produce a non-empty table name")

    suffix = _clean(title)[:40]
    readable = f"{ident}_{suffix}".strip("_") if suffix else ident

    # Keep identifiers readable when the source ID begins with a digit.
    if readable[0].isdigit():
        readable = f"t_{readable}"

    # Reserve 12 characters for a stable hash of the complete dataset ID.
    digest = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:12]

    return f"{readable[:50]}_{digest}"


EUROPEAN_CSV_ARGS = (
    "delim=';', header=true, comment='#', strict_mode=false, null_padding=true, all_varchar=true"
)


def _is_degenerate(names: list[str]) -> bool:
    """
    True if a set of sniffed column names indicates read_csv_auto guessed the
    dialect wrong WITHOUT raising. Two observed tells:

      1. Generic 'column0', 'column1', ... — no header was found at all.
      2. A single column whose name is a whole delimited line, e.g.
         'datum;station;waarde' or '# RIVM Luchtmeetnet'. The file collapsed
         into one field because the ';' delimiter was never detected.

    Tell 2 is the common one for RIVM/EU files with ragged rows or a repeated
    mid-file header; tell 1 alone misses them.
    """
    if not names:
        return True
    generic = sum(1 for n in names if n.startswith("column") and n[6:].isdigit())
    if generic >= max(1, len(names) // 2):
        return True
    if len(names) == 1 and (";" in names[0] or names[0].lstrip().startswith("#")):
        return True
    return False


def csv_scan_expr(con, url: str) -> str:
    """
    Decide ONCE how a CSV at `url` should be scanned, and return the DuckDB
    table function as a string with a '?' placeholder the caller binds.

    Returns either "read_csv_auto(?)" or the European-dialect fallback. The
    decision is made BEFORE any table exists, so probe_url and
    load_csv_to_table can share it — one decision, two callers, no drift.

    The '?' is never substituted here; callers still bind [url] as a
    parameter, so the SQL-injection defence is unchanged.

    Asymmetric by design: clean comma-CSVs keep auto-typing; only files that
    fail or mis-sniff fall back to all-VARCHAR, which loads losslessly and is
    cast in SQL when analysing.
    """
    try:
        cols = con.execute("DESCRIBE SELECT * FROM read_csv_auto(?) LIMIT 1", [url]).fetchall()
        if not _is_degenerate([str(c[0]) for c in cols]):
            return "read_csv_auto(?)"
    except Exception:
        pass
    return f"read_csv(?, {EUROPEAN_CSV_ARGS})"


def probe_csv_url(con, url: str, sample_rows: int) -> dict:
    """
    Probe a CSV at `url`: column definitions, a sample of rows, and the total
    row count. Shared by every tool that probes a discovered URL directly
    (CKANTool, TavilyTool) — they previously called read_csv_auto(?) on their
    own, which skipped the European-dialect fallback that csv_scan_expr
    provides and that load_csv_to_table / prober.probe_url already use. A
    ';'-delimited file found via CKAN or Tavily search would probe as
    garbage while the identical file loaded fine on download.

    Row count failure is tolerated (returns None) since it's a nice-to-have,
    not a correctness signal; describe/sample failures propagate to the
    caller, same as before this was extracted.
    """
    expr = csv_scan_expr(con, url)
    describe = con.execute(f"DESCRIBE SELECT * FROM {expr} LIMIT 1", [url]).fetchall()
    columns = [{"name": row[0], "type": row[1]} for row in describe]

    sample = con.execute(f"SELECT * FROM {expr} LIMIT {int(sample_rows)}", [url]).fetchall()

    try:
        row_count = con.execute(f"SELECT COUNT(*) FROM {expr}", [url]).fetchone()[0]
    except Exception:
        row_count = None

    return {
        "columns": columns,
        "sample": [list(row) for row in sample],
        "row_count": row_count,
    }


def download_csv_dataset(dataset: DatasetResult, db_path: str) -> DatasetResult:
    """
    Download dataset.download_url into DuckDB, updating `dataset` in place.

    Shared by every tool whose download is "httpfs-load a CSV" — CKANTool
    and TavilyTool previously carried byte-identical copies of this method,
    which is exactly the drift risk this module's helpers exist to prevent
    (see module docstring).
    """
    if not dataset.download_url:
        dataset.status = "failed"
        dataset.error = "No download URL available"
        return dataset

    try:
        import duckdb

        table_name = safe_table_name(dataset.id, dataset.title)

        if response_is_html(dataset.download_url):
            raise ValueError(
                f"URL serves an HTML page, not data: {dataset.download_url} "
                f"(landing/listing page - a direct file link is required)"
            )

        con = duckdb.connect(db_path)
        ensure_httpfs(con)
        actual_rows = load_csv_to_table(con, table_name, dataset.download_url)
        con.close()

        dataset.row_count = actual_rows
        dataset.status = "downloaded"
        return dataset

    except Exception as e:
        dataset.status = "failed"
        dataset.error = str(e)
        return dataset


def load_csv_to_table(con, table_name: str, url: str) -> int:
    """
    Load a CSV at `url` into `table_name`, returning the row count.

    `url` is ALWAYS bound as a parameter, never interpolated. It arrives from
    third-party catalogues and web search results and must be treated as
    hostile. DuckDB resolves a bound value as a literal path, so an injection
    payload fails as a bad filename instead of executing.

    `table_name` is ours, not the source's, and is identifier-quoted.
    The scan dialect comes from csv_scan_expr, shared with probe_url.
    """
    expr = csv_scan_expr(con, url)
    con.execute(
        f'CREATE OR REPLACE TABLE "{table_name}" AS SELECT * FROM {expr}',
        [url],
    )
    _reject_if_html(con, table_name, url)
    return con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]


def response_is_html(url: str, timeout: float = 5.0) -> bool:
    """
    Best-effort pre-check: does `url` serve an HTML page rather than data?

    Sends a HEAD (falling back to a streamed GET for servers that mishandle
    HEAD) and inspects Content-Type. Returns True only when the server clearly
    declares HTML. ANY failure — non-HTTP URL, timeout, connection error,
    blocked host — returns False, so this never blocks a download on its own;
    the post-load _reject_if_html guard remains the backstop.

    Kept network-free for non-HTTP inputs (local paths, file://) so unit tests
    that load fixture CSVs never touch the network.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        import requests

        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code >= 400 or not ctype:
            # Some data servers reject HEAD; confirm with a streamed GET that
            # reads headers only (no body download).
            resp = requests.get(url, stream=True, timeout=timeout)
            ctype = resp.headers.get("Content-Type", "")
            resp.close()
        mime = ctype.split(";")[0].strip().lower()
        return mime in ("text/html", "application/xhtml+xml")
    except Exception:
        return False


_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body", "<a href", "<table")


def _reject_if_html(con, table_name: str, url: str) -> None:
    """
    Raise ValueError if a freshly-loaded table looks like parsed HTML rather
    than tabular data. Heuristic, but high-signal: HTML pages parse into a
    single column whose NAME (the first physical line) or first row carries
    an HTML tag. Real CSVs don't name a column `<!DOCTYPE html>`.
    """
    cols = con.execute(f'DESCRIBE "{table_name}"').fetchall()
    header_blob = " ".join(str(c[0]).lower() for c in cols)

    first_cell = ""
    if cols:
        row = con.execute(f'SELECT * FROM "{table_name}" LIMIT 1').fetchone()
        if row and row[0] is not None:
            first_cell = str(row[0]).lower()

    looks_html = any(m in header_blob or m in first_cell for m in _HTML_MARKERS)
    # A single column whose header is one long line is itself suspicious for a
    # "CSV", but only flag it when combined with an HTML marker to avoid
    # rejecting legitimate single-column data files.
    if looks_html:
        con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        raise ValueError(
            f"URL returned an HTML page, not tabular data (redirect trap?): {url}. "
            f"This is often a landing/listing page — a direct CSV link is required."
        )


def ensure_httpfs(con) -> None:
    """
    Install AND load httpfs. prober.py previously only called LOAD, which
    fails on any machine with a cold DuckDB extension cache.
    """
    con.execute("INSTALL httpfs; LOAD httpfs;")
