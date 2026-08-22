"""
tests/unit/test_pure_functions.py

Tests for pure functions with no external dependencies.
These run in milliseconds and never touch the network or filesystem.
"""

from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.conftest import eligible_assessment_for_candidate


@contextmanager
def _guarded_local_download(url, **_kwargs):
    from dataset_prober.tools.guards import FetchedResource, UnsafeURLError

    path = Path(url)
    if not path.is_file():
        raise UnsafeURLError("test transport rejected non-local source")
    yield FetchedResource(
        source_url=str(url),
        final_url=str(url),
        path=str(path),
        headers={"Content-Type": "text/csv"},
    )


def _authorized_manual_load(monkeypatch, url, destination, *, name="dataset"):
    """Exercise the real manual policy and persistent writer without networking."""
    from dataset_prober import prober
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.prober import ProbeResult

    result = ProbeResult(
        url=str(url),
        name=name,
        status="ok",
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
        assessment=eligible_assessment_for_candidate(
            source_key="manual",
            adapter_identity="Manual URL",
            resource_id=str(url),
            retrieval_url=str(url),
        ),
    )
    session = LoadingPolicySession(download_enabled=True)
    session.register_probe_result(result)
    authorization = session.request_authorization(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id=result.url,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    monkeypatch.setattr(prober, "safe_download", _guarded_local_download)
    return prober.download_to_duckdb(result, str(destination), authorization)


# ─── SQL injection tests ─────────────────────────────────────────────────────


class TestSqlInjection:
    """
    Guarded local filenames still originate from untrusted source metadata and
    user input. These assert the property that matters — a payload does NOT
    execute — instead of asserting *how* we sanitise.

    The previous tests checked `"'" not in _safe_url(url)`. A function that
    returns "" would also pass that. They could not have caught the fact that
    ckan_tool and tavily_tool never called _safe_url at all.
    """

    def test_injection_payload_cannot_drop_a_table(self, monkeypatch, tmp_path):
        import duckdb

        destination = tmp_path / "victim.duckdb"
        con = duckdb.connect(str(destination))
        con.execute("CREATE TABLE canary AS SELECT 1 AS x")
        con.close()

        payload = tmp_path / "a'); DROP TABLE canary; --.csv"
        payload.write_text("value\n1\n", encoding="utf-8")
        _authorized_manual_load(monkeypatch, payload, destination, name="loot")

        con = duckdb.connect(str(destination))
        survived = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'canary'"
        ).fetchone()[0]
        con.close()
        assert survived == 1, "injection payload executed and dropped the canary"

    def test_legitimate_url_still_loads(self, monkeypatch, tmp_path):
        csv = tmp_path / "data.csv"
        csv.write_text("a,b\n1,x\n2,y\n")

        result = _authorized_manual_load(monkeypatch, csv, tmp_path / "data.duckdb")
        assert result.row_count == 2

    def test_quote_in_path_is_not_silently_mangled(self, monkeypatch, tmp_path):
        """
        A legitimate path containing a quote used to be corrupted by stripping
        the character — the old blocklist rewrote the user's URL behind their
        back. Binding passes it through intact.
        """
        csv = tmp_path / "it's.csv"
        csv.write_text("a\n1\n")

        result = _authorized_manual_load(monkeypatch, csv, tmp_path / "quoted.duckdb")
        assert result.row_count == 1


# ─── DatasetResult method tests ──────────────────────────────────────────────


class TestDatasetResultFreshness:
    """Tests for DatasetResult.freshness_days() and passes_freshness()."""

    def test_freshness_days_recent(self):
        from datetime import datetime, timedelta

        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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

        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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

        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
        from dataset_prober.tools.base import DatasetResult

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
            max_searches=10,
            max_results=15,
            max_probes=20,
            max_model_calls=30,
            max_tokens=8192,
            max_total_tokens=90000,
            timeout_minutes=30,
        )
        assert overridden.max_searches == 10
        assert overridden.max_results == 15
        assert overridden.max_probes == 20
        assert overridden.max_model_calls == 30
        assert overridden.max_tokens == 8192
        assert overridden.max_total_tokens == 90000
        assert overridden.timeout_minutes == 30


class TestRedirectTrapDetection:
    """HTML pages must not be silently stored as data."""

    def _html(self, tmp_path):
        p = tmp_path / "landing.csv"
        p.write_text("<!DOCTYPE html>\n<html><body>hi</body></html>\n")
        return str(p)

    def test_html_page_is_rejected(self, monkeypatch, tmp_path):
        import duckdb

        destination = tmp_path / "html.duckdb"
        _authorized_manual_load(
            monkeypatch,
            self._html(tmp_path),
            destination,
            name="trap",
        )
        connection = duckdb.connect(str(destination))
        assert connection.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0] == 0
        connection.close()

    def test_real_csv_still_loads(self, monkeypatch, tmp_path):
        csv = tmp_path / "d.csv"
        csv.write_text("station,no2\nDenHaag,28\nUtrecht,31\n")
        result = _authorized_manual_load(monkeypatch, csv, tmp_path / "real.duckdb")
        assert result.row_count == 2


class TestCsvScanExprSharedByProbeAndLoad:
    """
    The dialect decision lives in ONE place. probe_url and load_csv_to_table
    both call csv_scan_expr, so a file that probes must also load.

    Files are written to tmp_path rather than read from tests/fixtures so the
    pathologies under test are visible right here and cannot drift.
    """

    CLEAN = "id,name,value\n1,Alice,42.0\n2,Bob,37.5\n"
    # ';' delim, '#' preamble, blank line, ragged rows, repeated mid-file
    # header, CRLF — the RIVM/INSPIRE shape that defeats the auto-sniffer.
    EUROPEAN = (
        "# RIVM Luchtmeetnet\r\n"
        "# station data\r\n"
        "\r\n"
        "datum;station;waarde;extra\r\n"
        "2026-05-01;NL10404;12,5\r\n"
        "2026-05-02;NL10404;9,8;x;y;z\r\n"
        "datum;station;waarde;extra\r\n"
        "2026-05-03;NL10404;7,1\r\n"
    )

    def _write(self, tmp_path, name, text):
        p = tmp_path / name
        p.write_bytes(text.encode())
        return str(p)

    # ── _is_degenerate: the two tells ────────────────────────────────────

    def test_healthy_header_is_not_degenerate(self):
        from dataset_prober.tools.base import _is_degenerate

        assert _is_degenerate(["datum", "station", "waarde"]) is False

    def test_generic_column_names_are_degenerate(self):
        """DuckDB names columns column0..N when it finds no header at all."""
        from dataset_prober.tools.base import _is_degenerate

        assert _is_degenerate(["column0", "column1", "column2"]) is True

    def test_single_column_holding_a_delimited_line_is_degenerate(self):
        """The common RIVM failure: the whole line lands in one field."""
        from dataset_prober.tools.base import _is_degenerate

        assert _is_degenerate(["datum;station;waarde"]) is True

    def test_single_column_named_after_a_comment_line_is_degenerate(self):
        """A '#' preamble line mistaken for the header."""
        from dataset_prober.tools.base import _is_degenerate

        assert _is_degenerate(["# RIVM Luchtmeetnet"]) is True

    def test_no_columns_is_degenerate(self):
        from dataset_prober.tools.base import _is_degenerate

        assert _is_degenerate([]) is True

    # ── csv_scan_expr: the decision ──────────────────────────────────────

    def test_clean_csv_keeps_auto_typing(self, tmp_path):
        """Asymmetry by design: comma files keep their inferred types."""
        import duckdb

        from dataset_prober.tools.base import csv_scan_expr

        path = self._write(tmp_path, "clean.csv", self.CLEAN)
        con = duckdb.connect()
        assert csv_scan_expr(con, path) == "read_csv_auto(?)"

    def test_european_csv_falls_back(self, tmp_path):
        import duckdb

        from dataset_prober.tools.base import EUROPEAN_CSV_ARGS, csv_scan_expr

        path = self._write(tmp_path, "euro.csv", self.EUROPEAN)
        con = duckdb.connect()
        expr = csv_scan_expr(con, path)
        assert expr != "read_csv_auto(?)"
        assert EUROPEAN_CSV_ARGS in expr

    def test_expression_never_interpolates_the_url(self, tmp_path):
        """
        The '?' placeholder must survive. If the URL were ever baked into the
        expression, the SQL-injection defence would be gone.
        """
        import duckdb

        from dataset_prober.tools.base import csv_scan_expr

        path = self._write(tmp_path, "euro.csv", self.EUROPEAN)
        con = duckdb.connect()
        expr = csv_scan_expr(con, path)
        assert "?" in expr
        assert path not in expr

    # ── the shared-decision property ─────────────────────────────────────

    def test_probe_and_load_agree_on_the_european_file(self, monkeypatch, tmp_path):
        """
        The regression this whole change exists to prevent: probe_url used to
        have no fallback, so a European file errored at probe and never
        reached download. Both paths must now succeed on the same file.
        """
        from dataset_prober import prober

        path = self._write(tmp_path, "euro.csv", self.EUROPEAN)

        monkeypatch.setattr(prober, "safe_download", _guarded_local_download)
        result = prober.probe_url("euro", path)

        loaded = _authorized_manual_load(monkeypatch, path, tmp_path / "euro.duckdb", name="euro")

        assert result.status == "ok"
        assert result.row_count == loaded.row_count

    def test_probe_and_load_agree_on_the_clean_file(self, monkeypatch, tmp_path):
        from dataset_prober import prober

        path = self._write(tmp_path, "clean.csv", self.CLEAN)

        monkeypatch.setattr(prober, "safe_download", _guarded_local_download)
        result = prober.probe_url("clean", path)

        loaded = _authorized_manual_load(monkeypatch, path, tmp_path / "clean.duckdb", name="clean")

        assert result.status == "ok"
        assert result.row_count == loaded.row_count == 2
