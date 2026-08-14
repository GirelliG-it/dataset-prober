"""
tests/integration_light/test_config_loader.py

Tests for ConfigLoader using real YAML parsing but temporary directories.
No network calls — tests only the file loading and validation logic.
"""

import pytest
import yaml


def _write_ckan_profile(tmp_path, *, dialect="ckan_action", extra_catalog_fields=None):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    catalog = {
        "catalog_id": "synthetic_ckan",
        "adapter": "ckan",
        "name": "Synthetic CKAN",
        "base_url": "https://api.public.example/api/3",
        "api_key_env": None,
        "timeout_seconds": 10,
        "priority": 1,
        "required": True,
        "ckan_dialect": dialect,
        "landing_base_url": "https://catalog.public.example",
    }
    catalog.update(extra_catalog_fields or {})
    raw = {
        "name": "Synthetic CKAN Profile",
        "description": "Offline CKAN route contract",
        "language": "en",
        "cost_warning": False,
        "status": "enabled",
        "reason": None,
        "scope": {"regions": ["Synthetic"], "instruction": "Synthetic only."},
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
        "catalogs": [catalog],
        "trusted_hosts": [],
        "blocked_hosts": [],
        "license_preference": ["CC0"],
        "license_warn": [],
        "license_reject": [],
    }
    (profiles / "synthetic_ckan.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    return profiles


class TestConfigLoaderConfiguredProfileIds:
    """Tests for ConfigLoader.configured_profile_ids()."""

    def test_lists_available_profiles(self, profiles_dir):
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        profiles = loader.configured_profile_ids()
        assert "test_profile" in profiles
        assert "global" in profiles

    def test_empty_directory_returns_empty_list(self, tmp_path):
        from dataset_prober.config_loader import ConfigLoader

        empty = tmp_path / "profiles"
        empty.mkdir()
        loader = ConfigLoader(empty)
        assert loader.configured_profile_ids() == []

    def test_nonexistent_directory_returns_empty_list(self, tmp_path):
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(tmp_path / "nonexistent")
        assert loader.configured_profile_ids() == []

    def test_ignores_underscore_prefixed_files(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "_internal.yaml").write_text("name: internal")
        (profiles / "real_profile.yaml").write_text("""
name: Real Profile
description: test
language: en
cost_warning: false
status: disabled
reason: Test profile is not runnable.
scope:
  regions: [test]
  instruction: test
budget:
  max_searches: 1
  max_crawls: 1
  max_probes: 1
  max_tokens: 512
  timeout_minutes: 1
  sample_rows: 5
  download_timeout_seconds: 30
pricing:
  input_per_million: 3.0
  output_per_million: 15.0
  cache_read_per_million: 0.3
catalogs: []
trusted_hosts: []
blocked_hosts: []
license_preference: [CC0]
license_warn: []
license_reject: []
""")
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(profiles)
        profiles_list = loader.configured_profile_ids()
        assert "real_profile" in profiles_list
        assert "_internal" not in profiles_list


class TestConfigLoaderLoad:
    """Tests for ConfigLoader.load()."""

    def test_loads_test_profile(self, profiles_dir):
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        profile = loader.load("test_profile")
        assert profile.name == "Test Profile"
        assert profile.profile_id == "test_profile"
        assert profile.contract.profile_id == "test_profile"

    def test_missing_profile_raises_file_not_found(self, profiles_dir):
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        with pytest.raises(FileNotFoundError):
            loader.load("nonexistent_profile")

    def test_error_message_lists_available_profiles(self, profiles_dir):
        from dataset_prober.config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        with pytest.raises(FileNotFoundError, match="test_profile"):
            loader.load("nonexistent_profile")


class TestProfileAttributes:
    """Tests for correctly parsed profile attributes."""

    def test_budget_values_loaded(self, test_profile):
        assert test_profile.budget.max_searches == 3
        assert test_profile.budget.max_crawls == 3
        assert test_profile.budget.max_probes == 5
        assert test_profile.budget.timeout_minutes == 5

    def test_pricing_loaded(self, test_profile):
        assert test_profile.pricing.input_per_million == 3.00
        assert test_profile.pricing.output_per_million == 15.00
        assert test_profile.pricing.cache_read_per_million == 0.30

    def test_license_config_loaded(self, test_profile):
        assert "CC0" in test_profile.license.preference
        assert "CC-BY" in test_profile.license.preference
        assert "CC-BY-SA" in test_profile.license.warn
        assert "CC-BY-NC" in test_profile.license.reject

    def test_scope_loaded(self, test_profile):
        assert "Test Region" in test_profile.scope_regions
        assert "Test scope only." in test_profile.scope_instruction

    def test_trusted_domains_loaded(self, test_profile):
        assert "example.com" in test_profile.trusted_domains
        assert "test.gov" in test_profile.trusted_domains

    def test_blocked_sources_loaded(self, test_profile):
        assert "blocked.example.com" in test_profile.blocked_sources

    def test_cost_warning_false_by_default(self, test_profile):
        assert test_profile.cost_warning is False

    def test_global_profile_cost_warning_true(self, global_profile):
        assert global_profile.cost_warning is True

    def test_global_profile_no_catalogs(self, global_profile):
        assert len(global_profile.catalogs) == 0

    def test_catalog_loaded(self, test_profile):
        assert len(test_profile.catalogs) == 1
        catalog = test_profile.catalogs[0]
        assert catalog.catalog_id == "test_cbs"
        assert catalog.adapter == "cbs"
        assert catalog.name == "Test CBS"
        assert catalog.type == "cbs"
        assert catalog.priority == 1
        assert catalog.required is True


class TestProfileMethods:
    """Tests for Profile helper methods."""

    def test_is_domain_trusted(self, test_profile):
        assert test_profile.is_domain_trusted("https://example.com/data.csv") is True
        assert test_profile.is_domain_trusted("https://untrusted.net/data.csv") is False

    def test_is_source_blocked(self, test_profile):
        assert test_profile.is_source_blocked("https://blocked.example.com/data") is True
        assert test_profile.is_source_blocked("https://allowed.example.com/data") is False

    def test_empty_trusted_hosts_trust_nothing(self, global_profile):
        assert global_profile.is_domain_trusted("https://any-domain-at-all.com") is False

    def test_has_catalog_type(self, test_profile):
        assert test_profile.has_catalog_type("cbs") is True
        assert test_profile.has_catalog_type("ckan") is False
        assert test_profile.has_catalog_type("tavily") is False

    def test_system_prompt_context_contains_scope(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "SCOPE GUIDANCE" in ctx
        assert "Test scope only." in ctx

    def test_system_prompt_context_scope_appears_first(self, test_profile):
        """Scope restriction must appear before catalog sources."""
        ctx = test_profile.system_prompt_context()
        scope_pos = ctx.find("SCOPE GUIDANCE")
        catalog_pos = ctx.find("AVAILABLE CATALOG SOURCES")
        assert scope_pos < catalog_pos

    def test_system_prompt_context_contains_blocked_sources(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "blocked.example.com" in ctx

    def test_system_prompt_context_contains_license_rules(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "LICENSE RULES" in ctx


@pytest.mark.parametrize("dialect", ["ckan_action", "eu_hub"])
def test_ckan_route_fields_remain_typed_without_contract_runtime_divergence(tmp_path, dialect):
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.profile_contract import CKANDialect

    profile = ConfigLoader(_write_ckan_profile(tmp_path, dialect=dialect)).load("synthetic_ckan")
    contract_catalog = profile.contract.catalogs[0]
    runtime_catalog = profile.catalogs[0]

    assert contract_catalog.ckan_dialect is CKANDialect(dialect)
    assert runtime_catalog.ckan_dialect is contract_catalog.ckan_dialect
    assert runtime_catalog.landing_base_url == contract_catalog.landing_base_url
    assert runtime_catalog.landing_base_url == "https://catalog.public.example"


@pytest.mark.parametrize("field", ["search_path", "show_path", "landing_template"])
def test_ckan_arbitrary_route_and_template_fields_remain_rejected(tmp_path, field):
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.profile_contract import ProfileContractError

    loader = ConfigLoader(
        _write_ckan_profile(
            tmp_path,
            extra_catalog_fields={field: "arbitrary/value"},
        )
    )

    with pytest.raises(ProfileContractError) as error:
        loader.load("synthetic_ckan")

    assert ("unknown_field", f"catalogs[0].{field}") in [
        (issue.code, issue.path) for issue in error.value.issues
    ]
