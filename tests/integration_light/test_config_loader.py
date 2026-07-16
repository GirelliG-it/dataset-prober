"""
tests/integration_light/test_config_loader.py

Tests for ConfigLoader using real YAML parsing but temporary directories.
No network calls — tests only the file loading and validation logic.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


class TestConfigLoaderListProfiles:
    """Tests for ConfigLoader.list_profiles()."""

    def test_lists_available_profiles(self, profiles_dir):
        from config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        profiles = loader.list_profiles()
        assert "test_profile" in profiles
        assert "global" in profiles

    def test_empty_directory_returns_empty_list(self, tmp_path):
        from config_loader import ConfigLoader

        empty = tmp_path / "profiles"
        empty.mkdir()
        loader = ConfigLoader(empty)
        assert loader.list_profiles() == []

    def test_nonexistent_directory_returns_empty_list(self, tmp_path):
        from config_loader import ConfigLoader

        loader = ConfigLoader(tmp_path / "nonexistent")
        assert loader.list_profiles() == []

    def test_ignores_underscore_prefixed_files(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "_internal.yaml").write_text("name: internal")
        (profiles / "real_profile.yaml").write_text("""
name: Real Profile
description: test
language: en
cost_warning: false
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
trusted_domains: []
blocked_sources: []
license_preference: [CC0]
license_warn: []
license_reject: []
""")
        from config_loader import ConfigLoader

        loader = ConfigLoader(profiles)
        profiles_list = loader.list_profiles()
        assert "real_profile" in profiles_list
        assert "_internal" not in profiles_list


class TestConfigLoaderLoad:
    """Tests for ConfigLoader.load()."""

    def test_loads_test_profile(self, profiles_dir):
        from config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        profile = loader.load("test_profile")
        assert profile.name == "Test Profile"

    def test_missing_profile_raises_file_not_found(self, profiles_dir):
        from config_loader import ConfigLoader

        loader = ConfigLoader(profiles_dir)
        with pytest.raises(FileNotFoundError):
            loader.load("nonexistent_profile")

    def test_error_message_lists_available_profiles(self, profiles_dir):
        from config_loader import ConfigLoader

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
        assert catalog.name == "Test CBS"
        assert catalog.type == "cbs"
        assert catalog.priority == 1


class TestProfileMethods:
    """Tests for Profile helper methods."""

    def test_is_domain_trusted(self, test_profile):
        assert test_profile.is_domain_trusted("https://example.com/data.csv") is True
        assert test_profile.is_domain_trusted("https://untrusted.net/data.csv") is False

    def test_is_source_blocked(self, test_profile):
        assert test_profile.is_source_blocked("https://blocked.example.com/data") is True
        assert test_profile.is_source_blocked("https://allowed.example.com/data") is False

    def test_global_profile_trusts_all_domains(self, global_profile):
        """Global profile has no trusted_domains list — should trust everything."""
        assert global_profile.is_domain_trusted("https://any-domain-at-all.com") is True

    def test_has_catalog_type(self, test_profile):
        assert test_profile.has_catalog_type("cbs") is True
        assert test_profile.has_catalog_type("ckan") is False
        assert test_profile.has_catalog_type("tavily") is False

    def test_system_prompt_context_contains_scope(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "SCOPE RESTRICTION" in ctx
        assert "Test scope only." in ctx

    def test_system_prompt_context_scope_appears_first(self, test_profile):
        """Scope restriction must appear before catalog sources."""
        ctx = test_profile.system_prompt_context()
        scope_pos = ctx.find("SCOPE RESTRICTION")
        catalog_pos = ctx.find("AVAILABLE CATALOG SOURCES")
        assert scope_pos < catalog_pos

    def test_system_prompt_context_contains_blocked_sources(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "blocked.example.com" in ctx

    def test_system_prompt_context_contains_license_rules(self, test_profile):
        ctx = test_profile.system_prompt_context()
        assert "LICENSE RULES" in ctx
