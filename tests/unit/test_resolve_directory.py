"""
tests/unit/test_resolve_directory.py

Tests for crawler.resolve_directory — directory descent on Apache/nginx
autoindex listings (RIVM, INSPIRE folder portals). Fixtures are built from
the REAL RIVM Luchtmeetnet markup, so these prove behavior against the
genuine page shape, not an idealized one.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from dataset_prober import crawler  # noqa: E402

FIXTURES = Path(__file__).parent.parent / "fixtures"


class _FakeResp:
    def __init__(self, text, url):
        self.text = text
        self.url = url


def _serve(fixture_name):
    html = (FIXTURES / fixture_name).read_text()
    return lambda url, timeout=10: _FakeResp(html, url)


class TestResolveDirectory:
    def test_top_level_lists_subdirs(self):
        with patch.object(
            crawler, "safe_http_get", side_effect=_serve("rivm_luchtmeetnet_index.html")
        ):
            r = crawler.resolve_directory("https://data.rivm.nl/data/luchtmeetnet/")
        names = [d["name"] for d in r["subdirs"]]
        assert "Actueel-jaar/" in names
        assert len(r["subdirs"]) == 6

    def test_sort_and_parent_links_filtered(self):
        """Apache emits ?C=N;O=D sort links and a Parent Directory link;
        neither may appear as a candidate subdirectory."""
        with patch.object(
            crawler, "safe_http_get", side_effect=_serve("rivm_luchtmeetnet_index.html")
        ):
            r = crawler.resolve_directory("https://data.rivm.nl/data/luchtmeetnet/")
        assert not any("?C=" in d["url"] for d in r["subdirs"])
        assert not any("Parent" in d["name"] for d in r["subdirs"])

    def test_pdf_is_not_a_dataset_file(self):
        """readme.pdf must not be offered as a loadable dataset."""
        with patch.object(
            crawler, "safe_http_get", side_effect=_serve("rivm_luchtmeetnet_index.html")
        ):
            r = crawler.resolve_directory("https://data.rivm.nl/data/luchtmeetnet/")
        assert r["files"] == []

    def test_descend_finds_files_with_dates(self):
        with patch.object(crawler, "safe_http_get", side_effect=_serve("rivm_actueel_index.html")):
            r = crawler.resolve_directory("https://data.rivm.nl/data/luchtmeetnet/Actueel-jaar/")
        names = [f["name"] for f in r["files"]]
        assert "2026_NO2.csv" in names
        assert "stations.json" in names
        assert r["subdirs"] == []
        assert all(f["modified"] for f in r["files"])

    def test_file_extensions_recognized(self):
        with patch.object(crawler, "safe_http_get", side_effect=_serve("rivm_actueel_index.html")):
            r = crawler.resolve_directory("https://data.rivm.nl/data/luchtmeetnet/Actueel-jaar/")
        exts = {f["ext"] for f in r["files"]}
        assert "csv" in exts
        assert "json" in exts

    def test_fetch_failure_propagates(self):
        def boom(url, timeout=10):
            raise ConnectionError("unreachable")

        with patch.object(crawler, "safe_http_get", side_effect=boom):
            with pytest.raises(ConnectionError):
                crawler.resolve_directory("https://data.rivm.nl/nope/")
