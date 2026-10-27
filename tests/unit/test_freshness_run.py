"""Offline operational freshness and independent per-run report writing."""

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from dataset_prober import dataset_agent as agent
from dataset_prober.loading_policy import LoadingPolicySession
from dataset_prober.paths import AppPaths
from dataset_prober.tools import cbs_tool, ckan_tool

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
STAMP = "2026-10-22T12:00:00.0000000Z"


@pytest.fixture(params=["cbs", "ckan"])
def boundary(request, monkeypatch, test_profile, tmp_path):
    source = request.param
    module = cbs_tool if source == "cbs" else ckan_tool
    adapter = (
        cbs_tool.CBSTool({})
        if source == "cbs"
        else ckan_tool.CKANTool(
            {
                "base_url": "https://synthetic.test/api/3",
                "landing_base_url": "https://synthetic.test",
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
            "resources": [
                {
                    "id": "csv-1",
                    "format": "CSV",
                    "url": "https://synthetic.test/data.csv?token=secret",
                }
            ],
        }
    )
    envelope = {"value": [metadata]} if source == "cbs" else {"success": True, "result": metadata}
    transport = Mock(
        side_effect=[Mock(json=lambda: envelope), Mock(json=lambda: {"value": [{"Value": 1}]})]
    )
    monkeypatch.setattr(module, "safe_http_get", transport)
    monkeypatch.setattr(module, "utc_now", lambda: NOW - timedelta(hours=1))
    if source == "ckan":
        monkeypatch.setattr(adapter, "_probe_csv", lambda result, *args, **kwargs: result)
    return SimpleNamespace(
        source=source,
        adapter=adapter,
        transport=transport,
        metadata=metadata,
        identity={"source": source, "dataset_id": "37789ksz" if source == "cbs" else "fixture"},
        found=[],
        budget=agent.Budget.from_profile(test_profile.budget),
        profile=test_profile,
        session=LoadingPolicySession(download_enabled=False),
        cost=agent.SessionCost(),
        paths=AppPaths(output_dir=tmp_path / "out"),
    )


def execute(boundary, name, arguments, run=None):
    extra = {"freshness_run": run} if run is not None else {}
    return agent.execute_tool(
        name,
        arguments,
        {boundary.source: boundary.adapter},
        boundary.budget,
        boundary.profile,
        boundary.session,
        boundary.found,
        boundary.cost,
        boundary.paths,
        **extra,
    )


def test_current_provider_metadata_must_not_pass_content_freshness(boundary):
    execute(boundary, "fetch_dataset", boundary.identity)
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30000})
    assert result["passes"] is None


def start(boundary, monkeypatch):
    from dataset_prober.freshness_run import FreshnessRun

    monkeypatch.setattr(agent, "freshness_now", lambda: NOW)
    run = FreshnessRun()
    execute(boundary, "fetch_dataset", boundary.identity, run)
    return run


def test_fetch_evaluate_and_save_preserves_canonical_records(boundary, monkeypatch):
    run = start(boundary, monkeypatch)
    candidate = boundary.found[0]
    before = candidate.to_dict()
    assessment = candidate.assessment
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["verdict"] == "UNKNOWN" and result["passes"] is None
    assert result["reason_code"] == "freshness_unknown"
    assert result["evaluation_reason"] == "semantics_not_applicable"
    assert "parsed" in result["reason"] and "content freshness is unknown" in result["reason"]
    assert candidate.to_dict() == before and candidate.assessment is assessment
    assert boundary.budget.probes_used == 1 and boundary.budget.searches_used == 0
    assert len(run.observations) == len(run.evaluations) == len(run.requests) == 1
    evaluation = run.evaluations[0]
    assert evaluation.observation is candidate.freshness_observation
    assert evaluation.evaluated_at == NOW and evaluation.rule is None
    assert agent._save_run_outputs(boundary.found, run, boundary.paths) == 0
    report = json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
    assert report["report_format_version"] == "1"
    assert report["observations"] == [candidate.freshness_observation.to_dict()]
    assert report["evaluations"] == [evaluation.to_dict()]
    assert report["requests"][0]["evaluation_id"] == str(evaluation.evaluation_id)
    assert report["requests"][0]["grounding_reason"] is None
    assert json.loads(boundary.paths.agent_results_path.read_text()) == [before]
    assert "secret" not in json.dumps(report)


def test_repeated_checks_keep_distinct_ids_times_and_thresholds(boundary, monkeypatch):
    run = start(boundary, monkeypatch)
    references = iter([NOW, NOW + timedelta(seconds=1)])
    monkeypatch.setattr(agent, "freshness_now", lambda: next(references))
    for days in (0, 30):
        execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": days}, run)
    assert len(run.observations) == 1
    assert len({e.evaluation_id for e in run.evaluations}) == 2
    assert [e.max_days_old for e in run.evaluations] == [0, 30]
    assert [e.evaluated_at for e in run.evaluations] == [NOW, NOW + timedelta(seconds=1)]
    assert all(e.observation is run.observations[0] for e in run.evaluations)


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("missing", "candidate_not_inspected"),
        ("ambiguous", "candidate_identity_ambiguous"),
        ("no_observation", "observation_missing"),
        ("changed_link", "observation_link_changed"),
        ("dataset", "observation_identity_mismatch"),
        ("catalog", "observation_catalog_mismatch"),
        ("resource", "observation_resource_mismatch"),
        ("endpoint", "observation_endpoint_mismatch"),
        ("collector", "collector_not_supported"),
        ("field", "claim_not_found"),
        ("duplicate_claim", "claim_identity_ambiguous"),
        ("claim_resource", "claim_scope_mismatch"),
    ],
)
def test_grounding_failures_have_no_orphan_evaluation(boundary, monkeypatch, scenario, reason):
    from dataset_prober.freshness_run import FreshnessRun

    monkeypatch.setattr(
        agent,
        "freshness_now",
        Mock(side_effect=AssertionError("No evaluation clock for grounding failure")),
    )
    candidate = boundary.adapter.fetch(boundary.identity["dataset_id"], 1)
    obs = candidate.freshness_observation
    if scenario in ("dataset", "catalog", "resource", "endpoint", "collector"):
        field = {
            "dataset": "dataset_id",
            "catalog": "catalog_identity",
            "resource": "resource_identity",
            "endpoint": "metadata_endpoint",
            "collector": "collector_rule_version",
        }[scenario]
        candidate.freshness_observation = replace(obs, **{field: "wrong"})
    if scenario in ("field", "claim_resource"):
        change = {"field_path": "/wrong"} if scenario == "field" else {"resource": "wrong"}
        candidate.freshness_observation = replace(obs, claims=(replace(obs.claims[0], **change),))
    if scenario == "duplicate_claim":
        candidate.freshness_observation = replace(obs, claims=(obs.claims[0], obs.claims[0]))
    if scenario == "no_observation":
        candidate.freshness_observation = None
    monkeypatch.setattr(boundary.adapter, "fetch", Mock(return_value=candidate))
    run = FreshnessRun()
    execute(boundary, "fetch_dataset", boundary.identity, run)
    if scenario == "changed_link":
        candidate.freshness_observation = replace(obs, observation_id="substituted")
    if scenario == "missing":
        boundary.found.clear()
    if scenario == "ambiguous":
        boundary.found.append(candidate)
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["grounding_reason"] == reason
    assert result["passes"] is None and result["verdict"] == "UNKNOWN"
    assert not run.evaluations
    assert len(run.requests) == 1 and run.requests[0].evaluation_id is None


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED", "SUCCEEDED"])
def test_empty_claims_use_explicit_absence(boundary, monkeypatch, status):
    candidate = boundary.adapter.fetch(boundary.identity["dataset_id"], 1)
    candidate.freshness_observation = replace(
        candidate.freshness_observation,
        claims=(),
        collection_status=status,
        reason_code=None if status == "SUCCEEDED" else "fixture_failure",
    )
    monkeypatch.setattr(boundary.adapter, "fetch", Mock(return_value=candidate))
    run = start(boundary, monkeypatch)
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["evaluation_reason"] == (
        "timestamp_absent" if status == "SUCCEEDED" else "collection_" + status.lower()
    )
    assert run.evaluations[0].claim_reference is None


def test_malformed_calls_do_not_create_run_records(boundary, monkeypatch):
    from dataset_prober.freshness_run import FreshnessRun

    run = FreshnessRun()
    monkeypatch.setattr(
        agent, "freshness_now", Mock(side_effect=AssertionError("Unexpected clock"))
    )
    for name, value in [
        ("check_freshness", None),
        ("fetch_dataset", {}),
        ("check_freshness", {**boundary.identity, "max_days_old": True}),
        ("check_freshness", {**boundary.identity, "max_days_old": 1, "target": "DATASET_CONTENT"}),
    ]:
        assert execute(boundary, name, value, run)["error"]
    assert not run.observations and not run.evaluations and not run.requests
    boundary.transport.assert_not_called()
    assert boundary.budget.probes_used == boundary.budget.searches_used == 0


@pytest.mark.parametrize("report_only", [False, True])
def test_synthetic_content_policy_pass_fail_does_not_authorize_loading(
    boundary, monkeypatch, report_only
):
    from dataset_prober import freshness_run

    # Test-only selection policy. Never relabel or certify a production collector.
    policy = freshness_run.SELECTION_RULES[boundary.source]
    monkeypatch.setattr(
        freshness_run,
        "SELECTION_RULES",
        {
            **freshness_run.SELECTION_RULES,
            boundary.source: replace(
                policy,
                version="synthetic-test-v1",
                collector_version="synthetic-collector-v1",
                semantics="DATASET_CONTENT_UPDATE",
            ),
        },
    )
    candidate = boundary.adapter.fetch(boundary.identity["dataset_id"], 1)
    if report_only:
        from dataset_prober.resource_classification import unknown_assessment

        candidate.status = "failed"
        candidate.assessment = unknown_assessment()
    obs = candidate.freshness_observation
    candidate.freshness_observation = replace(
        obs,
        collector_rule_version="synthetic-collector-v1",
        claims=(replace(obs.claims[0], semantics="DATASET_CONTENT_UPDATE"),),
    )
    monkeypatch.setattr(boundary.adapter, "fetch", Mock(return_value=candidate))
    run = start(boundary, monkeypatch)
    before = candidate.to_dict()
    for threshold, verdict in [(0, "FAIL"), (1, "PASS")]:
        result = execute(
            boundary, "check_freshness", {**boundary.identity, "max_days_old": threshold}, run
        )
        assert result["verdict"] == verdict
        assert result["passes"] is (verdict == "PASS")
        assert result["selection_rule_version"] == "synthetic-test-v1"
    assert candidate.to_dict() == before
    assert not boundary.session.download_enabled


@pytest.mark.parametrize("changed", ["adapter", "download_url", "source", "alias"])
def test_fetch_association_does_not_infer_aliases_or_changed_scope(boundary, monkeypatch, changed):
    run = start(boundary, monkeypatch)
    candidate = boundary.found[0]
    if changed == "adapter":
        monkeypatch.setattr(
            boundary.adapter,
            "config",
            {**boundary.adapter.config, "base_url": "https://other.test"},
        )
    elif changed == "download_url":
        candidate.download_url = "https://other.test/data.csv"
    elif changed == "source":
        candidate.source = "different"
    else:
        candidate.id = "returned-alias"
    identity = {"source": candidate.source, "dataset_id": candidate.id}
    result = execute(boundary, "check_freshness", {**identity, "max_days_old": 30}, run)
    assert result["grounding_reason"] in {
        "observation_identity_mismatch",
        "observation_catalog_mismatch",
        "observation_resource_mismatch",
    }
    assert not run.evaluations


def test_conflicting_observation_ids_are_retained_but_not_evaluated(boundary, monkeypatch):
    run = start(boundary, monkeypatch)
    original = run.observations[0]
    conflicting = replace(original, observed_at=original.observed_at - timedelta(seconds=1))
    candidate = replace(boundary.found[0], freshness_observation=conflicting)
    monkeypatch.setattr(boundary.adapter, "fetch", Mock(return_value=candidate))
    execute(boundary, "fetch_dataset", boundary.identity, run)
    boundary.found.pop(0)  # Isolate the observation-ID collision from candidate ambiguity.
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["grounding_reason"] == "observation_id_conflict"
    assert run.observations == [original, conflicting]
    assert not run.evaluations


def test_nonfinite_summary_is_reported_without_blocking_freshness_report(boundary, monkeypatch):
    run = start(boundary, monkeypatch)
    boundary.found[0].tags = [float("nan")]
    assert agent._save_run_outputs(boundary.found, run, boundary.paths) == 2
    assert (
        json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
        == run.to_dict()
    )
    assert not boundary.paths.agent_results_path.exists()


def test_diagnostic_request_identity_is_sanitized_without_evidence_links(boundary):
    from dataset_prober.freshness_run import FreshnessRun

    run = FreshnessRun()
    raw = "https://synthetic.test/id?token=provider-secret"
    result = execute(
        boundary,
        "check_freshness",
        {**boundary.identity, "dataset_id": raw, "max_days_old": 30},
        run,
    )
    assert result["grounding_reason"] == "candidate_not_inspected"
    assert run.requests[0].dataset_id == raw
    assert run.to_dict()["requests"][0]["identity_sanitized"] is True
    assert "provider-secret" not in json.dumps(run.to_dict())
    assert not run.observations and not run.evaluations


@pytest.mark.parametrize("failure", ["summary", "report", "collision"])
def test_independent_writes_preserve_memory_and_refuse_overwrite(boundary, monkeypatch, failure):
    run = start(boundary, monkeypatch)
    execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 1}, run)
    before = run.to_dict()
    boundary.paths.ensure_output_dir()
    report_path = boundary.paths.output_dir / f"freshness-{run.run_id}.json"
    if failure == "summary":
        boundary.paths.agent_results_path.mkdir()
    elif failure == "report":
        report_path.mkdir()
    else:
        report_path.write_text("existing report", encoding="utf-8")
    assert agent._save_run_outputs(boundary.found, run, boundary.paths) == 2
    assert run.to_dict() == before
    if failure == "summary":
        assert json.loads(report_path.read_text()) == before
    else:
        assert boundary.paths.agent_results_path.is_file()
    if failure == "collision":
        assert report_path.read_text() == "existing report"


def _tool(name, arguments, identifier):
    return SimpleNamespace(type="tool_use", id=identifier, name=name, input=arguments)


def _response(blocks=(), stop="tool_use"):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def cli_setup(boundary, monkeypatch, responses, *options):
    client = Mock()
    client.messages.create.side_effect = responses
    monkeypatch.setattr(agent.anthropic, "Anthropic", Mock(return_value=client))
    monkeypatch.setattr(agent, "get_anthropic_api_key", lambda: "offline-key")
    monkeypatch.setattr(agent.console, "input", Mock(return_value="Inspect fixture"))
    monkeypatch.setattr(agent.AppPaths, "resolve", Mock(return_value=boundary.paths))
    monkeypatch.setattr(
        agent,
        "TOOL_REGISTRY",
        {**agent.TOOL_REGISTRY, boundary.source: lambda config: boundary.adapter},
    )
    monkeypatch.setattr(agent, "freshness_now", lambda: NOW)
    monkeypatch.setattr(sys, "argv", ["dataset-prober", "--profile", "dutch_government", *options])
    completed = []
    real_run = agent.run_profile

    def capture(**kwargs):
        result = real_run(**kwargs)
        completed.append(result)
        return result

    monkeypatch.setattr(agent, "run_profile", capture)
    return client, completed


@pytest.mark.parametrize("ending", ["normal", "timeout", "budget", "max_tokens", "unexpected"])
def test_cli_retains_evaluation_and_usage_after_malformed_call_and_stop(
    boundary, monkeypatch, ending
):
    blocks = [
        _tool("fetch_dataset", boundary.identity, "fetch"),
        _tool("check_freshness", {**boundary.identity, "max_days_old": 30}, "fresh"),
        _tool("check_freshness", {**boundary.identity, "max_days_old": "30"}, "bad"),
    ]
    later = (
        agent.anthropic.APITimeoutError(request=Mock())
        if ending == "timeout"
        else _response(stop="end_turn" if ending == "normal" else ending)
    )
    client, completed = cli_setup(
        boundary,
        monkeypatch,
        [_response(blocks), later],
        *(["--max-model-calls", "1"] if ending == "budget" else []),
    )
    forbidden = Mock(side_effect=AssertionError("No loading, consent or database"))
    monkeypatch.setattr(boundary.adapter, "download", forbidden)
    monkeypatch.setattr(LoadingPolicySession, "request_authorization", forbidden)
    monkeypatch.setattr("duckdb.connect", forbidden)
    assert agent.main() == 0
    result = completed[0]
    run = result.freshness_run
    assert len(run.requests) == len(run.evaluations) == len(run.observations) == 1
    assert run.evaluations[0].observation is result.datasets_found[0].freshness_observation
    assert result.tokens_used == (15 if ending in ("timeout", "budget") else 30)
    assert result.model_calls_completed == (1 if ending in ("timeout", "budget") else 2)
    assert result.model_calls_timed_out == (1 if ending == "timeout" else 0)
    assert (
        json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
        == run.to_dict()
    )
    saved = json.loads(boundary.paths.agent_results_path.read_text())
    assert saved[0]["freshness_observation"] == run.observations[0].to_dict()
    assert saved[0]["assessment"] == result.datasets_found[0].assessment.to_dict()
    if ending != "budget":
        tool_results = client.messages.create.call_args.kwargs["messages"][2]["content"]
        assert json.loads(tool_results[2]["content"])["reason_code"] == "invalid_tool_input"
    forbidden.assert_not_called()


def test_cli_diagnostics_without_candidates_still_writes_report(boundary, monkeypatch):
    _client, completed = cli_setup(
        boundary,
        monkeypatch,
        [
            _response(
                [_tool("check_freshness", {**boundary.identity, "max_days_old": 30}, "missing")]
            ),
            _response(stop="end_turn"),
        ],
    )
    assert agent.main() == 0
    assert completed[0].datasets_found == []
    assert not boundary.paths.agent_results_path.exists()
    report = json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
    assert report["observations"] == report["evaluations"] == []
    assert report["requests"][0]["grounding_reason"] == "candidate_not_inspected"
    assert report["requests"][0]["evaluation_id"] is None
    boundary.transport.assert_not_called()


@pytest.mark.parametrize("artifact", ["summary", "freshness"])
def test_cli_write_error_returns_two_and_keeps_completed_records(
    boundary, monkeypatch, artifact, capsys
):
    from dataset_prober.freshness_run import FreshnessRun

    run = FreshnessRun(run_id=UUID(int=1))
    monkeypatch.setattr(agent, "FreshnessRun", lambda: run)
    boundary.paths.ensure_output_dir()
    failed_path = (
        boundary.paths.agent_results_path
        if artifact == "summary"
        else boundary.paths.output_dir / f"freshness-{run.run_id}.json"
    )
    failed_path.mkdir()
    _client, completed = cli_setup(
        boundary,
        monkeypatch,
        [
            _response([_tool("fetch_dataset", boundary.identity, "fetch")]),
            _response(stop="end_turn"),
        ],
    )
    assert agent.main() == 2
    assert completed[0].freshness_run is run and len(run.observations) == 1
    assert len(completed[0].datasets_found) == 1
    output = capsys.readouterr().out
    assert "write failed" in output
    other_path = (
        boundary.paths.output_dir / f"freshness-{run.run_id}.json"
        if artifact == "summary"
        else boundary.paths.agent_results_path
    )
    assert other_path.is_file()


@pytest.mark.parametrize("args", [["--help"], ["--list-profiles"], ["--profile", "eu_open_data"]])
def test_read_only_cli_does_not_create_run_or_resolve_output(monkeypatch, tmp_path, args):
    forbidden = Mock(side_effect=AssertionError("Read-only CLI crossed runtime boundary"))
    monkeypatch.setattr(sys, "argv", ["dataset-prober", *args])
    monkeypatch.setattr(agent, "FreshnessRun", forbidden)
    monkeypatch.setattr(agent.AppPaths, "resolve", forbidden)
    monkeypatch.setattr(agent.console, "input", forbidden)
    monkeypatch.setattr(agent.anthropic, "Anthropic", forbidden)
    monkeypatch.setattr(agent, "resolve_profile", forbidden)
    monkeypatch.setenv("DATASET_PROBER_OUTPUT", str(tmp_path / "absent"))
    if args == ["--help"]:
        with pytest.raises(SystemExit) as exc:
            agent.main()
        assert exc.value.code == 0
    else:
        assert agent.main() is None
    forbidden.assert_not_called()
    assert not (tmp_path / "absent").exists()


def test_observations_without_checks_and_empty_run_writing(boundary, monkeypatch):
    from dataset_prober.freshness_run import FreshnessRun

    assert agent._save_run_outputs([], FreshnessRun(), boundary.paths) == 0
    assert not boundary.paths.output_dir.exists()
    run = start(boundary, monkeypatch)
    assert agent._save_run_outputs([], run, boundary.paths) == 0
    report = json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
    assert report["observations"] == [boundary.found[0].freshness_observation.to_dict()]
    assert report["requests"] == report["evaluations"] == []


@pytest.mark.parametrize("blocked", [False, True])
def test_adapter_collection_failure_is_retained_and_explained(boundary, monkeypatch, blocked):
    from dataset_prober.tools.guards import UnsafeURLError

    boundary.transport.side_effect = (
        UnsafeURLError("offline block") if blocked else OSError("offline failure")
    )
    run = start(boundary, monkeypatch)
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["evaluation_reason"] == ("collection_blocked" if blocked else "collection_failed")
    assert result["passes"] is None
    assert run.evaluations[0].observation is boundary.found[0].freshness_observation
    assert run.evaluations[0].claim_reference is None
    assert boundary.found[0].status == "failed"
    boundary.transport.assert_called_once()
    assert agent._save_run_outputs(boundary.found, run, boundary.paths) == 0
    report = json.loads(next(boundary.paths.output_dir.glob("freshness-*.json")).read_text())
    assert report["observations"] == [run.observations[0].to_dict()]


@pytest.mark.parametrize(
    "value,reason",
    [
        (None, "timestamp_null"),
        ("nonsense", "invalid_timestamp"),
        ("2026-10-22T12:00:00", "timezone_missing"),
        ("2026-10-22T12:00:00.12345678Z", "unsupported_precision"),
        ("2027-01-01T00:00:00Z", "future_timestamp"),
    ],
)
def test_operational_explanations_preserve_evaluator_limitations(
    boundary, monkeypatch, value, reason
):
    field = "Modified" if boundary.source == "cbs" else "metadata_modified"
    boundary.metadata[field] = value
    run = start(boundary, monkeypatch)
    candidate = boundary.found[0]
    before = candidate.to_dict()
    result = execute(boundary, "check_freshness", {**boundary.identity, "max_days_old": 30}, run)
    assert result["verdict"] == "UNKNOWN" and result["evaluation_reason"] == reason
    assert result["reason"]
    assert run.evaluations[0].reason == reason
    assert run.observations[0].claims[0].to_dict()["value"] == value
    assert candidate.to_dict() == before
