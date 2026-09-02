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

from dataset_prober.profile_contract import (
    CKANDialect,
    CKANSearchMode,
    ProfileContractError,
    ProfileStatus,
)

DUTCH_REASON = (
    "The CBS StatLine OData v3 and data.overheid.nl CKAN v3 routes are available only by "
    "explicit selection during v0.1 stabilization."
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
            "max_results": 10,
            "max_probes": 1,
            "max_model_calls": 24,
            "max_tokens": 512,
            "max_total_tokens": 50000,
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


def resolve_with_fake_tools(profile):
    """Resolve a validated test profile through deterministic local fake adapters."""
    from dataset_prober.profile_resolution import resolve_profile

    registry = {}
    for catalog in profile.agent_usable_catalogs:
        tool = Mock()
        tool.source_type = catalog.adapter
        tool.is_available.return_value = True
        registry[catalog.adapter] = Mock(return_value=tool)
    return resolve_profile(profile, registry=registry)


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
    assert [(catalog.catalog_id, catalog.adapter) for catalog in dutch_catalogs] == [
        ("cbs_statline", "cbs"),
        ("data_overheid", "ckan"),
    ]
    assert [catalog.priority for catalog in dutch_catalogs] == [1, 2]
    assert all(catalog.required is True for catalog in dutch_catalogs)
    assert all(catalog.api_key_env is None for catalog in dutch_catalogs)
    data_overheid = dutch_catalogs[1]
    assert data_overheid.base_url == "https://data.overheid.nl/data/api/3"
    assert data_overheid.landing_base_url == "https://data.overheid.nl"
    assert data_overheid.ckan_dialect is CKANDialect.CKAN_ACTION
    assert data_overheid.ckan_search_mode is CKANSearchMode.LOCAL_RESOURCE_METADATA
    assert profiles["dutch_government"].trusted_domains == (
        "opendata.cbs.nl",
        "data.overheid.nl",
    )
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
            expected_fields = {
                "catalog_id",
                "adapter",
                "name",
                "base_url",
                "api_key_env",
                "timeout_seconds",
                "priority",
                "required",
            }
            if catalog["adapter"] == "ckan":
                expected_fields.update({"ckan_dialect", "ckan_search_mode", "landing_base_url"})
            assert expected_fields == set(catalog)


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


def test_dutch_resolution_and_model_surfaces_use_exactly_cbs_and_ckan(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.paths import AppPaths
    from dataset_prober.profile_resolution import resolve_profile
    from dataset_prober.tools.cbs_tool import CBSTool
    from dataset_prober.tools.ckan_tool import CKANTool

    profile = ConfigLoader().load("dutch_government")
    assert [catalog.adapter for catalog in profile.catalogs] == ["cbs", "ckan"]
    assert [catalog.adapter for catalog in profile.agent_usable_catalogs] == ["cbs", "ckan"]

    cbs_factory = Mock(side_effect=CBSTool)
    ckan_factory = Mock(side_effect=CKANTool)

    resolved = resolve_profile(
        profile,
        registry={"cbs": cbs_factory, "ckan": ckan_factory},
    )
    assert isinstance(resolved.tools[0], CBSTool)
    assert isinstance(resolved.tools[1], CKANTool)
    assert tuple(resolved.execution_map) == resolved.source_keys == ("cbs", "ckan")
    cbs_factory.assert_called_once()
    ckan_factory.assert_called_once()
    assert cbs_factory.call_args.args[0]["catalog_id"] == "cbs_statline"
    assert ckan_factory.call_args.args[0]["catalog_id"] == "data_overheid"
    assert (
        ckan_factory.call_args.args[0]["ckan_search_mode"] is CKANSearchMode.LOCAL_RESOURCE_METADATA
    )
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
        resolved_profile=resolved,
        budget=dataset_agent.Budget.from_profile(profile.budget),
        loading_session=LoadingPolicySession(download_enabled=False),
        session_cost=dataset_agent.SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    call = client.messages.create.call_args.kwargs
    system = call["system"].lower()
    definitions = call["tools"]
    rendered = json.dumps(definitions).lower()

    for definition in definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source is not None:
            assert source["enum"] == ["cbs", "ckan"]
    assert "table_id" in rendered
    for inactive_claim in ("tavily", "web search", "opendatasoft"):
        assert inactive_claim not in rendered
        assert inactive_claim not in system
    assert "cbs" in system
    assert "data.overheid.nl" in system
    assert "dutch national data portal" in system
    client.messages.create.assert_called_once()
    anthropic_factory.assert_called_once_with(api_key="offline-key", max_retries=0)


def test_unsupported_opendatasoft_portals_configuration_fails_closed(tmp_path):
    from dataset_prober.config_loader import ConfigLoader

    raw = valid_raw_profile()
    raw["opendatasoft_portals"] = ["https://unsupported.example/catalog"]

    with pytest.raises(ProfileContractError) as error:
        ConfigLoader(write_profile(tmp_path, raw)).load("synthetic_profile")

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("unknown_field", "opendatasoft_portals")
    ]


@pytest.mark.parametrize("profile_id", ["us_government", "eu_open_data", "global"])
def test_disabled_profile_is_rejected_by_tool_and_execution_boundaries(profile_id):
    from dataset_prober.config_loader import ConfigLoader, ProfileUnavailableError
    from dataset_prober.profile_resolution import resolve_profile
    from dataset_prober.tools import TOOL_REGISTRY

    profile = ConfigLoader().load(profile_id)

    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        resolve_profile(profile, registry=TOOL_REGISTRY)
    with pytest.raises(ProfileUnavailableError, match=re.escape(profile.reason)):
        profile.system_prompt_context(profile.catalogs)


def install_cli_tripwires(monkeypatch, dataset_agent):
    blocked = Mock(side_effect=AssertionError("model or tool boundary was constructed"))
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", blocked)
    monkeypatch.setattr(dataset_agent, "resolve_profile", blocked)
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
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


def test_all_enabled_profiles_resolve_before_interpreter_construction(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    profiles_dir = write_profile(tmp_path, valid_raw_profile(), "first_profile")
    (profiles_dir / "second_profile.yaml").write_text(
        yaml.safe_dump(valid_raw_profile(), sort_keys=False),
        encoding="utf-8",
    )
    loader = ConfigLoader(profiles_dir)
    original_load = loader.load
    loaded_descriptors = []

    def load_once(profile_id):
        profile = original_load(profile_id)
        loaded_descriptors.append(profile)
        return profile

    loader.load = Mock(side_effect=load_once)
    loader.automatically_selectable_profile_ids = Mock(
        side_effect=AssertionError("automatic mode reloaded profile IDs")
    )
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    events = []
    resolved_descriptors = []
    original_resolver = dataset_agent.resolve_profile

    def tracked_resolver(profile, *, registry):
        events.append(f"resolve:{profile.profile_id}")
        resolved_descriptors.append(profile)
        return original_resolver(profile, registry=registry)

    monkeypatch.setattr(dataset_agent, "resolve_profile", tracked_resolver)

    def interpreter_factory(profiles):
        events.append("interpreter")
        assert [profile.profile_id for profile in profiles] == [
            "first_profile",
            "second_profile",
        ]
        assert all(
            interpreted is resolved
            for interpreted, resolved in zip(profiles, resolved_descriptors, strict=True)
        )
        raise ProfileInterpretationError("synthetic stop after constructor ordering check")

    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory)
    blocked = Mock(side_effect=AssertionError("ordering test reached profile agent"))
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    dataset_agent.main()

    assert events == ["resolve:first_profile", "resolve:second_profile", "interpreter"]
    assert [profile.profile_id for profile in loaded_descriptors] == [
        "first_profile",
        "second_profile",
    ]
    assert all(
        resolved is loaded
        for resolved, loaded in zip(resolved_descriptors, loaded_descriptors, strict=True)
    )
    assert loader.load.call_count == 2
    loader.automatically_selectable_profile_ids.assert_not_called()
    blocked.assert_not_called()


def test_automatic_resolution_excludes_failed_candidate_before_interpreter(
    monkeypatch,
    tmp_path,
):
    from dataset_prober import dataset_agent
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.profile_resolution import ProfileResolutionError, ResolutionIssue
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    profiles_dir = write_profile(tmp_path, valid_raw_profile(), "failed_profile")
    (profiles_dir / "successful_profile.yaml").write_text(
        yaml.safe_dump(valid_raw_profile(), sort_keys=False),
        encoding="utf-8",
    )
    source_loader = ConfigLoader(profiles_dir)
    failed_profile = source_loader.load("failed_profile")
    successful_profile = source_loader.load("successful_profile")
    successful_resolved = resolve_with_fake_tools(successful_profile)

    loader = Mock()
    loader.configured_profile_ids.return_value = ["failed_profile", "successful_profile"]
    loader.profile_descriptors.return_value = [failed_profile, successful_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    failure = ProfileResolutionError(
        (
            ResolutionIssue(
                code="adapter_unavailable",
                catalog_id=failed_profile.catalogs[0].catalog_id,
                adapter="cbs",
                required=True,
                blocking=True,
                message="Adapter is not locally available.",
            ),
        )
    )

    def resolve_candidate(profile, *, registry):
        assert registry is dataset_agent.TOOL_REGISTRY
        if profile is failed_profile:
            raise failure
        assert profile is successful_profile
        return successful_resolved

    resolver = Mock(side_effect=resolve_candidate)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    def interpreter_factory(profiles):
        assert len(profiles) == 1
        assert profiles[0] is successful_profile
        raise ProfileInterpretationError("stop after successful-candidate identity check")

    interpreter_factory_mock = Mock(side_effect=interpreter_factory)
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory_mock)
    blocked = Mock(side_effect=AssertionError("mixed preflight reached profile agent"))
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "Excluding profile 'failed_profile'" in rendered
    assert "adapter_unavailable" in rendered
    assert resolver.call_args_list[0].args[0] is failed_profile
    assert resolver.call_args_list[1].args[0] is successful_profile
    interpreter_factory_mock.assert_called_once()
    loader.profile_descriptors.assert_called_once_with()
    loader.load.assert_not_called()
    blocked.assert_not_called()


def test_interpreter_validation_error_stops_cli_after_resolution_before_profile_agent(
    monkeypatch,
    test_profile,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    resolved = resolve_with_fake_tools(test_profile)
    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    interpreter = Mock()
    interpreter.interpret.side_effect = ProfileInterpretationError(
        "invalid response from https://user:secret@example.com/path?token=hidden#fragment"
    )
    interpreter_factory = Mock(return_value=interpreter)
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory)

    downstream = Mock(side_effect=AssertionError("interpreter failure reached profile agent"))
    monkeypatch.setattr(dataset_agent, "run_profile", downstream)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", downstream)
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
    resolver.assert_called_once_with(test_profile, registry=dataset_agent.TOOL_REGISTRY)
    downstream.assert_not_called()


def test_interpreter_constructor_error_stops_cli_after_resolution_before_profile_agent(
    monkeypatch,
    test_profile,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import ProfileInterpretationError

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    resolved = resolve_with_fake_tools(test_profile)
    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    interpreter_factory = Mock(
        side_effect=ProfileInterpretationError(
            "invalid descriptor from https://user:secret@example.com/path?token=hidden#fragment"
        )
    )
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", interpreter_factory)

    profile_runner = Mock(side_effect=AssertionError("constructor failure reached agent run"))
    api_key = Mock(side_effect=AssertionError("constructor failure reached agent API key"))
    agent_client = Mock(side_effect=AssertionError("constructor failure reached agent client"))
    monkeypatch.setattr(dataset_agent, "run_profile", profile_runner)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", api_key)
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
    resolver.assert_called_once_with(test_profile, registry=dataset_agent.TOOL_REGISTRY)
    profile_runner.assert_not_called()
    api_key.assert_not_called()
    agent_client.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "source_type", "available", "issue_code"),
    [
        (
            ["dataset-prober", "--profile", "test_profile"],
            "cbs",
            False,
            "adapter_unavailable",
        ),
        (["dataset-prober"], "cbs", False, "no_executable_sources"),
        (["dataset-prober"], "ckan", True, "source_mismatch"),
    ],
)
def test_fatal_resolution_stops_both_anthropic_boundaries(
    monkeypatch,
    test_profile,
    argv,
    source_type,
    available,
    issue_code,
):
    from dataset_prober import dataset_agent

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    loader.load.return_value = test_profile
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    tool = Mock()
    tool.source_type = source_type
    tool.is_available.return_value = available
    factory = Mock(return_value=tool)
    monkeypatch.setattr(dataset_agent, "TOOL_REGISTRY", {"cbs": factory})
    resolver = Mock(wraps=dataset_agent.resolve_profile)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    blocked = Mock(side_effect=AssertionError("fatal resolution reached a model boundary"))
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", blocked)
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    assert issue_code in capture.get()
    resolver.assert_called_once_with(test_profile, registry=dataset_agent.TOOL_REGISTRY)
    factory.assert_called_once()
    assert tool.is_available.call_count == (0 if issue_code == "source_mismatch" else 1)
    blocked.assert_not_called()


def test_successful_automatic_execution_resolves_once_and_reuses_cached_object(
    monkeypatch,
    test_profile,
    tmp_path,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    events = []
    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    resolved = resolve_with_fake_tools(test_profile)

    def resolve_once(profile, *, registry):
        assert profile is test_profile
        assert registry is dataset_agent.TOOL_REGISTRY
        events.append("resolution")
        return resolved

    resolver = Mock(side_effect=resolve_once)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    interpretation = InterpretationResult(
        profiles=[
            ProfileSelection(
                profile_name=test_profile.profile_id,
                display_name=test_profile.name,
                confidence="high",
                reason="Synthetic selection",
                execution_order=1,
                keywords_detected=["data"],
                language_detected="en",
                what_to_find="Synthetic data",
                geographic_scope="Test Region",
                topic="data",
                freshness_rule="none",
                download_requested=False,
            )
        ],
        is_global=False,
        is_multi_profile=False,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
    )
    interpreter = Mock()

    def interpret(_prompt):
        events.append("interpreter_model")
        return interpretation

    interpreter.interpret.side_effect = interpret
    interpreter.present_and_confirm.side_effect = lambda *_args: (
        events.append("confirmation") or True
    )

    def interpreter_factory(profiles):
        assert profiles == [test_profile]
        events.append("interpreter_construction")
        return interpreter

    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(side_effect=interpreter_factory))

    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Done")],
        usage=usage,
    )
    client = Mock()

    def agent_model(**_kwargs):
        events.append("agent_model")
        return response

    client.messages.create.side_effect = agent_model

    def agent_factory(*, api_key, max_retries):
        assert api_key == "offline-key"
        assert max_retries == 0
        events.append("agent_construction")
        return client

    monkeypatch.setattr(
        dataset_agent,
        "get_anthropic_api_key",
        Mock(side_effect=lambda: events.append("agent_api_key") or "offline-key"),
    )
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", Mock(side_effect=agent_factory))

    original_run_profile = dataset_agent.run_profile

    def run_cached(**kwargs):
        assert kwargs["resolved_profile"] is resolved
        events.append("profile_run")
        return original_run_profile(**kwargs)

    monkeypatch.setattr(dataset_agent, "run_profile", run_cached)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    dataset_agent.main()

    assert events == [
        "resolution",
        "interpreter_construction",
        "interpreter_model",
        "confirmation",
        "profile_run",
        "agent_api_key",
        "agent_construction",
        "agent_model",
    ]
    resolver.assert_called_once()
    loader.profile_descriptors.assert_called_once_with()
    loader.load.assert_not_called()
    model_call = client.messages.create.call_args.kwargs
    source_enums = [
        definition["input_schema"]["properties"]["source"]["enum"]
        for definition in model_call["tools"]
        if "source" in definition["input_schema"]["properties"]
    ]
    assert source_enums and all(enum == list(resolved.source_keys) for enum in source_enums)
    assert tuple(resolved.execution_map) == resolved.source_keys
    assert test_profile.catalogs[0].name in model_call["system"]
    assert test_profile.catalogs[0].base_url in model_call["system"]


def test_optional_capability_warnings_are_printed_only_for_selected_profiles(
    monkeypatch,
    tmp_path,
):
    from dataset_prober import dataset_agent
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.orchestrator import ProfileResult
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    first_raw = valid_raw_profile()
    first_raw["catalogs"].append(
        {
            "catalog_id": "first_tavily",
            "adapter": "tavily",
            "name": "First excluded provider",
            "base_url": "https://api.tavily.com",
            "api_key_env": None,
            "timeout_seconds": 10,
            "priority": 2,
            "required": False,
        }
    )
    second_raw = copy.deepcopy(first_raw)
    second_raw["catalogs"][1]["catalog_id"] = "second_tavily"
    second_raw["catalogs"][1]["name"] = "Second excluded provider"

    profiles_dir = write_profile(tmp_path, first_raw, "first_profile")
    (profiles_dir / "second_profile.yaml").write_text(
        yaml.safe_dump(second_raw, sort_keys=False),
        encoding="utf-8",
    )
    source_loader = ConfigLoader(profiles_dir)
    first_profile = source_loader.load("first_profile")
    second_profile = source_loader.load("second_profile")
    first_resolved = resolve_with_fake_tools(first_profile)
    second_resolved = resolve_with_fake_tools(second_profile)
    assert first_resolved.issues[0].catalog_id == "first_tavily"
    assert second_resolved.issues[0].catalog_id == "second_tavily"

    loader = Mock()
    loader.configured_profile_ids.return_value = ["first_profile", "second_profile"]
    loader.profile_descriptors.return_value = [first_profile, second_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))

    resolved_by_identity = {
        id(first_profile): first_resolved,
        id(second_profile): second_resolved,
    }

    def resolve_candidate(profile, *, registry):
        assert registry is dataset_agent.TOOL_REGISTRY
        return resolved_by_identity[id(profile)]

    monkeypatch.setattr(dataset_agent, "resolve_profile", Mock(side_effect=resolve_candidate))

    interpretation = InterpretationResult(
        profiles=[
            ProfileSelection(
                profile_name="first_profile",
                display_name=first_profile.name,
                confidence="high",
                reason="Synthetic selection",
                execution_order=1,
                keywords_detected=["data"],
                language_detected="en",
            )
        ],
        is_global=False,
        is_multi_profile=False,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
    )
    interpreter = Mock()
    interpreter.interpret.return_value = interpretation
    interpreter.present_and_confirm.return_value = True
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(return_value=interpreter))

    profile_runner = Mock(
        return_value=ProfileResult(
            profile_name="first_profile",
            display_name=first_profile.name,
            objective=None,
        )
    )
    monkeypatch.setattr(dataset_agent, "run_profile", profile_runner)
    monkeypatch.setattr(dataset_agent.AppPaths, "resolve", Mock(return_value=tmp_path))
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    rendered = capture.get()
    assert "policy_excluded (first_tavily)" in rendered
    assert "second_tavily" not in rendered
    assert profile_runner.call_count == 1
    assert profile_runner.call_args.kwargs["resolved_profile"] is first_resolved


def test_automatic_cancellation_resolves_but_never_starts_profile_agent(
    monkeypatch,
    test_profile,
):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    resolved = resolve_with_fake_tools(test_profile)
    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(dataset_agent, "resolve_profile", resolver)

    interpretation = InterpretationResult(
        profiles=[
            ProfileSelection(
                profile_name=test_profile.profile_id,
                display_name=test_profile.name,
                confidence="high",
                reason="Synthetic selection",
                execution_order=1,
                keywords_detected=["data"],
                language_detected="en",
            )
        ],
        is_global=False,
        is_multi_profile=False,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
    )
    interpreter = Mock()
    interpreter.interpret.return_value = interpretation
    interpreter.present_and_confirm.return_value = False
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(return_value=interpreter))

    blocked = Mock(side_effect=AssertionError("cancellation reached profile-agent boundary"))
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    dataset_agent.main()

    resolver.assert_called_once_with(test_profile, registry=dataset_agent.TOOL_REGISTRY)
    blocked.assert_not_called()


def test_interpreted_unresolved_profile_is_never_substituted(monkeypatch, test_profile):
    from dataset_prober import dataset_agent
    from dataset_prober.prompt_interpreter import InterpretationResult, ProfileSelection

    loader = Mock()
    loader.configured_profile_ids.return_value = [test_profile.profile_id]
    loader.profile_descriptors.return_value = [test_profile]
    monkeypatch.setattr(dataset_agent, "ConfigLoader", Mock(return_value=loader))
    resolved = resolve_with_fake_tools(test_profile)
    monkeypatch.setattr(dataset_agent, "resolve_profile", Mock(return_value=resolved))

    interpretation = InterpretationResult(
        profiles=[
            ProfileSelection(
                profile_name="unresolved_profile",
                display_name="Unresolved",
                confidence="high",
                reason="Synthetic invalid selection",
                execution_order=1,
                keywords_detected=["data"],
                language_detected="en",
            )
        ],
        is_global=False,
        is_multi_profile=False,
        raw_prompt="Find data",
        interpreter_reasoning="Synthetic",
    )
    interpreter = Mock()
    interpreter.interpret.return_value = interpretation
    monkeypatch.setattr(dataset_agent, "PromptInterpreter", Mock(return_value=interpreter))
    blocked = Mock(side_effect=AssertionError("unresolved profile reached execution"))
    monkeypatch.setattr(dataset_agent, "run_profile", blocked)
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", blocked)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", blocked)
    monkeypatch.setattr(sys, "argv", ["dataset-prober"])
    monkeypatch.setattr(dataset_agent.console, "input", Mock(return_value="Find data"))

    with dataset_agent.console.capture() as capture:
        dataset_agent.main()

    assert "unresolved profile" in capture.get().lower()
    interpreter.present_and_confirm.assert_not_called()
    blocked.assert_not_called()


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
