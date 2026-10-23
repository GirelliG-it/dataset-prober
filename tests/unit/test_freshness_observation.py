"""Evidence-only fetch contracts: no live services or freshness policy changes."""

import copy
import json
import sys
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from dataset_prober.loading_policy import safe_url_identity, sanitize_url_text
from dataset_prober.tools import cbs_tool, ckan_tool
from dataset_prober.tools.base import DatasetResult, RunDeadlineExceeded
from dataset_prober.tools.guards import UnsafeURLError

STAMP = "2026-10-20T23:45:12.1234567+02:00"
NOW = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
RESOURCE = "https://example.test/data.csv?token=secret#fragment"
MISSING = object()


@pytest.fixture(params=["cbs", "ckan"])
def fetch_case(request, monkeypatch):
    source = request.param
    module = cbs_tool if source == "cbs" else ckan_tool
    clock = Mock(return_value=NOW)
    monkeypatch.setattr(module, "utc_now", clock, raising=False)
    adapter = (
        cbs_tool.CBSTool({})
        if source == "cbs"
        else ckan_tool.CKANTool(
            {
                "base_url": "https://example.test/api/3",
                "landing_base_url": "https://example.test",
                "ckan_dialect": "ckan_action",
                "ckan_search_mode": "local_resource_metadata",
            }
        )
    )
    metadata = (
        {"Title": "Fixture", "Modified": STAMP}
        if source == "cbs"
        else {
            "name": "fixture",
            "title": "Fixture",
            "metadata_modified": STAMP,
            "resources": [{"id": "resource-1", "format": "CSV", "url": RESOURCE}],
        }
    )
    envelope = {"value": [metadata]} if source == "cbs" else {"success": True, "result": metadata}
    transport = Mock(
        side_effect=[Mock(json=lambda: envelope), Mock(json=lambda: {"value": [{"Value": 1}]})]
    )
    monkeypatch.setattr(module, "safe_http_get", transport)
    probe = Mock(side_effect=lambda result, *args, **kwargs: result)
    if source == "ckan":
        monkeypatch.setattr(adapter, "_probe_csv", probe)
    return {
        "source": source,
        "adapter": adapter,
        "metadata": metadata,
        "envelope": envelope,
        "clock": clock,
        "transport": transport,
        "probe": probe,
        "id": "37789ksz" if source == "cbs" else "fixture",
        "field": "Modified" if source == "cbs" else "metadata_modified",
        "path": "/value/0/Modified" if source == "cbs" else "/result/metadata_modified",
    }


def fetch(case):
    return case["adapter"].fetch(case["id"], 2)


def claim_for(case, result):
    return next(c for c in result.freshness_observation.claims if c.field_path == case["path"])


def test_complete_claim_survives_fetch_and_explicit_serialization(fetch_case):
    result = fetch(fetch_case)
    observation = result.freshness_observation
    claim = claim_for(fetch_case, result)
    assert claim.to_dict()["value"] == STAMP
    assert claim.presence == "STRING"
    assert claim.sanitized is False
    assert claim.semantics == (
        "UNVERIFIED" if fetch_case["source"] == "cbs" else "CATALOG_METADATA_UPDATE"
    )
    assert observation.source == fetch_case["source"]
    assert observation.catalog_identity == sanitize_url_text(fetch_case["adapter"].adapter_identity)
    assert observation.dataset_id == result.id
    assert observation.resource_identity == safe_url_identity(result.download_url)
    assert observation.observed_at == NOW
    assert observation.observed_at.tzinfo is UTC
    assert observation.collection_status == "SUCCEEDED"
    assert observation.reason_code is None
    assert observation.record_version == "1"
    assert observation.collector_rule_version
    assert claim.resource == observation.metadata_endpoint
    assert UUID(observation.observation_id).version == 4
    serialized = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert serialized["freshness_observation"] == observation.to_dict()
    assert result.modified == serialized["modified"] == "2026-10-20"
    assert "secret" not in json.dumps(serialized)
    fetch_case["clock"].assert_called_once_with()
    assert fetch_case["transport"].call_count == (2 if fetch_case["source"] == "cbs" else 1)


@pytest.mark.parametrize(
    "value,presence",
    [
        (MISSING, "ABSENT"),
        (None, "NULL"),
        (17, "WRONG_TYPE"),
        ({"nested": ["original"]}, "WRONG_TYPE"),
        ([], "WRONG_TYPE"),
        ("", "STRING"),
        ("nonsense", "STRING"),
    ],
)
def test_collection_success_is_independent_of_claim_usability(fetch_case, value, presence):
    if value is MISSING:
        fetch_case["metadata"].pop(fetch_case["field"])
    else:
        fetch_case["metadata"][fetch_case["field"]] = copy.deepcopy(value)
    result = fetch(fetch_case)
    claim = claim_for(fetch_case, result)
    assert claim.presence == presence
    assert claim.to_dict()["value"] == (None if value is MISSING else value)
    assert result.freshness_observation.collection_status == "SUCCEEDED"
    # Keep legacy conversion behavior, including its existing type-error failures.
    if isinstance(value, (int, dict)):
        assert result.status == "failed"
        assert result.modified is None
    fetch_case["clock"].assert_called_once_with()


@pytest.mark.parametrize("stage", ["sample_request", "inspection"])
def test_later_failure_retains_successful_metadata(fetch_case, monkeypatch, stage):
    if fetch_case["source"] == "cbs":
        if stage == "sample_request":
            fetch_case["transport"].side_effect = [
                Mock(json=lambda: fetch_case["envelope"]),
                RuntimeError("sample failed"),
            ]
        else:
            monkeypatch.setattr(
                cbs_tool,
                "classify_record_payload",
                Mock(side_effect=RuntimeError("inspection failed")),
            )
    elif stage == "sample_request":
        monkeypatch.setattr(
            fetch_case["adapter"],
            "_probe_csv",
            ckan_tool.CKANTool._probe_csv.__get__(fetch_case["adapter"]),
        )
        monkeypatch.setattr(
            ckan_tool, "safe_download", Mock(side_effect=RuntimeError("sample failed"))
        )
    else:
        fetch_case["probe"].side_effect = RuntimeError("inspection failed")
    result = fetch(fetch_case)
    assert result.status == "failed"
    assert not result.assessment.load_eligible
    assert result.freshness_observation.collection_status == "SUCCEEDED"
    assert claim_for(fetch_case, result).to_dict()["value"] == STAMP
    fetch_case["clock"].assert_called_once_with()


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (RuntimeError("offline"), "FAILED", "metadata_collection_failed"),
        (UnsafeURLError("blocked"), "BLOCKED", "metadata_blocked"),
    ],
)
def test_failed_metadata_attempt_has_identity_and_no_invented_claim(
    fetch_case, error, status, reason
):
    fetch_case["transport"].side_effect = error
    result = fetch(fetch_case)
    observation = result.freshness_observation
    assert result.status == "failed"
    assert observation.collection_status == status
    assert observation.reason_code == reason
    assert observation.dataset_id == fetch_case["id"]
    assert observation.claims == ()
    fetch_case["clock"].assert_called_once_with()
    assert fetch_case["transport"].call_count == 1
    fetch_case["probe"].assert_not_called()


def test_metadata_error_envelope_is_not_success(fetch_case):
    fetch_case["envelope"].clear()
    fetch_case["envelope"].update({"error": {"message": "denied"}})
    result = fetch(fetch_case)
    assert result.freshness_observation.collection_status == "FAILED"
    assert result.freshness_observation.reason_code == "metadata_error_response"
    assert result.freshness_observation.claims == ()
    assert fetch_case["transport"].call_count == 1


@pytest.mark.parametrize("null_metadata", [False, True])
def test_malformed_metadata_reads_completion_clock_only_once(fetch_case, null_metadata):
    if fetch_case["source"] == "cbs":
        fetch_case["envelope"]["value"] = [None]
    else:
        fetch_case["envelope"]["result"] = (
            None if null_metadata else {"name": "fixture", "resources": None}
        )
    result = fetch(fetch_case)
    assert result.status == "failed"
    assert result.freshness_observation.collection_status == "FAILED"
    fetch_case["clock"].assert_called_once_with()


def test_capture_finishes_before_sample_operation(fetch_case):
    def sample(*args, **kwargs):
        fetch_case["clock"].assert_called_once_with()
        fetch_case["metadata"][fetch_case["field"]] = "changed during sample"
        return Mock(json=lambda: {"value": [{"Value": 1}]})

    if fetch_case["source"] == "cbs":
        metadata_response = Mock(json=lambda: fetch_case["envelope"])
        fetch_case["transport"].side_effect = lambda *a, **k: (
            metadata_response if fetch_case["transport"].call_count == 1 else sample()
        )
    else:

        def probe(result, *args, **kwargs):
            sample()
            return result

        fetch_case["probe"].side_effect = probe
    result = fetch(fetch_case)
    assert claim_for(fetch_case, result).to_dict()["value"] == STAMP
    assert result.modified == "2026-10-20"


def test_deadline_before_metadata_retains_failed_attempt(fetch_case):
    fetch_case["transport"].side_effect = RunDeadlineExceeded("deadline")
    with pytest.raises(RunDeadlineExceeded) as raised:
        fetch(fetch_case)
    observation = raised.value.freshness_observation
    assert observation.collection_status == "FAILED"
    assert observation.reason_code == "metadata_deadline_exceeded"
    assert observation.claims == ()
    fetch_case["clock"].assert_called_once_with()


def test_deadline_still_propagates_with_already_collected_evidence(fetch_case):
    if fetch_case["source"] == "cbs":
        fetch_case["transport"].side_effect = [
            Mock(json=lambda: fetch_case["envelope"]),
            RunDeadlineExceeded("deadline"),
        ]
    else:
        fetch_case["probe"].side_effect = RunDeadlineExceeded("deadline")
    with pytest.raises(RunDeadlineExceeded) as raised:
        fetch(fetch_case)
    observation = raised.value.freshness_observation
    assert observation.collection_status == "SUCCEEDED"
    assert observation.claims[0].to_dict()["value"] == STAMP
    fetch_case["clock"].assert_called_once_with()


def test_metadata_deadline_survives_agent_timeout_and_cli_save(
    fetch_case, monkeypatch, tmp_path, capsys
):
    from dataset_prober import dataset_agent
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.paths import AppPaths

    ticks = [0.0]
    budgets = []
    make_budget = dataset_agent.Budget.from_profile

    def budget_from_profile(config):
        budget = make_budget(config, clock=lambda: ticks[0])
        budgets.append(budget)
        return budget

    monkeypatch.setattr(dataset_agent.Budget, "from_profile", budget_from_profile)

    def metadata_then_expire():
        ticks[0] = 60.0
        return fetch_case["envelope"]

    fetch_case["transport"].side_effect = [Mock(json=metadata_then_expire)]
    adapter = fetch_case["adapter"]
    if fetch_case["source"] == "ckan":
        monkeypatch.setattr(adapter, "_probe_csv", ckan_tool.CKANTool._probe_csv.__get__(adapter))
    captured_observations = []
    real_fetch = adapter.fetch

    def capture_deadline(*args, **kwargs):
        try:
            return real_fetch(*args, **kwargs)
        except RunDeadlineExceeded as exc:
            captured_observations.append(exc.freshness_observation)
            raise

    fetch_spy = Mock(side_effect=capture_deadline)
    monkeypatch.setattr(adapter, "fetch", fetch_spy)
    forbidden = Mock(side_effect=AssertionError("Unexpected search, consent or loading"))
    monkeypatch.setattr(adapter, "search", forbidden)
    monkeypatch.setattr(adapter, "download", forbidden)
    monkeypatch.setattr(LoadingPolicySession, "register_dataset_result", forbidden)
    monkeypatch.setattr(LoadingPolicySession, "request_authorization", forbidden)
    monkeypatch.setattr("duckdb.connect", forbidden)
    monkeypatch.setattr(
        dataset_agent,
        "TOOL_REGISTRY",
        {
            **dataset_agent.TOOL_REGISTRY,
            fetch_case["source"]: lambda _config: adapter,
        },
    )

    identity = {"source": fetch_case["source"], "dataset_id": fetch_case["id"]}
    blocks = [
        SimpleNamespace(type="tool_use", id="fetch", name="fetch_dataset", input=identity),
        SimpleNamespace(
            type="tool_use",
            id="search",
            name="search_catalog",
            input={"source": fetch_case["source"], "keyword": "fixture", "max_results": 1},
        ),
        SimpleNamespace(
            type="tool_use",
            id="load",
            name="download_dataset",
            input={**identity, "title": "Fixture"},
        ),
    ]
    client = Mock()
    client.messages.create.side_effect = [
        SimpleNamespace(
            content=blocks,
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
    ]
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", Mock(return_value=client))
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", lambda: "offline-key")
    prompt = Mock(side_effect=["Inspect fixture", "3"])
    monkeypatch.setattr(dataset_agent.console, "input", prompt)
    paths = AppPaths(output_dir=tmp_path)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=paths))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dataset-prober",
            "--profile",
            "dutch_government",
            "--timeout",
            "1",
            "--max-model-calls",
            "3",
            "--download",
        ],
    )

    tool_results = []
    execute = dataset_agent.execute_tool

    def record_tool_result(**kwargs):
        result = execute(**kwargs)
        tool_results.append(result)
        return result

    monkeypatch.setattr(dataset_agent, "execute_tool", record_tool_result)
    returned = []
    run = dataset_agent.run_profile

    def capture_result(**kwargs):
        result = run(**kwargs)
        returned.append(result)
        return result

    monkeypatch.setattr(dataset_agent, "run_profile", capture_result)

    dataset_agent.main()

    assert paths.agent_results_path.exists()
    saved = json.loads(paths.agent_results_path.read_text())
    assert len(saved) == len(returned[0].datasets_found) == 1
    candidate = returned[0].datasets_found[0]
    assert candidate is returned[0].datasets_failed[0]
    assert returned[0].datasets_downloaded == []
    assert candidate.freshness_observation is captured_observations[0]
    assert saved[0]["status"] == "failed"
    assert saved[0]["assessment"]["load_eligible"] is False
    assert saved[0]["freshness_observation"] == captured_observations[0].to_dict()
    assert saved[0]["freshness_observation"]["collection_status"] == "SUCCEEDED"
    assert saved[0]["freshness_observation"]["claims"][0]["value"] == STAMP
    json.dumps(candidate.to_dict(), allow_nan=False)
    assert tool_results[0] == {
        "error": "Profile-agent run deadline exhausted before source operation completed"
    }
    assert "budget exhausted" in tool_results[1]["error"].lower()
    assert "has not passed inspection" in tool_results[2]["error"]
    assert budgets[0].probes_used == 1
    assert budgets[0].searches_used == 0
    assert budgets[0].time_remaining() == 0
    assert budgets[0].tokens_used == returned[0].tokens_used == 15
    assert returned[0].model_calls_attempted == returned[0].model_calls_completed == 1
    assert returned[0].model_calls_timed_out == 0
    client.messages.create.assert_called_once()
    fetch_spy.assert_called_once()
    fetch_case["transport"].assert_called_once()
    fetch_case["clock"].assert_called_once_with()
    forbidden.assert_not_called()
    assert prompt.call_count == 2
    assert "Your choice (1/2/3)" in prompt.call_args.args[0]
    assert "Time limit reached" in capsys.readouterr().out


def test_observation_and_retained_nested_value_are_immutable(fetch_case):
    payload = {"nested": ["original", {"url": RESOURCE}]}
    fetch_case["metadata"][fetch_case["field"]] = payload
    result = fetch(fetch_case)
    observation = result.freshness_observation
    claim = claim_for(fetch_case, result)
    before = observation.to_dict()
    payload["nested"][0] = "changed"
    payload["nested"][1]["url"] = "changed"
    claim.to_dict()["value"]["nested"].append("changed")
    with pytest.raises(FrozenInstanceError):
        observation.dataset_id = "changed"
    with pytest.raises(FrozenInstanceError):
        claim.value_json = "changed"
    assert observation.to_dict() == before
    assert claim.sanitized is True
    assert "secret" not in json.dumps(before)


def test_new_attempt_gets_new_id_but_serialization_does_not(fetch_case):
    fetch_case["transport"].side_effect = None
    fetch_case["transport"].return_value = Mock(json=lambda: fetch_case["envelope"])
    first = fetch(fetch_case).freshness_observation
    before = first.to_dict()
    second = fetch(fetch_case).freshness_observation
    assert first.to_dict() == before
    assert first.observation_id != second.observation_id
    assert first.observed_at == second.observed_at == NOW
    assert fetch_case["clock"].call_count == 2


def test_ckan_resources_with_redacted_query_values_remain_distinct(monkeypatch):
    monkeypatch.setattr(ckan_tool, "utc_now", lambda: NOW, raising=False)
    adapter = ckan_tool.CKANTool(
        {
            "base_url": "https://example.test/api/3",
            "landing_base_url": "https://example.test",
            "ckan_dialect": "ckan_action",
        }
    )
    monkeypatch.setattr(adapter, "_probe_csv", lambda result, *a, **k: result)
    observations = []
    for token in ("one", "two"):
        pkg = {
            "name": "fixture",
            "metadata_modified": STAMP,
            "resources": [
                {
                    "id": token,
                    "format": "CSV",
                    "url": f"https://example.test/data.csv?token={token}",
                }
            ],
        }
        monkeypatch.setattr(
            ckan_tool,
            "safe_http_get",
            Mock(return_value=Mock(json=lambda: {"success": True, "result": pkg})),
        )
        observations.append(adapter.fetch("fixture", 1).freshness_observation)
    assert observations[0].resource_identity != observations[1].resource_identity
    assert [o.resource_id for o in observations] == ["one", "two"]


def test_existing_positional_constructor_and_summary_are_compatible(sample_dataset_result_ok):
    names = "id title description source source_name url download_url format modified frequency license license_url row_count columns sample language tags status error tokens_used cost_usd assessment".split()
    assert [f.name for f in fields(DatasetResult)][: len(names)] == names
    rebuilt = DatasetResult(*(getattr(sample_dataset_result_ok, name) for name in names))
    assert rebuilt.freshness_observation is None
    assert rebuilt.to_dict() == sample_dataset_result_ok.to_dict()
    assert rebuilt.to_dict()["freshness_observation"] is None


def test_legacy_freshness_and_other_summary_fields_ignore_observation(fetch_case, monkeypatch):
    from dataset_prober.dataset_agent import execute_tool

    result = fetch(fetch_case)
    legacy = replace(result, freshness_observation=None)
    before = result.freshness_observation.to_dict()
    summary = result.to_dict()
    summary["freshness_observation"] = None
    assert summary == legacy.to_dict()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 12, tzinfo=tz)

    monkeypatch.setattr("datetime.datetime", FixedDatetime)

    def evaluate(candidate):
        return execute_tool(
            "check_freshness",
            {"source": candidate.source, "dataset_id": candidate.id, "max_days_old": 30},
            {},
            None,
            None,
            SimpleNamespace(download_enabled=False),
            [candidate],
            None,
            None,
        )

    assert evaluate(result) == evaluate(legacy)
    assert result.freshness_observation.to_dict() == before
    assert result.assessment is legacy.assessment


@pytest.mark.parametrize("location", ["top", "object", "array"])
@pytest.mark.parametrize("reverse", [False, True])
def test_claim_capture_rejects_sanitized_key_collisions(location, reverse):
    from dataset_prober.freshness_observation import TimestampClaim

    raw = "https://example.test/claim?token=provider-secret"
    safe = sanitize_url_text(raw)
    assert raw != safe
    assert sanitize_url_text(safe) == safe
    pairs = [(raw, "private-first-value"), (safe, "private-second-value")]
    conflicting = dict(reversed(pairs) if reverse else pairs)
    value = (
        conflicting
        if location == "top"
        else {"nested": conflicting}
        if location == "object"
        else {"nested": ["ordinary", conflicting]}
    )
    metadata = {"date": value}
    before = copy.deepcopy(metadata)
    original_items = list(conflicting.items())
    claim = None
    with pytest.raises(ValueError) as exc:
        claim = TimestampClaim.capture(
            metadata, "date", resource="endpoint", field_path="/date", semantics="UNVERIFIED"
        )
    assert str(exc.value) == "Provider JSON keys collide after sanitization"
    assert claim is None
    assert metadata == before
    assert list(conflicting.items()) == original_items


@pytest.mark.parametrize("mode", ["ordinary", "sensitive", "already_sanitized"])
def test_noncolliding_claim_objects_preserve_members_and_sanitization_flags(mode):
    from dataset_prober.freshness_observation import TimestampClaim

    raw = "https://example.test/claim?token=provider-secret"
    other = "https://example.test/claim?token=different-secret"
    safe, other_safe = sanitize_url_text(raw), sanitize_url_text(other)
    assert safe != other_safe
    if mode == "ordinary":
        value = {"first": [STAMP, {"second": None}], "third": 42}
        expected = copy.deepcopy(value)
    else:
        value = (
            {raw: [other, {other: 42}], "ordinary": False}
            if mode == "sensitive"
            else {safe: [other_safe, {other_safe: 42}], "ordinary": False}
        )
        expected = {safe: [other_safe, {other_safe: 42}], "ordinary": False}
    metadata = {"date": value}
    before = copy.deepcopy(metadata)
    claim = TimestampClaim.capture(
        metadata, "date", resource="endpoint", field_path="/date", semantics="UNVERIFIED"
    )
    serialized = json.loads(json.dumps(claim.to_dict(), allow_nan=False))
    assert serialized["value"] == expected
    assert serialized["presence"] == "WRONG_TYPE"
    assert serialized["sanitized"] is (mode == "sensitive")
    assert metadata == before


def test_colliding_claim_uses_existing_adapter_failure_contract(fetch_case):
    raw = "https://example.test/claim?token=provider-secret"
    fetch_case["metadata"][fetch_case["field"]] = {
        raw: "private-first-value",
        sanitize_url_text(raw): "private-second-value",
    }
    before = copy.deepcopy(fetch_case["metadata"])
    result = fetch(fetch_case)
    assert result.status == "failed"
    assert not result.assessment.load_eligible
    assert result.error == "Provider JSON keys collide after sanitization"
    assert result.freshness_observation.collection_status == "FAILED"
    assert result.freshness_observation.reason_code == "metadata_collection_failed"
    assert result.freshness_observation.claims == ()
    assert fetch_case["metadata"] == before
    fetch_case["transport"].assert_called_once()
    fetch_case["probe"].assert_not_called()
    fetch_case["clock"].assert_called_once_with()
    serialized = json.dumps(result.to_dict(), allow_nan=False)
    assert "provider-secret" not in serialized
    assert "private-first-value" not in serialized
    assert "private-second-value" not in serialized


def test_claim_capture_rejects_non_json_values_instead_of_stringifying():
    from dataset_prober.freshness_observation import TimestampClaim

    for value in (object(), {1: "value"}, float("nan"), (1, 2)):
        with pytest.raises((ValueError, TypeError)):
            TimestampClaim.capture(
                {"date": value},
                "date",
                resource="endpoint",
                field_path="/date",
                semantics="UNVERIFIED",
            )


def test_cbs_claims_keep_separate_field_origins(monkeypatch):
    monkeypatch.setattr(cbs_tool, "utc_now", lambda: NOW)
    metadata = {"Modified": STAMP, "MetaDataModified": "2026-10-21T09:00:00Z"}
    monkeypatch.setattr(
        cbs_tool,
        "safe_http_get",
        Mock(
            side_effect=[
                Mock(json=lambda: {"value": [metadata]}),
                Mock(json=lambda: {"value": [{"Value": 1}]}),
            ]
        ),
    )
    observation = cbs_tool.CBSTool({}).fetch("37789ksz", 1).freshness_observation
    assert [c.field_path for c in observation.claims] == [
        "/value/0/Modified",
        "/value/0/MetaDataModified",
    ]
    assert [c.to_dict()["value"] for c in observation.claims] == [
        STAMP,
        metadata["MetaDataModified"],
    ]
    assert [c.semantics for c in observation.claims] == ["UNVERIFIED", "CATALOG_METADATA_UPDATE"]


def test_ckan_claim_remains_package_metadata_for_exact_selected_resource(monkeypatch):
    monkeypatch.setattr(ckan_tool, "utc_now", lambda: NOW)
    pkg = {
        "name": "Exact Name",
        "metadata_modified": STAMP,
        "resources": [
            {"id": "pdf", "format": "PDF", "url": "https://example.test/doc.pdf"},
            {"id": "CSV id", "format": "CSV", "url": RESOURCE, "last_modified": "different"},
            {"id": "later", "format": "CSV", "url": "https://example.test/later.csv"},
        ],
    }
    observations = []
    for catalog in ("https://first.test/api/3", "https://second.test/api/3"):
        adapter = ckan_tool.CKANTool(
            {
                "base_url": catalog,
                "landing_base_url": "https://example.test",
                "ckan_dialect": "ckan_action",
                "ckan_search_mode": "local_resource_metadata",
            }
        )
        transport = Mock(return_value=Mock(json=lambda: {"success": True, "result": pkg}))
        monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
        probe = Mock(side_effect=lambda result, *args, **kwargs: result)
        monkeypatch.setattr(adapter, "_probe_csv", probe)
        result = adapter.fetch("requested-alias", 1)
        observation = result.freshness_observation
        assert observation.dataset_id == result.id == "Exact Name"
        assert observation.resource_id == "CSV id"
        assert observation.resource_identity == safe_url_identity(RESOURCE)
        assert observation.metadata_endpoint == safe_url_identity(
            f"{catalog}/action/package_show?id=requested-alias"
        )
        assert len(observation.claims) == 1
        assert observation.claims[0].field_path == "/result/metadata_modified"
        assert observation.claims[0].to_dict()["value"] == STAMP
        assert observation.claims[0].semantics == "CATALOG_METADATA_UPDATE"
        transport.assert_called_once()
        probe.assert_called_once()
        observations.append(observation)
    assert observations[0].catalog_identity != observations[1].catalog_identity


def test_search_conversion_does_not_create_inspected_observations(monkeypatch):
    adapter = ckan_tool.CKANTool(
        {
            "base_url": "https://example.test/api/3",
            "landing_base_url": "https://example.test",
            "ckan_dialect": "ckan_action",
        }
    )
    result = adapter._package_to_result({"name": "fixture", "metadata_modified": STAMP})
    assert result.freshness_observation is None
    monkeypatch.setattr(
        cbs_tool,
        "safe_http_get",
        Mock(
            return_value=Mock(
                json=lambda: {
                    "value": [{"Identifier": "37789ksz", "Title": "fixture", "Modified": STAMP}]
                }
            )
        ),
    )
    assert cbs_tool.CBSTool({}).search("fixture", 1)[0].freshness_observation is None
