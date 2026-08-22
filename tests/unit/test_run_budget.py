"""Offline contracts for deterministic profile-agent run-budget enforcement."""

from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
import pytest

from dataset_prober import dataset_agent
from dataset_prober.loading_policy import LoadingPolicySession
from dataset_prober.paths import AppPaths
from dataset_prober.profile_resolution import resolve_profile
from dataset_prober.resource_classification import unknown_assessment
from dataset_prober.tools.base import RunDeadlineExceeded


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ScriptedMessages:
    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("unexpected extra profile-agent model call")
        next_item = self.script.pop(0)
        if callable(next_item):
            next_item = next_item(kwargs)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item


class ScriptedClient:
    def __init__(self, script) -> None:
        self.messages = ScriptedMessages(script)


class ReadOnceUsage:
    def __init__(self) -> None:
        self.reads = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.values = {
            "input_tokens": 5,
            "output_tokens": 7,
            "cache_creation_input_tokens": 11,
            "cache_read_input_tokens": 13,
        }

    def _read(self, field: str) -> int:
        self.reads[field] += 1
        if self.reads[field] > 1:
            raise AssertionError(f"usage field read more than once: {field}")
        return self.values[field]

    @property
    def input_tokens(self) -> int:
        return self._read("input_tokens")

    @property
    def output_tokens(self) -> int:
        return self._read("output_tokens")

    @property
    def cache_creation_input_tokens(self) -> int:
        return self._read("cache_creation_input_tokens")

    @property
    def cache_read_input_tokens(self) -> int:
        return self._read("cache_read_input_tokens")


class ResultStub:
    def __init__(self, identifier: str) -> None:
        self.id = identifier
        self.title = identifier
        self.source = "cbs"
        self.modified = None
        self.columns = None
        self.error = None
        self.format = None
        self.download_url = None
        self.status = "found"
        self.assessment = unknown_assessment()

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id}


class RecordingAdapter:
    source_type = "cbs"
    adapter_identity = "Recording CBS"

    def __init__(self, search_results: int = 1) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.fetch_calls: list[tuple[str, int]] = []
        self.search_results = [ResultStub(f"result-{index}") for index in range(search_results)]

    def is_available(self) -> bool:
        return True

    def search(self, keyword: str, max_results: int, *, remaining_time=None):
        self.search_calls.append((keyword, max_results))
        return list(self.search_results)

    def fetch(self, dataset_id: str, sample_rows: int, *, remaining_time=None):
        self.fetch_calls.append((dataset_id, sample_rows))
        return ResultStub(dataset_id)


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _response(*, stop_reason: str = "end_turn", usage=None, content=None):
    if content is None:
        content = [SimpleNamespace(type="text", text="Done")]
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=usage or _usage(),
    )


def _tool_block(name: str, tool_input: dict, identifier: str):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=identifier)


def _resolved_profile(test_profile, adapter=None):
    adapter = adapter or RecordingAdapter()
    resolved = resolve_profile(test_profile, registry={"cbs": Mock(return_value=adapter)})
    return resolved, adapter


def _profile_with_identity_and_threshold(source, profile_id, name, threshold):
    from dataset_prober.profile_contract import build_profile_contract

    contract = source.contract
    copied_budget = {
        field: getattr(contract.budget, field)
        for field in (
            "max_searches",
            "max_results",
            "max_probes",
            "max_model_calls",
            "max_tokens",
            "max_total_tokens",
            "timeout_minutes",
            "sample_rows",
            "download_timeout_seconds",
        )
    }
    copied_budget["max_total_tokens"] = threshold
    copied_contract = build_profile_contract(
        profile_id=profile_id,
        status="enabled",
        reason=None,
        catalogs=[
            {
                "catalog_id": catalog.catalog_id,
                "adapter": catalog.adapter,
                "name": catalog.name,
                "base_url": catalog.base_url,
                "api_key_env": catalog.api_key_env,
                "timeout_seconds": catalog.timeout_seconds,
                "priority": catalog.priority,
                "required": catalog.required,
                "ckan_dialect": catalog.ckan_dialect,
                "landing_base_url": catalog.landing_base_url,
            }
            for catalog in contract.catalogs
        ],
        budget=copied_budget,
        supported_adapters={"cbs", "ckan", "tavily"},
        trusted_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in contract.trusted_hosts
        ],
        blocked_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in contract.blocked_hosts
        ],
    )
    return replace(source, contract=copied_contract, name=name)


def _budget(test_profile, *, clock=None, **overrides):
    config = replace(test_profile.budget, **overrides)
    return dataset_agent.Budget.from_profile(config, clock=clock or FakeClock())


def _run_profile_with_output(monkeypatch, tmp_path, resolved, budget, client):
    session_cost = dataset_agent.SessionCost()
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", Mock(return_value="offline-key"))
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", client_factory)
    with dataset_agent.console.capture() as capture:
        result = dataset_agent.run_profile(
            user_prompt="Find data",
            resolved_profile=resolved,
            budget=budget,
            loading_session=LoadingPolicySession(download_enabled=False),
            session_cost=session_cost,
            paths=AppPaths(output_dir=tmp_path),
        )
    return result, session_cost, client_factory, capture.get()


def _run_profile(monkeypatch, tmp_path, resolved, budget, client):
    result, session_cost, client_factory, _rendered = _run_profile_with_output(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )
    return result, session_cost, client_factory


def _execute_tool(test_profile, tmp_path, adapter, budget, name, tool_input):
    return dataset_agent.execute_tool(
        tool_name=name,
        tool_input=tool_input,
        tool_map={"cbs": adapter},
        budget=budget,
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[],
        session_cost=dataset_agent.SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )


def test_explicit_planning_uses_cli_overrides_and_final_report_uses_actual_usage(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.load.return_value = test_profile
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    monkeypatch.setattr(dataset_agent, "resolve_profile", Mock(return_value=resolved))
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", Mock(return_value="offline-key"))
    client = ScriptedClient(
        [
            _response(
                usage=_usage(
                    input_tokens=5,
                    output_tokens=7,
                    cache_creation_input_tokens=11,
                    cache_read_input_tokens=13,
                )
            )
        ]
    )
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", client_factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dataset-prober",
            "--profile",
            test_profile.profile_id,
            "--max-tokens",
            "77",
            "--max-results",
            "4",
            "--max-total-tokens",
            "123",
        ],
    )
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert test_profile.budget.max_total_tokens == 50000
    assert "Token planning estimate" in rendered
    assert "budget-based planning estimate" in rendered
    assert "Interpreter: not used (explicit profile selection)." in rendered
    assert "Between-call reported-token stop threshold: 123" in rendered
    assert "Selected-profile threshold subtotal: 123" in rendered
    assert "Nominal session planning figure: 123" in rendered
    assert "Actual reported model usage" in rendered
    assert "Total reported tokens: 36" in rendered
    assert "Between-call reported-token stop threshold: 123 | used: 29.3%" in rendered
    assert "Total reported tokens: 123" not in rendered
    assert "50,000" not in rendered
    assert client.messages.calls[0]["max_tokens"] == 77
    assert client.messages.calls[0]["timeout"] > 0
    system = " ".join(client.messages.calls[0]["system"].lower().split())
    assert "crawl" not in system
    assert "between-call reported-token stop threshold is 123" in system
    assert "each call requests at most 77 output tokens" in system
    assert "total-token ceiling" not in system
    search_definition = next(
        definition
        for definition in client.messages.calls[0]["tools"]
        if definition["name"] == "search_catalog"
    )
    max_results_schema = search_definition["input_schema"]["properties"]["max_results"]
    assert max_results_schema["minimum"] == 1
    assert max_results_schema["maximum"] == 4
    client_factory.assert_called_once_with(api_key="offline-key", max_retries=0)


@pytest.mark.parametrize(
    "flag",
    [
        "--timeout",
        "--max-searches",
        "--max-results",
        "--max-probes",
        "--max-model-calls",
        "--max-tokens",
        "--max-total-tokens",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_cli_budget_overrides_fail_before_profile_loading(
    monkeypatch,
    flag,
    value,
):
    blocked_loader = Mock(side_effect=AssertionError("invalid CLI budget reached profile loading"))
    monkeypatch.setattr(dataset_agent, "ConfigLoader", blocked_loader)
    monkeypatch.setattr(sys, "argv", ["dataset-prober", flag, value])

    with pytest.raises(SystemExit):
        dataset_agent.main()

    blocked_loader.assert_not_called()


def test_remaining_total_allowance_reduces_per_call_output_limit(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_tokens=100, max_total_tokens=50)
    budget.tokens_used = 30
    client = ScriptedClient([_response()])

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert client.messages.calls[0]["max_tokens"] == 20


@pytest.mark.parametrize("reported_tokens", [100, 125])
def test_total_token_threshold_prevents_call_n_plus_one(
    monkeypatch,
    test_profile,
    tmp_path,
    reported_tokens,
):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_tokens=100, max_total_tokens=100)
    client = ScriptedClient(
        [
            _response(
                stop_reason="tool_use",
                content=[],
                usage=_usage(input_tokens=reported_tokens),
            )
        ]
    )

    result, session_cost, _factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert budget.tokens_used == reported_tokens
    assert result.tokens_used == session_cost.total_tokens == reported_tokens


def test_cache_creation_and_read_usage_contribute_to_total_threshold(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_tokens=100, max_total_tokens=100)
    client = ScriptedClient(
        [
            _response(
                stop_reason="tool_use",
                content=[],
                usage=_usage(
                    input_tokens=5,
                    output_tokens=5,
                    cache_creation_input_tokens=40,
                    cache_read_input_tokens=50,
                ),
            )
        ]
    )

    result, session_cost, _factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert budget.tokens_used == result.tokens_used == session_cost.total_tokens == 100
    assert session_cost.model_calls_attempted == 1
    assert session_cost.model_calls_completed == 1
    assert session_cost.model_calls_timed_out == 0
    assert result.model_calls_attempted == 1
    assert result.model_calls_completed == 1
    assert result.model_calls_timed_out == 0


def test_usage_components_follow_one_path_into_profile_and_aggregate_totals(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober.orchestrator import AggregatedResult

    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_total_tokens=100)
    usage = ReadOnceUsage()
    client = ScriptedClient([_response(usage=usage)])

    result, session_cost, _factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )
    summary = AggregatedResult(profile_results=[result]).cost_summary()

    assert session_cost.input_tokens == result.input_tokens == 5
    assert session_cost.output_tokens == result.output_tokens == 7
    assert session_cost.cache_creation_tokens == result.cache_creation_input_tokens == 11
    assert session_cost.cache_read_tokens == result.cache_read_input_tokens == 13
    assert session_cost.total_tokens == result.tokens_used == 36
    assert set(usage.reads.values()) == {1}
    assert summary.count("Total reported tokens: 36") == 2
    assert "Cache-creation cost is not represented" in summary


def test_model_call_ceiling_prevents_call_n_plus_one_with_zero_usage(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_model_calls=1)
    client = ScriptedClient([_response(stop_reason="tool_use", content=[])])

    result, session_cost, _factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert budget.model_calls_used == result.api_calls == session_cost.total_calls == 1
    assert result.model_calls_completed == 1
    assert result.model_calls_timed_out == 0


def test_max_tokens_stops_after_one_request_without_tool_execution_or_retry(
    monkeypatch,
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    resolved, adapter = _resolved_profile(test_profile, adapter)
    budget = _budget(test_profile)
    client = ScriptedClient(
        [
            _response(
                stop_reason="max_tokens",
                content=[
                    _tool_block(
                        "fetch_dataset",
                        {"source": "cbs", "dataset_id": "must-not-run"},
                        "truncated-tool",
                    )
                ],
                usage=_usage(
                    input_tokens=5,
                    output_tokens=7,
                    cache_creation_input_tokens=11,
                    cache_read_input_tokens=13,
                ),
            )
        ]
    )

    result, session_cost, _factory, rendered = _run_profile_with_output(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert adapter.fetch_calls == []
    assert result.datasets_found == []
    assert budget.model_calls_used == 1
    assert budget.tokens_used == result.tokens_used == 36
    assert session_cost.input_tokens == 5
    assert session_cost.output_tokens == 7
    assert session_cost.cache_creation_tokens == 11
    assert session_cost.cache_read_tokens == 13
    assert session_cost.model_calls_attempted == 1
    assert session_cost.model_calls_completed == 1
    assert session_cost.model_calls_timed_out == 0
    assert "Model output limit reached; returning partial results." in rendered
    assert "Unexpected stop reason: max_tokens" not in rendered


def test_max_tokens_preserves_results_collected_by_an_earlier_turn(
    monkeypatch,
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    resolved, adapter = _resolved_profile(test_profile, adapter)
    budget = _budget(test_profile)
    client = ScriptedClient(
        [
            _response(
                stop_reason="tool_use",
                content=[
                    _tool_block(
                        "fetch_dataset",
                        {"source": "cbs", "dataset_id": "already-collected"},
                        "completed-tool",
                    )
                ],
            ),
            _response(
                stop_reason="max_tokens",
                content=[
                    _tool_block(
                        "fetch_dataset",
                        {"source": "cbs", "dataset_id": "must-not-run"},
                        "truncated-tool",
                    )
                ],
            ),
        ]
    )

    result, session_cost, _factory, rendered = _run_profile_with_output(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 2
    assert adapter.fetch_calls == [("already-collected", test_profile.budget.sample_rows)]
    assert [dataset.id for dataset in result.datasets_found] == ["already-collected"]
    assert result.model_calls_attempted == session_cost.model_calls_attempted == 2
    assert result.model_calls_completed == session_cost.model_calls_completed == 2
    assert result.model_calls_timed_out == session_cost.model_calls_timed_out == 0
    assert "Model output limit reached; returning partial results." in rendered
    assert "Unexpected stop reason: max_tokens" not in rendered


def test_unknown_stop_reason_remains_unexpected_without_retry(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile)
    client = ScriptedClient([_response(stop_reason="unknown_reason")])

    _result, _session_cost, _factory, rendered = _run_profile_with_output(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert "Unexpected stop reason: unknown_reason" in rendered


def test_expired_deadline_before_first_request_produces_zero_model_calls(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)
    clock.advance(60)
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="3"))
    client = ScriptedClient([])

    result, session_cost, _factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert client.messages.calls == []
    assert budget.model_calls_used == result.api_calls == session_cost.total_calls == 0
    assert result.model_calls_completed == 0
    assert result.model_calls_timed_out == 0
    assert budget.time_remaining() == 0


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "calls_name", "counter_name"),
    [
        (
            "search_catalog",
            {"source": "cbs", "keyword": "population", "max_results": 1},
            "search_calls",
            "searches_used",
        ),
        (
            "fetch_dataset",
            {"source": "cbs", "dataset_id": "table-a"},
            "fetch_calls",
            "probes_used",
        ),
    ],
)
def test_expired_deadline_prevents_source_operation_before_adapter_call(
    test_profile,
    tmp_path,
    tool_name,
    tool_input,
    calls_name,
    counter_name,
):
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)
    adapter = RecordingAdapter()
    clock.advance(60)

    result = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        tool_name,
        tool_input,
    )

    assert "timed out" in result["error"]
    assert getattr(adapter, calls_name) == []
    assert getattr(budget, counter_name) == 0


def test_executor_reports_search_deadline_without_fake_results_or_retry(
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)

    def expire_during_dispatch(_keyword, _max_results, *, remaining_time):
        clock.advance(60)
        assert remaining_time() == 0
        raise RunDeadlineExceeded("deadline at https://private.invalid/?token=secret")

    adapter.search = Mock(side_effect=expire_during_dispatch)
    loading_session = Mock()

    with dataset_agent.console.capture() as capture:
        result = dataset_agent.execute_tool(
            tool_name="search_catalog",
            tool_input={"source": "cbs", "keyword": "population", "max_results": 1},
            tool_map={"cbs": adapter},
            budget=budget,
            profile=test_profile,
            loading_session=loading_session,
            found_datasets=[],
            session_cost=dataset_agent.SessionCost(),
            paths=AppPaths(output_dir=tmp_path),
        )

    expected = "Profile-agent run deadline exhausted before source operation completed"
    rendered = capture.get()
    assert result == {"error": expected}
    assert "results" not in result
    assert expected in rendered
    assert "Found 1 results" not in rendered
    assert "private.invalid" not in rendered
    assert "secret" not in rendered
    assert adapter.search.call_count == 1
    assert budget.searches_used == 1
    assert budget.time_remaining() == 0
    loading_session.register_dataset_result.assert_not_called()


def test_executor_reports_fetch_deadline_without_dataset_or_registration(
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)

    def expire_during_dispatch(_dataset_id, _sample_rows, *, remaining_time):
        clock.advance(60)
        assert remaining_time() == 0
        raise RunDeadlineExceeded("deadline exhausted")

    adapter.fetch = Mock(side_effect=expire_during_dispatch)
    loading_session = Mock()
    existing = ResultStub("already-collected")
    found_datasets = [existing]

    with dataset_agent.console.capture() as capture:
        result = dataset_agent.execute_tool(
            tool_name="fetch_dataset",
            tool_input={"source": "cbs", "dataset_id": "table-a"},
            tool_map={"cbs": adapter},
            budget=budget,
            profile=test_profile,
            loading_session=loading_session,
            found_datasets=found_datasets,
            session_cost=dataset_agent.SessionCost(),
            paths=AppPaths(output_dir=tmp_path),
        )

    expected = "Profile-agent run deadline exhausted before source operation completed"
    assert result == {"error": expected}
    assert expected in capture.get()
    assert found_datasets == [existing]
    assert adapter.fetch.call_count == 1
    assert budget.probes_used == 1
    assert budget.time_remaining() == 0
    loading_session.register_dataset_result.assert_not_called()


def test_source_deadline_race_blocks_later_tool_blocks_and_next_model_request(
    monkeypatch,
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    resolved, adapter = _resolved_profile(test_profile, adapter)
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)

    def expire_during_first_search(_keyword, _max_results, *, remaining_time):
        clock.advance(60)
        assert remaining_time() == 0
        raise RunDeadlineExceeded("deadline exhausted")

    adapter.search = Mock(side_effect=expire_during_first_search)
    tool_input = {"source": "cbs", "keyword": "population", "max_results": 1}
    client = ScriptedClient(
        [
            _response(
                stop_reason="tool_use",
                content=[
                    _tool_block("search_catalog", tool_input, "search-1"),
                    _tool_block("search_catalog", tool_input, "search-2"),
                ],
            )
        ]
    )
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="3"))

    result, _session_cost, _factory, rendered = _run_profile_with_output(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert adapter.search.call_count == 1
    assert len(client.messages.calls) == 1
    assert budget.searches_used == 1
    assert budget.time_remaining() == 0
    assert result.datasets_found == []
    assert "Profile-agent run deadline exhausted before source operation completed" in rendered
    assert "Found 1 results" not in rendered
    assert "Time limit reached" in rendered


def test_source_operation_starts_only_after_explicit_timeout_continuation(
    monkeypatch,
    test_profile,
    tmp_path,
):
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)
    budget.probes_used = 1
    budget.model_calls_used = 2
    budget.tokens_used = 3
    adapter = RecordingAdapter()
    tool_input = {"source": "cbs", "keyword": "population", "max_results": 1}
    clock.advance(60)

    blocked = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        tool_input,
    )
    assert "timed out" in blocked["error"]
    assert adapter.search_calls == []

    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="1"))
    assert dataset_agent._handle_timeout(
        [],
        budget,
        {"cbs": adapter},
        LoadingPolicySession(download_enabled=False),
        AppPaths(output_dir=tmp_path),
    )
    allowed = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        tool_input,
    )

    assert "results" in allowed
    assert adapter.search_calls == [("population", 1)]
    assert budget.searches_used == 1
    assert budget.probes_used == 1
    assert budget.model_calls_used == 2
    assert budget.tokens_used == 3
    assert budget.time_remaining() == 60


def test_deadline_expiration_between_calls_prevents_next_request(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)

    def expire_after_first(_kwargs):
        clock.advance(60)
        return _response(stop_reason="tool_use", content=[])

    client = ScriptedClient([expire_after_first])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="3"))

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert len(client.messages.calls) == 1
    assert budget.model_calls_used == 1


def test_each_request_receives_positive_decreasing_monotonic_timeout(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)

    def advance_five(_kwargs):
        clock.advance(5)
        return _response(stop_reason="tool_use", content=[])

    def advance_seven(_kwargs):
        clock.advance(7)
        return _response(stop_reason="tool_use", content=[])

    client = ScriptedClient([advance_five, advance_seven, _response()])

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    timeouts = [call["timeout"] for call in client.messages.calls]
    assert timeouts == [60, 55, 48]
    assert all(timeout > 0 for timeout in timeouts)


def test_wall_clock_changes_do_not_affect_monotonic_deadline(monkeypatch, test_profile):
    clock = FakeClock(10)
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)
    monkeypatch.setattr(dataset_agent.time, "time", Mock(return_value=10**12))

    assert budget.time_remaining() == 60
    clock.advance(7)
    assert budget.time_remaining() == 53


def test_api_timeout_records_one_attempt_and_returns_partial_results_without_retry(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober.orchestrator import AggregatedResult

    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile)
    timeout_error = anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://offline.invalid/messages")
    )
    client = ScriptedClient([timeout_error])

    result, session_cost, factory = _run_profile(
        monkeypatch,
        tmp_path,
        resolved,
        budget,
        client,
    )

    assert len(client.messages.calls) == 1
    assert budget.model_calls_used == result.api_calls == session_cost.total_calls == 1
    assert session_cost.model_calls_attempted == 1
    assert session_cost.model_calls_completed == 0
    assert session_cost.model_calls_timed_out == 1
    assert result.model_calls_attempted == 1
    assert result.model_calls_completed == 0
    assert result.model_calls_timed_out == 1
    assert result.tokens_used == 0
    assert result.datasets_found == []
    assert result.model_calls_attempted == (
        result.model_calls_completed + result.model_calls_timed_out
    )
    summary = AggregatedResult(profile_results=[result]).cost_summary()
    assert "Total reported tokens: 0" in summary
    assert "attempted: 1 | completed: 0 | timed out: 1" in summary
    assert (
        "Timed-out attempts returned no usage; their server-side token usage, if any, is unknown."
    ) in summary
    factory.assert_called_once_with(api_key="offline-key", max_retries=0)


def test_timeout_choice_one_resets_only_time_and_permits_next_call(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    clock = FakeClock()
    budget = _budget(test_profile, clock=clock, timeout_minutes=1)
    budget.searches_used = 1
    budget.probes_used = 1
    budget.tokens_used = 10
    clock.advance(60)
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="1"))
    client = ScriptedClient([_response()])

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["timeout"] == 60
    assert budget.searches_used == 1
    assert budget.probes_used == 1
    assert budget.tokens_used == 10
    assert budget.model_calls_used == 1


def test_timeout_continuation_cannot_reset_any_non_time_stop_boundary(
    monkeypatch,
    test_profile,
    tmp_path,
):
    resolved, _adapter = _resolved_profile(test_profile)
    clock = FakeClock()
    budget = _budget(
        test_profile,
        clock=clock,
        max_searches=1,
        max_probes=1,
        max_model_calls=1,
        max_total_tokens=1,
        timeout_minutes=1,
    )
    budget.searches_used = 1
    budget.probes_used = 1
    budget.model_calls_used = 1
    budget.tokens_used = 1
    clock.advance(60)
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="1"))

    assert dataset_agent._handle_timeout(
        [],
        budget,
        {},
        LoadingPolicySession(download_enabled=False),
        AppPaths(output_dir=tmp_path),
    )
    client = ScriptedClient([])
    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert client.messages.calls == []
    assert budget.model_calls_exhausted()
    assert budget.total_tokens_exhausted()
    assert not budget.can_search()
    assert not budget.can_probe()


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "counter_name", "calls_name"),
    [
        (
            "search_catalog",
            {"source": "cbs", "keyword": "population", "max_results": 1},
            "searches_used",
            "search_calls",
        ),
        (
            "fetch_dataset",
            {"source": "cbs", "dataset_id": "table-a"},
            "probes_used",
            "fetch_calls",
        ),
    ],
)
def test_timeout_continuation_cannot_bypass_exhausted_tool_quota(
    monkeypatch,
    test_profile,
    tmp_path,
    tool_name,
    tool_input,
    counter_name,
    calls_name,
):
    adapter = RecordingAdapter()
    resolved, adapter = _resolved_profile(test_profile, adapter)
    clock = FakeClock()
    budget = _budget(
        test_profile,
        clock=clock,
        max_searches=1,
        max_probes=1,
        timeout_minutes=1,
    )
    setattr(budget, counter_name, 1)
    clock.advance(60)
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="1"))
    client = ScriptedClient(
        [
            _response(
                stop_reason="tool_use",
                content=[_tool_block(tool_name, tool_input, "tool-1")],
            ),
            _response(),
        ]
    )

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert len(client.messages.calls) == 2
    assert getattr(adapter, calls_name) == []
    assert getattr(budget, counter_name) == 1


@pytest.mark.parametrize("tool_name", ["search_catalog", "fetch_dataset"])
def test_two_same_kind_tool_blocks_with_one_unit_remaining_call_adapter_once(
    monkeypatch,
    test_profile,
    tmp_path,
    tool_name,
):
    adapter = RecordingAdapter()
    resolved, adapter = _resolved_profile(test_profile, adapter)
    budget = _budget(test_profile, max_searches=1, max_probes=1)
    if tool_name == "search_catalog":
        tool_input = {"source": "cbs", "keyword": "population", "max_results": 1}
    else:
        tool_input = {"source": "cbs", "dataset_id": "table-a"}
    blocks = [
        _tool_block(tool_name, dict(tool_input), "tool-1"),
        _tool_block(tool_name, dict(tool_input), "tool-2"),
    ]
    observed_tool_results = []

    def inspect_tool_results(kwargs):
        observed_tool_results.extend(kwargs["messages"][-1]["content"])
        return _response()

    client = ScriptedClient(
        [_response(stop_reason="tool_use", content=blocks), inspect_tool_results]
    )

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    calls = adapter.search_calls if tool_name == "search_catalog" else adapter.fetch_calls
    assert len(calls) == 1
    assert len(observed_tool_results) == 2
    assert "budget exhausted" in observed_tool_results[1]["content"].lower()


@pytest.mark.parametrize("invalid_value", [None, True, "1", 0, -1])
def test_invalid_max_results_never_reaches_adapter_or_consumes_search_budget(
    test_profile,
    tmp_path,
    invalid_value,
):
    adapter = RecordingAdapter()
    budget = _budget(test_profile)
    tool_input = {"source": "cbs", "keyword": "population"}
    if invalid_value is not None:
        tool_input["max_results"] = invalid_value

    result = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        tool_input,
    )

    assert "positive integer" in result["error"]
    assert adapter.search_calls == []
    assert budget.searches_used == 0


def test_oversized_max_results_is_capped_and_over_return_is_truncated(test_profile, tmp_path):
    adapter = RecordingAdapter(search_results=20)
    budget = _budget(test_profile, max_results=3)

    result = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        {"source": "cbs", "keyword": "population", "max_results": 999},
    )

    assert adapter.search_calls == [("population", 3)]
    assert len(result["results"]) == 3


def test_exact_max_results_limit_succeeds(test_profile, tmp_path):
    adapter = RecordingAdapter(search_results=4)
    budget = _budget(test_profile, max_results=4)

    result = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        {"source": "cbs", "keyword": "population", "max_results": 4},
    )

    assert adapter.search_calls == [("population", 4)]
    assert len(result["results"]) == 4


def test_requested_results_below_configured_cap_are_preserved_and_truncated(
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter(search_results=6)
    budget = _budget(test_profile, max_results=4)

    result = _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "search_catalog",
        {"source": "cbs", "keyword": "population", "max_results": 2},
    )

    assert adapter.search_calls == [("population", 2)]
    assert len(result["results"]) == 2


def test_search_schema_uses_exact_effective_result_bounds(test_profile):
    resolved, _adapter = _resolved_profile(test_profile)
    budget = _budget(test_profile, max_results=4)

    definitions = dataset_agent.build_tool_definitions(resolved, budget)
    search_definition = next(item for item in definitions if item["name"] == "search_catalog")
    max_results = search_definition["input_schema"]["properties"]["max_results"]

    assert max_results["minimum"] == 1
    assert max_results["maximum"] == 4


def test_mixed_tool_response_enforces_search_and_probe_quotas_independently(
    monkeypatch,
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter(search_results=2)
    resolved, adapter = _resolved_profile(test_profile, adapter)
    budget = _budget(test_profile, max_searches=1, max_probes=1, sample_rows=7)
    blocks = [
        _tool_block(
            "search_catalog",
            {"source": "cbs", "keyword": "first", "max_results": 1},
            "search-1",
        ),
        _tool_block(
            "fetch_dataset",
            {"source": "cbs", "dataset_id": "table-a"},
            "fetch-1",
        ),
        _tool_block(
            "search_catalog",
            {"source": "cbs", "keyword": "second", "max_results": 1},
            "search-2",
        ),
        _tool_block(
            "fetch_dataset",
            {"source": "cbs", "dataset_id": "table-b"},
            "fetch-2",
        ),
    ]
    observed_tool_results = []

    def inspect_tool_results(kwargs):
        observed_tool_results.extend(kwargs["messages"][-1]["content"])
        return _response()

    client = ScriptedClient(
        [_response(stop_reason="tool_use", content=blocks), inspect_tool_results]
    )

    _run_profile(monkeypatch, tmp_path, resolved, budget, client)

    assert adapter.search_calls == [("first", 1)]
    assert adapter.fetch_calls == [("table-a", 7)]
    assert len(observed_tool_results) == 4
    assert "budget exhausted" in observed_tool_results[2]["content"].lower()
    assert "budget exhausted" in observed_tool_results[3]["content"].lower()


def test_sample_rows_is_not_model_visible_and_unsolicited_values_are_ignored(
    test_profile,
    tmp_path,
):
    adapter = RecordingAdapter()
    resolved, _adapter = _resolved_profile(test_profile, adapter)
    budget = _budget(test_profile, max_probes=2, sample_rows=7)
    definitions = dataset_agent.build_tool_definitions(resolved, budget)
    fetch_definition = next(item for item in definitions if item["name"] == "fetch_dataset")

    assert "sample_rows" not in fetch_definition["input_schema"]["properties"]
    assert "sample_rows" not in fetch_definition["input_schema"]["required"]
    _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "fetch_dataset",
        {"source": "cbs", "dataset_id": "without-sample"},
    )
    _execute_tool(
        test_profile,
        tmp_path,
        adapter,
        budget,
        "fetch_dataset",
        {"source": "cbs", "dataset_id": "unsolicited", "sample_rows": 999},
    )

    assert adapter.fetch_calls == [("without-sample", 7), ("unsolicited", 7)]


def test_runtime_status_cli_help_and_profile_schema_do_not_advertise_crawls(
    monkeypatch,
    capsys,
    test_profile,
):
    budget = _budget(test_profile)
    assert "crawl" not in budget.status_line().lower()
    assert not hasattr(test_profile.budget, "max_crawls")
    assert "max_crawls" not in test_profile.raw["budget"]

    monkeypatch.setattr(sys, "argv", ["dataset-prober", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        dataset_agent.main()

    help_text = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "--max-crawls" not in help_text
    for flag in ("--max-results", "--max-model-calls", "--max-total-tokens"):
        assert flag in help_text
    assert "Per-call requested profile-agent output-token cap" in help_text
    assert "Between-call reported-token stop threshold" in help_text


def test_interpreter_actual_usage_is_reported_but_not_charged_to_profile_budget(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober.orchestrator import ProfileResult
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    resolved, _adapter = _resolved_profile(test_profile)
    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    monkeypatch.setattr(dataset_agent, "resolve_profile", Mock(return_value=resolved))
    selection = ProfileSelection(
        profile_name=test_profile.profile_id,
        display_name=test_profile.name,
        confidence="high",
        reason="Synthetic",
        execution_order=1,
        keywords_detected=["data"],
        language_detected="en",
    )
    interpretation = InterpretationResult(
        profiles=[selection],
        is_global=False,
        is_multi_profile=False,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
        input_tokens=10,
        output_tokens=20,
        cache_creation_tokens=40,
        cache_read_tokens=30,
    )
    interpreter = Mock()
    interpreter.interpret.return_value = interpretation
    interpreter.present_and_confirm.return_value = True
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(return_value=interpreter))
    runner = Mock(
        return_value=ProfileResult(
            profile_name=test_profile.profile_id,
            display_name=test_profile.name,
            objective=None,
        )
    )
    monkeypatch.setattr(dataset_agent, "run_profile", runner)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "Interpreter actual reported tokens already spent: 100" in rendered
    assert "Interpreter:" in rendered
    assert "Total reported tokens: 100" in rendered
    supplied_budget = runner.call_args.kwargs["budget"]
    assert supplied_budget.tokens_used == 0
    assert supplied_budget.model_calls_used == 0


def test_automatic_multi_profile_planning_and_actual_totals_are_separate(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober.orchestrator import ProfileResult
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    first_profile = _profile_with_identity_and_threshold(
        test_profile,
        "first_profile",
        "First Profile",
        111,
    )
    second_profile = _profile_with_identity_and_threshold(
        test_profile,
        "second_profile",
        "Second Profile",
        222,
    )
    first_resolved, _first_adapter = _resolved_profile(first_profile)
    second_resolved, _second_adapter = _resolved_profile(second_profile)
    resolved = {
        first_profile.profile_id: first_resolved,
        second_profile.profile_id: second_resolved,
    }
    loader = Mock()
    loader.configured_profile_ids.return_value = list(resolved)
    loader.profile_descriptors.return_value = [first_profile, second_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    monkeypatch.setattr(
        dataset_agent,
        "resolve_profile",
        Mock(side_effect=lambda profile, *, registry: resolved[profile.profile_id]),
    )
    selections = [
        ProfileSelection(
            profile_name=profile.profile_id,
            display_name=profile.name,
            confidence="high",
            reason="Synthetic",
            execution_order=index,
            keywords_detected=["data"],
            language_detected="en",
        )
        for index, profile in enumerate((first_profile, second_profile), 1)
    ]
    interpretation = InterpretationResult(
        profiles=selections,
        is_global=False,
        is_multi_profile=True,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
        input_tokens=5,
        output_tokens=7,
        cache_creation_tokens=11,
        cache_read_tokens=13,
    )
    interpreter = Mock()
    interpreter.interpret.return_value = interpretation
    interpreter.present_and_confirm.return_value = True
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(return_value=interpreter))

    def run_profile(**kwargs):
        profile = kwargs["resolved_profile"].profile
        tokens = 2 if profile.profile_id == "first_profile" else 3
        return ProfileResult(
            profile_name=profile.profile_id,
            display_name=profile.name,
            objective=None,
            input_tokens=tokens,
            model_calls_attempted=1,
            model_calls_completed=1,
            token_stop_threshold=kwargs["budget"].max_total_tokens,
        )

    runner = Mock(side_effect=run_profile)
    monkeypatch.setattr(dataset_agent, "run_profile", runner)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    planning = rendered.split("Actual reported model usage", 1)[0]
    actual = rendered.split("Actual reported model usage", 1)[1]
    assert "Interpreter actual reported tokens already spent: 36" in planning
    assert planning.count("Between-call reported-token stop threshold: 111") == 1
    assert planning.count("Between-call reported-token stop threshold: 222") == 1
    assert "Selected-profile threshold subtotal: 333" in planning
    assert (
        "Nominal session planning figure: 369 (36 interpreter actual + 333 thresholds)" in planning
    )
    assert "Total reported tokens: 36" in actual
    assert "Total reported tokens: 41" in actual
    assert "Total reported tokens: 333" not in actual
    assert runner.call_count == 2


def test_planning_uses_cached_config_and_starts_one_timer_immediately_before_run(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober.orchestrator import ProfileResult

    resolved, _adapter = _resolved_profile(test_profile)
    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.load.return_value = test_profile
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    monkeypatch.setattr(dataset_agent, "resolve_profile", Mock(return_value=resolved))
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dataset-prober",
            "--profile",
            test_profile.profile_id,
            "--max-total-tokens",
            "321",
        ],
    )
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    events = []
    planned_configs = []
    fake_clock = FakeClock()
    original_planning = dataset_agent._print_token_planning_estimate

    def display_plan(profile_names, resolved_profiles, effective_configs, interpretation):
        events.append("planning")
        planned_configs.append(effective_configs[test_profile.profile_id])
        original_planning(
            profile_names,
            resolved_profiles,
            effective_configs,
            interpretation,
        )
        fake_clock.advance(45)

    monkeypatch.setattr(dataset_agent, "_print_token_planning_estimate", display_plan)
    original_budget_builder = dataset_agent.Budget.from_profile.__func__

    def construct_budget(cls, budget_config, *, clock=None):
        events.append("budget")
        assert budget_config is planned_configs[0]
        return original_budget_builder(cls, budget_config, clock=fake_clock)

    monkeypatch.setattr(
        dataset_agent.Budget,
        "from_profile",
        classmethod(construct_budget),
    )

    def run_profile(**kwargs):
        events.append("run")
        budget = kwargs["budget"]
        assert budget.max_total_tokens == 321
        assert budget.time_remaining() == budget.timeout_seconds
        return ProfileResult(
            profile_name=test_profile.profile_id,
            display_name=test_profile.name,
            objective=None,
            token_stop_threshold=budget.max_total_tokens,
        )

    runner = Mock(side_effect=run_profile)
    monkeypatch.setattr(dataset_agent, "run_profile", runner)

    with dataset_agent.console.capture():
        dataset_agent.main()

    assert events == ["planning", "budget", "run"]
    assert len(planned_configs) == 1
    assert runner.call_count == 1
