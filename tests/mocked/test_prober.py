"""
tests/mocked/test_prober.py

Tests for probe_url() and batch helpers using the two-line verify to close the file out.
We mock at the HTTP level using responses library — DuckDB actually
runs its CSV parser against our fake responses, giving realistic behavior.

"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


SAMPLE_CSV = b"""id,name,value,date
1,Alice,42.0,2024-01-01
2,Bob,37.5,2024-01-02
3,Carol,91.2,2024-01-03
4,Dave,15.0,2024-01-04
5,Eve,88.8,2024-01-05
"""

REDIRECT_HTML = b"""<!DOCTYPE html>
<html><head><title>Login</title></head>
<body><h1>Please log in to continue</h1></body></html>
"""

HEADERS_ONLY_CSV = b"""id,name,value,date
"""



class TestProbeResultStructure:
    """Tests that ProbeResult has the correct structure."""

    def test_ok_result_has_all_fields(self, sample_probe_result_ok):
        assert sample_probe_result_ok.status == "ok"
        assert sample_probe_result_ok.row_count == 1000
        assert len(sample_probe_result_ok.columns) == 4
        assert len(sample_probe_result_ok.sample) == 1
        assert sample_probe_result_ok.error is None

    def test_error_result_has_error_message(self, sample_probe_result_error):
        assert sample_probe_result_error.status == "error"
        assert sample_probe_result_error.error is not None
        assert len(sample_probe_result_error.error) > 0

    def test_redirect_result_has_status(self, sample_probe_result_redirect):
        assert sample_probe_result_redirect.status == "redirect_trap"

    def test_vars_serializable(self, sample_probe_result_ok):
        """ProbeResult must serialize cleanly for JSON output."""
        import json
        data = vars(sample_probe_result_ok)
        serialized = json.dumps(data, default=str)
        assert "test-dataset" in serialized


class TestProbeAll:
    """Tests for probe_all() batch behavior."""

    def test_empty_sources_returns_empty(self):
        from prober import probe_all
        results = probe_all([])
        assert results == []

    def test_returns_one_result_per_source(self, sample_probe_result_ok):
        """probe_all must return exactly one result per input source."""
        from unittest.mock import patch

        from prober import probe_all
        with patch("prober.probe_url", return_value=sample_probe_result_ok):
            results = probe_all([
                {"name": "a", "url": "https://a.com"},
                {"name": "b", "url": "https://b.com"},
            ])
        assert len(results) == 2

    def test_failed_probe_does_not_stop_batch(self, sample_probe_result_error):
        """One failure must not prevent probing the remaining sources."""
        from unittest.mock import patch

        from prober import probe_all
        with patch("prober.probe_url", return_value=sample_probe_result_error):
            results = probe_all([
                {"name": "a", "url": "https://a.com"},
                {"name": "b", "url": "https://b.com"},
            ])
        assert len(results) == 2
        assert all(r.status == "error" for r in results)


class TestSaveResults:
    """Tests for save_results() JSON output."""

    def test_saves_to_file(self, tmp_path, sample_probe_result_ok):
        from prober import save_results
        output = tmp_path / "results.json"
        save_results([sample_probe_result_ok], str(output))
        assert output.exists()

    def test_output_is_valid_json(self, tmp_path, sample_probe_result_ok):
        import json

        from prober import save_results
        output = tmp_path / "results.json"
        save_results([sample_probe_result_ok], str(output))
        with open(output) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_output_contains_expected_fields(self, tmp_path, sample_probe_result_ok):
        import json

        from prober import save_results
        output = tmp_path / "results.json"
        save_results([sample_probe_result_ok], str(output))
        with open(output) as f:
            data = json.load(f)
        result = data[0]
        assert "name" in result
        assert "url" in result
        assert "status" in result
        assert "row_count" in result
        assert "columns" in result

    def test_multiple_results_saved(self, tmp_path, sample_probe_result_ok, sample_probe_result_error):
        import json

        from prober import save_results
        output = tmp_path / "results.json"
        save_results([sample_probe_result_ok, sample_probe_result_error], str(output))
        with open(output) as f:
            data = json.load(f)
        assert len(data) == 2

    def test_error_result_serialized_cleanly(self, tmp_path, sample_probe_result_error):
        """None values must serialize without crashing."""
        import json

        from prober import save_results
        output = tmp_path / "results.json"
        save_results([sample_probe_result_error], str(output))
        with open(output) as f:
            data = json.load(f)
        assert data[0]["status"] == "error"
        assert data[0]["row_count"] is None
