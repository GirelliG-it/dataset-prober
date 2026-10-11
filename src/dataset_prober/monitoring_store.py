"""Standalone, append-only monitoring history; no collection or ingestion authority.

One caller owns a store/connection at a time. Each append commits independently;
context exit closes the connection, not a batch transaction. Only normal restart
durability is covered by the initial acceptance tests.
"""

import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from dataset_prober.monitoring import Evidence, Observation

_VERSION = 1
_OBSERVATION_FIELDS = {
    "observation_id",
    "source_id",
    "checked_at",
    "check_status",
    "evidence",
    "failure_reason",
    "contract_version",
    "checker_version",
}
_EVIDENCE_FIELDS = {"kind", "resource", "rule_version", "availability", "value"}


class MonitoringStorageError(RuntimeError):
    """Storage failed or history cannot be read completely and faithfully."""


class DuplicateObservationError(MonitoringStorageError):
    """The observation ID is already stored; the original was not replaced."""


def _timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is not UTC or value.fold != 0:
        raise ValueError("Storage v1 requires a standard UTC datetime with fold=0")
    return value.isoformat(timespec="microseconds")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonstandard JSON constant")


def _fields(value, expected):
    if not isinstance(value, dict) or value.keys() != expected:
        raise ValueError("Invalid or missing stored fields")


def _decode(observation_id: str, version: int, payload: str) -> Observation:
    if type(version) is not int or version != _VERSION:
        raise MonitoringStorageError("Unsupported observation storage-format version")
    try:
        data = json.loads(payload, object_pairs_hook=_object, parse_constant=_invalid_constant)
        _fields(data, _OBSERVATION_FIELDS)
        if data["observation_id"] != observation_id:
            raise ValueError("Observation ID does not match storage key")
        raw_time = data["checked_at"]
        checked_at = datetime.fromisoformat(raw_time)
        if _timestamp(checked_at) != raw_time:
            raise ValueError("Noncanonical storage timestamp")
        if not isinstance(data["evidence"], list):
            raise ValueError("Stored evidence must be an array")
        items = []
        for item in data["evidence"]:
            _fields(item, _EVIDENCE_FIELDS)
            items.append(Evidence(**item))
        return Observation(**{**data, "checked_at": checked_at, "evidence": tuple(items)})
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise MonitoringStorageError("Invalid stored observation or evidence") from exc


def _encode(observation: Observation) -> str:
    try:
        if not isinstance(observation, Observation):
            raise ValueError("append requires an Observation")
        checked_at = _timestamp(observation.checked_at)
        data = asdict(observation)
        data["checked_at"] = checked_at
        payload = json.dumps(data, ensure_ascii=True, allow_nan=False)
        # Validate both sides and reject representations that JSON changes.
        if _decode(observation.observation_id, _VERSION, payload) != observation:
            raise ValueError("Storage v1 requires an exact observation round trip")
        return payload
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise MonitoringStorageError("Observation cannot be encoded in storage v1") from exc


class MonitoringStore:
    """A caller-selected DuckDB file, with an existing parent directory.

    history() returns all observations ordered by observation_id (Python Unicode
    code-point order), independent of insertion time, source, or check status.
    The ordering has no baseline-selection meaning. Instances are not thread-safe.
    """

    def __init__(self, path: str | Path):
        self._connection = None
        try:
            # Absolute filesystem paths also prevent DuckDB's special in-memory
            # names from accidentally satisfying a persistent-store request.
            destination = Path(path).expanduser().resolve()
            self._connection = duckdb.connect(str(destination))
            with self._transaction() as connection:
                tables = connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_catalog = current_database()"
                ).fetchall()
                if not tables:
                    connection.execute(
                        "CREATE TABLE monitoring_format (format_version INTEGER NOT NULL)"
                    )
                    connection.execute("INSERT INTO monitoring_format VALUES (?)", [_VERSION])
                    connection.execute(
                        "CREATE TABLE monitoring_observations ("
                        "observation_id VARCHAR PRIMARY KEY, "
                        "format_version INTEGER NOT NULL, payload VARCHAR NOT NULL)"
                    )
                # Missing or incompatible tables must not be recreated as empty history.
                self._validate_schema(connection)
                self._validate_format(connection)
        except BaseException as exc:
            try:
                self.close()
            except MonitoringStorageError:
                exc.add_note("Closing the failed store also failed")
            if isinstance(exc, MonitoringStorageError) or not isinstance(exc, Exception):
                raise
            raise MonitoringStorageError("Cannot open monitoring store") from exc

    def _require_connection(self):
        if self._connection is None:
            raise MonitoringStorageError("Monitoring store is closed")
        return self._connection

    @staticmethod
    def _validate_schema(connection):
        required_tables = {
            "monitoring_format": {"format_version": "INTEGER"},
            "monitoring_observations": {
                "observation_id": "VARCHAR",
                "format_version": "INTEGER",
                "payload": "VARCHAR",
            },
        }
        for table, required_columns in required_tables.items():
            rows = connection.execute(
                'SELECT name, type, "notnull", pk FROM pragma_table_info(?)', [table]
            ).fetchall()
            columns = {name: (kind, not_null) for name, kind, not_null, _ in rows}
            for name, kind in required_columns.items():
                if columns.get(name) != (kind, True):
                    raise MonitoringStorageError(
                        f"Incompatible monitoring schema: {table}.{name} requires {kind} NOT NULL"
                    )
            # A composite key would still allow multiple records with the same ID.
            if table == "monitoring_observations" and {
                name for name, _, _, primary_key in rows if primary_key
            } != {"observation_id"}:
                raise MonitoringStorageError(
                    "Incompatible monitoring schema: observation_id must be the sole primary key"
                )

    @staticmethod
    def _validate_format(connection):
        rows = connection.execute("SELECT format_version FROM monitoring_format").fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != _VERSION:
            raise MonitoringStorageError("Invalid or unsupported store storage-format version")

    @contextmanager
    def _transaction(self):
        connection = self._require_connection()
        try:
            connection.execute("BEGIN TRANSACTION")
        except duckdb.Error as exc:
            raise MonitoringStorageError("Cannot start monitoring transaction") from exc
        try:
            yield connection
            connection.commit()
        except BaseException as exc:
            try:
                connection.rollback()
            except Exception:
                exc.add_note("Rollback failed; reconcile by observation_id after reopening")
                try:
                    self.close()
                except MonitoringStorageError:
                    exc.add_note("Closing the failed store also failed")
            if isinstance(exc, MonitoringStorageError) or not isinstance(exc, Exception):
                raise
            raise MonitoringStorageError(
                "Monitoring transaction failed; reconcile uncertain saves by observation_id"
            ) from exc

    def append(self, observation: Observation) -> None:
        """Commit one complete observation, or raise an explicit storage error.

        A normal return acknowledges commit. An error is not proof of absence if
        commit acknowledgement was lost; reconcile against history after reopen.
        """
        self._require_connection()
        payload = _encode(observation)
        with self._transaction() as connection:
            self._validate_format(connection)
            existing = connection.execute(
                "SELECT 1 FROM monitoring_observations WHERE observation_id = ?",
                [observation.observation_id],
            ).fetchone()
            if existing is not None:
                raise DuplicateObservationError("Observation ID already exists")
            connection.execute(
                "INSERT INTO monitoring_observations "
                "(observation_id, format_version, payload) VALUES (?, ?, ?)",
                [observation.observation_id, _VERSION, payload],
            )

    def history(self) -> tuple[Observation, ...]:
        """Return a complete validated snapshot; never yield a partial history."""
        with self._transaction() as connection:
            self._validate_format(connection)
            rows = connection.execute(
                "SELECT observation_id, format_version, payload FROM monitoring_observations"
            ).fetchall()
            observations = [_decode(*row) for row in rows]
            if len({item.observation_id for item in observations}) != len(observations):
                raise MonitoringStorageError("Duplicate observation IDs in stored history")
            result = tuple(sorted(observations, key=lambda item: item.observation_id))
        return result

    def close(self) -> None:
        """Close the connection; repeated calls are harmless."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except duckdb.Error as exc:
                raise MonitoringStorageError("Cannot close monitoring store") from exc

    def __enter__(self):
        self._require_connection()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except MonitoringStorageError:
            if exc is None:
                raise
            exc.add_note("Closing the monitoring store also failed")
        return False
