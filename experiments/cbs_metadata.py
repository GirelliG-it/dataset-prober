"""Opt-in, two-observation CBS metadata experiment; never retrieves dataset rows."""

import argparse
import json
import signal
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from dataset_prober.loading_policy import sanitize_url_text
from dataset_prober.monitoring import (
    Availability,
    CheckStatus,
    Evidence,
    EvidenceKind,
    Observation,
    compare_observation,
)
from dataset_prober.tools.guards import (
    SafeHttpClient,
    SafeTransportError,
    UnsafeResourceError,
    UnsafeURLError,
    _PinnedConnector,
)

TABLE_ID = "83583NED"
SOURCE_ID = f"cbs:{TABLE_ID}"
ENDPOINT = f"https://opendata.cbs.nl/ODataApi/odata/{TABLE_ID}/TableInfos"
MAX_REQUESTS = 4
REQUEST_SECONDS = 15
OVERALL_SECONDS = 60
MAX_BYTES = 1024 * 1024
INTERVAL_SECONDS = 1
FIELDS = (
    "Identifier",
    "Title",
    "Modified",
    "MetaDataModified",
    "Frequency",
    "Period",
    "OutputStatus",
)
HEADERS = (
    "content-type",
    "content-length",
    "date",
    "etag",
    "last-modified",
    "age",
    "cache-control",
)


class ExperimentDeadline(SafeTransportError):
    """An independent wall timer interrupted a stalled operation."""


@contextmanager
def hard_timeout(seconds):
    """POSIX main-thread watchdog, including blocking reads; nesting keeps the earlier alarm."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "setitimer"
    ):
        raise RuntimeError("This experiment requires POSIX timers in the main thread")
    if seconds <= 0:
        raise ExperimentDeadline("experiment_deadline")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise ExperimentDeadline("experiment_deadline")

    signal.signal(signal.SIGALRM, expired)
    limit = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def sanitized(value):
    if not isinstance(value, str):
        return None
    return sanitize_url_text("".join(char for char in value if unicodedata.category(char) != "Cc"))


def field_evidence(kind, value, origin, *, failed=False, checked=True):
    if failed:
        availability = Availability.CHECK_FAILED
    elif not checked:
        availability = Availability.NOT_CHECKED
    elif value is None:
        availability = Availability.NOT_PROVIDED
    elif not isinstance(value, str) or not value.strip() or sanitized(value) != value:
        availability = Availability.INVALID
    else:
        availability = Availability.PRESENT
    return Evidence(
        kind=kind,
        resource=ENDPOINT,
        rule_version=f"cbs-tableinfos:{origin}:verbatim-v1",
        availability=availability,
        value=value if availability is Availability.PRESENT else None,
    )


def observation_evidence(metadata, headers, *, failed=False, checked=True):
    # No dataset revision or schema interpretation is inferred from TableInfos.
    return (
        field_evidence(
            EvidenceKind.MODIFIED,
            metadata.get("Modified"),
            "Modified",
            failed=failed,
            checked=checked,
        ),
        field_evidence(
            EvidenceKind.MODIFIED,
            metadata.get("MetaDataModified"),
            "MetaDataModified",
            failed=failed,
            checked=checked,
        ),
        field_evidence(
            EvidenceKind.TITLE, metadata.get("Title"), "Title", failed=failed, checked=checked
        ),
        field_evidence(
            EvidenceKind.ETAG, headers.get("etag"), "HTTP-ETag", failed=failed, checked=checked
        ),
        field_evidence(EvidenceKind.REVISION, None, "revision-unmapped", checked=False),
        field_evidence(EvidenceKind.SCHEMA, None, "schema-not-requested", checked=False),
    )


class MeasuredResponse:
    def __init__(self, response, record):
        self.response = response
        self.record = record
        self.status = response.status
        self.headers = response.headers

    def read(self, amount=-1):
        content = self.response.read(amount)
        self.record["body_bytes_read"] += len(content)
        return content

    def close(self):
        self.response.close()


class Experiment:
    """Use the existing connector seam solely to restrict and measure guarded requests."""

    def __init__(
        self,
        *,
        resolver=None,
        connector=None,
        clock=time.monotonic,
        utc_now=lambda: datetime.now(UTC),
        sleep=time.sleep,
    ):
        self.clock = clock
        self.utc_now = utc_now
        self.sleep = sleep
        self.wire = connector if connector is not None else _PinnedConnector()
        self.requests = []
        self.deadline = None
        self.client = SafeHttpClient(
            resolver=resolver,
            connector=self.connect,
            max_redirects=1,
            # Leave room for the guard's one-byte overflow probe, so even a
            # rejected body cannot make us read more than the 1 MiB wire budget.
            max_response_bytes=MAX_BYTES - 1,
        )

    def connect(self, target, method, headers, deadline):
        parsed = urlsplit(target.url)
        expected = urlsplit(ENDPOINT)
        if (
            method != "GET"
            or parsed.scheme != expected.scheme
            or parsed.netloc != expected.netloc
            or parsed.path.rstrip("/") != expected.path
            or parsed.query not in ("", "$format=json", "%24format=json")
        ):
            raise UnsafeURLError("metadata_endpoint_only")
        if len(self.requests) >= MAX_REQUESTS:
            raise UnsafeURLError("request_cap")
        if self.clock() >= self.deadline:
            raise ExperimentDeadline("experiment_deadline")
        started = self.clock()
        record = {
            "attempt": len(self.requests) + 1,
            "endpoint": ENDPOINT,
            "started_at": self.utc_now().isoformat(),
            "status": None,
            "body_bytes_read": 0,
            "headers_elapsed_seconds": None,
        }
        self.requests.append(record)
        # validate_url has already checked ALL DNS answers. Select one validated
        # address so the existing pinned connector cannot retry another address
        # following a send/read failure. TLS and pinned transport remain intact.
        try:
            response = self.wire(
                replace(target, addresses=target.addresses[:1]), method, headers, deadline
            )
        except (SafeTransportError, OSError) as exc:
            record["error_type"] = type(exc).__name__
            raise
        record["status"] = response.status
        record["headers_elapsed_seconds"] = round(self.clock() - started, 6)
        record["headers"] = {
            key.lower(): sanitized(value)
            for key, value in response.headers.items()
            if key.lower() in HEADERS
        }
        return MeasuredResponse(response, record)

    def collect(self, sequence, *, blocked_reason=None):
        started = self.clock()
        started_at = self.utc_now()
        request_start = len(self.requests)
        timeout = min(REQUEST_SECONDS, max(0, self.deadline - started))
        metadata = {}
        headers = {}
        reason = blocked_reason
        status = CheckStatus.BLOCKED
        body_size = None
        if reason is None and timeout <= 0:
            reason = "overall_deadline"
        checked = reason is None
        if reason is None:
            try:
                with hard_timeout(timeout):
                    response = self.client.get(
                        ENDPOINT,
                        params={"$format": "json"},
                        headers={"Accept": "application/json"},
                        timeout=timeout,
                    )
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    body_size = len(response.content)
                    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if response.status_code != 200 or media_type != "application/json":
                        raise UnsafeResourceError("unexpected_metadata_response")
                    payload = response.json()
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get("value"), list)
                        or len(payload["value"]) != 1
                        or not isinstance(payload["value"][0], dict)
                        or payload["value"][0].get("Identifier") != TABLE_ID
                        or payload.get("odata.nextLink")
                        or payload.get("@odata.nextLink")
                    ):
                        raise UnsafeResourceError("invalid_metadata_identity_or_envelope")
                    metadata = payload["value"][0]
                    status = CheckStatus.SUCCEEDED
            except UnsafeURLError:
                reason = "transport_policy_blocked"
            except (
                SafeTransportError,
                UnsafeResourceError,
                ValueError,
                UnicodeError,
                OSError,
                RecursionError,
            ) as exc:
                status = CheckStatus.FAILED
                reason = type(
                    exc
                ).__name__  # Never persist raw exceptions or provider error bodies.
        observation = Observation(
            observation_id=f"attempt-{sequence}",
            source_id=SOURCE_ID,
            checked_at=self.utc_now(),
            check_status=status,
            evidence=observation_evidence(
                metadata,
                headers,
                failed=checked and status is not CheckStatus.SUCCEEDED,
                checked=checked,
            ),
            failure_reason=reason,
            checker_version="cbs-tableinfos-experiment-1",
        )
        return observation, {
            "started_at": started_at.isoformat(),
            "completed_at": observation.checked_at.isoformat(),
            "elapsed_seconds": round(self.clock() - started, 6),
            "timeout_seconds": timeout,
            "connection_attempts": len(self.requests) - request_start,
            "body_bytes": body_size,
            "metadata": {key: sanitized(metadata[key]) for key in FIELDS if key in metadata},
            "origins": {
                "Modified": "TableInfos value[0].Modified; provider string, meaning not yet certified",
                "MetaDataModified": "TableInfos value[0].MetaDataModified; provider string",
                "Title": "TableInfos value[0].Title; descriptive only",
                "HTTP-ETag": "HTTP response ETag; validator for this metadata resource only",
                "revision-unmapped": "No verified provider revision mapping; NOT_CHECKED",
                "schema-not-requested": "No schema endpoint requested; NOT_CHECKED",
            },
        }

    def run(self):
        started = self.clock()
        self.deadline = started + OVERALL_SECONDS
        observations = []
        outcomes = []
        with hard_timeout(OVERALL_SECONDS):
            for sequence in (1, 2):
                blocked = (
                    "previous_attempt_failed"
                    if observations and observations[-1].check_status is not CheckStatus.SUCCEEDED
                    else None
                )
                try:
                    if observations and blocked is None:
                        self.sleep(min(INTERVAL_SECONDS, max(0, self.deadline - self.clock())))
                    observation, outcome = self.collect(sequence, blocked_reason=blocked)
                except ExperimentDeadline:
                    observation, outcome = self.collect(sequence, blocked_reason="overall_deadline")
                observations.append(observation)
                outcomes.append(outcome)
        return {
            "artifact_format": "experiment-only-1; not a monitoring persistence contract",
            "source_id": SOURCE_ID,
            "endpoint": ENDPOINT,
            "request_format": "OData JSON",
            "limits": {
                "requests": MAX_REQUESTS,
                "request_seconds": REQUEST_SECONDS,
                "overall_seconds": OVERALL_SECONDS,
                "response_bytes": MAX_BYTES,
                "automatic_retries": 0,
                "redirects_per_observation": 1,
            },
            "elapsed_seconds": round(self.clock() - started, 6),
            "connection_attempts": len(self.requests),
            "http_responses_received": sum(item["status"] is not None for item in self.requests),
            "requests": self.requests,
            "observations": [asdict(item) for item in observations],
            "outcomes": outcomes,
            "comparisons": [
                asdict(compare_observation(item, observations[:index]))
                for index, item in enumerate(observations)
            ],
            "limitations": [
                "No dataset records, samples, schema request, content hash, or ingestion.",
                "Metadata validators and timestamps do not prove dataset content or freshness.",
                "Equal metadata cannot exclude an intervening reverted change.",
                "Failed connection attempts may send no HTTP request; count is a conservative bound.",
                "Accepted bodies are capped at 1 MiB minus one byte, reserving the guard's overflow probe.",
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Authorize this bounded metadata experiment",
    )
    args = parser.parse_args()
    if not args.live:
        return 1
    # Refuse unsupported watchdog environments before creating any network activity.
    with hard_timeout(1):
        pass
    directory = (
        Path(__file__).resolve().parents[1]
        / "output"
        / "cbs-metadata-experiment"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    )
    directory.mkdir(parents=True, exist_ok=False)
    report = Experiment().run()
    (directory / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=lambda value: value.isoformat())
        + "\n",
        encoding="utf-8",
    )
    print(f"Artifacts: {directory}")
    print(
        f"Connection attempts: {report['connection_attempts']}; responses: {report['http_responses_received']}"
    )
    print(f"Comparison: {report['comparisons'][-1]['change_status']}")
    return (
        0
        if all(item["check_status"] == CheckStatus.SUCCEEDED for item in report["observations"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
