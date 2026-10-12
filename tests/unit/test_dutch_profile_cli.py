"""Exercise the bundled Dutch CLI with real resolution and mocked service boundaries."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("mode", ["automatic", "explicit", "cancelled", "invalid_confidence"])
def test_bundled_dutch_selection_inspection_and_confirmation(monkeypatch, tmp_path, capsys, mode):
    from dataset_prober import dataset_agent, prompt_interpreter
    from dataset_prober.paths import AppPaths
    from dataset_prober.tools import cbs_tool, ckan_tool

    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=10,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )

    def response(block, stop_reason="end_turn"):
        return SimpleNamespace(content=[block], stop_reason=stop_reason, usage=usage)

    selection = {
        "profiles": [
            {
                "profile_name": "dutch_government",
                "confidence": "high",
                "reason": "Dutch population request",
                "execution_order": 1,
                "keywords_detected": ["bevolking"],
                "language_detected": "nl",
                "objective": {
                    "what_to_find": "Dutch population",
                    "geographic_scope": "Netherlands",
                    "topic": "population",
                    "freshness_rule": "none",
                    "download_requested": False,
                },
            }
        ],
        "interpreter_reasoning": "Use the configured Dutch catalogs",
    }
    interpreter_client = Mock()
    if mode == "invalid_confidence":
        selection["profiles"][0]["confidence"] = 0.95
    interpreter_client.messages.create.return_value = response(
        SimpleNamespace(type="text", text=json.dumps(selection))
    )
    agent_client = Mock()
    agent_client.messages.create.side_effect = [
        response(
            SimpleNamespace(
                type="tool_use",
                id="search-1",
                name="search_catalog",
                input={"source": "cbs", "keyword": "bevolking", "max_results": 1},
            ),
            "tool_use",
        ),
        response(
            SimpleNamespace(
                type="tool_use",
                id="fetch-1",
                name="fetch_dataset",
                input={"source": "cbs", "dataset_id": "83474NED"},
            ),
            "tool_use",
        ),
        response(SimpleNamespace(type="text", text="Inspected the returned resource.")),
    ]
    clients = [agent_client] if mode == "explicit" else [interpreter_client, agent_client]
    factory = Mock(side_effect=clients)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", factory)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", lambda: "offline-key")
    monkeypatch.setattr(prompt_interpreter, "get_anthropic_api_key", lambda: "offline-key")
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Vind bevolking"))
    confirmation = Mock(return_value="n" if mode == "cancelled" else "y")
    monkeypatch.setattr(prompt_interpreter.console, "input", confirmation)

    metadata = {"Identifier": "83474NED", "Title": "Bevolking", "Modified": "2026-10-01"}
    http = Mock(
        side_effect=[
            Mock(json=Mock(return_value={"value": [metadata]})),
            Mock(json=Mock(return_value={"value": [metadata]})),
            Mock(json=Mock(return_value={"value": [{"Perioden": "2025JJ00", "Aantal": 100}]})),
        ]
    )
    monkeypatch.setattr(cbs_tool, "safe_http_get", http)
    forbidden = Mock(side_effect=AssertionError("Unexpected retrieval or persistent loading"))
    monkeypatch.setattr(ckan_tool, "safe_http_get", forbidden)
    monkeypatch.setattr(cbs_tool.CBSTool, "download", forbidden)
    monkeypatch.setattr(ckan_tool.CKANTool, "download", forbidden)
    paths = AppPaths(output_dir=tmp_path)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=paths))
    args = [
        "dataset-prober",
        "--max-model-calls",
        "3",
        "--max-searches",
        "1",
        "--max-results",
        "1",
        "--max-probes",
        "1",
        "--max-tokens",
        "128",
    ]
    if mode == "explicit":
        args += ["--profile", "dutch_government"]
    monkeypatch.setattr(sys, "argv", args)

    dataset_agent.main()

    if mode != "explicit":
        interpreter_client.messages.create.assert_called_once()
        rendered = interpreter_client.messages.create.call_args.kwargs["system"]
        assert "dutch_government" in rendered
        for excluded in ("us_government", "eu_open_data", "global", "tavily"):
            assert excluded not in rendered.lower()
        if mode == "invalid_confidence":
            confirmation.assert_not_called()
            assert "profiles[0].confidence must be a string" in capsys.readouterr().out
        else:
            confirmation.assert_called_once()
    else:
        interpreter_client.messages.create.assert_not_called()
        confirmation.assert_not_called()
    forbidden.assert_not_called()
    if mode in ("cancelled", "invalid_confidence"):
        assert factory.call_count == 1
        agent_client.messages.create.assert_not_called()
        http.assert_not_called()
        assert not paths.agent_results_path.exists()
        return

    assert agent_client.messages.create.call_count == 3
    assert http.call_count == 3
    assert [call.args[0].rsplit("/", 1)[-1] for call in http.call_args_list] == [
        "Tables",
        "TableInfos",
        "TypedDataSet",
    ]
    for call in agent_client.messages.create.call_args_list:
        assert call.kwargs["max_tokens"] == 128
        for tool in call.kwargs["tools"]:
            source = tool["input_schema"]["properties"].get("source")
            if source is not None:
                assert source["enum"] == ["cbs", "ckan"]
    results = json.loads(paths.agent_results_path.read_text())
    assert len(results) == 1
    assert results[0]["id"] == "83474NED"
    assert results[0]["assessment"]["load_eligible"] is True
    assert results[0]["assessment"]["queryability_outcome"] == "verified_non_empty"
