"""Offline contracts for truthful bundled-profile availability."""

from __future__ import annotations

import copy
import json
import re
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from dataset_prober.profile_contract import ProfileContractError, ProfileStatus

DUTCH_REASON = (
    "Only the supported CBS StatLine OData v3 route is exposed; broader Dutch "
    "catalogue discovery awaits repair and certification."
)
US_REASON = (
    "Data.gov v4 requires a source-specific adapter; the legacy v3 CKAN route "
    "is not the normal v0.1 release target."
)
EU_REASON = (
    "The data.europa.eu route is incompatible with the generic /action/* CKAN "
    "adapter, and no Eurostat adapter is registered."
)
GLOBAL_REASON = (
    "No supported and certified safe discovery transport is available to the "
    "Global profile in v0.1."
)


def valid_raw_profile() -> dict[str, object]:
    return {
        "name": "Synthetic Profile",
        "description": "Synthetic offline profile",
        "language": "en",
        "cost_warning": False,
        "status": "enabled",
        "reason": None,
        "scope": {
            "regions": ["Synthetic region"],
            "instruction": "Use only the configured catalog as selection guidance.",
        },
        "budget": {
            "max_searches": 1,
            "max_crawls": 1,
            "max_probes": 1,
            "max_tokens": 512,
            "timeout_minutes": 1,
            "sample_rows": 2,
            "download_timeout_seconds": 30,
        },
        "pricing": {
            "input_per_million": 3.0,
            "output_per_million": 15.0,
            "cache_read_per_million": 0.3,
        },
        "catalogs": [
            {
                "catalog_id": "synthetic_cbs",
                "adapter": "cbs",
                "name": "Synthetic CBS",
                "base_url": "https://opendata.cbs.nl/ODataCatalog",
                "api_key_env": None,
                "timeout_seconds": 10,
                "priority": 1,
                "required": True,
            }
        ],
        "trusted_hosts": [
            {"hostname": "example.com", "include_subdomains": True},
        ],
        "blocked_hosts": [
            {"hostname": "blocked.example", "include_subdomains": False},
        ],
        "license_preference": ["CC0"],
        "license_warn": [],
        "license_reject": [],
    }


def write_profile(tmp_path, raw: object, profile_id: str = "synthetic_profile"):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / f"{profile_id}.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    return profiles


def test_bundled_profiles_have_truthful_lifecycle_and_catalogs():
    from dataset_prober.config_loader import ConfigLoader

    loader = ConfigLoader()
    profiles = {profile.profile_id: profile for profile in loader.profile_descriptors()}

    assert set(profiles) == {
        "dutch_government",
        "us_government",
        "eu_open_data",
        "global",
    }
    assert profiles["dutch_government"].status is ProfileStatus.MANUAL_ONLY
    assert profiles["dutch_government"].reason == DUTCH_REASON
    assert profiles["us_government"].status is ProfileStatus.DISABLED
    assert profiles["us_government"].reason == US_REASON
    assert profiles["eu_open_data"].status is ProfileStatus.DISABLED
    assert profiles["eu_open_data"].reason == EU_REASON
    assert profiles["global"].status is ProfileStatus.DISABLED
    assert profiles["global"].reason == GLOBAL_REASON

    dutch_catalogs = profiles["dutch_government"].catalogs
    assert len(dutch_catalogs) == 1
    assert dutch_catalogs[0].catalog_id == "cbs_statline"
    assert dutch_catalogs[0].adapter == "cbs"
    assert dutch_catalogs[0].required is True
    assert dutch_catalogs[0].api_key_env is None
    assert all(
        not profile.catalogs for key, profile in profiles.items() if key != "dutch_government"
    )


def test_every_bundled_yaml_uses_only_the_explicit_contract_shape():
    from dataset_prober.config_loader import ConfigLoader

    for profile in ConfigLoader().profile_descriptors():
        assert profile.contract.profile_id == profile.profile_id
        assert "status" in profile.raw
        assert "reason" in profile.raw
        assert "trusted_domains" not in profile.raw
        assert "blocked_sources" not in profile.raw
        for catalog in profile.raw["catalogs"]:
            assert "type" not in catalog
            assert {
                "catalog_id",
                "adapter",
                "name",
                "base_url",
                "api_key_env",
                "timeout_seconds",
                "priority",
                "required",
            } == set(catalog)


def test_contract_and_runtime_views_cannot_diverge(profiles_dir):
    from dataset_prober.config_loader import ConfigLoader

    profile = ConfigLoader(profiles_dir).load("test_profile")
    contract_catalog = profile.contract.catalogs[0]
    runtime_catalog = profile.catalogs[0]

    assert runtime_catalog.catalog_id == contract_catalog.catalog_id
    assert runtime_catalog.adapter == contract_catalog.adapter
    assert runtime_catalog.required == contract_catalog.required
    assert profile.budget.max_searches == profile.contract.budget.max_searches
    assert profile.trusted_hosts is profile.contract.trusted_hosts
    assert profile.blocked_hosts is profile.contract.blocked_hosts

    profile.raw["catalogs"][0]["adapter"] = "ckan"
    assert profile.catalogs[0].adapter == "cbs"


def test_selection_queries_have_unambiguous_lifecycle_meaning():
    from dataset_prober.config_loader import ConfigLoader

    loader = ConfigLoader()

    assert loader.configured_profile_ids() == [
        "dutch_government",
        "eu_open_data",
        "global",
        "us_government",
    ]
    assert loader.automatically_selectable_profile_ids() == []
    assert loader.explicitly_selectable_profile_ids() == ["dutch_government"]


@pytest.mark.parametrize(
    "case",
    [
        "old_type",
        "missing_status",
        "missing_reason",
        "missing_catalog_id",
        "missing_required",
        "old_trusted_hosts",
        "old_blocked_hosts",
        "unsupported_adapter",
        "unknown_catalog_field",
    ],
)
def test_obsolete_missing_and_unknown_contract_fields_fail_closed(tmp_path, case):
    from dataset_prober.config_loader import ConfigLoader

    raw = copy.deepcopy(valid_raw_profile())
    catalog = raw["catalogs"][0]
    if case == "old_type":
        catalog["type"] = catalog.pop("adapter")
    elif case == "missing_status":
        del raw["status"]
    elif case == "missing_reason":
        del raw["reason"]
    elif case == "missing_catalog_id":
        del catalog["catalog_id"]
    elif case == "missing_required":
        del catalog["required"]
    elif case == "old_trusted_hosts":
        raw["trusted_domains"] = ["example.com"]
        del raw["trusted_hosts"]
    elif case == "old_blocked_hosts":
        raw["blocked_sources"] = ["blocked.example"]
        del raw["blocked_hosts"]
    elif case == "unsupported_adapter":
        catalog["adapter"] = "unregistered"
    elif case == "unknown_catalog_field":
        catalog["unexpected"] = "value"

    with pytest.raises(ProfileContractError):
        ConfigLoader(write_profile(tmp_path, raw)).load("synthetic_profile")


def test_old_catalog_type_reports_obsolete_field_deterministically(tmp_path):
    from dataset_prober.config_loader import ConfigLoader

    raw = valid_raw_profile()
    raw["catalogs"][0]["type"] = raw["catalogs"][0].pop("adapter")
    loader = ConfigLoader(write_profile(tmp_path, raw))

    with pytest.raises(ProfileContractError) as first:
        loader.load("synthetic_profile")
    with pytest.raises(ProfileContractError) as second:
        loader.load("synthetic_profile")

    expected = [(issue.code, issue.path) for issue in first.value.issues]
    assert expected == [(issue.code, issue.path) for issue in second.value.issues]
    assert ("obsolete_field", "catalogs[0].type") in expected
    assert ("missing_field", "catalogs[0].adapter") in expected


def test_non_mapping_top_level_fails_with_structured_error(tmp_path):
    from dataset_prober.config_loader import ConfigLoader

    with pytest.raises(ProfileContractError) as error:
        ConfigLoader(write_profile(tmp_path, ["not", "a", "mapping"])).load("synthetic_profile")

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("invalid_type", "profile")
    ]


def test_host_rules_use_parsed_hostname_boundaries(test_profile, global_profile):
    assert test_profile.is_domain_trusted("https://example.com/data.csv")
    assert test_profile.is_domain_trusted("https://api.example.com/data.csv")
    assert not test_profile.is_domain_trusted("https://notexample.com/data.csv")
    assert not test_profile.is_domain_trusted("https://example.com.evil/data.csv")
    assert test_profile.is_domain_trusted("https://test.gov/data.csv")
    assert not test_profile.is_domain_trusted("https://sub.test.gov/data.csv")
    assert not test_profile.is_domain_trusted("not a URL")
    assert not global_profile.is_domain_trusted("https://anything.example/data.csv")

    assert test_profile.is_source_blocked("https://blocked.example.com/data.csv")
    assert test_profile.is_source_blocked("https://sub.blocked.example.com/data.csv")
    assert not test_profile.is_source_blocked("https://blocked.example.com.evil/data.csv")


@pytest.mark.parametrize(
    "url",
    [
        "https://opendata.cbs.nl:bad/x",
        "https://opendata.cbs.nl:99999/x",
        "https://evil.example\\@opendata.cbs.nl/x",
        "https://opendata.cbs.nl/%zz",
        "https://user:pass@opendata.cbs.nl/x",
        "https://opendata.cbs.nl:/x",
        "ftp://opendata.cbs.nl/x",
        "https:///opendata.cbs.nl/x",
        "https://opendata.cbs.nl/%5cadmin",
        "https://opendata.cbs.nl/%0aheader",
        "https://opendata.cbs.nl/\x00data",
        "https://opendata.cbs.nl/\u0080data",
        "https://opendata.cbs.nl/%C2%80data",
        "https://opendata.cbs.nl/\u0085data",
        "https://opendata.cbs.nl/%C2%85data",
    ],
)
def test_malformed_runtime_urls_match_neither_trusted_nor_blocked_hosts(tmp_path, url):
    from dataset_prober.config_loader import ConfigLoader

    raw = valid_raw_profile()
    host_rule = {"hostname": "opendata.cbs.nl", "include_subdomains": False}
    raw["trusted_hosts"] = [host_rule]
    raw["blocked_hosts"] = [host_rule]
    profile = ConfigLoader(write_profile(tmp_path, raw)).load("synthetic_profile")

    assert profile.is_domain_trusted(url) is False
    assert profile.is_source_blocked(url) is False


def test_valid_runtime_url_paths_queries_and_standard_ports_still_match(tmp_path):
    from dataset_prober.config_loader import ConfigLoader

    raw = valid_raw_profile()
    host_rule = {"hostname": "opendata.cbs.nl", "include_subdomains": False}
    raw["trusted_hosts"] = [host_rule]
    raw["blocked_hosts"] = [host_rule]
    profile = ConfigLoader(write_profile(tmp_path, raw)).load("synthetic_profile")

    for url in (
        "https://opendata.cbs.nl/path/to/table?format=json&rows=5",
        "https://opendata.cbs.nl/path%20with%20spaces?label=some%20value",
        "https://opendata.cbs.nl:443/path",
        "http://opendata.cbs.nl:80/path?query=value",
    ):
        assert profile.is_domain_trusted(url) is True
        assert profile.is_source_blocked(url) is True


def test_dutch_model_arguments_and_tool_schema_contain_only_cbs(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.paths import AppPaths

    profile = ConfigLoader().load("dutch_government")
    assert [catalog.adapter for catalog in profile.catalogs] == ["cbs"]
    assert [catalog.adapter for catalog in profile.agent_usable_catalogs] == ["cbs"]

    local_tools = Mock(return_value=[])
    monkeypatch.setattr(dataset_agent, "tools_for_profile", local_tools)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", Mock(return_value="offline-key"))
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Done")],
        usage=usage,
    )
    client = Mock()
    client.messages.create.return_value = response
    anthropic_factory = Mock(return_value=client)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", anthropic_factory)

    dataset_agent.run_profile(
        user_prompt="Find Dutch population data",
        profile=profile,
        budget=dataset_agent.Budget.from_profile(profile.budget),
        loading_session=LoadingPolicySession(download_enabled=False),
        session_cost=dataset_agent.SessionCost(),
        cli_overrides={},
        paths=AppPaths(output_dir=tmp_path),
    )

    call = client.messages.create.call_args.kwargs
    system = call["system"].lower()
    definitions = call["tools"]
    rendered = json.dumps(definitions).lower()

    for definition in definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source is not None:
            assert source["enum"] == ["cbs"]
    assert "table_id" in rendered
    for inactive_claim in ("ckan", "tavily", "web search", "download_url", "csv"):
        assert inactive_claim not in rendered
        assert inactive_claim not in system
    assert "cbs" in system
    client.messages.create.assert_called_once()
    anthropic_factory.assert_called_once_with(api_key="offline-key")
    local_tools.assert_called_once_with(profile)


@pytest.mark.parametrize("profile_id", ["us_government", "eu_open_data", "global"])
def test_disabled_profile_is_rejected_by_tool_and_execution_boundaries(profile_id, tmp_path):
    from dataset_prober.config_loader import ConfigLoader, ProfileUnavailableError
    from dataset_prober.dataset_agent import build_tool_definitions, run_profile
    from dataset_prober.tools import tools_for_profile

    profile = ConfigLoader().load(profile_id)

    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        tools_for_profile(profile)
    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        build_tool_definitions(profile)
    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        profile.system_prompt_context()
    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        run_profile(
            user_prompt="must not run",
            profile=profile,
            budget=None,
            loading_session=None,
            session_cost=None,
            cli_overrides={},
            paths=tmp_path,
        )


def install_cli_tripwires(monkeypatch, dataset_agent):
    blocked = Mock(side_effect=AssertionError("model or tool boundary was constructed"))
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", blocked)
    monkeypatch.setattr(dataset_agent, "tools_for_profile", blocked)
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    return blocked


def test_list_profiles_shows_all_statuses_and_reasons_without_model_or_tools(monkeypatch):
    from dataset_prober import dataset_agent

    blocked = install_cli_tripwires(monkeypatch, dataset_agent)
    monkeypatch.setattr(sys, "argv", ["dataset-prober", "--list-profiles"])

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "dutch_government" in rendered
    assert "manual_only" in rendered
    assert DUTCH_REASON in rendered
    assert "us_government" in rendered
    assert "disabled" in rendered
    assert US_REASON in rendered
    assert EU_REASON in rendered
    assert GLOBAL_REASON in rendered
    blocked.assert_not_called()


def test_automatic_cli_with_no_enabled_profiles_stops_before_prompt_model_or_tools(monkeypatch):
    from dataset_prober import dataset_agent

    blocked = install_cli_tripwires(monkeypatch, dataset_agent)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(
        dataset_agent.console,
        "input",
        Mock(side_effect=AssertionError("automatic mode prompted despite no enabled profiles")),
    )

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    assert "no profile is enabled" in capture.get().lower()
    blocked.assert_not_called()


def test_interpreter_validation_error_stops_cli_before_tools_or_agent(
    monkeypatch,
    test_profile,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.automatically_selectable_profile_ids.return_value = [test_profile.profile_id]
    loader.load.return_value = test_profile
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    interpreter = Mock()
    interpreter.interpret.side_effect = ProfileInterpretationError(
        "invalid response from https://user:secret@example.com/path?token=hidden#fragment"
    )
    interpreter_factory = Mock(return_value=interpreter)
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory)

    downstream = Mock(side_effect=AssertionError("interpreter failure reached agent tools"))
    monkeypatch.setattr(dataset_agent, "tools_for_profile", downstream)
    monkeypatch.setattr(dataset_agent, "run_profile", downstream)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", downstream)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find population data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "profile interpretation failed" in rendered.lower()
    assert "secret" not in rendered
    assert "hidden" not in rendered
    assert "fragment" not in rendered
    interpreter_factory.assert_called_once_with([test_profile])
    interpreter.interpret.assert_called_once_with("Find population data")
    downstream.assert_not_called()


def test_interpreter_constructor_error_stops_cli_before_tools_or_agent(
    monkeypatch,
    test_profile,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.automatically_selectable_profile_ids.return_value = [test_profile.profile_id]
    loader.load.return_value = test_profile
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    interpreter_factory = Mock(
        side_effect=ProfileInterpretationError(
            "invalid descriptor from https://user:secret@example.com/path?token=hidden#fragment"
        )
    )
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory)

    tool_factory = Mock(side_effect=AssertionError("constructor failure reached tools"))
    profile_runner = Mock(side_effect=AssertionError("constructor failure reached agent run"))
    agent_client = Mock(side_effect=AssertionError("constructor failure reached agent client"))
    monkeypatch.setattr(dataset_agent, "tools_for_profile", tool_factory)
    monkeypatch.setattr(dataset_agent, "run_profile", profile_runner)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", agent_client)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "profile interpretation failed" in rendered.lower()
    assert "traceback" not in rendered.lower()
    assert "secret" not in rendered
    assert "hidden" not in rendered
    assert "fragment" not in rendered
    interpreter_factory.assert_called_once_with([test_profile])
    tool_factory.assert_not_called()
    profile_runner.assert_not_called()
    agent_client.assert_not_called()


def test_forced_disabled_cli_stops_before_prompt_model_or_tools(monkeypatch):
    from dataset_prober import dataset_agent

    blocked = install_cli_tripwires(monkeypatch, dataset_agent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dataset-prober", "--profile", "us_government"],
    )
    monkeypatch.setattr(
        dataset_agent.console,
        "input",
        Mock(side_effect=AssertionError("disabled profile reached user prompt")),
    )

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    assert "disabled" in capture.get().lower()
    assert US_REASON in capture.get()
    blocked.assert_not_called()


def test_explicit_dutch_profile_is_identified_as_manual_only(monkeypatch):
    from dataset_prober import dataset_agent

    blocked = install_cli_tripwires(monkeypatch, dataset_agent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dataset-prober", "--profile", "dutch_government"],
    )
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value=""))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "manual_only" in rendered
    assert DUTCH_REASON in rendered
    assert "No prompt provided" in rendered
    blocked.assert_not_called()
