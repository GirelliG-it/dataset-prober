import sys
from unittest.mock import Mock

import pytest

from dataset_prober import crawler


def test_crawler_help_exits_before_runtime_work(monkeypatch, capsys):
    resolve_paths = Mock(side_effect=AssertionError("help resolved runtime paths"))
    prompt = Mock(side_effect=AssertionError("help prompted for input"))
    crawl = Mock(side_effect=AssertionError("help started crawling"))

    monkeypatch.setattr(crawler.AppPaths, "resolve", resolve_paths)
    monkeypatch.setattr(crawler.console, "input", prompt)
    monkeypatch.setattr(crawler, "crawl", crawl)
    monkeypatch.setattr(sys, "argv", ["dataset-prober-crawl", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        crawler.main()

    help_text = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "usage: dataset-prober-crawl" in help_text
    resolve_paths.assert_not_called()
    prompt.assert_not_called()
    crawl.assert_not_called()
