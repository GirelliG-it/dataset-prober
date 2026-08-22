"""Offline tests for fail-closed profile interpretation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_MISSING_CONTENT = object()


def make_mock_response(
    payload: object,
    *,
    raw_text: str | None = None,
    cache_creation_input_tokens: int = 10,
):
    """Return one Anthropic-shaped response without making an API call."""

    response = MagicMock()
    text = raw_text if raw_text is not None else json.dumps(payload)
    response.content = [MagicMock(text=text, type="text")]
    response.usage = MagicMock(
        input_tokens=500,
        output_tokens=200,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=25,
    )
    return response


def selection(profile_id: str, execution_order: int = 1) -> dict[str, object]:
    return {
        "profile_name": profile_id,
        "display_name": "Model-supplied display name",
        "confidence": "high",
        "reason": f"The request matches {profile_id}",
        "execution_order": execution_order,
        "keywords_detected": [profile_id],
        "language_detected": "en",
        "objective": {
            "what_to_find": f"Data for {profile_id}",
            "geographic_scope": "Configured catalog scope",
            "topic": "population",
            "freshness_rule": "within 12 months",
            "download_requested": True,
        },
    }


def response_for(*profile_ids: str) -> dict[str, object]:
    return {
        "profiles": [
            selection(profile_id, index) for index, profile_id in enumerate(profile_ids, 1)
        ],
        "is_global": False,
        "is_multi_profile": len(profile_ids) > 1,
        "interpreter_reasoning": "Selection uses only the supplied enabled profiles.",
    }


@pytest.fixture
def second_enabled_profile(test_profile):
    from dataset_prober.profile_contract import build_profile_contract

    contract = test_profile.contract
    second_contract = build_profile_contract(
        profile_id="second_profile",
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
            }
            for catalog in contract.catalogs
        ],
        budget={
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
        },
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
    return replace(
        test_profile,
        contract=second_contract,
        name="Second Test Profile",
        description="A second enabled test profile",
    )


def make_interpreter(profiles, response):
    from dataset_prober.prompt_interpreter import PromptInterpreter

    client = MagicMock()
    client.messages.create.return_value = response
    return PromptInterpreter(profiles, client=client), client


def test_default_client_disables_sdk_retries_without_model_call(monkeypatch, test_profile):
    from dataset_prober import prompt_interpreter

    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    api_key = MagicMock(return_value="offline-key")
    monkeypatch.setattr(prompt_interpreter, "get_anthropic_api_key", api_key)
    monkeypatch.setattr(prompt_interpreter.anthropic, "Anthropic", client_factory)

    interpreter = prompt_interpreter.PromptInterpreter([test_profile])

    assert interpreter.client is client
    api_key.assert_called_once_with()
    client_factory.assert_called_once_with(api_key="offline-key", max_retries=0)
    client.messages.create.assert_not_called()


def test_explicit_client_is_reused_without_default_factory(monkeypatch, test_profile):
    from dataset_prober import prompt_interpreter

    client = MagicMock()
    client_factory = MagicMock()
    api_key = MagicMock()
    monkeypatch.setattr(prompt_interpreter, "get_anthropic_api_key", api_key)
    monkeypatch.setattr(prompt_interpreter.anthropic, "Anthropic", client_factory)

    interpreter = prompt_interpreter.PromptInterpreter([test_profile], client=client)

    assert interpreter.client is client
    api_key.assert_not_called()
    client_factory.assert_not_called()
    client.messages.create.assert_not_called()


def test_actual_model_arguments_contain_only_supplied_enabled_profiles(test_profile):
    interpreter, client = make_interpreter(
        [test_profile],
        make_mock_response(response_for(test_profile.profile_id)),
    )

    interpreter.interpret("Find population observations")

    call = client.messages.create.call_args.kwargs
    system = call["system"]
    user = call["messages"][0]["content"]
    for rendered in (system, user):
        assert test_profile.profile_id in rendered
        assert test_profile.name in rendered
        assert "dutch_government" not in rendered
        assert "us_government" not in rendered
        assert "eu_open_data" not in rendered
        assert "global" not in rendered.lower()
    assert "selection guidance" in system.lower()
    assert "deterministic semantic verification" in system.lower()


def test_empty_enabled_profiles_fail_before_client_construction_or_call():
    from dataset_prober.prompt_interpreter import ProfileInterpretationError, PromptInterpreter

    client = MagicMock()
    with pytest.raises(ProfileInterpretationError, match="enabled profile"):
        PromptInterpreter([], client=client)

    client.messages.create.assert_not_called()


def test_manual_only_or_disabled_descriptors_are_not_accepted(global_profile):
    from dataset_prober.prompt_interpreter import ProfileInterpretationError, PromptInterpreter

    with pytest.raises(ProfileInterpretationError, match="not enabled"):
        PromptInterpreter([global_profile], client=MagicMock())


@pytest.mark.parametrize(
    "raw_text",
    [
        "not JSON",
        '```json\n{"profiles": []}\n```',
        '{"profiles": [}',
    ],
)
def test_malformed_json_fails_closed(raw_text, test_profile):
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response({}, raw_text=raw_text),
    )

    with pytest.raises(ProfileInterpretationError, match="valid JSON"):
        interpreter.interpret("Find data")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_MISSING_CONTENT, id="missing-content"),
        pytest.param(None, id="none-content"),
        pytest.param("{}", id="string-content"),
        pytest.param(b"{}", id="bytes-content"),
        pytest.param(bytearray(b"{}"), id="bytearray-content"),
        pytest.param(42, id="non-sequence-content"),
        pytest.param([], id="empty-content"),
        pytest.param(
            [SimpleNamespace(type="tool_use", text="ignored")],
            id="non-text-block",
        ),
        pytest.param([SimpleNamespace(text="{}")], id="missing-block-type"),
        pytest.param([SimpleNamespace(type="text")], id="missing-text"),
        pytest.param([SimpleNamespace(type="text", text=None)], id="none-text"),
        pytest.param([SimpleNamespace(type="text", text=7)], id="non-string-text"),
        pytest.param([SimpleNamespace(type="text", text="")], id="empty-text"),
        pytest.param([SimpleNamespace(type="text", text=" \n\t")], id="whitespace-text"),
        pytest.param(
            [
                SimpleNamespace(type="text", text="{}"),
                SimpleNamespace(type="tool_use", text="ignored"),
            ],
            id="text-then-non-text",
        ),
        pytest.param(
            [
                SimpleNamespace(type="tool_use", text="ignored"),
                SimpleNamespace(type="text", text="{}"),
            ],
            id="non-text-then-text",
        ),
    ],
)
def test_malformed_anthropic_content_envelopes_fail_closed(content, test_profile):
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    response = SimpleNamespace()
    if content is not _MISSING_CONTENT:
        response.content = content
    interpreter, _client = make_interpreter([test_profile], response)

    with pytest.raises(ProfileInterpretationError, match="content"):
        interpreter.interpret("Find data")


def test_fragmented_text_content_is_concatenated_before_json_parsing(test_profile):
    payload = json.dumps(response_for(test_profile.profile_id))
    split_at = len(payload) // 2
    response = make_mock_response({})
    response.content = [
        SimpleNamespace(type="text", text=payload[:split_at]),
        SimpleNamespace(type="text", text=payload[split_at:]),
    ]
    interpreter, client = make_interpreter([test_profile], response)

    result = interpreter.interpret("Find data")

    assert result.profile_names == [test_profile.profile_id]
    client.messages.create.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"profiles": []},
        {"profiles": None},
        {"profiles": [selection("unknown_profile")]},
        {
            "profiles": [
                selection("test_profile", 1),
                selection("test_profile", 2),
            ]
        },
    ],
)
def test_missing_empty_unknown_and_duplicate_selections_fail_closed(payload, test_profile):
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response(payload),
    )

    with pytest.raises(ProfileInterpretationError):
        interpreter.interpret("Find data")


_MISSING = object()


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("profile_name",), _MISSING),
        (("profile_name",), 1),
        (("confidence",), _MISSING),
        (("confidence",), 1),
        (("confidence",), "certain"),
        (("reason",), _MISSING),
        (("reason",), None),
        (("execution_order",), _MISSING),
        (("execution_order",), "1"),
        (("execution_order",), True),
        (("keywords_detected",), _MISSING),
        (("keywords_detected",), "population"),
        (("keywords_detected",), ["population", 7]),
        (("language_detected",), _MISSING),
        (("language_detected",), 7),
        (("objective",), _MISSING),
        (("objective",), []),
        (("objective", "what_to_find"), _MISSING),
        (("objective", "what_to_find"), 7),
        (("objective", "geographic_scope"), _MISSING),
        (("objective", "geographic_scope"), False),
        (("objective", "topic"), _MISSING),
        (("objective", "topic"), ["population"]),
        (("objective", "freshness_rule"), _MISSING),
        (("objective", "freshness_rule"), 30),
        (("objective", "download_requested"), _MISSING),
        (("objective", "download_requested"), "yes"),
        (("interpreter_reasoning",), 42),
    ],
)
def test_required_model_fields_are_validated_without_coercion(
    field_path,
    value,
    test_profile,
):
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    payload = deepcopy(response_for(test_profile.profile_id))
    target = payload
    if field_path[0] != "interpreter_reasoning":
        target = payload["profiles"][0]
    for component in field_path[:-1]:
        target = target[component]
    if value is _MISSING:
        target.pop(field_path[-1])
    else:
        target[field_path[-1]] = value

    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response(payload),
    )

    with pytest.raises(ProfileInterpretationError):
        interpreter.interpret("Find data")


def test_valid_single_profile_preserves_objective_and_cost(test_profile):
    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response(response_for(test_profile.profile_id)),
    )

    result = interpreter.interpret("Find data")

    assert result.profile_names == [test_profile.profile_id]
    assert result.profiles[0].display_name == test_profile.name
    assert result.profiles[0].what_to_find == f"Data for {test_profile.profile_id}"
    assert result.profiles[0].download_requested is True
    assert result.input_tokens == 500
    assert result.output_tokens == 200
    assert result.cache_creation_tokens == 10
    assert result.cache_read_tokens == 25
    assert result.total_tokens == 735
    assert result.cost_usd > 0
    objective = result.to_objectives()[0]
    assert objective.profile_name == test_profile.profile_id
    assert objective.what_to_find == f"Data for {test_profile.profile_id}"


def test_confirmation_has_no_hard_coded_global_web_search_claim(
    monkeypatch,
    test_profile,
):
    from dataset_prober import prompt_interpreter

    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response(response_for(test_profile.profile_id)),
    )
    result = interpreter.interpret("Find data")
    result.is_global = True
    monkeypatch.setattr(prompt_interpreter.console, "input", MagicMock(return_value="n"))

    with prompt_interpreter.console.capture() as capture:
        assert interpreter.present_and_confirm(result) is False

    rendered = " ".join(capture.get().split())
    rendered_lower = rendered.lower()
    assert "uses web search" not in rendered_lower
    assert "worldwide" not in rendered_lower
    assert "interpretation actual reported usage: 735 tokens" in rendered_lower
    assert "| estimated cost:" in rendered_lower
    assert "| cost:" not in rendered_lower
    assert (
        "Cache-creation cost is not represented because the pricing contract has no "
        "cache-write rate."
    ) in rendered


def test_confirmation_omits_cache_creation_caveat_when_usage_is_zero(
    monkeypatch,
    test_profile,
):
    from dataset_prober import prompt_interpreter

    interpreter, _client = make_interpreter(
        [test_profile],
        make_mock_response(
            response_for(test_profile.profile_id),
            cache_creation_input_tokens=0,
        ),
    )
    result = interpreter.interpret("Find data")
    monkeypatch.setattr(prompt_interpreter.console, "input", MagicMock(return_value="yes"))

    with prompt_interpreter.console.capture() as capture:
        assert interpreter.present_and_confirm(result) is True

    rendered = " ".join(capture.get().split())
    assert "Interpretation actual reported usage: 725 tokens" in rendered
    assert "| estimated cost:" in rendered.lower()
    assert "| cost:" not in rendered.lower()
    assert "Cache-creation cost is not represented" not in rendered


def test_valid_multi_profile_response_preserves_order_and_objectives(
    test_profile,
    second_enabled_profile,
):
    payload = response_for(test_profile.profile_id, second_enabled_profile.profile_id)
    payload["profiles"] = list(reversed(payload["profiles"]))
    interpreter, _client = make_interpreter(
        [test_profile, second_enabled_profile],
        make_mock_response(payload),
    )

    result = interpreter.interpret("Find data from both configured scopes")

    assert result.profile_names == [test_profile.profile_id, second_enabled_profile.profile_id]
    assert result.is_multi_profile is True
    assert [objective.execution_order for objective in result.to_objectives()] == [1, 2]


def test_model_cannot_expand_the_trusted_enabled_set(test_profile):
    payload = response_for(test_profile.profile_id, "global")
    interpreter, client = make_interpreter([test_profile], make_mock_response(payload))

    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    with pytest.raises(ProfileInterpretationError, match="outside the enabled set"):
        interpreter.interpret("Find data")

    client.messages.create.assert_called_once()
