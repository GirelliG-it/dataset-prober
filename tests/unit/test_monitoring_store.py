"""Offline persistence acceptance tests using real temporary DuckDB files."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from itertools import permutations, product
from zoneinfo import ZoneInfo

import duckdb
import pytest

from dataset_prober.monitoring import (
    Availability,
    ChangeStatus,
    CheckStatus,
    Evidence,
    EvidenceKind,
    Observation,
    compare_observation,
)
from dataset_prober.monitoring_store import (
    DuplicateObservationError,
    MonitoringStorageError,
    MonitoringStore,
)


def signal(value="2026-09-14T09:12:34.1234567+02:00", **overrides):
    return Evidence(
        **{
            "kind": EvidenceKind.MODIFIED,
            "resource": "https://example.test/metadata",
            "rule_version": "verbatim-v2",
            "availability": Availability.PRESENT,
            "value": value,
            **overrides,
        }
    )


def observation(identifier="monday", day=14, **overrides):
    return Observation(
        **{
            "observation_id": identifier,
            "source_id": "cbs:synthetic",
            "checked_at": datetime(2026, 9, day, 12, 34, 56, 123456, tzinfo=UTC),
            "check_status": CheckStatus.SUCCEEDED,
            "evidence": (signal(),),
            "failure_reason": None,
            "contract_version": "contract-7",
            "checker_version": "checker-9",
            **overrides,
        }
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / "monitoring.duckdb"


def persist(path, observations):
    with MonitoringStore(path) as store:
        for item in observations:
            store.append(item)
    with MonitoringStore(path) as store:
        return store.history()


def raw_rows(path):
    with duckdb.connect(str(path)) as connection:
        return connection.execute(
            "SELECT * FROM monitoring_observations ORDER BY observation_id"
        ).fetchall()


def database_snapshot(path):
    with duckdb.connect(str(path)) as connection:
        return (
            connection.execute(
                "SELECT table_name, sql FROM duckdb_tables() ORDER BY table_name"
            ).fetchall(),
            connection.execute("SELECT * FROM monitoring_format").fetchall(),
            connection.execute(
                "SELECT * FROM monitoring_observations ORDER BY observation_id"
            ).fetchall(),
        )


def redefine_table(path, table, definition, columns):
    with duckdb.connect(str(path)) as connection:
        connection.execute(f"ALTER TABLE {table} RENAME TO original")
        connection.execute(f"CREATE TABLE {table} ({definition})")
        connection.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM original")
        connection.execute("DROP TABLE original")


@pytest.mark.parametrize(
    "table, definition",
    [
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR NOT NULL, format_version INTEGER NOT NULL, payload VARCHAR NOT NULL",
            id="missing-primary-key",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR NOT NULL UNIQUE, format_version INTEGER NOT NULL, payload VARCHAR NOT NULL",
            id="unique-is-not-primary-key",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR, format_version INTEGER NOT NULL, payload VARCHAR NOT NULL, PRIMARY KEY (observation_id, format_version)",
            id="composite-primary-key",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id BLOB PRIMARY KEY, format_version INTEGER NOT NULL, payload VARCHAR NOT NULL",
            id="incorrect-id-type",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR PRIMARY KEY, format_version BIGINT NOT NULL, payload VARCHAR NOT NULL",
            id="incorrect-record-version-type",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR PRIMARY KEY, format_version INTEGER NOT NULL, payload BLOB NOT NULL",
            id="incorrect-payload-type",
        ),
        pytest.param(
            "monitoring_format", "format_version BIGINT NOT NULL", id="incorrect-header-type"
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR PRIMARY KEY, format_version INTEGER, payload VARCHAR NOT NULL",
            id="nullable-record-version",
        ),
        pytest.param(
            "monitoring_observations",
            "observation_id VARCHAR PRIMARY KEY, format_version INTEGER NOT NULL, payload VARCHAR",
            id="nullable-payload",
        ),
        pytest.param("monitoring_format", "format_version INTEGER", id="nullable-header"),
    ],
)
def test_incompatible_table_definitions_are_rejected_without_changes(path, table, definition):
    persist(path, [observation()])
    columns = (
        "format_version"
        if table == "monitoring_format"
        else "observation_id, format_version, payload"
    )
    redefine_table(path, table, definition, columns)
    before = database_snapshot(path)
    assert before[1] == [(1,)]
    try:
        with pytest.raises(MonitoringStorageError, match="Incompatible monitoring schema"):
            with MonitoringStore(path):
                pass
    finally:
        assert database_snapshot(path) == before


@pytest.mark.parametrize(
    "columns",
    [
        order
        for order in permutations(("observation_id", "format_version", "payload"))
        if order != ("observation_id", "format_version", "payload")
    ],
)
def test_reordered_columns_support_append_and_reload(path, columns):
    first, second = observation("first"), observation("second", 15)
    persist(path, [first])
    definitions = {
        "observation_id": "VARCHAR PRIMARY KEY",
        "format_version": "INTEGER NOT NULL",
        "payload": "VARCHAR NOT NULL",
    }
    redefine_table(
        path,
        "monitoring_observations",
        ", ".join(f"{name} {definitions[name]}" for name in columns),
        ", ".join(columns),
    )
    schema_before = database_snapshot(path)[0]
    with MonitoringStore(path) as store:
        assert store.history() == (first,)
        store.append(second)
    with MonitoringStore(path) as store:
        assert store.history() == (first, second)
        with pytest.raises(DuplicateObservationError):
            store.append(second)
        assert store.history() == (first, second)
    assert database_snapshot(path)[0] == schema_before


def test_every_field_status_and_evidence_order_survive_reopen(path):
    evidence = (
        signal("  Café\n2026-09-14T09:12:34.1234567  ", kind=EvidenceKind.TITLE),
        signal(),
        signal('W/"unchanged"', kind=EvidenceKind.ETAG),
        signal("00001", kind=EvidenceKind.REVISION),
        signal("hash:v3:aabb", kind=EvidenceKind.SCHEMA),
        *(
            signal(None, resource=f"resource:{state}", availability=state)
            for state in Availability
            if state is not Availability.PRESENT
        ),
    )
    observations = [
        observation("z-success", evidence=evidence),
        observation(
            "a-failed",
            15,
            check_status=CheckStatus.FAILED,
            failure_reason="timeout",
            evidence=evidence,
        ),
        observation(
            "m-blocked",
            16,
            source_id="another-source",
            check_status=CheckStatus.BLOCKED,
            failure_reason="budget",
            evidence=(),
        ),
    ]
    loaded = persist(path, observations)
    assert loaded == tuple(sorted(observations, key=lambda item: item.observation_id))
    assert {item.check_status for item in loaded} == set(CheckStatus)
    assert all(type(item) is Observation and item.checked_at.tzinfo is UTC for item in loaded)
    assert all(type(item) is Evidence for row in loaded for item in row.evidence)
    assert loaded[-1].evidence == evidence
    assert loaded[-1].failure_reason is None


def test_lossy_string_round_trip_is_rejected_without_changing_history(path):
    first = observation(evidence=(signal("\U0001f600"),))
    attempted = observation("attempted", 15, evidence=(signal("\ud83d\ude00"),))
    expected = compare_observation(attempted, [first])
    assert expected.change_status is ChangeStatus.POSSIBLY_UPDATED
    assert persist(path, [first]) == (first,)
    before = raw_rows(path)

    with MonitoringStore(path) as store:
        with pytest.raises(MonitoringStorageError):
            store.append(attempted)
        assert store.history() == (first,)

    with MonitoringStore(path) as store:
        loaded = store.history()
        assert loaded == (first,)
        assert compare_observation(attempted, loaded) == expected
    assert raw_rows(path) == before
    assert attempted.evidence[0].value == "\ud83d\ude00"


@pytest.mark.parametrize("status", [CheckStatus.FAILED, CheckStatus.BLOCKED])
def test_restarts_retain_unsuccessful_attempt_and_compare_against_monday(path, status):
    monday = observation()
    tuesday = observation("tuesday", 15, check_status=status, failure_reason="timeout", evidence=())
    wednesday = observation("wednesday", 16)
    expected = compare_observation(wednesday, [monday, tuesday])
    for item in (monday, tuesday, wednesday):
        with MonitoringStore(path) as store:
            store.append(item)
    with MonitoringStore(path) as store:
        loaded = store.history()
    assert loaded == (monday, tuesday, wednesday)
    for history in permutations(loaded):
        result = compare_observation(loaded[-1], history)
        assert result == expected
        assert result.baseline_observation_id == monday.observation_id
        assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert compare_observation(loaded[1], loaded).change_status is ChangeStatus.UNKNOWN


@pytest.mark.parametrize("conflicting", [False, True])
@pytest.mark.parametrize("additional_check", [False, True])
def test_duplicate_rejected_and_connection_remains_usable(path, conflicting, additional_check):
    original = observation()
    duplicate = replace(original, source_id="different") if conflicting else original
    persist(path, [original])
    if additional_check:
        redefine_table(
            path,
            "monitoring_observations",
            "observation_id VARCHAR PRIMARY KEY CHECK (observation_id <> 'new'), "
            "format_version INTEGER NOT NULL, payload VARCHAR NOT NULL",
            "observation_id, format_version, payload",
        )
    with MonitoringStore(path) as store:
        with pytest.raises(DuplicateObservationError):
            store.append(duplicate)
        assert store.history() == (original,)
        store.append(observation("next", 15))
    with MonitoringStore(path) as store:
        assert store.history() == (original, observation("next", 15))


@pytest.mark.parametrize("populated", [False, True])
def test_check_constraint_failure_is_not_a_duplicate_and_preserves_history(path, populated):
    originals = (observation(),) if populated else ()
    persist(path, originals)
    redefine_table(
        path,
        "monitoring_observations",
        "observation_id VARCHAR PRIMARY KEY CHECK (observation_id <> 'new'), "
        "format_version INTEGER NOT NULL, payload VARCHAR NOT NULL",
        "observation_id, format_version, payload",
    )
    before = database_snapshot(path)
    with MonitoringStore(path) as store:
        with pytest.raises(MonitoringStorageError) as error:
            store.append(observation("new", 15))
        assert not isinstance(error.value, DuplicateObservationError)
        assert isinstance(error.value.__cause__, duckdb.ConstraintException)
        assert store.history() == originals
    with MonitoringStore(path) as store:
        assert store.history() == originals
    assert database_snapshot(path) == before


class CommitFailure:
    """Inject failure at the real transaction boundary, after the actual insert."""

    def __init__(self, connection, *, committed=False):
        self.connection = connection
        self.committed = committed
        self.fail = True

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        if self.fail:
            self.fail = False
            assert self.connection.execute(
                "SELECT count(*) FROM monitoring_observations"
            ).fetchone() == (2,)
            if self.committed:
                self.connection.commit()
            raise RuntimeError("injected commit failure")
        return self.connection.commit()


def test_failure_after_insert_rolls_back_entire_observation(path, monkeypatch):
    first = observation()
    attempted = observation("attempted", 15, evidence=(signal(), signal("r2", kind="revision")))
    with MonitoringStore(path) as store:
        store.append(first)
        monkeypatch.setattr(store, "_connection", CommitFailure(store._connection))
        with pytest.raises(MonitoringStorageError) as error:
            store.append(attempted)
        assert isinstance(error.value.__cause__, RuntimeError)
        assert store.history() == (first,)
    with MonitoringStore(path) as store:
        assert store.history() == (first,)
    assert attempted.check_status is CheckStatus.SUCCEEDED
    assert attempted.failure_reason is None


def test_uncertain_commit_is_reconciled_from_history_without_replacement(path, monkeypatch):
    first, second = observation(), observation("second", 15)
    with MonitoringStore(path) as store:
        store.append(first)
        monkeypatch.setattr(store, "_connection", CommitFailure(store._connection, committed=True))
        with pytest.raises(MonitoringStorageError):
            store.append(second)
    with MonitoringStore(path) as store:
        assert {item.observation_id: item for item in store.history()}[
            second.observation_id
        ] == second
        with pytest.raises(DuplicateObservationError):
            store.append(second)


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 14, 12, 0, 0, 999999, tzinfo=UTC),
        datetime(2026, 9, 14, 23, 59, 59, 999999, tzinfo=UTC),
    ],
)
def test_microsecond_ordering_and_baseline_eligibility_survive_storage(path, instant):
    current = observation("current", checked_at=instant)
    earlier = replace(
        current, observation_id="earlier", checked_at=instant - timedelta(microseconds=1)
    )
    equal = replace(current, observation_id="equal")
    later = replace(current, observation_id="later", checked_at=instant + timedelta(microseconds=1))
    original = (later, current, equal, earlier)
    loaded = persist(path, original)
    by_id = {item.observation_id: item for item in loaded}
    for a, b in product(original, repeat=2):
        x, y = by_id[a.observation_id].checked_at, by_id[b.observation_id].checked_at
        assert (x == y, x < y, x > y) == (
            a.checked_at == b.checked_at,
            a.checked_at < b.checked_at,
            a.checked_at > b.checked_at,
        )
        assert x.microsecond == a.checked_at.microsecond
    expected = compare_observation(current, original)
    for history in permutations(loaded):
        assert compare_observation(by_id["current"], history) == expected
    assert expected.baseline_observation_id == "earlier"
    assert compare_observation(current, [equal, later]).baseline_observation_id is None


def test_equal_latest_timestamps_remain_ambiguous_after_reload(path):
    first = observation("first")
    tied = replace(first, observation_id="tied")
    current = observation("current", 16)
    loaded = persist(path, [current, tied, first])
    for history in permutations(loaded):
        result = compare_observation(current, history)
        assert result == compare_observation(current, [first, tied])
        assert result.change_status is ChangeStatus.UNKNOWN
        assert result.baseline_observation_id is None


class CustomUTC(tzinfo):
    def utcoffset(self, dt):
        return timedelta(0)


class CustomDatetime(datetime):
    pass


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 9, 14),
        datetime(2026, 9, 14, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 9, 14, tzinfo=timezone(timedelta(0), "custom-zero")),
        datetime(2026, 9, 14, tzinfo=CustomUTC()),
        datetime(2026, 9, 14, tzinfo=UTC, fold=1),
        CustomDatetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 14, tzinfo=ZoneInfo("UTC")),
        *(
            datetime(2026, month, day, hour, 30, tzinfo=ZoneInfo("Europe/Amsterdam"), fold=fold)
            for month, day, hour, fold in [
                (3, 29, 1, 0),
                (3, 29, 2, 0),
                (3, 29, 3, 0),
                (10, 25, 2, 0),
                (10, 25, 2, 1),
                (10, 25, 3, 0),
            ]
        ),
    ],
)
def test_unsupported_timestamps_are_rejected_without_normalization(path, timestamp):
    first = observation()
    invalid = observation("unsupported")
    # Naive datetimes cannot pass normal model validation; exercise the storage
    # boundary defensively as well, without changing the model's validator.
    object.__setattr__(invalid, "checked_at", timestamp)
    with MonitoringStore(path) as store:
        store.append(first)
        with pytest.raises(MonitoringStorageError):
            store.append(invalid)
        assert store.history() == (first,)
    assert invalid.checked_at is timestamp


@pytest.mark.parametrize(
    "raw_time",
    [
        "2026-09-14T12:34:56+00:00",
        "2026-09-14T12:34:56.123456Z",
        "2026-09-14T12:34:56.1234567+00:00",
        "2026-09-14T12:34:56.123456+02:00",
        "2026-09-14 12:34:56.123456+00:00",
        "2026-09-14T12:34:56.123456",
        None,
    ],
)
def test_noncanonical_stored_timestamps_raise_storage_error(path, raw_time):
    persist(path, [observation()])
    payload = json.loads(raw_rows(path)[0][2])
    payload["checked_at"] = raw_time
    with duckdb.connect(str(path)) as connection:
        connection.execute("UPDATE monitoring_observations SET payload = ?", [json.dumps(payload)])
    with MonitoringStore(path) as store:
        with pytest.raises(MonitoringStorageError):
            store.history()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_source",
        "missing_default",
        "failed_without_reason",
        "bad_status",
        "present_null",
        "unavailable_value",
        "duplicate_evidence",
        "missing_evidence_value",
        "bad_evidence_kind",
        "evidence_not_array",
        "extra_field",
        "id_mismatch",
        "malformed_json",
        "duplicate_json_field",
        "nan",
        "non_object",
    ],
)
@pytest.mark.parametrize("mixed", [False, True])
def test_invalid_records_fail_the_whole_history_without_repair(path, corruption, mixed):
    originals = [observation("bad")]
    if mixed:
        originals += [observation("a-valid"), observation("z-valid", 16)]
    persist(path, originals)
    data = json.loads(next(row[2] for row in raw_rows(path) if row[0] == "bad"))
    if corruption == "missing_source":
        del data["source_id"]
    elif corruption == "missing_default":
        del data["contract_version"]
    elif corruption == "failed_without_reason":
        data["check_status"] = "FAILED"
    elif corruption == "bad_status":
        data["check_status"] = "BOGUS"
    elif corruption == "present_null":
        data["evidence"][0]["value"] = None
    elif corruption == "unavailable_value":
        data["evidence"][0]["availability"] = "NOT_CHECKED"
    elif corruption == "duplicate_evidence":
        data["evidence"] *= 2
    elif corruption == "missing_evidence_value":
        del data["evidence"][0]["value"]
    elif corruption == "bad_evidence_kind":
        data["evidence"][0]["kind"] = "BOGUS"
    elif corruption == "evidence_not_array":
        data["evidence"] = {}
    elif corruption == "extra_field":
        data["unexpected"] = "ignored?"
    elif corruption == "id_mismatch":
        data["observation_id"] = "another"
    payload = json.dumps(data)
    if corruption == "duplicate_json_field":
        # Keep every required field valid: accepting the repeated key would
        # otherwise reconstruct the original complete observation successfully.
        payload = payload[:-1] + ', "source_id": ' + json.dumps(data["source_id"]) + "}"
        assert json.loads(payload) == data
    payload = {
        "malformed_json": "{",
        "nan": '{"source_id":NaN}',
        "non_object": "null",
    }.get(corruption, payload)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "UPDATE monitoring_observations SET payload = ? WHERE observation_id = 'bad'", [payload]
        )
    before = raw_rows(path)
    with MonitoringStore(path) as store:
        with pytest.raises(MonitoringStorageError, match="Invalid stored") as error:
            store.history()
        if corruption == "duplicate_json_field":
            assert str(error.value.__cause__) == "Duplicate JSON field"
    assert raw_rows(path) == before


@pytest.mark.parametrize("mixed", [False, True])
def test_unsupported_record_version_never_returns_partial_history(path, mixed):
    persist(path, [observation("bad")] + ([observation("valid")] if mixed else []))
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "UPDATE monitoring_observations SET format_version = 2 WHERE observation_id = 'bad'"
        )
    before = raw_rows(path)
    with MonitoringStore(path) as store:
        with pytest.raises(MonitoringStorageError, match="Unsupported"):
            store.history()
    assert raw_rows(path) == before


@pytest.mark.parametrize("header", [(2,), (), (1, 2)])
def test_invalid_or_unsupported_store_version_is_not_reinitialized(path, header):
    persist(path, [observation()])
    before = raw_rows(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute("DELETE FROM monitoring_format")
        for version in header:
            connection.execute("INSERT INTO monitoring_format VALUES (?)", [version])
    with pytest.raises(MonitoringStorageError, match="storage-format version"):
        MonitoringStore(path)
    assert raw_rows(path) == before
    with duckdb.connect(str(path)) as connection:
        assert connection.execute(
            "SELECT format_version FROM monitoring_format ORDER BY 1"
        ).fetchall() == [(value,) for value in header]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_id": "different"},
        {"contract_version": "different"},
        {"checker_version": "different"},
        {"evidence": ()},
    ],
)
def test_baseline_compatibility_and_missing_evidence_survive_reload(path, overrides):
    first = observation("first")
    latest = observation("latest", 15, **overrides)
    current = observation("current", 16)
    loaded = persist(path, [latest, first, current])
    expected = compare_observation(current, [first, latest])
    assert compare_observation(loaded[0], loaded) == expected
    if "evidence" in overrides:
        assert expected.baseline_observation_id == "latest"
        assert expected.change_status is ChangeStatus.UNKNOWN
    else:
        assert expected.baseline_observation_id == "first"


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": EvidenceKind.ETAG},
        {"resource": "other"},
        {"rule_version": "other"},
        {"value": None, "availability": Availability.CHECK_FAILED},
    ],
)
def test_partial_evidence_and_incompatible_keys_keep_comparison_results(path, overrides, changed):
    first = observation(evidence=(signal(), signal("r1", kind="revision")))
    current = observation(
        "current",
        16,
        evidence=(
            replace(signal(), **overrides),
            signal("r2" if changed else "r1", kind="revision"),
        ),
    )
    loaded = persist(path, [first, current])
    expected = compare_observation(current, [first])
    assert compare_observation(loaded[0], loaded) == expected
    assert expected.change_status is (
        ChangeStatus.POSSIBLY_UPDATED if changed else ChangeStatus.UNKNOWN
    )


def test_lifecycle_empty_history_and_context_exception(path):
    with pytest.raises(RuntimeError, match="caller failure"):
        with MonitoringStore(str(path)) as store:
            assert store.history() == ()
            store.append(observation())
            raise RuntimeError("caller failure")
    store.close()
    with pytest.raises(MonitoringStorageError, match="closed"):
        store.history()
    with pytest.raises(MonitoringStorageError, match="closed"):
        store.append(observation("next"))
    with MonitoringStore(path) as reopened:
        assert reopened.history() == (observation(),)


def test_missing_parent_and_unrelated_database_fail_explicitly(path):
    with pytest.raises(MonitoringStorageError):
        MonitoringStore(path / "missing" / "file.duckdb")
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
        connection.execute("INSERT INTO unrelated VALUES (42)")
    with pytest.raises(MonitoringStorageError):
        MonitoringStore(path)
    with duckdb.connect(str(path)) as connection:
        assert connection.execute("SHOW TABLES").fetchall() == [("unrelated",)]
        assert connection.execute("SELECT * FROM unrelated").fetchall() == [(42,)]


def test_unrelated_table_in_nondefault_schema_is_rejected_without_changes(path):
    schemas_query = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE catalog_name = current_database() ORDER BY schema_name"
    )
    tables_query = (
        "SELECT schema_name, table_name, sql FROM duckdb_tables() ORDER BY schema_name, table_name"
    )
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE SCHEMA other")
        connection.execute("CREATE TABLE other.unrelated (value INTEGER)")
        connection.execute("INSERT INTO other.unrelated VALUES (42)")
        schemas_before = connection.execute(schemas_query).fetchall()
        tables_before = connection.execute(tables_query).fetchall()
        data_before = connection.execute("SELECT * FROM other.unrelated").fetchall()

    with pytest.raises(MonitoringStorageError):
        with MonitoringStore(path):
            pass

    with duckdb.connect(str(path)) as connection:
        assert connection.execute(schemas_query).fetchall() == schemas_before
        assert connection.execute(tables_query).fetchall() == tables_before
        assert connection.execute("SELECT * FROM other.unrelated").fetchall() == data_before
