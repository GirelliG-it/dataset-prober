"""Characterize the valid loading paths before tightening Task 1 policy."""

import sys
from unittest.mock import Mock

import pytest

from dataset_prober.loading_policy import AuthorizedLoad, LoadingPolicySession
from dataset_prober.paths import AppPaths
from dataset_prober.prober import ProbeResult
from dataset_prober.tools.base import DatasetResult


def _dataset(*, source: str, format: str, dataset_id: str) -> DatasetResult:
    download_url = (
        f"https://opendata.cbs.nl/ODataApi/odata/{dataset_id}/TypedDataSet"
        if source == "cbs"
        else f"https://example.com/{dataset_id}.csv"
    )
    return DatasetResult(
        id=dataset_id,
        title=f"Dataset {dataset_id}",
        description="",
        source=source,
        source_name=source.upper(),
        url=download_url or f"https://example.com/{dataset_id}",
        download_url=download_url,
        format=format,
        modified=None,
        frequency=None,
        license=None,
        license_url=None,
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        sample=[[1], [2]],
        language=None,
        tags=[],
        status="probed",
    )


class RecordingTool:
    """A loader double that proves the authorized path reaches the tool."""

    def __init__(self, source_name: str):
        self.source_name = source_name
        self.adapter_identity = source_name
        self.download_calls = []

    def download(self, dataset, destination, authorization):
        assert isinstance(authorization, AuthorizedLoad)
        self.download_calls.append((dataset, destination, authorization))
        dataset.status = "downloaded"
        return dataset


@pytest.mark.parametrize(
    ("source", "format"),
    [("ckan", "CSV"), ("cbs", "OData")],
)
def test_policy_session_allows_probed_csv_and_cbs_loads(monkeypatch, tmp_path, source, format):
    """A registered probe plus exact consent reaches each supported adapter."""
    from dataset_prober import dataset_agent

    dataset = _dataset(source=source, format=format, dataset_id=f"{source}-1")
    tool = RecordingTool(dataset.source_name)
    paths = AppPaths(output_dir=tmp_path)
    loading_session = LoadingPolicySession(download_enabled=True)
    loading_session.register_dataset_result(dataset, tool.adapter_identity)

    # Task 1 adds an exact affirmative prompt. The characterization remains
    # focused on preserving the supported, authorized outcome.
    monkeypatch.setattr(dataset_agent.console, "input", lambda _prompt: "yes")

    result = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input={
            "source": source,
            "dataset_id": dataset.id,
            "title": dataset.title,
            "download_url": dataset.download_url,
        },
        tool_map={source: tool},
        budget=Mock(),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=[dataset],
        session_cost=Mock(),
        paths=paths,
    )

    assert result["status"] == "downloaded"
    assert len(tool.download_calls) == 1
    assert tool.download_calls[0][:2] == (dataset, str(paths.duckdb_path))


def test_manual_download_flag_and_exact_selection_load_only_that_result(monkeypatch, tmp_path):
    """The supported manual route needs --download, selection, and consent."""
    from dataset_prober import run

    first = ProbeResult(
        name="First",
        url="https://example.com/first.csv",
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    second = ProbeResult(
        name="Second",
        url="https://example.com/second.csv",
        status="ok",
        row_count=3,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    paths = AppPaths(output_dir=tmp_path)
    load_calls = []

    monkeypatch.setattr(sys, "argv", ["dataset-prober-probe", "--download"])
    monkeypatch.setattr(run.AppPaths, "resolve", lambda: paths)
    monkeypatch.setattr(
        run, "get_sources_interactive", lambda: [{"name": first.name, "url": first.url}]
    )
    monkeypatch.setattr(run, "expand_directories", lambda sources: sources)
    monkeypatch.setattr(run, "probe_all", lambda _sources: [first, second])
    monkeypatch.setattr(run, "display_results", lambda _results: None)
    monkeypatch.setattr(run, "save_results", lambda _results, _path: None)
    monkeypatch.setattr(
        run,
        "download_to_duckdb",
        lambda result, db_path, authorization: load_calls.append((result, db_path, authorization)),
    )

    def answer(prompt: str) -> str:
        if "Download which datasets?" in prompt:
            return "1"
        if "Approve this exact" in prompt:
            return "yes"
        raise AssertionError(f"Unexpected prompt: {prompt}")

    monkeypatch.setattr(run.console, "input", answer)

    run.main()

    assert len(load_calls) == 1
    assert load_calls[0][:2] == (first, str(paths.duckdb_path))
    assert isinstance(load_calls[0][2], AuthorizedLoad)


def test_manual_multi_selection_issues_one_authorization_and_attempt_per_resource(
    monkeypatch, tmp_path
):
    from dataset_prober import run

    results = [
        ProbeResult(
            name=name,
            url=f"https://example.com/{name.lower()}.csv",
            status="ok",
            row_count=index,
            columns=[{"name": "value", "type": "INTEGER"}],
            format="CSV",
        )
        for index, name in enumerate(("First", "Second"), 1)
    ]
    paths = AppPaths(output_dir=tmp_path)
    calls = []
    answers = iter(["all", "yes", "yes"])
    monkeypatch.setattr(sys, "argv", ["dataset-prober-probe", "--download"])
    monkeypatch.setattr(run.AppPaths, "resolve", lambda: paths)
    monkeypatch.setattr(
        run, "get_sources_interactive", lambda: [{"name": "input", "url": results[0].url}]
    )
    monkeypatch.setattr(run, "expand_directories", lambda sources: sources)
    monkeypatch.setattr(run, "probe_all", lambda _sources: results)
    monkeypatch.setattr(run, "display_results", lambda _results: None)
    monkeypatch.setattr(run, "save_results", lambda _results, _path: None)
    monkeypatch.setattr(run.console, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        run,
        "download_to_duckdb",
        lambda result, destination, authorization: calls.append(
            (result, destination, authorization)
        ),
    )

    run.main()

    assert [call[0] for call in calls] == results
    assert all(call[1] == str(paths.duckdb_path) for call in calls)
    assert all(isinstance(call[2], AuthorizedLoad) for call in calls)
    assert calls[0][2] is not calls[1][2]
