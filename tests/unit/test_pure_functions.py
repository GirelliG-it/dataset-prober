"""
tests/unit/test_pure_functions.py

Tests for pure functions with no external dependencies.
These run in milliseconds and never touch the network or filesystem.
"""

import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ─── _safe_url tests ─────────────────────────────────────────────────────────

class TestSafeUrl:
    """Tests for SQL injection prevention in prober._safe_url."""

    def test_clean_url_unchanged(self):
        from prober import _safe_url
        url = "https://example.com/data.csv"
        assert _safe_url(url) == url

    def test_single_quote_stripped(self):
        from prober import _safe_url
        url = "https://example.com/data's.csv"
        assert "'" not in _safe_url(url)

    def test_multiple_quotes_stripped(self):
        from prober import _safe_url
        url = "https://example.com/it's-a-trap's.csv"
        result = _safe_url(url)
        assert "'" not in result

    def test_sql_injection_attempt(self):
        from prober import _safe_url
        url = "https://example.com/data.csv'); DROP TABLE results; --"
        result = _safe_url(url)
        assert "'" not in result
        assert "DROP" in result  # content preserved, only quotes stripped

    def test_empty_url(self):
        from prober import _safe_url
        assert _safe_url("") == ""

    def test_url_with_query_params(self):
        from prober import _safe_url
        url = "https://example.com/data.csv?format=csv&year=2024"
        assert _safe_url(url) == url


# ─── DatasetResult method tests ──────────────────────────────────────────────

class TestDatasetResultFreshness:
    """Tests for DatasetResult.freshness_days() and passes_freshness()."""

    def test_freshness_days_recent(self):
        from tools.base import DatasetResult
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified=recent, download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.freshness_days() == pytest.approx(10, abs=1)

    def test_freshness_days_old(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified="2020-01-01", download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.freshness_days() > 365 * 4

    def test_freshness_days_unknown(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified=None, download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.freshness_days() is None

    def test_freshness_days_unparseable(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified="not-a-date", download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.freshness_days() is None

    def test_passes_freshness_recent(self):
        from tools.base import DatasetResult
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified=recent, download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.passes_freshness(365) is True

    def test_passes_freshness_too_old(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified="2020-01-01", download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.passes_freshness(365) is False

    def test_passes_freshness_unknown_returns_none(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified=None, download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.passes_freshness(365) is None

    def test_freshness_iso_datetime_format(self):
        """CBS returns ISO datetime with time component — must parse correctly."""
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", modified="2026-06-12T02:00:00", download_url=None, format=None,
            frequency=None, license=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.freshness_days() is not None
        assert d.freshness_days() < 30  # very recent


class TestDatasetResultLicenseGrade:
    """Tests for DatasetResult.license_grade()."""

    def test_cc0_is_grade_a(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="CC0", modified=None, download_url=None, format=None,
            frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "A"

    def test_public_domain_is_grade_a(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="Public Domain", modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "A"

    def test_cc_by_is_grade_b(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="CC-BY", modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "B"

    def test_cc_by_sa_is_grade_b_minus(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="CC-BY-SA", modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "B-"

    def test_cc_by_nc_is_grade_c(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="CC-BY-NC", modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "C"

    def test_unknown_license_is_question_mark(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license=None, modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "?"

    def test_other_license_is_question_mark(self):
        from tools.base import DatasetResult
        d = DatasetResult(
            id="x", title="x", description="", source="cbs", source_name="CBS",
            url="", license="proprietary", modified=None, download_url=None,
            format=None, frequency=None, license_url=None, row_count=None,
            columns=None, sample=None, language=None, tags=[]
        )
        assert d.license_grade() == "?"


# ─── PricingConfig tests ─────────────────────────────────────────────────────

class TestPricingConfig:
    """Tests for cost calculation accuracy."""

    def test_zero_tokens_zero_cost(self, test_profile):
        cost = test_profile.pricing.calculate_cost(0, 0, 0)
        assert cost == 0.0

    def test_input_tokens_only(self, test_profile):
        cost = test_profile.pricing.calculate_cost(1_000_000, 0, 0)
        assert cost == pytest.approx(3.00)

    def test_output_tokens_only(self, test_profile):
        cost = test_profile.pricing.calculate_cost(0, 1_000_000, 0)
        assert cost == pytest.approx(15.00)

    def test_cache_read_tokens(self, test_profile):
        cost = test_profile.pricing.calculate_cost(0, 0, 1_000_000)
        assert cost == pytest.approx(0.30)

    def test_combined_cost(self, test_profile):
        cost = test_profile.pricing.calculate_cost(10_000, 2_000, 5_000)
        expected = (10_000/1_000_000 * 3.00) + (2_000/1_000_000 * 15.00) + (5_000/1_000_000 * 0.30)
        assert cost == pytest.approx(expected)

    def test_format_cost_small(self, test_profile):
        result = test_profile.pricing.format_cost(0.00005)
        assert "$" in result

    def test_format_cost_normal(self, test_profile):
        result = test_profile.pricing.format_cost(0.05)
        assert result == "$0.0500"


# ─── BudgetConfig override tests ─────────────────────────────────────────────

class TestBudgetOverride:
    """Tests for CLI flag override logic."""

    def test_override_single_field(self, test_profile):
        original = test_profile.budget.max_searches
        overridden = test_profile.budget.override(max_searches=99)
        assert overridden.max_searches == 99
        assert overridden.max_crawls == test_profile.budget.max_crawls

    def test_none_values_ignored(self, test_profile):
        original_timeout = test_profile.budget.timeout_minutes
        overridden = test_profile.budget.override(timeout_minutes=None, max_searches=10)
        assert overridden.timeout_minutes == original_timeout
        assert overridden.max_searches == 10

    def test_override_does_not_mutate_original(self, test_profile):
        original_max = test_profile.budget.max_searches
        test_profile.budget.override(max_searches=999)
        assert test_profile.budget.max_searches == original_max

    def test_override_all_fields(self, test_profile):
        overridden = test_profile.budget.override(
            max_searches=10,
            max_crawls=15,
            max_probes=20,
            max_tokens=8192,
            timeout_minutes=30
        )
        assert overridden.max_searches == 10
        assert overridden.max_crawls == 15
        assert overridden.max_probes == 20
        assert overridden.max_tokens == 8192
        assert overridden.timeout_minutes == 30
