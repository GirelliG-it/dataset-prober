"""Offline transport and comparison checks for the opt-in metadata experiment."""

import importlib.util
import io
import json
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    assert report["comparisons"][1]["baseline_observation_id"] == "attempt-1"


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
