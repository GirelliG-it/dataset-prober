"""
tests/unit/test_pure_functions.py

Tests for pure functions with no external dependencies.
These run in milliseconds and never touch the network or filesystem.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ─── SQL injection tests ─────────────────────────────────────────────────────


class TestSqlInjection:
    """
    URLs reach DuckDB from untrusted places: CKAN catalogues, Tavily web search
    results, user input. These assert the property that matters — a payload does
    NOT execute — instead of asserting *how* we sanitise.

    The previous tests checked `"'" not in _safe_url(url)`. A function that
    returns "" would also pass that. They could not have caught the fact that
    ckan_tool and tavily_tool never called _safe_url at all.
    """

    def test_injection_payload_cannot_drop_a_table(self, tmp_path):
        import duckdb

        from tools.base import load_csv_to_table

        con = duckdb.connect(str(tmp_path / "victim.duckdb"))
        con.execute("CREATE TABLE canary AS SELECT 1 AS x")

        payload = "http://x/a.csv'); DROP TABLE canary; --"
        with pytest.raises(Exception):
            load_csv_to_table(con, "loot", payload)

        survived = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'canary'"
        ).fetchone()[0]
        con.close()
        assert survived == 1, "injection payload executed and dropped the canary"

    def test_legitimate_url_still_loads(self, tmp_path):
        import duckdb

        from tools.base import load_csv_to_table

        csv = tmp_path / "data.csv"
        csv.write_text("a,b\n1,x\n2,y\n")

        con = duckdb.connect()
        rows = load_csv_to_table(con, "ok", str(csv))
        con.close()
        assert rows == 2

    def test_quote_in_path_is_not_silently_mangled(self, tmp_path):
        """
        A legitimate path containing a quote used to be corrupted by stripping
        the character — the old blocklist rewrote the user's URL behind their
        back. Binding passes it through intact.
        """
        import duckdb

        from tools.base import load_csv_to_table

        csv = tmp_path / "it's.csv"
        csv.write_text("a\n1\n")

        con = duckdb.connect()
        rows = load_csv_to_table(con, "quoted", str(csv))
        con.close()
        assert rows == 1


# ─── Table naming tests ──────────────────────────────────────────────────────


class TestSafeTableName:
    """Table identity must rest on the source ID, never the human title."""

    def test_distinct_ids_never_collide_despite_identical_titles(self):
        from tools.base import safe_table_name

        a = safe_table_name("83765NED", "Bevolking per gemeente, 2024")
        b = safe_table_name("85496NED", "Bevolking per gemeente (2024)")
        assert a != b, "two different datasets mapped to one table"

    def test_same_id_is_stable(self):
        from tools.base import safe_table_name

        assert safe_table_name("83765NED", "x") == safe_table_name("83765NED", "x")

    def test_empty_id_raises_rather_than_producing_junk(self):
        from tools.base import safe_table_name

        with pytest.raises(ValueError):
            safe_table_name("!!!", "title")

    def test_leading_digit_is_prefixed(self):
        from tools.base import safe_table_name

        assert not safe_table_name("2024data", "x")[0].isdigit()

    def test_respects_duckdb_identifier_length(self):
        from tools.base import safe_table_name

        assert len(safe_table_name("x" * 200, "y" * 200)) <= 63


# ─── DatasetResult method tests ──────────────────────────────────────────────


class TestDatasetResultFreshness:
    """Tests for DatasetResult.freshness_days() and passes_freshness()."""

    def test_freshness_days_recent(self):
        from datetime import datetime, timedelta

        from tools.base import DatasetResult

        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified=recent,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.freshness_days() == pytest.approx(10, abs=1)

    def test_freshness_days_old(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified="2020-01-01",
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.freshness_days() > 365 * 4

    def test_freshness_days_unknown(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.freshness_days() is None

    def test_freshness_days_unparseable(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified="not-a-date",
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.freshness_days() is None

    def test_passes_freshness_recent(self):
        from datetime import datetime, timedelta

        from tools.base import DatasetResult

        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified=recent,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.passes_freshness(365) is True

    def test_passes_freshness_too_old(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified="2020-01-01",
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.passes_freshness(365) is False

    def test_passes_freshness_unknown_returns_none(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.passes_freshness(365) is None

    def test_freshness_iso_datetime_format(self):
        """CBS returns ISO datetime with time component — must parse correctly."""
        from datetime import datetime, timedelta

        from tools.base import DatasetResult

        # Relative, not hardcoded: a literal date silently rots into a failure
        # once it drifts past the threshold being asserted.
        five_days_ago = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            modified=five_days_ago,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.freshness_days() == 5


class TestDatasetResultLicenseGrade:
    """Tests for DatasetResult.license_grade()."""

    def test_cc0_is_grade_a(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="CC0",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "A"

    def test_public_domain_is_grade_a(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="Public Domain",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "A"

    def test_cc_by_is_grade_b(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="CC-BY",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "B"

    def test_cc_by_sa_is_grade_b_minus(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="CC-BY-SA",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "B-"

    def test_cc_by_nc_is_grade_c(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="CC-BY-NC",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "C"

    def test_unknown_license_is_question_mark(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license=None,
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )
        assert d.license_grade() == "?"

    def test_other_license_is_question_mark(self):
        from tools.base import DatasetResult

        d = DatasetResult(
            id="x",
            title="x",
            description="",
            source="cbs",
            source_name="CBS",
            url="",
            license="proprietary",
            modified=None,
            download_url=None,
            format=None,
            frequency=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
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
        expected = (
            (10_000 / 1_000_000 * 3.00) + (2_000 / 1_000_000 * 15.00) + (5_000 / 1_000_000 * 0.30)
        )
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
        assert test_profile.budget.max_searches == original  # <- add: override must not mutate

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
            max_searches=10, max_crawls=15, max_probes=20, max_tokens=8192, timeout_minutes=30
        )
        assert overridden.max_searches == 10
        assert overridden.max_crawls == 15
        assert overridden.max_probes == 20
        assert overridden.max_tokens == 8192
        assert overridden.timeout_minutes == 30
