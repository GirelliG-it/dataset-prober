"""Offline transport and comparison checks for the opt-in metadata experiment."""

import importlib.util
import io
import json
import signal
import socket
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from dataset_prober.monitoring import Evidence, Observation, compare_observation
from dataset_prober.monitoring_store import (
    DuplicateObservationError,
    MonitoringStorageError,
    MonitoringStore,
)
from dataset_prober.tools.guards import SafeTransportError, UnsafeURLError, _PinnedConnector

PATH = Path(__file__).resolve().parents[2] / "experiments/cbs_metadata.py"
SPEC = importlib.util.spec_from_file_location("cbs_metadata_experiment", PATH)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def resolver(_host, port, **_kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
        for address in ("93.184.216.34", "93.184.216.35")
    ]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def utc(self):
        return datetime(2026, 9, 16, tzinfo=UTC) + timedelta(seconds=self.now)


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.body = io.BytesIO(body if body is not None else metadata())
        self.closed = False
        self.read_sizes = []

    def read(self, amount):
        self.read_sizes.append(amount)
        return self.body.read(amount)

    def close(self):
        self.closed = True


def metadata(**overrides):
    return json.dumps(
        {
            "value": [
                {
                    "Identifier": experiment.TABLE_ID,
                    "Title": "Synthetic metadata fixture",
                    "Modified": "2026-09-14T09:12:34.1234567",
                    "MetaDataModified": "2026-09-14T09:12:34.1234567+02:00",
                    **overrides,
                }
            ]
        }
    ).encode()


class Wire:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, target, method, headers, deadline):
        assert self.responses, "Unexpected extra request"
        assert len(target.addresses) == 1
        assert method == "GET"
        self.calls.append((target, deadline.remaining(target.url)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def runner(*responses, wire=None):
    clock = Clock()
    wire = wire if wire is not None else Wire(*responses)
    run = experiment.Experiment(
        resolver=resolver, connector=wire, clock=clock, utc_now=clock.utc, sleep=clock.sleep
    )
    return run, wire, clock


def test_two_separate_metadata_requests_preserve_precision_and_compare():
    first, second = Response(), Response()
    run, wire, _clock = runner(first, second)
    report = run.run()
    assert len(wire.calls) == report["connection_attempts"] == 2
    assert report["http_responses_received"] == 2
    assert [item["change_status"] for item in report["comparisons"]] == [
        "BASELINE",
        "NO_CHANGE_DETECTED",
    ]
    assert all(target.url.startswith(experiment.ENDPOINT) for target, _timeout in wire.calls)
    assert all(0 < timeout <= 15 for _target, timeout in wire.calls)
    assert first.closed and second.closed
    assert report["observations"][1]["checked_at"] > report["observations"][0]["checked_at"]
    values = report["observations"][0]["evidence"]
    assert values[0]["value"] == "2026-09-14T09:12:34.1234567"
    assert values[1]["value"] == "2026-09-14T09:12:34.1234567+02:00"
    assert values[3]["availability"] == "NOT_PROVIDED"
    assert values[4]["availability"] == values[5]["availability"] == "NOT_CHECKED"
    assert (
        report["comparisons"][1]["baseline_observation_id"]
        == report["observations"][0]["observation_id"]
    )


def test_changed_metadata_validator_is_scoped_to_metadata_only():
    run, _wire, _clock = runner(
        Response(headers={"ETag": '"one"'}), Response(headers={"ETag": '"two"'})
    )
    report = run.run()
    assert report["comparisons"][1]["change_status"] == "POSSIBLY_UPDATED"
    signal = next(item for item in report["comparisons"][1]["evidence"] if item["kind"] == "etag")
    assert signal["resource"] == experiment.ENDPOINT
    assert "metadata resource only" in report["outcomes"][0]["origins"]["HTTP-ETag"]


@pytest.mark.parametrize(
    "value, availability", [(None, "NOT_PROVIDED"), (5, "INVALID"), ("", "INVALID")]
)
def test_unavailable_timestamp_has_explicit_reason(value, availability):
    run, _wire, _clock = runner(
        Response(metadata(Modified=value)), Response(metadata(Modified=value))
    )
    report = run.run()
    assert report["observations"][0]["evidence"][0]["availability"] == availability


@pytest.mark.parametrize(
    "body", [b"<html>unavailable</html>", b"{}", metadata(Identifier="another-table")]
)
def test_unavailable_metadata_stops_without_retry_or_replacement(body):
    run, wire, _clock = runner(Response(body))
    report = run.run()
    assert len(wire.calls) == 1
    assert [item["check_status"] for item in report["observations"]] == ["FAILED", "BLOCKED"]
    assert all(item["change_status"] == "UNKNOWN" for item in report["comparisons"])
    assert report["observations"][0]["evidence"][0]["availability"] == "CHECK_FAILED"
    skipped = report["observations"][1]
    assert skipped["failure_reason"] == "previous_attempt_failed"
    assert report["outcomes"][1]["connection_attempts"] == 0
    assert all(item["availability"] == "NOT_CHECKED" for item in skipped["evidence"])


@pytest.mark.parametrize(
    "content_type, accepted",
    [
        ("application/json", True),
        ("APPLICATION/JSON", True),
        ("application/json; charset=utf-8", True),
        (" Application/Json ; charset=UTF-8", True),
        ("application/jsonp", False),
        ("application/jsonp; charset=utf-8", False),
        ("application/json-extra", False),
        ("text/json", False),
        ("text/html", False),
        ("", False),
    ],
)
def test_metadata_media_type_must_be_exact_json(content_type, accepted):
    first = Response(headers={"Content-Type": content_type})
    run, wire, _clock = runner(first, Response())
    report = run.run()
    assert first.closed
    if accepted:
        assert len(wire.calls) == 2
        assert all(item["check_status"] == "SUCCEEDED" for item in report["observations"])
    else:
        assert len(wire.calls) == 1
        assert report["observations"][0]["check_status"] == "FAILED"
        assert report["observations"][0]["failure_reason"] == "UnsafeResourceError"
        assert report["comparisons"][0]["change_status"] == "UNKNOWN"


def test_main_saves_nested_json_failure_without_network(tmp_path, monkeypatch):
    body = b"[" * 10000 + b"0" + b"]" * 10000
    response = Response(body)
    run, wire, _clock = runner(response)

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected real network activity")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(experiment, "Experiment", lambda: run)
    monkeypatch.setattr(experiment, "__file__", str(tmp_path / "experiments" / "cbs_metadata.py"))
    monkeypatch.setattr(sys, "argv", ["cbs_metadata.py", "--live"])
    assert experiment.main() == 1
    paths = list((tmp_path / "output" / "cbs-metadata-experiment").glob("*/report.json"))
    assert len(paths) == 1
    report = json.loads(paths[0].read_text())
    assert len(wire.calls) == report["connection_attempts"] == 1
    assert response.closed
    assert report["requests"][0]["status"] == 200
    assert report["outcomes"][0]["body_bytes"] == len(body)
    failed, skipped = report["observations"]
    assert failed["check_status"] == "FAILED"
    assert failed["failure_reason"] == "RecursionError"
    assert all(item["availability"] == "CHECK_FAILED" for item in failed["evidence"][:4])
    assert skipped["check_status"] == "BLOCKED"
    assert skipped["failure_reason"] == "previous_attempt_failed"
    assert all(item["availability"] == "NOT_CHECKED" for item in skipped["evidence"])
    assert all(item["change_status"] == "UNKNOWN" for item in report["comparisons"])


def test_four_request_limit_includes_redirects():
    def redirect():
        return Response(status=302, headers={"Location": experiment.ENDPOINT + "/"})

    run, wire, _clock = runner(redirect(), Response(), redirect(), Response())
    report = run.run()
    assert len(wire.calls) == report["connection_attempts"] == 4
    assert [item["status"] for item in report["requests"]] == [302, 200, 302, 200]
    with pytest.raises(UnsafeURLError, match="request_cap"):
        run.client.get(experiment.ENDPOINT, timeout=1)
    assert len(wire.calls) == 4


@pytest.mark.parametrize(
    "location",
    [
        "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet",
        "https://another.example/TableInfos",
        experiment.ENDPOINT + "?$top=1",
    ],
)
def test_redirect_cannot_reach_records_or_another_resource(location):
    run, wire, _clock = runner(Response(status=302, headers={"Location": location}))
    report = run.run()
    assert len(wire.calls) == 1
    assert report["observations"][0]["check_status"] == "BLOCKED"


def test_declared_and_undeclared_oversize_are_rejected_with_bounded_reads():
    for headers in ({}, {"Content-Length": str(experiment.MAX_BYTES + 20)}):
        response = Response(b"x" * (experiment.MAX_BYTES + 20), headers=headers)
        run, wire, _clock = runner(response)
        report = run.run()
        assert report["observations"][0]["check_status"] == "FAILED"
        assert len(wire.calls) == 1
        assert response.closed
        assert response.body.tell() <= experiment.MAX_BYTES
        assert not response.read_sizes if headers else response.read_sizes == [experiment.MAX_BYTES]


def test_real_pinned_connector_cannot_retry_a_second_address():
    calls = []

    class FailedSocket:
        def settimeout(self, seconds):
            assert 0 < seconds <= 15

        def connect(self, address):
            calls.append(address)
            raise OSError("synthetic connection failure")

        def close(self):
            pass

    wire = _PinnedConnector(socket_factory=lambda *args: FailedSocket())
    run, _wire, _clock = runner(wire=wire)
    report = run.run()
    assert len(calls) == report["connection_attempts"] == 1
    assert report["http_responses_received"] == 0


def test_deadline_stops_next_request_and_caps_remaining_timeout():
    run, wire, clock = runner(Response(), Response())
    run.deadline = 60
    clock.now = 54
    _observation, outcome = run.collect(1)
    assert outcome["timeout_seconds"] == 6
    assert 0 < wire.calls[0][1] <= 6
    clock.now = 60
    observation, _outcome = run.collect(2)
    assert observation.check_status == "BLOCKED"
    assert observation.failure_reason == "overall_deadline"
    assert _outcome["connection_attempts"] == 0
    assert all(item.availability == "NOT_CHECKED" for item in observation.evidence)
    assert len(wire.calls) == 1


def test_hard_timer_interrupts_blocking_operation_and_restores_handler():
    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(experiment.ExperimentDeadline):
        with experiment.hard_timeout(0.02):
            time.sleep(1)
    assert time.monotonic() - started < 0.8
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_nested_request_timer_cannot_extend_overall_deadline():
    started = time.monotonic()
    with pytest.raises(experiment.ExperimentDeadline):
        with experiment.hard_timeout(0.02):
            with experiment.hard_timeout(1):
                time.sleep(1)
    assert time.monotonic() - started < 0.8
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_stalled_body_is_interrupted_closed_and_not_retried(monkeypatch):
    class StalledResponse(Response):
        def read(self, amount):
            time.sleep(1)
            return b""

    response = StalledResponse()
    run, wire, _clock = runner(response)
    monkeypatch.setattr(experiment, "REQUEST_SECONDS", 0.02)
    report = run.run()
    assert response.closed
    assert len(wire.calls) == 1
    assert report["observations"][0]["failure_reason"] == "ExperimentDeadline"
    assert report["observations"][1]["check_status"] == "BLOCKED"


def test_sanitized_artifacts_omit_cookies_sensitive_urls_and_raw_errors():
    sensitive = "https://user:password@example.test/x?token=secret#private"
    run, _wire, _clock = runner(
        Response(metadata(Title=sensitive), headers={"Set-Cookie": "secret", "ETag": sensitive}),
        SafeTransportError(sensitive),
    )
    report = run.run()
    serialized = json.dumps(report, default=str)
    for secret in ("user:password", "token=secret", "#private", "Set-Cookie"):
        assert secret not in serialized
    assert report["observations"][0]["evidence"][2]["availability"] == "INVALID"
    assert report["observations"][1]["failure_reason"] == "SafeTransportError"


def test_existing_guard_still_blocks_private_dns_before_connection():
    wire = Wire()
    run = experiment.Experiment(
        resolver=lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
        connector=wire,
    )
    report = run.run()
    assert report["observations"][0]["check_status"] == "BLOCKED"
    assert wire.calls == []


@pytest.fixture
def database(tmp_path, monkeypatch):
    # Deterministic UUIDs and an explicit temporary destination for every test.
    identifiers = iter(range(1, 100))
    monkeypatch.setattr(experiment, "uuid4", lambda: UUID(int=next(identifiers)))
    return tmp_path / "monitoring.duckdb"


def reported_observations(report):
    return tuple(
        Observation(**{**item, "evidence": tuple(Evidence(**e) for e in item["evidence"])})
        for item in report["observations"]
    )


def test_persistence_is_optional_without_a_default_path(monkeypatch):
    def unexpected_store(*args, **kwargs):
        pytest.fail("Persistence was not requested")

    monkeypatch.setattr(experiment, "MonitoringStore", unexpected_store)
    run, _wire, _clock = runner(Response(), Response())
    report = run.run()
    assert report["storage"] == {
        "requested": False,
        "history_count": 0,
        "saves": [],
        "close_error": None,
    }


def test_persistent_runs_reopen_history_with_unique_ids_and_unchanged_comparisons(database):
    first, _wire, _clock = runner(Response(), Response())
    first_report = first.run(database_path=database)
    original = reported_observations(first_report)
    with MonitoringStore(database) as store:
        assert set(store.history()) == set(original)
        # Later incompatible, unsuccessful, equal-time and future entries must
        # not displace the latest strictly earlier compatible success.
        latest = original[-1]
        for identifier, overrides in (
            ("other-source", {"source_id": "other"}),
            ("other-contract", {"contract_version": "other"}),
            ("other-checker", {"checker_version": "other"}),
            ("failed", {"check_status": "FAILED", "failure_reason": "timeout"}),
            ("blocked", {"check_status": "BLOCKED", "failure_reason": "policy"}),
            ("equal", {"checked_at": _clock.utc() + timedelta(days=1) - timedelta(seconds=1)}),
            ("future", {"checked_at": _clock.utc() + timedelta(days=2)}),
        ):
            store.append(
                replace(
                    latest,
                    observation_id=identifier,
                    **{"checked_at": latest.checked_at + timedelta(seconds=1), **overrides},
                )
            )
        history = store.history()
    second, _wire, clock = runner(Response(), Response())
    clock.now = 86400
    report = second.run(database_path=str(database))
    current = reported_observations(report)
    ids = [item.observation_id for item in (*original, *current)]
    assert len(set(ids)) == 4
    assert report["storage"]["history_count"] == len(history)
    assert all(item["status"] == "SAVED" for item in report["storage"]["saves"])
    assert report["comparisons"][0]["baseline_observation_id"] == latest.observation_id
    for index, item in enumerate(current):
        assert report["comparisons"][index] == asdict(
            compare_observation(item, (*history, *current[:index]))
        )
    with MonitoringStore(database) as store:
        assert set(store.history()) == set((*history, *current))


def test_failed_and_blocked_attempts_persist_without_replacing_baseline(database):
    first, _wire, _clock = runner(Response(), Response())
    first_report = first.run(database_path=database)
    baseline_id = first_report["observations"][-1]["observation_id"]
    failed, wire, clock = runner(Response(b"{}"))
    clock.now = 86400
    failed_report = failed.run(database_path=database)
    assert len(wire.calls) == 1
    assert [item["check_status"] for item in failed_report["observations"]] == ["FAILED", "BLOCKED"]
    assert all(item["status"] == "SAVED" for item in failed_report["storage"]["saves"])
    assert all(item["change_status"] == "UNKNOWN" for item in failed_report["comparisons"])
    recovery, _wire, clock = runner(Response(), Response())
    clock.now = 172800
    report = recovery.run(database_path=database)
    assert report["comparisons"][0]["baseline_observation_id"] == baseline_id
    assert report["comparisons"][0]["change_status"] == "NO_CHANGE_DETECTED"
    with MonitoringStore(database) as store:
        assert len(store.history()) == 6
        assert set(reported_observations(failed_report)).issubset(set(store.history()))


def test_id_collision_is_reported_without_replacement_or_new_identity(database, monkeypatch):
    first, _wire, _clock = runner(Response(), Response())
    original = reported_observations(first.run(database_path=database))[0]
    second, _wire, clock = runner(Response(), Response())
    clock.now = 86400
    collect = second.collect

    def colliding_collect(sequence, **kwargs):
        observation, outcome = collect(sequence, **kwargs)
        if sequence == 1:
            observation = replace(observation, observation_id=original.observation_id)
        return observation, outcome

    monkeypatch.setattr(second, "collect", colliding_collect)
    report = second.run(database_path=database)
    assert report["storage"]["saves"][0] == {
        "observation_id": original.observation_id,
        "status": "ERROR",
        "error_type": "DuplicateObservationError",
    }
    assert report["observations"][0]["check_status"] == "SUCCEEDED"
    with MonitoringStore(database) as store:
        history = {item.observation_id: item for item in store.history()}
    assert len(history) == 3
    assert history[original.observation_id] == original


@pytest.mark.parametrize("committed", [False, True])
def test_save_error_keeps_outcomes_and_id_for_reconciliation(database, monkeypatch, committed):
    append = MonitoringStore.append
    attempted = []

    def fail_first(store, observation):
        attempted.append(observation)
        if len(attempted) == 1:
            if committed:
                append(store, observation)
            raise MonitoringStorageError("sensitive diagnostic must not enter report")
        return append(store, observation)

    run, _wire, _clock = runner(Response(), Response())
    with monkeypatch.context() as patch:
        patch.setattr(MonitoringStore, "append", fail_first)
        report = run.run(database_path=database)
    assert [item["status"] for item in report["storage"]["saves"]] == ["ERROR", "SAVED"]
    assert all(
        item["check_status"] == "SUCCEEDED" and item["failure_reason"] is None
        for item in report["observations"]
    )
    assert reported_observations(report) == tuple(attempted)
    for index, item in enumerate(attempted):
        assert report["comparisons"][index] == asdict(compare_observation(item, attempted[:index]))
    assert "sensitive diagnostic" not in json.dumps(report, default=str)
    with MonitoringStore(database) as store:
        if committed:
            assert attempted[0] in store.history()
            with pytest.raises(DuplicateObservationError):
                store.append(attempted[0])
        else:
            assert store.history() == (attempted[1],)
            store.append(attempted[0])  # Retry the exact same object and ID.
    with MonitoringStore(database) as store:
        assert set(store.history()) == set(attempted)


@pytest.mark.parametrize("failure", ["missing_parent", "invalid_history", "unsupported_format"])
def test_storage_open_or_read_failure_stops_before_collection(database, failure):
    if failure == "missing_parent":
        database = database.parent / "missing" / database.name
    else:
        first, _wire, _clock = runner(Response(), Response())
        first.run(database_path=database)
        with duckdb.connect(str(database)) as connection:
            if failure == "invalid_history":
                connection.execute("UPDATE monitoring_observations SET payload = '{}'")
            else:
                connection.execute("UPDATE monitoring_format SET format_version = 2")
    run, wire, _clock = runner()
    with pytest.raises(MonitoringStorageError):
        run.run(database_path=database)
    assert wire.calls == []
    assert run.requests == []
    if failure == "missing_parent":
        assert not database.parent.exists()


@pytest.mark.parametrize(
    "failure, exit_code",
    [
        (None, 0),
        ("collection", 1),
        ("append", 2),
        ("collection_and_append", 2),
        ("open", 2),
    ],
)
def test_main_explicit_database_reports_storage_separately(
    database, monkeypatch, capsys, failure, exit_code
):
    run, wire, _clock = (
        runner(Response(b"{}"))
        if failure in ("collection", "collection_and_append")
        else runner(Response(), Response())
    )
    monkeypatch.setattr(experiment, "Experiment", lambda: run)
    monkeypatch.setattr(
        experiment, "__file__", str(database.parent / "experiments" / "cbs_metadata.py")
    )
    destination = database if failure != "open" else database.parent / "missing" / database.name
    monkeypatch.setattr(sys, "argv", ["cbs_metadata.py", "--live", "--database", str(destination)])
    if failure in ("append", "collection_and_append"):

        def fail_append(store, observation):
            raise MonitoringStorageError("injected save error")

        monkeypatch.setattr(MonitoringStore, "append", fail_append)
    assert experiment.main() == exit_code
    output = capsys.readouterr()
    paths = list((database.parent / "output" / "cbs-metadata-experiment").glob("*/report.json"))
    if failure == "open":
        assert wire.calls == []
        assert not paths
        assert "Monitoring storage error" in output.err
    else:
        assert len(paths) == 1
        report = json.loads(paths[0].read_text())
        assert report["storage"]["requested"] is True
        if failure in ("append", "collection_and_append"):
            assert all(item["status"] == "ERROR" for item in report["storage"]["saves"])
            expected = ["SUCCEEDED", "SUCCEEDED"] if failure == "append" else ["FAILED", "BLOCKED"]
            assert [item["check_status"] for item in report["observations"]] == expected
            assert "Storage: 0/2" in output.out
        else:
            with MonitoringStore(database) as store:
                assert len(store.history()) == 2
            assert "Storage: 2/2" in output.out


def test_database_option_requires_an_explicit_value(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cbs_metadata.py", "--live", "--database"])
    with pytest.raises(SystemExit) as error:
        experiment.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "append_outcome, report_write_fails",
    [("saved", False), ("failed", False), ("uncertain", False), ("saved", True)],
)
def test_close_error_retains_report_and_reconciliation_payloads(
    database, monkeypatch, capsys, append_outcome, report_write_fails
):
    run, wire, _clock = runner(Response(), Response())
    monkeypatch.setattr(experiment, "Experiment", lambda: run)
    monkeypatch.setattr(
        experiment, "__file__", str(database.parent / "experiments" / "cbs_metadata.py")
    )
    monkeypatch.setattr(sys, "argv", ["cbs_metadata.py", "--live", "--database", str(database)])
    append, close, run_body = MonitoringStore.append, MonitoringStore.close, run._run
    attempted, completed, attempted_reports = [], [], []

    def save(store, observation):
        attempted.append(observation)
        if len(attempted) == 1 and append_outcome != "saved":
            if append_outcome == "uncertain":
                append(store, observation)
            raise MonitoringStorageError("private append diagnostic")
        append(store, observation)

    def fail_close(store):
        close(store)
        raise MonitoringStorageError("private close diagnostic")

    def capture_completed(history, store):
        report = run_body(history, store)
        completed.append(json.loads(json.dumps(report, default=lambda value: value.isoformat())))
        return report

    def fail_report_write(path, text, **kwargs):
        attempted_reports.append(json.loads(text))
        raise OSError("injected report write failure")

    with monkeypatch.context() as patch:
        patch.setattr(MonitoringStore, "append", save)
        patch.setattr(MonitoringStore, "close", fail_close)
        patch.setattr(run, "_run", capture_completed)
        if report_write_fails:
            patch.setattr(Path, "write_text", fail_report_write)
        assert experiment.main() == 2

    assert len(wire.calls) == 2
    paths = list((database.parent / "output" / "cbs-metadata-experiment").glob("*/report.json"))
    if report_write_fails:
        assert not paths
        assert len(attempted_reports) == 1
        report = attempted_reports[0]
    else:
        assert len(paths) == 1
        report = json.loads(paths[0].read_text())
    expected = completed[0]
    expected["storage"]["close_error"] = {"error_type": "MonitoringStorageError"}
    assert report == expected
    assert [item["status"] for item in report["storage"]["saves"]] == (
        ["SAVED", "SAVED"] if append_outcome == "saved" else ["ERROR", "SAVED"]
    )
    reconstructed = tuple(
        Observation(
            **{
                **item,
                "checked_at": datetime.fromisoformat(item["checked_at"]),
                "evidence": tuple(Evidence(**evidence) for evidence in item["evidence"]),
            }
        )
        for item in report["observations"]
    )
    assert reconstructed == tuple(attempted)
    output = capsys.readouterr()
    assert "private" not in json.dumps(report) + output.out + output.err
    with MonitoringStore(database) as store:
        if append_outcome == "failed":
            assert store.history() == (reconstructed[1],)
            store.append(reconstructed[0])  # Retry the report payload with its original ID.
        else:
            assert set(store.history()) == set(reconstructed)
    with MonitoringStore(database) as store:
        assert set(store.history()) == set(reconstructed)
