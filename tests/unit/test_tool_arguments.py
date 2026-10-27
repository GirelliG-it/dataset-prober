"""Offline argument-boundary regressions; invalid input must have no side effects."""

import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dataset_prober import dataset_agent
from dataset_prober.loading_policy import AuthorizedLoad, LoadingPolicySession, safe_url_identity
from dataset_prober.paths import AppPaths
from dataset_prober.profile_resolution import resolve_profile

VALID_INPUTS = {
    "search_catalog": {"source": "cbs", "keyword": "population", "max_results": 2},
    "fetch_dataset": {"source": "cbs", "dataset_id": "37789ksz"},
    "check_freshness": {"source": "cbs", "dataset_id": "37789ksz", "max_days_old": 30},
    "download_dataset": {"source": "cbs", "dataset_id": "37789ksz", "title": "Dataset"},
}


def invalid_requests():
    for tool, valid in VALID_INPUTS.items():
        for label, value in (
            ("none_object", None),
            ("list_object", []),
            ("string_object", "source"),
            ("number_object", 1),
            ("bool_object", True),
            ("empty_object", {}),
        ):
            yield pytest.param(tool, value, True, id=f"{tool}-{label}")
        for key in valid:
            yield pytest.param(
                tool,
                {k: v for k, v in valid.items() if k != key},
                True,
                id=f"{tool}-missing-{key}",
            )
        yield pytest.param(
            tool, {**valid, "last_updated": "2000-01-01"}, True, id=f"{tool}-extra-field"
        )
        for key, original in valid.items():
            if isinstance(original, str):
                for label, value in (
                    ("null", None),
                    ("list", []),
                    ("object", {}),
                    ("bool", True),
                    ("number", 17),
                    ("empty", ""),
                    ("blank", " \t\n"),
                ):
                    yield pytest.param(
                        tool, {**valid, key: value}, False, id=f"{tool}-{key}-{label}"
                    )
        numeric_key = {"search_catalog": "max_results", "check_freshness": "max_days_old"}.get(tool)
        if numeric_key:
            for label, value in (
                ("str", "30"),
                ("null", None),
                ("bool", True),
                ("false", False),
                ("fraction", 0.5),
                ("float", 1.0),
                ("negative", -1),
                ("list", []),
                ("object", {}),
            ):
                yield pytest.param(
                    tool, {**valid, numeric_key: value}, False, id=f"{tool}-{numeric_key}-{label}"
                )
        if tool == "search_catalog":
            yield pytest.param(tool, {**valid, "max_results": 0}, False, id="search-zero")
        if tool == "download_dataset":
            for key in ("table_id", "download_url"):
                for label, value in (
                    ("null", None),
                    ("list", []),
                    ("bool", False),
                    ("number", 17),
                    ("empty", ""),
                    ("blank", " \t"),
                ):
                    yield pytest.param(
                        tool, {**valid, key: value}, False, id=f"{tool}-{key}-{label}"
                    )


@pytest.fixture
def boundary(test_profile, sample_dataset_result_ok, tmp_path, monkeypatch):
    candidate = sample_dataset_result_ok
    candidate.download_url = candidate.url  # Match the fixture's classifier-bound retrieval URL.
    candidate.modified = date.today().isoformat()  # Usable evidence exposes threshold type errors.
    adapter = Mock(
        spec=["source_type", "adapter_identity", "is_available", "search", "fetch", "download"]
    )
    adapter.source_type = "cbs"
    adapter.adapter_identity = "CBS Statistics Netherlands"
    adapter.is_available.return_value = True
    adapter.search.return_value = [candidate]
    adapter.fetch.return_value = candidate
    adapter.download.return_value = candidate
    session = LoadingPolicySession(download_enabled=True)
    session.register_dataset_result(candidate, adapter.adapter_identity)
    consent = Mock(wraps=session.request_authorization)
    register = Mock(wraps=session.register_dataset_result)
    monkeypatch.setattr(session, "request_authorization", consent)
    monkeypatch.setattr(session, "register_dataset_result", register)
    prompt = Mock(return_value="yes")
    monkeypatch.setattr(dataset_agent.console, "input", prompt)
    connect = Mock(side_effect=AssertionError("Unexpected database access"))
    monkeypatch.setattr("duckdb.connect", connect)
    return SimpleNamespace(
        candidate=candidate,
        adapter=adapter,
        session=session,
        consent=consent,
        register=register,
        prompt=prompt,
        connect=connect,
        budget=dataset_agent.Budget.from_profile(test_profile.budget),
        profile=test_profile,
        found=[candidate],
        cost=dataset_agent.SessionCost(),
        paths=AppPaths(output_dir=tmp_path / "not-created"),
    )


def execute(boundary, name, arguments):
    return dataset_agent.execute_tool(
        tool_name=name,
        tool_input=arguments,
        tool_map={"cbs": boundary.adapter},
        budget=boundary.budget,
        profile=boundary.profile,
        loading_session=boundary.session,
        found_datasets=boundary.found,
        session_cost=boundary.cost,
        paths=boundary.paths,
    )


@pytest.mark.parametrize("name, arguments, shape_error", list(invalid_requests()))
def test_invalid_arguments_have_no_operational_effect(boundary, name, arguments, shape_error):
    original = copy.deepcopy(boundary.candidate.to_dict())
    assessment = boundary.candidate.assessment
    input_before = copy.deepcopy(arguments)
    result = execute(boundary, name, arguments)

    assert result.get("error"), result
    assert result["reason_code"] == (
        "unexpected_tool_input" if shape_error else "invalid_tool_input"
    )
    if name == "check_freshness":
        assert result["passes"] is None
    json.dumps(result, allow_nan=False)  # No default=str escape hatch for invalid error payloads.
    assert arguments == input_before
    boundary.adapter.search.assert_not_called()
    boundary.adapter.fetch.assert_not_called()
    boundary.adapter.download.assert_not_called()
    boundary.consent.assert_not_called()
    boundary.register.assert_not_called()
    boundary.prompt.assert_not_called()
    boundary.connect.assert_not_called()
    assert boundary.budget.searches_used == boundary.budget.probes_used == 0
    assert boundary.found == [boundary.candidate]
    assert boundary.candidate.to_dict() == original
    assert boundary.candidate.assessment is assessment
    assert not boundary.paths.output_dir.exists()


@pytest.mark.parametrize(
    "arguments", [None, [], {}, {**VALID_INPUTS["download_dataset"], "title": None}]
)
def test_disabled_download_guard_still_precedes_argument_validation(boundary, arguments):
    boundary.session = LoadingPolicySession(download_enabled=False)
    result = execute(boundary, "download_dataset", arguments)
    assert "not permitted" in result["error"].lower()
    boundary.adapter.download.assert_not_called()
    boundary.prompt.assert_not_called()
    boundary.connect.assert_not_called()
    assert boundary.budget.searches_used == boundary.budget.probes_used == 0


@pytest.mark.parametrize("threshold", [0, 30])
def test_valid_freshness_integer_including_zero(boundary, threshold):
    result = execute(
        boundary, "check_freshness", {**VALID_INPUTS["check_freshness"], "max_days_old": threshold}
    )
    assert result["passes"] is None
    assert result["max_days_old"] == threshold
    assert result["reason_code"] == "freshness_unknown"
    assert result["grounding_reason"] == "observation_missing"
    assert "error" not in result
    assert boundary.candidate.assessment.load_eligible


def test_fetch_preserves_exact_identifier_without_trimming(boundary):
    identifier = " 37789ksz "
    execute(boundary, "fetch_dataset", {"source": "cbs", "dataset_id": identifier})
    assert boundary.adapter.fetch.call_args.args[0] == identifier


@pytest.mark.parametrize("name", ["check_freshness", "download_dataset"])
@pytest.mark.parametrize("key", ["source", "dataset_id"])
def test_padded_identity_does_not_match_inspected_candidate(boundary, name, key):
    arguments = {**VALID_INPUTS[name], key: f" {VALID_INPUTS[name][key]} "}
    result = execute(boundary, name, arguments)
    if name == "check_freshness":
        assert result["reason_code"] == "candidate_not_inspected"
    else:
        assert "not an inspected candidate" in result["error"]
    boundary.consent.assert_not_called()
    boundary.adapter.download.assert_not_called()


def test_valid_search_preserves_keyword_and_caps_adapter_and_returned_results(boundary):
    boundary.budget.max_results = 2
    boundary.adapter.search.return_value = [boundary.candidate] * 5
    result = execute(
        boundary, "search_catalog", {"source": "cbs", "keyword": " population ", "max_results": 999}
    )
    assert boundary.adapter.search.call_args.args == (" population ", 2)
    assert len(result["results"]) == 2
    assert boundary.budget.searches_used == 1


def test_valid_download_still_requires_exact_consent_and_uses_inspected_identity(boundary):
    arguments = {
        **VALID_INPUTS["download_dataset"],
        "table_id": boundary.candidate.id,
        "download_url": "https://model.example/untrusted.csv",
    }
    boundary.prompt.return_value = "no"
    denied = execute(boundary, "download_dataset", arguments)
    assert "error" in denied
    boundary.adapter.download.assert_not_called()
    boundary.prompt.return_value = "yes"
    execute(boundary, "download_dataset", arguments)
    dataset, destination, authorization = boundary.adapter.download.call_args.args
    assert dataset is boundary.candidate
    assert destination == str(boundary.paths.duckdb_path)
    assert isinstance(authorization, AuthorizedLoad)
    assert safe_url_identity(boundary.candidate.download_url) in boundary.prompt.call_args.args[0]
    assert "model.example" not in boundary.prompt.call_args.args[0]
    boundary.connect.assert_not_called()


@pytest.mark.parametrize("sources", [("cbs",), ("ckan",), ("cbs", "ckan")])
def test_model_schemas_describe_runtime_shape_and_value_constraints(boundary, sources):
    definitions = dataset_agent.build_tool_definitions(
        SimpleNamespace(source_keys=sources), boundary.budget
    )
    for definition in definitions:
        name = definition["name"]
        schema = definition["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(VALID_INPUTS[name])
        expected_keys = set(VALID_INPUTS[name]) | (
            {"table_id", "download_url"} if name == "download_dataset" else set()
        )
        assert set(schema["properties"]) == expected_keys
        assert schema["properties"]["source"]["enum"] == list(sources)
        for key, field in schema["properties"].items():
            if key in ("max_results", "max_days_old"):
                assert field["type"] == "integer"
                assert field["minimum"] == (1 if key == "max_results" else 0)
            else:
                assert field["type"] == "string"
                assert field["minLength"] == 1
                assert field["pattern"] == r"\S"
        if name == "search_catalog":
            assert schema["properties"]["max_results"]["maximum"] == boundary.budget.max_results


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("fetch_dataset", {}),
        ("search_catalog", None),
        ("check_freshness", {**VALID_INPUTS["check_freshness"], "max_days_old": "30"}),
        ("download_dataset", []),
    ],
)
def test_agent_returns_prior_candidate_and_completed_usage_after_malformed_call(
    boundary, monkeypatch, name, arguments
):
    usage = SimpleNamespace(
        input_tokens=17, output_tokens=11, cache_creation_input_tokens=3, cache_read_input_tokens=2
    )

    def response(content, stop_reason="tool_use"):
        return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)

    def tool_call(tool, arguments, identifier):
        return SimpleNamespace(type="tool_use", name=tool, input=arguments, id=identifier)

    seen_errors = []

    def finish(**kwargs):
        seen_errors.append(json.loads(kwargs["messages"][-1]["content"][0]["content"]))
        return response([SimpleNamespace(type="text", text="Done")], "end_turn")

    script = iter(
        [
            response([tool_call("fetch_dataset", VALID_INPUTS["fetch_dataset"], "fetch")]),
            response([tool_call(name, arguments, "malformed")]),
            finish,
        ]
    )

    def next_response(**kwargs):
        item = next(script)
        return item(**kwargs) if callable(item) else item

    client = Mock()
    client.messages.create.side_effect = next_response
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", lambda: "offline-key")
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", Mock(return_value=client))
    resolved = resolve_profile(
        boundary.profile, registry={"cbs": Mock(return_value=boundary.adapter)}
    )
    assessment = boundary.candidate.assessment
    result = dataset_agent.run_profile(
        "Find data",
        resolved,
        boundary.budget,
        LoadingPolicySession(download_enabled=True),
        boundary.cost,
        boundary.paths,
    )
    assert seen_errors[0]["error"]
    assert result.datasets_found == [boundary.candidate]
    assert boundary.candidate.assessment is assessment
    assert result.model_calls_attempted == result.model_calls_completed == 3
    assert result.model_calls_timed_out == 0
    assert (
        result.input_tokens,
        result.output_tokens,
        result.cache_creation_input_tokens,
        result.cache_read_input_tokens,
    ) == (51, 33, 9, 6)
    assert result.tokens_used == boundary.budget.tokens_used == boundary.cost.total_tokens == 99
    assert boundary.budget.probes_used == 1
    assert boundary.budget.searches_used == 0
    boundary.adapter.fetch.assert_called_once()
    boundary.adapter.download.assert_not_called()
    boundary.prompt.assert_not_called()
    boundary.connect.assert_not_called()


def test_valid_arguments_do_not_hide_unrelated_adapter_defects(boundary):
    boundary.adapter.fetch.side_effect = RuntimeError("adapter programming defect")
    with pytest.raises(RuntimeError, match="adapter programming defect"):
        execute(boundary, "fetch_dataset", VALID_INPUTS["fetch_dataset"])
