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

from unittest.mock import Mock, patch

import pytest

from dataset_prober.loading_policy import AuthorizedLoad, LoadingPolicySession
from dataset_prober.paths import AppPaths
from dataset_prober.tools.base import DatasetResult
from tests.conftest import eligible_assessment_for_candidate


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
    adapter_identity = "configured fake CKAN"

    def __init__(self):
        self.download_calls = []

    def download(self, dataset, destination, authorization):
        assert isinstance(authorization, AuthorizedLoad)
        self.download_calls.append((dataset, destination, authorization))
        return FakeDownloadResult()


@pytest.fixture
def tool():
    return RecordingTool()


@pytest.fixture
def download_input():
    return {
        "source": "ckan",
        "dataset_id": "some-dataset",
        "title": "Some Dataset",
        "download_url": "https://example.com/data.csv",
    }


@pytest.fixture
def paths(tmp_path):
    return AppPaths(output_dir=tmp_path)


def call_execute_tool(tool, tool_input, *, download_enabled, paths):
    """
    Invoke execute_tool for a download_dataset call.

    `profile` and `session_cost` are not consulted by this branch, so Mock()
    is sufficient; if that changes, these tests should be updated rather than
    silently accommodating it.
    """
    from dataset_prober.dataset_agent import execute_tool

    assessment = eligible_assessment_for_candidate(
        source_key=tool_input["source"],
        adapter_identity=tool.adapter_identity,
        resource_id=tool_input["dataset_id"],
        retrieval_url=tool_input["download_url"],
    )
    inspected = DatasetResult(
        id=tool_input["dataset_id"],
        title=tool_input["title"],
        description="",
        source=tool_input["source"],
        source_name=tool.source_name,
        url=tool_input["download_url"],
        download_url=tool_input["download_url"],
        format="CSV",
        modified=None,
        frequency=None,
        license=None,
        license_url=None,
        row_count=42,
        columns=[{"name": "value", "type": "INTEGER"}],
        sample=[[42]],
        language=None,
        tags=[],
        status="probed",
        assessment=assessment,
    )
    loading_session = LoadingPolicySession(download_enabled=download_enabled)
    loading_session.register_dataset_result(inspected, tool.adapter_identity)
    with patch("dataset_prober.dataset_agent.console.input", return_value="yes"):
        return execute_tool(
            tool_name="download_dataset",
            tool_input=tool_input,
            tool_map={"ckan": tool},
            budget=Mock(),
            profile=Mock(),
            loading_session=loading_session,
            found_datasets=[inspected],
            session_cost=Mock(),
            paths=paths,
        )


class TestDownloadConsentGate:
    def test_download_blocked_without_permission(self, tool, download_input, paths):
        """The hard contract: a disabled policy session means no download happens."""
        result = call_execute_tool(tool, download_input, download_enabled=False, paths=paths)

        assert tool.download_calls == [], (
            "Consent gate failed open — tool.download() was called with allow_download=False"
        )
        assert "error" in result

    def test_blocked_result_explains_why(self, tool, download_input, paths):
        """The refusal is reported back to the model, not silently swallowed."""
        result = call_execute_tool(tool, download_input, download_enabled=False, paths=paths)

        assert "not permitted" in result["error"].lower()

    def test_download_proceeds_when_permitted(self, tool, download_input, paths):
        """
        Control test. The Boolean offer gate plus exact affirmative consent
        can reach a supported, inspected resource's loader.
        """
        result = call_execute_tool(tool, download_input, download_enabled=True, paths=paths)

        assert len(tool.download_calls) == 1
        assert result["status"] == "downloaded"

    def test_gate_precedes_tool_lookup(self, download_input, paths):
        """
        With no tools registered at all, a blocked download must still report
        the permission error — not 'tool not available'. This pins the gate's
        POSITION: first statement in the branch, before any other check.
        """
        from dataset_prober.dataset_agent import execute_tool

        loading_session = LoadingPolicySession(download_enabled=False)

        result = execute_tool(
            tool_name="download_dataset",
            tool_input=download_input,
            tool_map={},
            budget=Mock(),
            profile=Mock(),
            loading_session=loading_session,
            found_datasets=[],
            session_cost=Mock(),
            paths=paths,
        )

        assert "not permitted" in result["error"].lower()

    def test_application_does_not_create_destination_before_writer_activation(
        self, tool, download_input, tmp_path
    ):
        """Only the authorized persistent wrapper may create the destination directory."""
        fresh = tmp_path / "not-yet-created"
        paths = AppPaths(output_dir=fresh)
        assert not fresh.exists()

        call_execute_tool(tool, download_input, download_enabled=True, paths=paths)

        assert not fresh.exists()
