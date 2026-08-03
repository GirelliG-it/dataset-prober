"""
tests/unit/test_consent_gate.py

Contract test: no dataset may be loaded into DuckDB unless download
permission was explicitly given.

This is a *contract* test rather than a unit test — it exists to fail loudly
if a refactor ever routes around the gate, not to check a calculation. Keep it
green through the package restructure.

Design note on why the fake tool is "capable" of downloading:
    Passing an empty tool_map would make execute_tool return
    {"error": "Tool ... not available"} — and the test would pass even with
    the consent gate deleted. The fake tool must therefore be able to succeed,
    so that removing the gate makes these tests FAIL rather than pass for the
    wrong reason. test_download_proceeds_when_permitted is the control that
    proves the setup can in fact reach a download.
"""

from unittest.mock import Mock

import pytest

from dataset_prober.paths import AppPaths


class FakeDownloadResult:
    """Stand-in for whatever tool.download() returns."""

    def __init__(self):
        self.status = "downloaded"
        self.row_count = 42
        self.error = None

    def to_dict(self):
        return {"status": self.status, "row_count": self.row_count}


class RecordingTool:
    """
    A source tool that records download attempts instead of performing them.

    Nothing here touches the network or DuckDB, so if the gate ever fails open
    the test reports it rather than writing to the real database.
    """

    source_name = "fake source"

    def __init__(self):
        self.download_calls = []

    def download(self, dataset, db_path):
        self.download_calls.append((dataset, db_path))
        return FakeDownloadResult()


@pytest.fixture
def tool():
    return RecordingTool()


@pytest.fixture
def download_input():
    return {
        "source": "fake",
        "dataset_id": "some-dataset",
        "title": "Some Dataset",
        "download_url": "https://example.com/data.csv",
    }


@pytest.fixture
def paths(tmp_path):
    return AppPaths(output_dir=tmp_path)


def call_execute_tool(tool, tool_input, *, allow_download, paths):
    """
    Invoke execute_tool for a download_dataset call.

    `profile` and `session_cost` are not consulted by this branch, so Mock()
    is sufficient; if that changes, these tests should be updated rather than
    silently accommodating it.
    """
    from dataset_prober.dataset_agent import execute_tool

    return execute_tool(
        tool_name="download_dataset",
        tool_input=tool_input,
        tool_map={"fake": tool},
        budget=Mock(),
        profile=Mock(),
        allow_download=allow_download,
        found_datasets=[],
        session_cost=Mock(),
        paths=paths,
    )


class TestDownloadConsentGate:
    def test_download_blocked_without_permission(self, tool, download_input, paths):
        """The hard contract: allow_download=False means no download happens."""
        result = call_execute_tool(tool, download_input, allow_download=False, paths=paths)

        assert tool.download_calls == [], (
            "Consent gate failed open — tool.download() was called with allow_download=False"
        )
        assert "error" in result

    def test_blocked_result_explains_why(self, tool, download_input, paths):
        """The refusal is reported back to the model, not silently swallowed."""
        result = call_execute_tool(tool, download_input, allow_download=False, paths=paths)

        assert "not permitted" in result["error"].lower()

    def test_download_proceeds_when_permitted(self, tool, download_input, paths):
        """
        Control test. Without this, the test above could pass for the wrong
        reason — this proves the fixture setup CAN reach a download, so a
        blocked download is a real signal.
        """
        result = call_execute_tool(tool, download_input, allow_download=True, paths=paths)

        assert len(tool.download_calls) == 1
        assert result["status"] == "downloaded"

    def test_gate_precedes_tool_lookup(self, download_input, paths):
        """
        With no tools registered at all, a blocked download must still report
        the permission error — not 'tool not available'. This pins the gate's
        POSITION: first statement in the branch, before any other check.
        """
        from dataset_prober.dataset_agent import execute_tool

        result = execute_tool(
            tool_name="download_dataset",
            tool_input=download_input,
            tool_map={},
            budget=Mock(),
            profile=Mock(),
            allow_download=False,
            found_datasets=[],
            session_cost=Mock(),
            paths=paths,
        )

        assert "not permitted" in result["error"].lower()
