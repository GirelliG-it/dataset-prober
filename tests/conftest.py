"""
tests/conftest.py

Shared fixtures for all test layers.
- No real API calls anywhere in this file
- No real network access
- All fixtures are deterministic and fast
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─── Sample data fixtures ────────────────────────────────────────────────────


@pytest.fixture
def sample_columns():
    return [
        {"name": "id", "type": "BIGINT"},
        {"name": "name", "type": "VARCHAR"},
        {"name": "value", "type": "DOUBLE"},
        {"name": "date", "type": "DATE"},
    ]


@pytest.fixture
def sample_probe_result_ok(sample_columns):
    """A successful ProbeResult."""
    from prober import ProbeResult

    return ProbeResult(
        url="https://example.com/data.csv",
        name="test-dataset",
        status="ok",
        row_count=1000,
        columns=sample_columns,
        sample=[[1, "Alice", 42.0, "2024-01-01"]],
        error=None,
    )


@pytest.fixture
def sample_probe_result_error():
    """A failed ProbeResult."""
    from prober import ProbeResult

    return ProbeResult(
        url="https://example.com/blocked.csv",
        name="blocked-dataset",
        status="error",
        row_count=None,
        columns=[],
        sample=[],
        error="HTTP Error: connection refused",
    )


@pytest.fixture
def sample_probe_result_redirect():
    """A redirect-trap ProbeResult."""
    from prober import ProbeResult

    return ProbeResult(
        url="https://example.com/login.csv",
        name="redirect-dataset",
        status="redirect_trap",
        row_count=None,
        columns=[],
        sample=[],
        error="Expected CSV but got HTML",
    )


@pytest.fixture
def sample_dataset_result_ok(sample_columns):
    """A successfully probed DatasetResult."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from tools.base import DatasetResult

    return DatasetResult(
        id="37789ksz",
        title="Sociale zekerheid; kerncijfers",
        description="Social security key figures from CBS",
        source="cbs",
        source_name="CBS Statistics Netherlands",
        url="https://opendata.cbs.nl/statline/#/CBS/nl/dataset/37789ksz",
        download_url=None,
        format="OData",
        modified="2026-06-12",
        frequency="monthly",
        license="CC-BY",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        row_count=367,
        columns=sample_columns,
        sample=[[1, "2024-01", 1000.0, "2024-01-01"]],
        language="nl",
        tags=["social security", "benefits"],
        status="probed",
    )


@pytest.fixture
def sample_dataset_result_downloaded(sample_dataset_result_ok):
    """A downloaded DatasetResult."""
    sample_dataset_result_ok.status = "downloaded"
    return sample_dataset_result_ok


@pytest.fixture
def sample_dataset_result_failed():
    """A failed DatasetResult."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from tools.base import DatasetResult

    return DatasetResult(
        id="ssa-blocked",
        title="SSA Disability Data",
        description="SSA disability statistics",
        source="tavily",
        source_name="Web Search (Tavily)",
        url="https://www.ssa.gov/data/SSA-SA-FYWL.csv",
        download_url="https://www.ssa.gov/data/SSA-SA-FYWL.csv",
        format="CSV",
        modified="2025-11-01",
        frequency="annual",
        license="US-PD",
        license_url=None,
        row_count=None,
        columns=[],
        sample=[],
        language="en",
        tags=["social security", "disability"],
        status="failed",
        error="HTTP Error: HTTP 0 Internal Server Error",
    )


# ─── Profile fixtures ────────────────────────────────────────────────────────

MINIMAL_PROFILE_YAML = """
name: Test Profile
description: Minimal profile for testing
language: en
cost_warning: false
scope:
  regions:
    - Test Region
  instruction: Test scope only.
budget:
  max_searches: 3
  max_crawls: 3
  max_probes: 5
  max_tokens: 1024
  timeout_minutes: 5
  sample_rows: 5
  download_timeout_seconds: 60
pricing:
  input_per_million: 3.00
  output_per_million: 15.00
  cache_read_per_million: 0.30
catalogs:
  - name: Test CBS
    type: cbs
    base_url: https://opendata.cbs.nl/ODataCatalog
    api_key_env: null
    timeout_seconds: 10
    priority: 1
trusted_domains:
  - example.com
  - test.gov
blocked_sources:
  - blocked.example.com
license_preference:
  - CC0
  - CC-BY
license_warn:
  - CC-BY-SA
license_reject:
  - CC-BY-NC
"""

GLOBAL_PROFILE_YAML = """
name: Global Discovery
description: Global test profile
language: auto
cost_warning: true
warning_message: "Test warning"
scope:
  regions:
    - Worldwide
  instruction: No geographic restriction.
budget:
  max_searches: 5
  max_crawls: 5
  max_probes: 10
  max_tokens: 2048
  timeout_minutes: 10
  sample_rows: 10
  download_timeout_seconds: 120
pricing:
  input_per_million: 3.00
  output_per_million: 15.00
  cache_read_per_million: 0.30
catalogs: []
trusted_domains: []
blocked_sources: []
license_preference:
  - CC0
license_warn:
  - CC-BY
license_reject:
  - CC-BY-NC
"""


@pytest.fixture
def profiles_dir(tmp_path):
    """Create a temporary profiles directory with test profiles."""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test_profile.yaml").write_text(MINIMAL_PROFILE_YAML)
    (profiles / "global.yaml").write_text(GLOBAL_PROFILE_YAML)
    return profiles


@pytest.fixture
def test_profile(profiles_dir):
    """Load the minimal test profile."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from config_loader import ConfigLoader

    return ConfigLoader(profiles_dir).load("test_profile")


@pytest.fixture
def global_profile(profiles_dir):
    """Load the global test profile."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from config_loader import ConfigLoader

    return ConfigLoader(profiles_dir).load("global")


# ─── Orchestrator fixtures ───────────────────────────────────────────────────


@pytest.fixture
def dutch_objective():
    """A ProfileObjective for Dutch government data."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestrator import ProfileObjective

    return ProfileObjective(
        profile_name="dutch_government",
        display_name="Dutch Government",
        what_to_find="One Dutch social security dataset",
        geographic_scope="Netherlands only",
        topic="social security",
        freshness_rule="within 12 months",
        download_requested=True,
        execution_order=1,
    )


@pytest.fixture
def us_objective():
    """A ProfileObjective for US government data."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestrator import ProfileObjective

    return ProfileObjective(
        profile_name="us_government",
        display_name="US Government",
        what_to_find="One US social security dataset",
        geographic_scope="United States only",
        topic="social security",
        freshness_rule="within 12 months",
        download_requested=True,
        execution_order=2,
    )


@pytest.fixture
def profile_result_met(dutch_objective, sample_dataset_result_downloaded):
    """A ProfileResult where the objective was fully met."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestrator import ProfileResult

    pr = ProfileResult(
        profile_name="dutch_government", display_name="Dutch Government", objective=dutch_objective
    )
    pr.datasets_found = [sample_dataset_result_downloaded]
    pr.datasets_downloaded = [sample_dataset_result_downloaded]
    pr.objective_met = True
    pr.tokens_used = 5000
    pr.cost_usd = 0.07
    pr.api_calls = 4
    return pr


@pytest.fixture
def profile_result_partial(us_objective, sample_dataset_result_failed):
    """A ProfileResult where dataset was found but not downloaded."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestrator import ProfileResult

    pr = ProfileResult(
        profile_name="us_government", display_name="US Government", objective=us_objective
    )
    pr.datasets_found = [sample_dataset_result_failed]
    pr.datasets_downloaded = []
    pr.datasets_failed = [sample_dataset_result_failed]
    pr.partial_success = True
    pr.failure_reason = "SSA.gov blocks programmatic access"
    pr.tokens_used = 8000
    pr.cost_usd = 0.12
    pr.api_calls = 6
    return pr


@pytest.fixture
def profile_result_empty(us_objective):
    """A ProfileResult where nothing was found."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestrator import ProfileResult

    pr = ProfileResult(
        profile_name="us_government", display_name="US Government", objective=us_objective
    )
    pr.datasets_found = []
    pr.datasets_downloaded = []
    pr.objective_met = False
    pr.partial_success = False
    pr.failure_reason = "No datasets found"
    pr.tokens_used = 3000
    pr.cost_usd = 0.04
    pr.api_calls = 3
    return pr


# ─── Mock HTTP response helpers ──────────────────────────────────────────────

SAMPLE_CSV = b"""id,name,value,date
1,Alice,42.0,2024-01-01
2,Bob,37.5,2024-01-02
3,Carol,91.2,2024-01-03
"""

REDIRECT_HTML = b"""<!DOCTYPE html>
<html><body><h1>Login Required</h1></body></html>
"""

MALFORMED_CSV = b"""id,name,value
1,Alice,42.0
2,Bob
3,Carol,91.2,extra_col
"""


@pytest.fixture
def sample_csv():
    return SAMPLE_CSV


@pytest.fixture
def redirect_html():
    return REDIRECT_HTML


@pytest.fixture
def malformed_csv():
    return MALFORMED_CSV


# ─── Mock Anthropic client ──────────────────────────────────────────────────


@pytest.fixture
def mock_anthropic_response():
    """A mock Anthropic API response with realistic structure."""
    mock = MagicMock()
    mock.content = [MagicMock(text="Mock analysis response", type="text")]
    mock.stop_reason = "end_turn"
    mock.usage = MagicMock(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0)
    return mock


@pytest.fixture
def mock_interpreter_response():
    """A mock Anthropic response for prompt interpretation."""
    mock = MagicMock()
    mock.content = [
        MagicMock(
            text=json.dumps(
                {
                    "profiles": [
                        {
                            "profile_name": "dutch_government",
                            "display_name": "Dutch Government",
                            "confidence": "high",
                            "reason": "Prompt mentions Dutch government",
                            "execution_order": 1,
                            "keywords_detected": ["Dutch", "government"],
                            "language_detected": "en",
                            "objective": {
                                "what_to_find": "Dutch social security dataset",
                                "geographic_scope": "Netherlands only",
                                "topic": "social security",
                                "freshness_rule": "within 12 months",
                                "download_requested": True,
                            },
                        }
                    ],
                    "is_global": False,
                    "is_multi_profile": False,
                    "interpreter_reasoning": "Clear Dutch government request",
                }
            ),
            type="text",
        )
    ]
    mock.usage = MagicMock(input_tokens=500, output_tokens=300, cache_read_input_tokens=0)
    return mock
