"""
tests/unit/test_expand_directories.py

Tests for run.expand_directories — the interactive pre-pass that turns an
autoindex directory URL into concrete file sources.

Two things are pinned here:
  1. Non-directory sources pass through untouched and cost zero requests.
  2. Each directory level is fetched EXACTLY ONCE. The listing is handed to
     _walk_directory rather than refetched, and invalidated on descend.
     A regression here shows up as call_count == 3 instead of 2.

Nothing touches the network: `response_is_html`, `resolve_directory` and
`console.input` are all patched in run's namespace (that is where the names
are looked up, since run.py imports them by name).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import run  # noqa: E402

TOP_URL = "https://data.rivm.nl/data/luchtmeetnet/"
SUB_URL = "https://data.rivm.nl/data/luchtmeetnet/Actueel-jaar/"
CSV_URL = "https://data.rivm.nl/data/luchtmeetnet/Actueel-jaar/2026_05_NO2.csv"

LISTING_TOP = {
    "url": TOP_URL,
    "subdirs": [{"name": "Actueel-jaar/", "url": SUB_URL}],
    "files": [],
}

LISTING_SUB = {
    "url": SUB_URL,
    "subdirs": [],
    "files": [
        {
            "name": "2026_05_NO2.csv",
            "url": CSV_URL,
            "ext": "csv",
            "modified": "2026-06-01",
        }
    ],
}


class TestExpandDirectories:
    def test_plain_file_passes_through_without_a_listing_fetch(self):
        """A direct CSV URL is not a directory — it must not be walked."""
        sources = [{"name": "cbs", "url": "https://example.com/data.csv"}]
        resolve = MagicMock()

        with (
            patch.object(run, "response_is_html", return_value=False),
            patch.object(run, "resolve_directory", resolve),
        ):
            out = run.expand_directories(sources)

        assert out == sources
        assert resolve.call_count == 0

    def test_each_directory_level_is_fetched_exactly_once(self):
        """
        Walk top -> descend into Actueel-jaar/ -> pick the first file.

        Two levels are visited, so resolve_directory must be called twice.
        Before the listing was passed forward this was three: the top level
        was fetched once to test it, then again inside _walk_directory.
        """
        resolve = MagicMock(side_effect=[LISTING_TOP, LISTING_SUB])

        with (
            patch.object(run, "response_is_html", return_value=True),
            patch.object(run, "resolve_directory", resolve),
            patch.object(run.console, "input", side_effect=["d1", "f1"]),
        ):
            out = run.expand_directories([{"name": "rivm", "url": TOP_URL}])

        assert out == [{"name": "2026_05_NO2.csv", "url": CSV_URL}]
        assert resolve.call_count == 2
        assert [c.args[0] for c in resolve.call_args_list] == [TOP_URL, SUB_URL]

    def test_descend_refetches_rather_than_reusing_the_parent(self):
        """
        The guard against the obvious bug in passing the listing forward:
        if `listing` is not cleared on descend, the parent folder is shown
        again and the walk never advances.
        """
        resolve = MagicMock(side_effect=[LISTING_TOP, LISTING_SUB])

        with (
            patch.object(run, "response_is_html", return_value=True),
            patch.object(run, "resolve_directory", resolve),
            patch.object(run.console, "input", side_effect=["d1", "f1"]),
        ):
            run.expand_directories([{"name": "rivm", "url": TOP_URL}])

        # The second call must be for the SUBFOLDER, not the parent again.
        assert resolve.call_args_list[1].args[0] == SUB_URL

    def test_skip_returns_nothing_for_that_source(self):
        """Backing out of a directory drops it — it is not probed blindly."""
        resolve = MagicMock(side_effect=[LISTING_TOP])

        with (
            patch.object(run, "response_is_html", return_value=True),
            patch.object(run, "resolve_directory", resolve),
            patch.object(run.console, "input", side_effect=["skip"]),
        ):
            out = run.expand_directories([{"name": "rivm", "url": TOP_URL}])

        assert out == []
        assert resolve.call_count == 1
