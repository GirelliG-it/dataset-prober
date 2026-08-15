"""Task 4 route, authorization, and load-time classification contracts."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest

from dataset_prober.loading_policy import (
    AuthorizationMismatchError,
    AuthorizationState,
    InspectedResourceError,
    LoadClaims,
    LoadingPolicySession,
    canonical_candidate_identity,
    claims_for_dataset,
    claims_for_probe,
    configured_adapter_identity,
    parse_exact_selection,
)
from dataset_prober.prober import ProbeResult
from dataset_prober.resource_classification import (
    AssessmentReason,
    FormatSupport,
    InspectionOutcome,
    QueryabilityOutcome,
    ResourceAssessment,
    ResourceKind,
    classify_record_payload,
)
from dataset_prober.tools.base import DatasetResult
from dataset_prober.tools.guards import FetchedResource


def _assessment(
    *,
    kind=ResourceKind.UNKNOWN,
    inspection=InspectionOutcome.SUCCEEDED,
    queryability=QueryabilityOutcome.NOT_QUERYABLE,
    support=FormatSupport.SUPPORTED,
    reason=AssessmentReason.CONTRADICTORY_EVIDENCE,
) -> ResourceAssessment:
    return ResourceAssessment(
        resource_kind=kind,
        inspection_outcome=inspection,
        queryability_outcome=queryability,
        format_support=support,
        reason=reason,
    )


def _verified_assessment(
    *,
    source="manual",
    adapter_identity=None,
    resource_id="https://public.example/data.csv",
    retrieval_url="https://public.example/data.csv",
) -> ResourceAssessment:
    identity = canonical_candidate_identity(
        source,
        adapter_identity or configured_adapter_identity(source, {}),
        resource_id,
        retrieval_url,
    )
    return classify_record_payload(
        {"value": [{"value": 1}]},
        candidate_identity=identity,
    )[0]


def _probe(assessment=None) -> ProbeResult:
    return ProbeResult(
        url="https://public.example/data.csv",
        name="Inspected resource",
        status="ok",
        row_count=2,
        columns=[{"name": "id", "type": "BIGINT"}],
        sample=[[1], [2]],
        format="CSV",
        assessment=assessment or _verified_assessment(),
    )


def _dataset(*, source="ckan", assessment=None, adapter_identity=None) -> DatasetResult:
    if source == "cbs":
        url = "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet"
        resource_format = "OData"
    else:
        url = "https://public.example/data.csv"
        resource_format = "CSV"
    if assessment is None:
        assessment = _verified_assessment(
            source=source,
            adapter_identity=adapter_identity,
            resource_id="83583NED" if source == "cbs" else "resource-a",
            retrieval_url=url,
        )
    return DatasetResult(
        id="83583NED" if source == "cbs" else "resource-a",
        title="Inspected resource",
        description="",
        source=source,
        source_name=source.upper(),
        url=url,
        download_url=url,
        format=resource_format,
        modified=None,
        frequency=None,
        license=None,
        license_url=None,
        row_count=2,
        columns=[{"name": "id", "type": "BIGINT"}],
        sample=[[1], [2]],
        language=None,
        tags=[],
        status="probed",
        assessment=assessment,
    )


def _authorize_probe(result, destination):
    session = LoadingPolicySession(download_enabled=True)
    session.register_probe_result(result)
    authorization = session.request_authorization(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id=result.url,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    assert authorization is not None
    return authorization


def _authorize_dataset(dataset, adapter_identity, destination):
    session = LoadingPolicySession(download_enabled=True)
    session.register_dataset_result(dataset, adapter_identity)
    authorization = session.request_authorization(
        source_key=dataset.source,
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    assert authorization is not None
    return authorization


def _local_download(path, source_url):
    @contextmanager
    def download(url, **_kwargs):
        assert url == source_url
        yield FetchedResource(
            source_url=url,
            final_url=url,
            path=str(path),
            headers={"Content-Type": "text/csv"},
        )

    return download


@pytest.mark.parametrize(
    "assessment",
    [
        ResourceAssessment(
            ResourceKind.UNKNOWN,
            InspectionOutcome.NOT_INSPECTED,
            QueryabilityOutcome.UNVERIFIED,
            FormatSupport.UNVERIFIED,
            AssessmentReason.UNKNOWN_UNVERIFIED,
        ),
        _assessment(
            kind=ResourceKind.DOCUMENT,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.PDF_CONTENT,
        ),
        _assessment(
            kind=ResourceKind.DATASET,
            queryability=QueryabilityOutcome.STRUCTURED_EMPTY,
            reason=AssessmentReason.STRUCTURED_EMPTY,
        ),
        _assessment(
            queryability=QueryabilityOutcome.AMBIGUOUS,
            reason=AssessmentReason.AMBIGUOUS_SINGLE_COLUMN,
        ),
        _assessment(
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.UNSUPPORTED_FORMAT,
        ),
    ],
)
def test_status_ok_cannot_authorize_report_only_assessment(assessment, tmp_path):
    session = LoadingPolicySession(download_enabled=True)

    with pytest.raises(InspectedResourceError, match="report-only"):
        session.register_probe_result(_probe(assessment))


def test_registration_rejects_caller_assembled_eligible_assessment(tmp_path):
    caller_assessment = ResourceAssessment(
        ResourceKind.DATASET,
        InspectionOutcome.SUCCEEDED,
        QueryabilityOutcome.VERIFIED_NON_EMPTY,
        FormatSupport.SUPPORTED,
        AssessmentReason.VERIFIED_TABULAR_DATA,
    )
    result = _probe(caller_assessment)
    result.status = "ok"
    result.row_count = 2

    session = LoadingPolicySession(download_enabled=True)
    with pytest.raises(InspectedResourceError, match="classifier-issued"):
        session.register_probe_result(result)


def test_assessment_mutation_after_consent_rejects_exact_authorization(tmp_path):
    result = _probe()
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_probe(result, destination)
    result.assessment = _verified_assessment()

    with pytest.raises(AuthorizationMismatchError):
        with authorization.activate(claims_for_probe(result, destination)):
            pass

    assert authorization.state is AuthorizationState.REJECTED


def _reconstruct_assessment(assessment):
    return ResourceAssessment(
        resource_kind=assessment.resource_kind,
        inspection_outcome=assessment.inspection_outcome,
        queryability_outcome=assessment.queryability_outcome,
        format_support=assessment.format_support,
        reason=assessment.reason,
        explanation=assessment.explanation,
    )


@pytest.mark.parametrize(
    "replacement",
    [copy, deepcopy, replace, _reconstruct_assessment],
    ids=["copy", "deepcopy", "dataclass-replace", "field-reconstruction"],
)
def test_unissued_equal_assessment_cannot_activate_with_transplanted_claims(tmp_path, replacement):
    result = _probe()
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_probe(result, destination)
    original_claims = claims_for_probe(result, destination)
    unissued = replacement(result.assessment)
    crafted_claims = replace(original_claims, assessment=unissued)

    assert unissued == result.assessment
    assert unissued is not result.assessment
    assert unissued.load_eligible is False
    assert crafted_claims == original_claims
    with pytest.raises(AuthorizationMismatchError):
        with authorization.activate(crafted_claims):
            pass

    assert authorization.state is AuthorizationState.REJECTED


def test_direct_load_claims_cannot_carry_reusable_evidence_token(tmp_path):
    result = _probe()
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_probe(result, destination)
    original_claims = claims_for_probe(result, destination)
    values = {field.name: getattr(original_claims, field.name) for field in fields(LoadClaims)}
    values["assessment"] = copy(result.assessment)
    directly_constructed = LoadClaims(**values)

    assert "assessment_evidence" not in LoadClaims.__dataclass_fields__
    with pytest.raises(AuthorizationMismatchError):
        with authorization.activate(directly_constructed):
            pass

    assert authorization.state is AuthorizationState.REJECTED


def test_classifier_assessment_cannot_transfer_between_candidates(monkeypatch, tmp_path):
    from dataset_prober import prober

    payload = tmp_path / "candidate-a.csv"
    payload.write_text("id,name\n1,Alice\n", encoding="utf-8")
    candidate_a_url = "https://public.example/candidate-a.csv"
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, candidate_a_url))
    candidate_a = prober.probe_url("Candidate A", candidate_a_url)
    candidate_b = ProbeResult(
        url="https://public.example/candidate-b.csv",
        name="Candidate B",
        status="ok",
        row_count=candidate_a.row_count,
        columns=candidate_a.columns,
        sample=candidate_a.sample,
        format="CSV",
        assessment=candidate_a.assessment,
    )
    session = LoadingPolicySession(download_enabled=True)
    consent = Mock(side_effect=AssertionError("transferred evidence reached consent"))

    with pytest.raises(InspectedResourceError, match="candidate"):
        session.register_probe_result(candidate_b)

    consent.assert_not_called()


def test_agent_route_downgrades_transferred_evidence_before_selection_or_consent(
    monkeypatch, tmp_path
):
    from dataset_prober import dataset_agent
    from dataset_prober.paths import AppPaths

    adapter_identity = "Configured CKAN"
    candidate_a = _dataset(adapter_identity=adapter_identity)
    candidate_b = _dataset(adapter_identity=adapter_identity)
    candidate_b.id = "resource-b"
    candidate_b.url = "https://public.example/candidate-b.csv"
    candidate_b.download_url = candidate_b.url
    candidate_b.assessment = candidate_a.assessment

    tool = Mock()
    tool.adapter_identity = adapter_identity
    tool.fetch.return_value = candidate_b
    tool.download.side_effect = AssertionError("transferred evidence reached writer")
    budget = Mock()
    budget.can_probe.return_value = True
    budget.probes_used = 0
    loading_session = LoadingPolicySession(download_enabled=True)
    found = []
    consent = Mock(side_effect=AssertionError("transferred evidence reached consent"))
    monkeypatch.setattr(loading_session, "request_authorization", consent)

    fetched = dataset_agent.execute_tool(
        tool_name="fetch_dataset",
        tool_input={"source": "ckan", "dataset_id": candidate_b.id, "sample_rows": 3},
        tool_map={"ckan": tool},
        budget=budget,
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=found,
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )
    attempted = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input={
            "source": "ckan",
            "dataset_id": candidate_b.id,
            "title": "LLM says verified",
            "download_url": candidate_b.download_url,
        },
        tool_map={"ckan": tool},
        budget=budget,
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=found,
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert fetched["assessment"]["load_eligible"] is False
    assert fetched["assessment"]["assessment_reason"] == "unknown_unverified"
    assert found == [candidate_b]
    assert candidate_b.assessment.load_eligible is False
    assert candidate_b.status == "failed"
    assert candidate_b.row_count is None
    assert candidate_b.columns is None
    assert candidate_b.sample is None
    assert candidate_b.error is not None
    assert "registration failed" in candidate_b.error.lower()
    assert fetched["status"] == "failed"
    assert fetched["row_count"] is None
    assert fetched["columns"] is None
    assert "sample" not in fetched
    assert fetched["assessment"]["inspection_outcome"] == "not_inspected"
    assert "registration failed" in fetched["error"].lower()
    assert fetched["id"] == candidate_b.id
    assert fetched["download_url"] == candidate_b.download_url
    assert "has not passed inspection" in attempted["error"]
    consent.assert_not_called()
    tool.download.assert_not_called()


def test_genuine_agentic_result_keeps_verified_inspection_facts(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent
    from dataset_prober.paths import AppPaths

    adapter_identity = "Configured CKAN"
    candidate = _dataset(adapter_identity=adapter_identity)
    tool = Mock()
    tool.adapter_identity = adapter_identity
    tool.fetch.return_value = candidate
    loading_session = LoadingPolicySession(download_enabled=True)
    found = []

    fetched = dataset_agent.execute_tool(
        tool_name="fetch_dataset",
        tool_input={"source": "ckan", "dataset_id": candidate.id, "sample_rows": 3},
        tool_map={"ckan": tool},
        budget=Mock(can_probe=Mock(return_value=True), probes_used=0),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=found,
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert found == [candidate]
    assert candidate.status == "probed"
    assert candidate.row_count == 2
    assert candidate.columns == [{"name": "id", "type": "BIGINT"}]
    assert candidate.sample == [[1], [2]]
    assert candidate.error is None
    assert fetched["assessment"]["load_eligible"] is True


def test_run_profile_counts_registration_downgrade_as_failed(monkeypatch, tmp_path, test_profile):
    from dataset_prober import dataset_agent
    from dataset_prober.paths import AppPaths
    from dataset_prober.profile_resolution import resolve_profile

    adapter_identity = "Configured CBS"
    candidate_a = _dataset(source="cbs", adapter_identity=adapter_identity)
    candidate_b = _dataset(source="cbs", adapter_identity=adapter_identity)
    candidate_b.id = "resource-b"
    candidate_b.url = "https://public.example/candidate-b.csv"
    candidate_b.download_url = candidate_b.url
    candidate_b.assessment = candidate_a.assessment

    tool = Mock()
    tool.source_type = "cbs"
    tool.adapter_identity = adapter_identity
    tool.fetch.return_value = candidate_b
    tool.is_available.return_value = True
    resolved = resolve_profile(test_profile, registry={"cbs": Mock(return_value=tool)})
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", lambda: "test-key")

    tool_call = SimpleNamespace(
        type="tool_use",
        name="fetch_dataset",
        input={"source": "cbs", "dataset_id": candidate_b.id, "sample_rows": 3},
        id="fetch-1",
    )
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[tool_call], usage=usage),
        SimpleNamespace(stop_reason="end_turn", content=[], usage=usage),
    ]
    client = Mock()
    client.messages.create.side_effect = responses
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", Mock(return_value=client))

    result = dataset_agent.run_profile(
        user_prompt="Find data",
        resolved_profile=resolved,
        budget=dataset_agent.Budget.from_profile(test_profile.budget),
        loading_session=LoadingPolicySession(download_enabled=True),
        session_cost=dataset_agent.SessionCost(),
        cli_overrides={},
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result.datasets_found == [candidate_b]
    assert result.datasets_failed == [candidate_b]
    assert result.datasets_downloaded == []
    assert candidate_b.status == "failed"
    assert candidate_b.assessment.load_eligible is False

    system_prompt = client.messages.create.call_args_list[0].kwargs["system"].lower()
    model_tools = client.messages.create.call_args_list[0].kwargs["tools"]
    rendered_tools = json.dumps(model_tools).lower()
    assert "verified/load-eligible" in system_prompt
    assert "report-only/ineligible" in system_prompt
    assert "canonical assessment category" in system_prompt
    assert "canonical assessment reason" in system_prompt
    assert "never describe a report-only resource as a verified dataset" in system_prompt
    assert "never recommend a report-only resource for download" in system_prompt
    assert "discovery metadata, filenames, snippets, catalog metadata" in system_prompt
    assert "model prose cannot grant loading authority" in system_prompt
    assert "table_id" in rendered_tools
    for inactive_claim in ("ckan", "tavily", "web search", "download_url", "csv"):
        assert inactive_claim not in rendered_tools
        assert inactive_claim not in system_prompt
    for definition in model_tools:
        source = definition["input_schema"]["properties"].get("source")
        if source is not None:
            assert source["enum"] == ["cbs"]


def test_timeout_batch_cannot_select_registration_downgrade(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent
    from dataset_prober.paths import AppPaths

    downgraded = _dataset(
        assessment=ResourceAssessment(
            ResourceKind.UNKNOWN,
            InspectionOutcome.NOT_INSPECTED,
            QueryabilityOutcome.UNVERIFIED,
            FormatSupport.UNVERIFIED,
            AssessmentReason.UNKNOWN_UNVERIFIED,
        )
    )
    downgraded.status = "failed"
    downgraded.row_count = None
    downgraded.columns = None
    downgraded.sample = None
    authorization = Mock(side_effect=AssertionError("downgraded result reached consent"))
    loading_session = LoadingPolicySession(download_enabled=True)
    monkeypatch.setattr(loading_session, "request_authorization", authorization)
    tool = Mock()
    tool.download.side_effect = AssertionError("downgraded result reached loading")
    answers = iter(["2", "all"])
    monkeypatch.setattr(dataset_agent.console, "input", lambda _prompt: next(answers))

    dataset_agent._handle_timeout(
        [downgraded],
        Mock(),
        {"ckan": tool},
        loading_session,
        AppPaths(output_dir=tmp_path),
    )

    authorization.assert_not_called()
    tool.download.assert_not_called()


def test_result_serialization_is_additive_and_reason_coded():
    result = _probe()
    serialized = result.to_dict()

    assert {
        "url",
        "name",
        "status",
        "row_count",
        "columns",
        "sample",
        "error",
        "format",
    }.issubset(serialized)
    assert serialized["assessment"]["load_eligible"] is True
    assert serialized["assessment"]["assessment_reason"] == "verified_tabular_data"

    dataset_serialized = _dataset().to_dict()
    assert {"id", "title", "source", "status", "format", "row_count"}.issubset(dataset_serialized)
    assert dataset_serialized["assessment"]["load_eligible"] is True
    assert dataset_serialized["assessment"]["assessment_reason"] == ("verified_tabular_data")


def test_dataset_result_preserves_historical_positional_trailing_fields():
    result = DatasetResult(
        "legacy-id",
        "Legacy resource",
        "Historical positional construction",
        "ckan",
        "CKAN",
        "https://public.example/legacy.csv",
        "https://public.example/legacy.csv",
        "CSV",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        [],
        "failed",
        "legacy error",
        321,
        4.25,
    )

    assert result.status == "failed"
    assert result.error == "legacy error"
    assert result.tokens_used == 321
    assert result.cost_usd == 4.25
    assert result.assessment.load_eligible is False

    serialized = result.to_dict()
    assert serialized["tokens_used"] == 321
    assert serialized["cost_usd"] == 4.25
    assert serialized["assessment"]["assessment_reason"] == "unknown_unverified"
    assert serialized["assessment"]["load_eligible"] is False


def test_report_only_output_and_analysis_prompt_preserve_reason():
    from dataset_prober import agent, run

    result = _probe(
        _assessment(
            kind=ResourceKind.LANDING_PAGE,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
        )
    )
    with run.console.capture() as capture:
        run.display_results([result])

    rendered = capture.get()
    prompt = agent._build_prompt([result.to_dict()])
    assert "report-only" in rendered
    assert "html_content" in rendered
    assert "report-only (html_content)" in prompt
    assert "Rows:" not in prompt
    assert "Columns:" not in prompt


def test_orchestrator_does_not_describe_report_only_result_as_manual_download():
    from dataset_prober import orchestrator
    from dataset_prober.orchestrator import (
        AggregatedResult,
        Orchestrator,
        ProfileObjective,
        ProfileResult,
    )

    objective = ProfileObjective(
        profile_name="test",
        display_name="Test",
        what_to_find="one verified dataset",
        geographic_scope="test",
        topic="test",
        freshness_rule="none",
        download_requested=False,
        execution_order=1,
    )
    report_only = _dataset(
        assessment=_assessment(
            kind=ResourceKind.LANDING_PAGE,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
        )
    )
    report_only.title = "Report"
    report_only.id = "r"
    report_only.source_name = "C"
    result = ProfileResult("test", "Test", objective, datasets_found=[report_only])

    evaluated = Orchestrator([objective]).evaluate_result(result, objective)
    handoff = evaluated.handoff_summary()

    assert evaluated.objective_met is False
    assert "none were verified load-eligible" in evaluated.failure_reason
    assert "report-only: html_content" in handoff
    assert "manual download" not in handoff.lower()

    aggregated = AggregatedResult(profile_results=[evaluated])
    with orchestrator.console.capture() as capture:
        aggregated.print_summary_table()

    rendered = capture.get()
    assert "Resource" in rendered
    assert "Dataset" not in rendered
    assert "Report" in rendered
    assert "report-only:" in rendered
    assert "html_content" in rendered


@pytest.mark.parametrize(
    "assessment",
    [
        _assessment(
            kind=ResourceKind.LANDING_PAGE,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
        ),
        ResourceAssessment(
            ResourceKind.DATASET,
            InspectionOutcome.SUCCEEDED,
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
            FormatSupport.SUPPORTED,
            AssessmentReason.VERIFIED_TABULAR_DATA,
        ),
    ],
    ids=["report-only", "caller-assembled-eligible"],
)
def test_manual_report_only_result_is_visible_but_never_selectable(
    monkeypatch, tmp_path, assessment
):
    from dataset_prober import run
    from dataset_prober.paths import AppPaths

    report_only = _probe(assessment)
    paths = AppPaths(output_dir=tmp_path)
    prompt = Mock(side_effect=AssertionError("report-only result requested selection or consent"))
    display = Mock()
    save = Mock()
    monkeypatch.setattr(sys, "argv", ["dataset-prober-probe", "--download"])
    monkeypatch.setattr(run.AppPaths, "resolve", lambda: paths)
    monkeypatch.setattr(
        run,
        "get_sources_interactive",
        lambda: [{"name": report_only.name, "url": report_only.url}],
    )
    monkeypatch.setattr(run, "expand_directories", lambda sources: sources)
    monkeypatch.setattr(run, "probe_all", lambda _sources: [report_only])
    monkeypatch.setattr(run, "display_results", display)
    monkeypatch.setattr(run, "save_results", save)
    monkeypatch.setattr(run.console, "input", prompt)

    run.main()

    display.assert_called_once_with([report_only])
    save.assert_called_once()
    prompt.assert_not_called()


@pytest.mark.parametrize(
    "assessment",
    [
        _assessment(
            queryability=QueryabilityOutcome.AMBIGUOUS,
            reason=AssessmentReason.AMBIGUOUS_SINGLE_COLUMN,
        ),
        ResourceAssessment(
            ResourceKind.DATASET,
            InspectionOutcome.SUCCEEDED,
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
            FormatSupport.SUPPORTED,
            AssessmentReason.VERIFIED_TABULAR_DATA,
        ),
    ],
    ids=["report-only", "caller-assembled-eligible"],
)
def test_agent_metadata_cannot_make_report_only_resource_downloadable(
    monkeypatch, tmp_path, assessment
):
    from dataset_prober import dataset_agent
    from dataset_prober.paths import AppPaths

    report_only = _dataset(assessment=assessment)
    tool = Mock()
    tool.adapter_identity = "Configured CKAN"
    tool.download = Mock(side_effect=AssertionError("report-only resource reached writer"))
    loading_session = LoadingPolicySession(download_enabled=True)
    consent = Mock(side_effect=AssertionError("report-only resource reached consent"))
    monkeypatch.setattr(loading_session, "request_authorization", consent)

    response = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input={
            "source": report_only.source,
            "dataset_id": report_only.id,
            "title": "LLM says verified dataset",
            "download_url": "https://model.example/alternate.csv",
        },
        tool_map={"ckan": tool},
        budget=Mock(),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=[report_only],
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert "report-only" in response["error"]
    consent.assert_not_called()
    tool.download.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><html><body>login</body></html>",
        b"%PDF-1.7\nreport",
        b'{"error":"rate limited"}',
        b"",
        b"id,name\n",
        b"message\nthis is prose\nnot records\n",
    ],
)
def test_manual_load_payload_is_reclassified_before_persistent_access(monkeypatch, tmp_path, body):
    from dataset_prober import prober

    result = _probe()
    payload = tmp_path / "actual.csv"
    payload.write_bytes(body)
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_probe(result, destination)
    persistent = Mock(side_effect=AssertionError("ineligible payload opened persistent DuckDB"))
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, result.url))
    monkeypatch.setattr(prober, "AuthorizedDuckDBConnection", persistent)

    prober.download_to_duckdb(result, str(destination), authorization)

    persistent.assert_not_called()
    assert authorization.state is AuthorizationState.CONSUMED
    assert not destination.exists()


@pytest.mark.parametrize(
    "body",
    [
        b"<div>,<span>\nrow,more\n",
        b'<?report version="1.0"?>,value\nrow,1\n',
        b'<!ENTITY report "summary">,value\nrow,1\n',
        b"<custom,value\nrow,1\n",
    ],
)
def test_generic_markup_is_refused_before_persistent_access(monkeypatch, tmp_path, body):
    from dataset_prober import prober

    result = _probe()
    payload = tmp_path / "actual.csv"
    payload.write_bytes(body)
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_probe(result, destination)
    persistent = Mock(side_effect=AssertionError("markup payload opened persistent DuckDB"))
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, result.url))
    monkeypatch.setattr(prober, "AuthorizedDuckDBConnection", persistent)

    prober.download_to_duckdb(result, str(destination), authorization)

    persistent.assert_not_called()
    assert authorization.state is AuthorizationState.CONSUMED
    assert not destination.exists()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<_custom>,value\nrow,1\n", id="underscore-name-start"),
        pytest.param(
            b"  \n\xef\xbb\xbf<custom,value\nrow,1\n",
            id="whitespace-before-utf8-bom",
        ),
    ],
)
def test_remaining_markup_prefixes_cannot_access_or_mutate_persistent_database(
    monkeypatch, tmp_path, body
):
    from dataset_prober import prober

    result = _probe()
    payload = tmp_path / "actual.csv"
    payload.write_bytes(body)
    destination = tmp_path / "datasets.duckdb"
    setup = duckdb.connect(str(destination))
    try:
        setup.execute("CREATE TABLE sentinel (marker VARCHAR, value INTEGER)")
        setup.execute("INSERT INTO sentinel VALUES ('unchanged', 7)")
    finally:
        setup.close()
    authorization = _authorize_probe(result, destination)
    persistent = Mock(side_effect=AssertionError("markup payload opened persistent DuckDB"))
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, result.url))
    monkeypatch.setattr(prober, "AuthorizedDuckDBConnection", persistent)

    prober.download_to_duckdb(result, str(destination), authorization)

    persistent.assert_not_called()
    assert authorization.state is AuthorizationState.CONSUMED
    verification = duckdb.connect(str(destination), read_only=True)
    try:
        assert verification.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall() == [("sentinel",)]
        assert verification.execute("DESCRIBE sentinel").fetchall() == [
            ("marker", "VARCHAR", "YES", None, None, None),
            ("value", "INTEGER", "YES", None, None, None),
        ]
        assert verification.execute("SELECT * FROM sentinel").fetchall() == [("unchanged", 7)]
    finally:
        verification.close()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            b" " * 8191 + b"\xef\xbb\xbf" + b"<_custom>,value\nrow,1\n",
            id="utf8-bom-split-after-byte-one",
        ),
        pytest.param(
            b" " * 8190 + b"\xef\xbb\xbf" + "<élement>,value\nrow,1\n".encode(),
            id="utf8-bom-split-after-byte-two",
        ),
        pytest.param(
            b" " * 8192 + b"\xef\xbb\xbf" + b"<custom,value\nrow,1\n",
            id="utf8-bom-begins-in-second-chunk",
        ),
        pytest.param(
            b"\xef\xbb\xbf"
            + b" " * 8188
            + b"\xef\xbb\xbf\n\t\xef\xbb\xbf"
            + b"<:custom>,value\nrow,1\n",
            id="repeated-whitespace-and-boms-across-chunks",
        ),
        pytest.param(
            b" " * 8191 + b"\xef\xbb\xbf\n\t\xef\xbb\xbf  ",
            id="split-bom-padding-only",
        ),
    ],
)
def test_chunk_boundary_rejections_cannot_access_or_mutate_persistent_database(
    monkeypatch, tmp_path, body
):
    from dataset_prober import prober

    result = _probe()
    payload = tmp_path / "actual.csv"
    payload.write_bytes(body)
    destination = tmp_path / "datasets.duckdb"
    setup = duckdb.connect(str(destination))
    try:
        setup.execute("CREATE TABLE sentinel (marker VARCHAR, value INTEGER)")
        setup.execute("INSERT INTO sentinel VALUES ('unchanged', 7)")
    finally:
        setup.close()
    authorization = _authorize_probe(result, destination)
    persistent = Mock(side_effect=AssertionError("rejected payload opened persistent DuckDB"))
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, result.url))
    monkeypatch.setattr(prober, "AuthorizedDuckDBConnection", persistent)

    prober.download_to_duckdb(result, str(destination), authorization)

    persistent.assert_not_called()
    assert authorization.state is AuthorizationState.CONSUMED
    verification = duckdb.connect(str(destination), read_only=True)
    try:
        assert verification.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall() == [("sentinel",)]
        assert verification.execute("DESCRIBE sentinel").fetchall() == [
            ("marker", "VARCHAR", "YES", None, None, None),
            ("value", "INTEGER", "YES", None, None, None),
        ]
        assert verification.execute("SELECT * FROM sentinel").fetchall() == [("unchanged", 7)]
    finally:
        verification.close()


def test_eligible_manual_payload_reclassifies_then_loads_transactionally(monkeypatch, tmp_path):
    from dataset_prober import prober

    payload = tmp_path / "actual.csv"
    payload.write_text("id,name\n1,Alice\n2,Bob\n", encoding="utf-8")
    source_url = "https://public.example/data.csv"
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, source_url))
    result = prober.probe_url("Inspected resource", source_url)
    destination = tmp_path / "datasets.duckdb"
    claims = claims_for_probe(result, destination)
    authorization = _authorize_probe(result, destination)

    prober.download_to_duckdb(result, str(destination), authorization)

    assert authorization.state is AuthorizationState.CONSUMED
    connection = duckdb.connect(str(destination), read_only=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [claims.planned_table_name],
        ).fetchone() == (1,)
        assert connection.execute(
            f'SELECT * FROM "{claims.planned_table_name}" ORDER BY id'
        ).fetchall() == [(1, "Alice"), (2, "Bob")]
    finally:
        connection.close()


def test_empty_cbs_load_payload_is_rejected_before_persistent_access(monkeypatch, tmp_path):
    from dataset_prober.tools import cbs_tool

    tool = cbs_tool.CBSTool({"name": "CBS"})
    dataset = _dataset(source="cbs", adapter_identity=tool.adapter_identity)
    destination = tmp_path / "datasets.duckdb"
    authorization = _authorize_dataset(dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(tool, "_download_odata_rows", lambda *_args, **_kwargs: [])
    persistent = Mock(side_effect=AssertionError("empty CBS payload opened persistent DuckDB"))
    monkeypatch.setattr(cbs_tool, "AuthorizedDuckDBConnection", persistent)

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "failed"
    assert "structured_empty" in returned.error
    persistent.assert_not_called()
    assert authorization.state is AuthorizationState.CONSUMED
    assert not destination.exists()


def test_eligible_cbs_payload_reclassifies_then_loads_transactionally(monkeypatch, tmp_path):
    from dataset_prober.tools.cbs_tool import CBSTool

    records = [
        {"Period": "2025", "Value": 1},
        {"Period": "2026", "Value": 2},
    ]
    tool = CBSTool({"name": "CBS"})
    dataset = _dataset(source="cbs", adapter_identity=tool.adapter_identity)
    destination = tmp_path / "datasets.duckdb"
    claims = claims_for_dataset(dataset, tool.adapter_identity, destination)
    authorization = _authorize_dataset(dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(
        tool,
        "_download_odata_rows",
        lambda *_args, **_kwargs: records,
    )

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "downloaded"
    assert authorization.state is AuthorizationState.CONSUMED
    connection = duckdb.connect(str(destination), read_only=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [claims.planned_table_name],
        ).fetchone() == (1,)
        assert connection.execute(
            f'SELECT * FROM "{claims.planned_table_name}" ORDER BY Value'
        ).fetchall() == [("2025", 1), ("2026", 2)]
    finally:
        connection.close()


def test_manual_probe_route_uses_central_assessment(monkeypatch, tmp_path):
    from dataset_prober import prober

    payload = tmp_path / "manual.csv"
    payload.write_text("id,name\n1,Alice\n", encoding="utf-8")
    source_url = "https://public.example/data.csv"
    monkeypatch.setattr(prober, "safe_download", _local_download(payload, source_url))

    result = prober.probe_url("Manual", source_url)

    assert result.status == "ok"
    assert result.assessment.load_eligible is True
    assert result.assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_csv_adapter_probe_routes_use_central_assessment(monkeypatch, tmp_path, source):
    from dataset_prober.tools import ckan_tool, tavily_tool

    payload = tmp_path / f"{source}.csv"
    payload.write_text("id,name\n1,Alice\n", encoding="utf-8")
    source_url = "https://public.example/data.csv"
    module = ckan_tool if source == "ckan" else tavily_tool
    monkeypatch.setattr(module, "safe_download", _local_download(payload, source_url))

    if source == "ckan":
        tool = ckan_tool.CKANTool({"name": "CKAN"})
        candidate = _dataset(source="ckan")
        candidate.status = "found"
        candidate.assessment = ResourceAssessment(
            ResourceKind.UNKNOWN,
            InspectionOutcome.NOT_INSPECTED,
            QueryabilityOutcome.UNVERIFIED,
            FormatSupport.UNVERIFIED,
            AssessmentReason.UNKNOWN_UNVERIFIED,
        )
        result = tool._probe_csv(candidate, sample_rows=3, timeout=1)
    else:
        tool = tavily_tool.TavilyTool({"name": "Direct resources"})
        result = tool._probe_direct(source_url, sample_rows=3, timeout=1)

    assert result.status == "probed"
    assert result.assessment.load_eligible is True
    assert result.assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA


@pytest.fixture(params=["ckan", "tavily"])
def csv_adapter_harness(monkeypatch, tmp_path, request):
    from dataset_prober.tools import base, ckan_tool, tavily_tool

    source = request.param
    inspection_payload = tmp_path / f"{source}-inspection.csv"
    inspection_payload.write_text("id,name\n1,Alice\n2,Bob\n", encoding="utf-8")
    load_payload = tmp_path / f"{source}-load.csv"
    load_payload.write_text("id,name\n10,Carol\n20,Dan\n", encoding="utf-8")
    source_url = "https://public.example/data.csv?token=secret#private"
    module = ckan_tool if source == "ckan" else tavily_tool
    monkeypatch.setattr(
        module,
        "safe_download",
        _local_download(inspection_payload, source_url),
    )
    monkeypatch.setattr(base, "safe_download", _local_download(load_payload, source_url))

    if source == "ckan":
        tool = ckan_tool.CKANTool(
            {
                "name": "Offline CKAN",
                "base_url": "https://catalog.public.example/api/3",
                "ckan_dialect": "ckan_action",
                "landing_base_url": "https://catalog.public.example",
            }
        )

        def inspect_candidate():
            candidate = tool._package_to_result(
                {
                    "name": "resource-a",
                    "title": "Offline CKAN resource",
                    "resources": [{"format": "CSV", "url": source_url}],
                    "license_id": "cc-zero",
                }
            )
            assert candidate is not None
            return tool._probe_csv(candidate, sample_rows=3, timeout=1)

    else:
        tool = tavily_tool.TavilyTool({"name": "Offline direct resource"})
        assert tool.is_available() is False

        def inspect_candidate():
            return tool._probe_direct(source_url, sample_rows=3, timeout=1)

    return SimpleNamespace(
        source=source,
        source_url=source_url,
        tool=tool,
        inspect_candidate=inspect_candidate,
        load_payload=load_payload,
    )


def _register_select_and_authorize(result, tool, destination, source):
    session = LoadingPolicySession(download_enabled=True)
    session.register_dataset_result(result, tool.adapter_identity)

    indices = parse_exact_selection("1", 1)
    assert indices == [0]
    selected = [result][indices[0]]
    assert selected is result

    claims = claims_for_dataset(selected, tool.adapter_identity, destination)
    prompts = []
    authorization = session.request_authorization(
        source_key=source,
        adapter_identity=tool.adapter_identity,
        resource_id=selected.id,
        destination=destination,
        input_func=lambda prompt: prompts.append(prompt) or "yes",
    )
    assert authorization is not None
    assert len(prompts) == 1
    assert claims.database_path in prompts[0]
    assert claims.planned_table_name in prompts[0]
    assert "token=secret" not in prompts[0]
    assert "#private" not in prompts[0]
    return selected, claims, authorization


def test_csv_adapter_positive_chain_preserves_real_inspection_evidence(
    csv_adapter_harness, tmp_path
):
    harness = csv_adapter_harness
    result = harness.inspect_candidate()

    inspected_assessment = result.assessment
    expected_identity = canonical_candidate_identity(
        harness.source,
        harness.tool.adapter_identity,
        result.id,
        harness.source_url,
    )
    assert inspected_assessment.load_eligible is True
    assert all("Carol" not in str(row) for row in result.sample)

    destination = tmp_path / f"{harness.source}.duckdb"
    selected, claims, authorization = _register_select_and_authorize(
        result,
        harness.tool,
        destination,
        harness.source,
    )
    assert claims.candidate_identity == expected_identity
    assert selected.assessment is inspected_assessment

    returned = harness.tool.download(selected, destination, authorization)

    assert returned is selected
    assert returned.status == "downloaded"
    assert authorization.state is AuthorizationState.CONSUMED
    serialized = returned.to_dict()
    assert "token=secret" not in str(serialized)
    assert "#private" not in str(serialized)

    connection = duckdb.connect(claims.database_path, read_only=True)
    try:
        assert connection.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall() == [(claims.planned_table_name,)]
        assert connection.execute(
            f'SELECT * FROM "{claims.planned_table_name}" ORDER BY id'
        ).fetchall() == [(10, "Carol"), (20, "Dan")]
    finally:
        connection.close()


def test_csv_adapter_reassessment_failure_precedes_persistent_access(csv_adapter_harness, tmp_path):
    harness = csv_adapter_harness
    report_payload = "<!doctype html><html><body>not a dataset</body></html>"

    missing_destination = tmp_path / f"{harness.source}-must-not-exist.duckdb"
    result = harness.inspect_candidate()
    inspected_assessment = result.assessment
    selected, claims, authorization = _register_select_and_authorize(
        result,
        harness.tool,
        missing_destination,
        harness.source,
    )
    assert selected.assessment is inspected_assessment
    assert claims.candidate_identity == canonical_candidate_identity(
        harness.source,
        harness.tool.adapter_identity,
        selected.id,
        harness.source_url,
    )
    harness.load_payload.write_text(report_payload, encoding="utf-8")

    returned = harness.tool.download(selected, missing_destination, authorization)

    assert returned.status == "failed"
    assert "html_content" in returned.error
    assert authorization.state is AuthorizationState.CONSUMED
    assert not missing_destination.exists()

    sentinel_destination = tmp_path / f"{harness.source}-sentinel.duckdb"
    connection = duckdb.connect(str(sentinel_destination))
    try:
        connection.execute("CREATE TABLE sentinel (marker VARCHAR, sequence INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES ('keep', 7)")
    finally:
        connection.close()

    result = harness.inspect_candidate()
    inspected_assessment = result.assessment
    selected, claims, authorization = _register_select_and_authorize(
        result,
        harness.tool,
        sentinel_destination,
        harness.source,
    )
    assert selected.assessment is inspected_assessment
    assert claims.candidate_identity == canonical_candidate_identity(
        harness.source,
        harness.tool.adapter_identity,
        selected.id,
        harness.source_url,
    )

    returned = harness.tool.download(selected, sentinel_destination, authorization)

    assert returned.status == "failed"
    assert "html_content" in returned.error
    assert authorization.state is AuthorizationState.CONSUMED
    connection = duckdb.connect(str(sentinel_destination), read_only=True)
    try:
        assert connection.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall() == [("sentinel",)]
        assert [
            (row[1], row[2])
            for row in connection.execute("PRAGMA table_info('sentinel')").fetchall()
        ] == [("marker", "VARCHAR"), ("sequence", "INTEGER")]
        assert connection.execute("SELECT * FROM sentinel").fetchall() == [("keep", 7)]
    finally:
        connection.close()


def test_cbs_fetch_route_uses_record_assessment(monkeypatch):
    from dataset_prober.tools import cbs_tool

    metadata = Mock()
    metadata.json.return_value = {
        "value": [{"Title": "CBS table", "Modified": "2026-08-01", "Summary": ""}]
    }
    sample = Mock()
    sample.json.return_value = {
        "value": [{"Period": "2025", "Value": 1}, {"Period": "2026", "Value": 2}]
    }
    monkeypatch.setattr(cbs_tool, "safe_http_get", Mock(side_effect=[metadata, sample]))

    result = cbs_tool.CBSTool({"name": "CBS"}).fetch("83583NED", sample_rows=2)

    assert result.status == "probed"
    assert result.assessment.load_eligible is True
    assert result.assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA


def test_cbs_error_envelope_remains_report_only(monkeypatch):
    from dataset_prober.tools import cbs_tool

    metadata = Mock()
    metadata.json.return_value = {"value": [{"Title": "CBS table"}]}
    sample = Mock()
    sample.json.return_value = {"error": {"code": "Denied"}}
    monkeypatch.setattr(cbs_tool, "safe_http_get", Mock(side_effect=[metadata, sample]))

    result = cbs_tool.CBSTool({"name": "CBS"}).fetch("83583NED", sample_rows=2)

    assert result.status == "probed"
    assert result.assessment.load_eligible is False
    assert result.assessment.resource_kind is ResourceKind.ERROR_RESPONSE
    assert result.assessment.reason is AssessmentReason.ERROR_RESPONSE


def test_cbs_metadata_error_envelope_blocks_later_sample_inspection(monkeypatch):
    from dataset_prober.tools import cbs_tool

    metadata = Mock()
    metadata.json.return_value = {"error": {"code": "Denied"}}
    sample = Mock(side_effect=AssertionError("metadata error reached sample request"))
    transport = Mock(side_effect=[metadata, sample])
    monkeypatch.setattr(cbs_tool, "safe_http_get", transport)

    result = cbs_tool.CBSTool({"name": "CBS"}).fetch("83583NED", sample_rows=2)

    assert transport.call_count == 1
    assert result.status == "failed"
    assert result.assessment.load_eligible is False
    assert result.assessment.resource_kind is ResourceKind.ERROR_RESPONSE
    assert result.assessment.reason is AssessmentReason.ERROR_RESPONSE


def test_ckan_api_error_and_unsupported_package_are_reason_coded(monkeypatch):
    from dataset_prober.tools import ckan_tool

    response = Mock()
    response.json.return_value = {"success": False, "error": {"message": "Denied"}}
    transport = Mock(return_value=response)
    monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
    tool = ckan_tool.CKANTool(
        {
            "name": "CKAN",
            "base_url": "https://catalog.public.example/api/3",
            "ckan_dialect": "ckan_action",
            "landing_base_url": "https://catalog.public.example",
        }
    )

    error_result = tool.fetch("resource-a", sample_rows=2)
    response.json.return_value = {
        "success": True,
        "result": {"name": "resource-a", "title": "Report", "resources": []},
    }
    unsupported_result = tool.fetch("resource-a", sample_rows=2)

    assert error_result.status == "failed"
    assert error_result.assessment.resource_kind is ResourceKind.ERROR_RESPONSE
    assert error_result.assessment.reason is AssessmentReason.ERROR_RESPONSE
    assert unsupported_result.status == "found"
    assert unsupported_result.row_count is None
    assert unsupported_result.columns is None
    assert unsupported_result.sample is None
    assert unsupported_result.assessment.reason is AssessmentReason.UNSUPPORTED_FORMAT
    assert unsupported_result.assessment.load_eligible is False


def test_ckan_outer_fetch_failure_uses_failed_unverified_assessment(monkeypatch):
    from dataset_prober.tools import ckan_tool

    sensitive_url = "https://user:password@public.example/data.csv?token=secret#fragment"
    monkeypatch.setattr(
        ckan_tool,
        "safe_http_get",
        Mock(side_effect=RuntimeError(f"Transport failed for {sensitive_url}")),
    )
    tool = ckan_tool.CKANTool(
        {
            "name": "CKAN",
            "base_url": "https://catalog.public.example/api/3",
            "ckan_dialect": "ckan_action",
            "landing_base_url": "https://catalog.public.example",
        }
    )

    result = tool.fetch("resource-a", sample_rows=2)

    assert result.id == "resource-a"
    assert result.title == "resource-a"
    assert result.status == "failed"
    assert result.row_count is None
    assert result.columns is None
    assert result.sample is None
    assert result.assessment.resource_kind is ResourceKind.UNKNOWN
    assert result.assessment.inspection_outcome is InspectionOutcome.FAILED
    assert result.assessment.queryability_outcome is QueryabilityOutcome.UNVERIFIED
    assert result.assessment.format_support is FormatSupport.UNVERIFIED
    assert result.assessment.reason is AssessmentReason.INSPECTION_FAILED
    assert result.assessment.load_eligible is False
    assert "Transport failed" in result.error
    assert "password" not in result.error
    assert "token=secret" not in result.error

    with pytest.raises(InspectedResourceError):
        LoadingPolicySession(download_enabled=True).register_dataset_result(
            result, tool.adapter_identity
        )


def test_ckan_probe_failure_is_failed_and_clears_rejected_inspection_facts(
    monkeypatch,
):
    from dataset_prober.tools import ckan_tool

    tool = ckan_tool.CKANTool({"name": "CKAN"})
    candidate = _dataset(source="ckan", adapter_identity=tool.adapter_identity)
    candidate.row_count = 99
    candidate.columns = [{"name": "stale", "type": "BIGINT"}]
    candidate.sample = [[99]]
    monkeypatch.setattr(
        ckan_tool,
        "safe_download",
        Mock(side_effect=RuntimeError("deterministic probe failure")),
    )

    result = tool._probe_csv(candidate, sample_rows=2, timeout=1)

    assert result.status == "failed"
    assert result.row_count is None
    assert result.columns is None
    assert result.sample is None
    assert result.assessment.inspection_outcome is InspectionOutcome.FAILED
    assert result.assessment.queryability_outcome is QueryabilityOutcome.UNVERIFIED
    assert result.assessment.format_support is FormatSupport.UNVERIFIED
    assert result.assessment.reason is AssessmentReason.INSPECTION_FAILED
    assert result.assessment.load_eligible is False
    assert result.error == "Probe failed: deterministic probe failure"
