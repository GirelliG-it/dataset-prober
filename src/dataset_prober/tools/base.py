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

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    CandidateIdentity,
    LoaderKind,
    _ActivePermit,
    canonicalize_destination,
    claims_for_dataset,
    configured_adapter_identity,
    detect_resource_format,
    safe_url_identity,
    sanitize_for_presentation,
    sanitize_url_text,
)
from dataset_prober.resource_classification import (
    ResourceAssessment,
    ResourceClassificationError,
    _bind_classifier_evidence,
    classify_blocking_content,
    classify_tabular_structure,
    inspection_failed_assessment,
    unknown_assessment,
)
from dataset_prober.tools.guards import (
    MAX_HTTP_RESPONSE_BYTES,
    FetchedResource,
    safe_download,
    safe_http_head,
)

RemainingTimeProvider = Callable[[], float]


class RunDeadlineExceeded(TimeoutError):
    """A profile-agent source operation has no remaining run time."""


def bounded_source_timeout(
    source_timeout: float,
    remaining_time: RemainingTimeProvider | None,
) -> float:
    """Cap one source operation by freshly observed profile-agent run time."""

    configured_timeout = float(source_timeout)
    if not math.isfinite(configured_timeout) or configured_timeout <= 0:
        raise ValueError("Source timeout must be a positive finite number")
    if remaining_time is None:
        return configured_timeout

    try:
        remaining = float(remaining_time())
    except (TypeError, ValueError, OverflowError) as exc:
        raise RunDeadlineExceeded("Profile-agent run deadline is unavailable") from exc
    if not math.isfinite(remaining) or remaining <= 0:
        raise RunDeadlineExceeded("Profile-agent run deadline exhausted")
    return min(configured_timeout, remaining)


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

    # Deterministic safety assessment; legacy/discovery-only construction fails closed.
    assessment: ResourceAssessment = field(default_factory=unknown_assessment)

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
            error=sanitize_url_text(error),
        )

    def to_dict(self) -> dict:
        """Serialize a presentation-safe copy without changing internal identity."""
        return sanitize_for_presentation(
            {
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
                "assessment": self.assessment.to_dict(),
                "tokens_used": self.tokens_used,
                "cost_usd": round(self.cost_usd, 6),
            }
        )


class DataSourceTool(ABC):
    """
    Abstract base class for all data source tools.

    Every tool must implement:
      - search(keyword, max_results, remaining_time=...) → list[DatasetResult]
      - fetch(dataset_id, sample_rows, remaining_time=...) → DatasetResult
      - download(dataset, destination, authorization) → DatasetResult

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
    def search(
        self,
        keyword: str,
        max_results: int,
        *,
        remaining_time: RemainingTimeProvider | None = None,
    ) -> list[DatasetResult]:
        """
        Search the catalog for datasets matching a keyword.

        Args:
            keyword: Search term (may be in any language)
            max_results: Maximum number of results to return (from config)
            remaining_time: Optional profile-agent monotonic remaining-time provider

        Returns:
            List of DatasetResult objects with status="found"
        """
        pass

    @abstractmethod
    def fetch(
        self,
        dataset_id: str,
        sample_rows: int,
        *,
        remaining_time: RemainingTimeProvider | None = None,
    ) -> DatasetResult:
        """
        Fetch metadata and sample data for a specific dataset.

        Args:
            dataset_id: Source-specific identifier
            sample_rows: Number of sample rows to retrieve (from config)
            remaining_time: Optional profile-agent monotonic remaining-time provider

        Returns:
            DatasetResult with status="probed", columns and sample populated
        """
        pass

    @abstractmethod
    def download(
        self,
        dataset: DatasetResult,
        destination: str | Path,
        authorization: AuthorizedLoad,
    ) -> DatasetResult:
        """
        Download the full dataset into a local DuckDB database.

        Args:
            dataset: A DatasetResult with status="probed"
            destination: Exact persistent DuckDB destination shown during consent
            authorization: One-shot authorization for this resource and destination

        Returns:
            DatasetResult with status="downloaded" and row_count updated,
            or status="failed" with error populated
        """
        pass

    @property
    def adapter_identity(self) -> str:
        """Configured identity bound into inspection and writer claims."""
        return configured_adapter_identity(self.source_type, self.config)

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


def _existing_local_csv_path(path: str | Path) -> str:
    """Return an absolute local file path; never allow DuckDB to own source retrieval."""
    try:
        local_path = Path(path).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("CSV scanning requires an existing local file") from exc
    if not local_path.is_file():
        raise ValueError("CSV scanning requires an existing local file")
    return str(local_path)


def csv_scan_expr(con, path: str | Path) -> str:
    """
    Decide ONCE how a local CSV should be scanned, and return the DuckDB
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
    local_path = _existing_local_csv_path(path)
    try:
        cols = con.execute(
            "DESCRIBE SELECT * FROM read_csv_auto(?) LIMIT 1", [local_path]
        ).fetchall()
        if not _is_degenerate([str(c[0]) for c in cols]):
            return "read_csv_auto(?)"
    except Exception:
        pass
    return f"read_csv(?, {EUROPEAN_CSV_ARGS})"


def probe_csv_url(con, path: str | Path, sample_rows: int) -> dict:
    """
    Probe a safely retrieved local CSV: column definitions, a sample of rows, and the total
    row count. Shared by every tool that probes a discovered URL directly
    (CKANTool, TavilyTool) — they previously called read_csv_auto(?) on their
    own, which skipped the European-dialect fallback that csv_scan_expr
    provides and that load_csv_to_table / prober.probe_url already use. A
    ';'-delimited file found via CKAN or Tavily search would probe as
    garbage while the identical file loaded fine on download.

    Describe, sample, and exact row counting must all succeed. Task 4 uses
    the row count as one part of deterministic non-empty evidence.
    """
    local_path = _existing_local_csv_path(path)
    expr = csv_scan_expr(con, local_path)
    describe = con.execute(f"DESCRIBE SELECT * FROM {expr} LIMIT 1", [local_path]).fetchall()
    columns = [{"name": row[0], "type": row[1]} for row in describe]

    sample = con.execute(f"SELECT * FROM {expr} LIMIT {int(sample_rows)}", [local_path]).fetchall()

    row_count = con.execute(f"SELECT COUNT(*) FROM {expr}", [local_path]).fetchone()[0]

    return {
        "columns": columns,
        "sample": [list(row) for row in sample],
        "row_count": row_count,
    }


def _content_type(headers) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            return str(value)
    return None


def _leading_content(path: Path) -> tuple[bool, bytes]:
    """Return all-whitespace status and the first significant content bytes."""
    utf8_bom = b"\xef\xbb\xbf"
    leading_whitespace = b" \t\r\n\f\v"
    pending = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8192)
            at_eof = not chunk
            pending += chunk

            while True:
                stripped = pending.lstrip(leading_whitespace)
                if stripped != pending:
                    pending = stripped
                    continue
                if pending.startswith(utf8_bom):
                    pending = pending[len(utf8_bom) :]
                    continue
                break

            if not pending:
                if at_eof:
                    return True, b""
                continue
            if not at_eof and len(pending) < len(utf8_bom) and utf8_bom.startswith(pending):
                continue
            return False, pending


def inspect_csv_resource(
    con,
    fetched: FetchedResource,
    sample_rows: int,
    *,
    candidate_identity: CandidateIdentity | None = None,
) -> dict:
    """Classify one guarded local CSV candidate using content plus parser evidence."""
    if not isinstance(fetched, FetchedResource):
        raise TypeError("CSV inspection requires a guarded fetched resource")
    local_path = Path(_existing_local_csv_path(fetched.path))
    empty, leading = _leading_content(local_path)

    json_detected = leading.startswith((b"{", b"["))
    json_value = None
    if json_detected and local_path.stat().st_size <= MAX_HTTP_RESPONSE_BYTES:
        try:
            with local_path.open("r", encoding="utf-8-sig") as handle:
                json_value = json.load(handle)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            json_value = None

    final_format = detect_resource_format(fetched.final_url)
    blocker = classify_blocking_content(
        empty=empty,
        leading_bytes=leading,
        content_type=_content_type(fetched.headers),
        json_detected=json_detected,
        json_value=json_value,
        format_conflict=final_format is not None and final_format.value != "CSV",
    )
    if blocker is not None:
        return {
            "columns": [],
            "sample": [],
            "row_count": None,
            "assessment": blocker,
        }

    try:
        probe = probe_csv_url(con, local_path, sample_rows)
    except Exception as exc:
        return {
            "columns": [],
            "sample": [],
            "row_count": None,
            "assessment": inspection_failed_assessment(
                f"CSV inspection failed ({type(exc).__name__})"
            ),
        }
    assessment = classify_tabular_structure(probe["columns"], probe["row_count"])
    if candidate_identity is not None:
        _bind_classifier_evidence(assessment, candidate_identity)
    probe["assessment"] = assessment
    return probe


def require_eligible_csv_payload(
    fetched: FetchedResource,
    candidate_identity: CandidateIdentity,
) -> dict:
    """Reclassify actual load bytes before any persistent DuckDB connection opens."""
    import duckdb

    connection = duckdb.connect()
    try:
        inspection = inspect_csv_resource(
            connection,
            fetched,
            sample_rows=3,
            candidate_identity=candidate_identity,
        )
    finally:
        connection.close()
    assessment = inspection["assessment"]
    if not assessment.load_eligible:
        raise ResourceClassificationError(
            "Actual load payload for "
            f"{safe_url_identity(fetched.final_url)} is report-only: "
            f"{assessment.reason.value}"
        )
    return inspection


def download_csv_dataset(
    dataset: DatasetResult,
    adapter_identity: str,
    destination: str | Path,
    permit: _ActivePermit,
) -> DatasetResult:
    """
    Download dataset.download_url into DuckDB, updating `dataset` in place.

    Shared by every tool whose download safely retrieves and loads a CSV — CKANTool
    and TavilyTool previously carried byte-identical copies of this method,
    which is exactly the drift risk this module's helpers exist to prevent
    (see module docstring).
    """
    if not isinstance(permit, _ActivePermit):
        raise TypeError("Persistent CSV loading requires a live active permit")

    actual_claims = claims_for_dataset(dataset, adapter_identity, destination)
    permit._assert_claims(actual_claims)

    try:
        with safe_download(actual_claims.retrieval_url) as fetched:
            require_eligible_csv_payload(fetched, actual_claims.candidate_identity)
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                actual_rows = load_csv_to_table(connection, fetched)

        dataset.row_count = actual_rows
        dataset.status = "downloaded"
        return dataset

    except Exception as e:
        dataset.status = "failed"
        dataset.error = sanitize_url_text(str(e))
        return dataset


class AuthorizedDuckDBConnection:
    """Own one transactional persistent connection while an active permit is live."""

    __slots__ = ("__permit", "__destination", "__connection")

    def __init__(self, permit: _ActivePermit, destination: str | Path) -> None:
        if not isinstance(permit, _ActivePermit):
            raise TypeError("Persistent DuckDB access requires a live active permit")
        self.__permit = permit
        self.__destination = destination
        self.__connection = None

    def __enter__(self) -> "AuthorizedDuckDBConnection":
        self.__permit._assert_destination(self.__destination)
        self.__permit._current_claims()
        destination = canonicalize_destination(self.__destination)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)

        import duckdb

        connection = duckdb.connect(destination)
        try:
            connection.execute("BEGIN TRANSACTION")
        except BaseException as begin_error:
            try:
                connection.close()
            except BaseException as close_error:
                raise begin_error from close_error
            raise
        self.__connection = connection
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        connection = self.__connection
        self.__connection = None
        if connection is None:
            return False

        if exc is not None:
            rollback_error = None
            try:
                connection.execute("ROLLBACK")
            except BaseException as caught_rollback_error:
                rollback_error = caught_rollback_error
            try:
                connection.close()
            except BaseException as close_error:
                exc.add_note(
                    f"Persistent DuckDB connection close also failed ({type(close_error).__name__})"
                )
            if rollback_error is not None:
                raise exc.with_traceback(traceback) from rollback_error
            return False

        try:
            connection.execute("COMMIT")
        except BaseException as commit_error:
            rollback_error = None
            try:
                connection.execute("ROLLBACK")
            except BaseException as caught_rollback_error:
                rollback_error = caught_rollback_error
            try:
                connection.close()
            except BaseException as close_error:
                commit_error.add_note(
                    f"Persistent DuckDB connection close also failed ({type(close_error).__name__})"
                )
            if rollback_error is not None:
                raise commit_error from rollback_error
            raise
        else:
            connection.close()
        return False

    def __active_claims(self, loader_kind: LoaderKind):
        claims = self.__permit._current_claims()
        self.__permit._assert_destination(self.__destination)
        if self.__connection is None:
            raise RuntimeError("Authorized DuckDB connection is not open")
        if claims.loader_kind is not loader_kind:
            raise TypeError(f"Persistent operation requires loader {loader_kind.value}")
        return claims

    def _create_csv_table(self, fetched: FetchedResource) -> None:
        claims = self.__active_claims(LoaderKind.DUCKDB_CSV)
        if not isinstance(fetched, FetchedResource):
            raise TypeError("Persistent CSV loading requires a guarded fetched resource")
        if fetched.source_url != claims.retrieval_url:
            raise ValueError("Guarded resource does not match the authorized retrieval URL")
        local_path = str(Path(fetched.path).resolve())
        if not Path(local_path).is_file():
            raise ValueError("Guarded resource is not an available local file")
        expr = csv_scan_expr(self.__connection, local_path)
        claims = self.__active_claims(LoaderKind.DUCKDB_CSV)
        self.__connection.execute(
            f'CREATE TABLE "{claims.planned_table_name}" AS SELECT * FROM {expr}',
            [local_path],
        )

    def _csv_row_count(self) -> int:
        claims = self.__active_claims(LoaderKind.DUCKDB_CSV)
        result = self.__connection.execute(
            f'SELECT COUNT(*) FROM "{claims.planned_table_name}"'
        ).fetchone()
        return result[0]

    def _reject_html_csv_table(self) -> None:
        claims = self.__active_claims(LoaderKind.DUCKDB_CSV)
        cols = self.__connection.execute(f'DESCRIBE "{claims.planned_table_name}"').fetchall()
        header_blob = " ".join(str(column[0]).lower() for column in cols)

        first_cell = ""
        if cols:
            claims = self.__active_claims(LoaderKind.DUCKDB_CSV)
            row = self.__connection.execute(
                f'SELECT * FROM "{claims.planned_table_name}" LIMIT 1'
            ).fetchone()
            if row and row[0] is not None:
                first_cell = str(row[0]).lower()

        if any(marker in header_blob or marker in first_cell for marker in _HTML_MARKERS):
            raise ValueError(
                "URL returned an HTML page, not tabular data (redirect trap?): "
                f"{safe_url_identity(claims.retrieval_url)}. "
                "This is often a landing/listing page — a direct CSV link is required."
            )

    def _create_dataframe_table(self, dataframe) -> None:
        claims = self.__active_claims(LoaderKind.CBS_ODATA)
        frame_name = "_dataset_prober_authorized_frame"
        self.__connection.register(frame_name, dataframe)
        claims = self.__active_claims(LoaderKind.CBS_ODATA)
        self.__connection.execute(
            f'CREATE TABLE "{claims.planned_table_name}" AS SELECT * FROM {frame_name}'
        )

    def _dataframe_row_count(self) -> int:
        claims = self.__active_claims(LoaderKind.CBS_ODATA)
        result = self.__connection.execute(
            f'SELECT COUNT(*) FROM "{claims.planned_table_name}"'
        ).fetchone()
        return result[0]


def _authorized_connection(connection) -> AuthorizedDuckDBConnection:
    if not isinstance(connection, AuthorizedDuckDBConnection):
        raise TypeError("Persistent SQL requires an authorization-aware DuckDB connection")
    return connection


def load_csv_to_table(
    connection: AuthorizedDuckDBConnection,
    fetched: FetchedResource,
) -> int:
    """
    Load the authorized CSV into its planned table, returning the row count.

    The guarded local path is ALWAYS bound as a parameter, never interpolated.
    The planned table name is authorization-derived and identifier-quoted.

    The scan dialect comes from csv_scan_expr, shared with probe_url.
    """
    authorized = _authorized_connection(connection)
    authorized._create_csv_table(fetched)
    _reject_if_html(authorized)
    return authorized._csv_row_count()


def load_dataframe_to_table(connection: AuthorizedDuckDBConnection, dataframe) -> int:
    """Persist one CBS dataframe through the authorized CBS CTAS boundary."""
    authorized = _authorized_connection(connection)
    authorized._create_dataframe_table(dataframe)
    return authorized._dataframe_row_count()


def response_is_html(url: str, timeout: float = 5.0) -> bool:
    """
    Best-effort pre-check: does `url` serve an HTML page rather than data?

    Sends a guarded HEAD and inspects Content-Type. Returns True only when the
    server clearly declares HTML. Any failure returns False, so this remains a
    directory-discovery hint; guarded retrieval and the post-load HTML check
    remain the enforcement points.

    Kept network-free for non-HTTP inputs (local paths, file://) so unit tests
    that load fixture CSVs never touch the network.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        response = safe_http_head(url, timeout=timeout)
        ctype = response.headers.get("Content-Type", "")
        mime = ctype.split(";")[0].strip().lower()
        return mime in ("text/html", "application/xhtml+xml")
    except Exception:
        return False


_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body", "<a href", "<table")


def _reject_if_html(connection: AuthorizedDuckDBConnection) -> None:
    """
    Raise ValueError if a freshly-loaded table looks like parsed HTML rather
    than tabular data. Heuristic, but high-signal: HTML pages parse into a
    single column whose NAME (the first physical line) or first row carries
    an HTML tag. Real CSVs don't name a column `<!DOCTYPE html>`.
    """
    _authorized_connection(connection)._reject_html_csv_table()
