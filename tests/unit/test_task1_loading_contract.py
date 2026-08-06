"""Task 1 contracts for exact selection, consent, and loader admission."""

import sys
from unittest.mock import Mock

import pytest

from dataset_prober.loading_policy import (
    AuthorizedLoad,
    InspectedResourceError,
    LoadingPolicySession,
)
from dataset_prober.paths import AppPaths
from dataset_prober.prober import ProbeResult
from dataset_prober.tools.base import DatasetResult


def make_dataset(
    *,
    dataset_id: str = "resource-a",
    source: str = "ckan",
    format: str | None = "CSV",
    resource_url: str | None = None,
) -> DatasetResult:
    resource_url = resource_url or (
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
        url=resource_url,
        download_url=resource_url,
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
    source_name = "Recording source"
    adapter_identity = "Configured recording source"

    def __init__(self):
        self.download_calls = []

    def download(self, dataset, destination, authorization):
        assert isinstance(authorization, AuthorizedLoad)
        self.download_calls.append((dataset, destination, authorization))
        dataset.status = "downloaded"
        return dataset


def execute_download(
    monkeypatch,
    tmp_path,
    *,
    dataset,
    tool_input=None,
    answer="yes",
    allow_download=True,
):
    from dataset_prober import dataset_agent

    tool = RecordingTool()
    paths = AppPaths(output_dir=tmp_path)
    loading_session = LoadingPolicySession(download_enabled=allow_download)
    try:
        loading_session.register_dataset_result(dataset, tool.adapter_identity)
    except InspectedResourceError:
        pass

    if isinstance(answer, BaseException):

        def respond(_prompt):
            raise answer

    else:

        def respond(_prompt):
            return answer

    monkeypatch.setattr(dataset_agent.console, "input", respond)
    tool_input = tool_input or {
        "source": dataset.source,
        "dataset_id": dataset.id,
        "title": dataset.title,
        "download_url": dataset.download_url,
    }

    result = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input=tool_input,
        tool_map={dataset.source: tool},
        budget=Mock(),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=[dataset],
        session_cost=Mock(),
        paths=paths,
    )
    return result, tool


@pytest.mark.parametrize(
    "answer",
    ["", "no", "n", "not yes", "yes, but not this one", "yess", "1"],
)
def test_only_exact_affirmative_consent_loads(monkeypatch, tmp_path, answer):
    dataset = make_dataset()

    result, tool = execute_download(
        monkeypatch, tmp_path, dataset=dataset, answer=answer, allow_download=True
    )

    assert tool.download_calls == []
    assert "error" in result


@pytest.mark.parametrize("exc", [EOFError(), KeyboardInterrupt()])
def test_eof_and_interrupted_consent_fail_closed(monkeypatch, tmp_path, exc):
    dataset = make_dataset()

    result, tool = execute_download(monkeypatch, tmp_path, dataset=dataset, answer=exc)

    assert tool.download_calls == []
    assert "error" in result


def test_approval_for_resource_a_cannot_load_resource_b(monkeypatch, tmp_path):
    dataset_a = make_dataset(dataset_id="resource-a")
    requested_b = {
        "source": dataset_a.source,
        "dataset_id": "resource-b",
        "title": "Dataset resource-b",
        "download_url": "https://example.com/resource-b.csv",
    }

    result, tool = execute_download(
        monkeypatch,
        tmp_path,
        dataset=dataset_a,
        tool_input=requested_b,
        answer="yes",
    )

    assert tool.download_calls == []
    assert "not an inspected candidate" in result["error"].lower()


def test_duplicate_inspected_identity_is_denied_before_consent(monkeypatch, tmp_path):
    """A generic source/id pair must not silently select the first of two resources."""
    from dataset_prober import dataset_agent

    first = make_dataset(dataset_id="shared-id", resource_url="https://catalog-a.example/data.csv")
    second = make_dataset(dataset_id="shared-id", resource_url="https://catalog-b.example/data.csv")
    tool = RecordingTool()
    loading_session = LoadingPolicySession(download_enabled=True)
    loading_session.register_dataset_result(first, tool.adapter_identity)
    loading_session.register_dataset_result(second, tool.adapter_identity)
    consent = Mock(side_effect=AssertionError("ambiguous identity must not ask for consent"))
    monkeypatch.setattr(dataset_agent.console, "input", consent)

    result = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input={
            "source": "ckan",
            "dataset_id": "shared-id",
            "title": "Model-selected title",
            "download_url": "https://model.example/selected.csv",
        },
        tool_map={"ckan": tool},
        budget=Mock(),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=[first, second],
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert "ambiguous" in result["error"].lower()
    consent.assert_not_called()
    assert tool.download_calls == []


def test_consent_identity_comes_from_inspected_resource(monkeypatch, tmp_path):
    """Model-supplied display fields cannot replace the inspected identity."""
    from dataset_prober import dataset_agent

    inspected = make_dataset(
        dataset_id="resource-a", resource_url="https://catalog.example/inspected.csv"
    )
    tool = RecordingTool()
    prompts = []
    loading_session = LoadingPolicySession(download_enabled=True)
    loading_session.register_dataset_result(inspected, tool.adapter_identity)

    def approve(prompt):
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr(dataset_agent.console, "input", approve)
    result = dataset_agent.execute_tool(
        tool_name="download_dataset",
        tool_input={
            "source": "ckan",
            "dataset_id": inspected.id,
            "title": "Model-controlled title",
            "download_url": "https://model.example/uninspected.csv",
        },
        tool_map={"ckan": tool},
        budget=Mock(),
        profile=Mock(),
        loading_session=loading_session,
        found_datasets=[inspected],
        session_cost=Mock(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["status"] == "downloaded"
    assert tool.download_calls[0][0] is inspected
    assert "https://catalog.example/inspected.csv" in prompts[0]
    assert "https://model.example/uninspected.csv" not in prompts[0]


@pytest.mark.parametrize("format", [None, "", "JSON", "Parquet", "PDF"])
def test_unsupported_or_unknown_agent_format_stops_before_loader(monkeypatch, tmp_path, format):
    dataset = make_dataset(format=format)

    result, tool = execute_download(monkeypatch, tmp_path, dataset=dataset, answer="yes")

    assert tool.download_calls == []
    assert "unsupported" in result["error"].lower()


@pytest.mark.parametrize(
    "prompt",
    [
        "find and download this dataset",
        "load it into DuckDB",
        "do not download anything",
        "explain how downloading might work",
    ],
)
def test_prompt_text_never_enables_agent_download_offers(prompt):
    del prompt
    assert LoadingPolicySession(download_enabled=False).download_enabled is False
    assert LoadingPolicySession(download_enabled=True).download_enabled is True


@pytest.mark.parametrize(
    "selection",
    ["", "0", "-1", "3", "1,garbage", "garbage,1", "1,,2", "all,1", "1,1"],
)
def test_invalid_or_ambiguous_selection_denies_entire_selection(selection):
    from dataset_prober.loading_policy import parse_exact_selection

    with pytest.raises(ValueError):
        parse_exact_selection(selection, candidate_count=2)


def test_exact_selection_returns_unique_zero_based_indices():
    from dataset_prober.loading_policy import parse_exact_selection

    assert parse_exact_selection("2,1", candidate_count=2) == [1, 0]
    assert parse_exact_selection("all", candidate_count=2) == [0, 1]
    assert parse_exact_selection("none", candidate_count=2) == []


@pytest.mark.parametrize("format", [None, "", "JSON", "GeoJSON", "XLS", "XLSX", "Parquet"])
def test_unsupported_manual_format_stops_before_database_open(monkeypatch, tmp_path, format):
    result = ProbeResult(
        name="Unsupported or unknown",
        url="https://example.com/resource",
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format=format,
    )
    connect = Mock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr("duckdb.connect", connect)
    session = LoadingPolicySession(download_enabled=True)

    with pytest.raises(InspectedResourceError):
        session.register_probe_result(result)

    connect.assert_not_called()


@pytest.mark.parametrize(
    "url", ["https://example.com/data.json", "https://example.com/extensionless"]
)
def test_manual_csv_label_cannot_override_url_format(monkeypatch, tmp_path, url):
    """Admission derives manual format from the resource, not a mutable label."""
    result = ProbeResult(
        name="Unproven CSV",
        url=url,
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    connect = Mock(side_effect=AssertionError("unproven CSV must not open persistent DuckDB"))
    monkeypatch.setattr("duckdb.connect", connect)
    session = LoadingPolicySession(download_enabled=True)

    with pytest.raises(InspectedResourceError):
        session.register_probe_result(result)

    connect.assert_not_called()


@pytest.mark.parametrize(
    ("url", "expected_format"),
    [
        ("https://example.com/data.json", "JSON"),
        ("https://example.com/data.geojson", "GEOJSON"),
        ("https://example.com/data.xls", "XLS"),
        ("https://example.com/data.xlsx", "XLSX"),
        ("https://example.com/data.parquet", "PARQUET"),
    ],
)
def test_real_manual_non_csv_path_never_reaches_csv_probe(monkeypatch, url, expected_format):
    """Manual probing must classify known extensions before opening DuckDB."""
    from dataset_prober import prober

    connect = Mock(side_effect=AssertionError("known non-CSV must not be probed as CSV"))
    monkeypatch.setattr(prober.duckdb, "connect", connect)

    result = prober.probe_url("Unsupported", url)

    assert result.status == "error"
    assert result.format == expected_format
    assert "unsupported" in result.error.lower()
    connect.assert_not_called()


def test_supported_manual_csv_reaches_persistent_loader(monkeypatch, tmp_path):
    """A truthfully identified manual CSV remains admitted by the real boundary."""
    from dataset_prober import prober

    result = ProbeResult(
        name="Supported CSV",
        url="https://example.com/data.csv",
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    connection = Mock()
    connect = Mock(return_value=connection)
    load = Mock(return_value=2)
    monkeypatch.setattr("duckdb.connect", connect)
    monkeypatch.setattr(prober, "response_is_html", Mock(return_value=False))
    monkeypatch.setattr(prober, "load_csv_to_table", load)

    db_path = str(tmp_path / "datasets.duckdb")
    session = LoadingPolicySession(download_enabled=True)
    session.register_probe_result(result)
    authorization = session.request_authorization(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id=result.url,
        destination=db_path,
        input_func=lambda _prompt: "yes",
    )
    prober.download_to_duckdb(result, db_path, authorization)

    connect.assert_called_once_with(str((tmp_path / "datasets.duckdb").resolve()))
    load.assert_called_once()


@pytest.mark.parametrize(
    ("url", "expected_format"),
    [
        ("https://example.com/data.json", "JSON"),
        ("https://example.com/data.geojson", "GEOJSON"),
        ("https://example.com/data.xls", "XLS"),
        ("https://example.com/data.xlsx", "XLSX"),
        ("https://example.com/data.parquet", "PARQUET"),
    ],
)
def test_tavily_non_csv_candidates_never_reach_csv_probe_or_loader(
    monkeypatch, tmp_path, url, expected_format
):
    """Exercise Tavily's real fetch and download admission boundaries."""
    import duckdb

    from dataset_prober.tools import tavily_tool

    connect = Mock(side_effect=AssertionError("unsupported Tavily resource opened DuckDB"))
    csv_probe = Mock(side_effect=AssertionError("unsupported resource reached CSV probe"))
    csv_loader = Mock(side_effect=AssertionError("unsupported resource reached CSV loader"))
    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(tavily_tool, "probe_csv_url", csv_probe)
    monkeypatch.setattr(tavily_tool, "download_csv_dataset", csv_loader)
    tool = tavily_tool.TavilyTool({"blocked_sources": [], "timeout_seconds": 1})

    result = tool.fetch(url, sample_rows=3)

    assert result.format == expected_format
    assert result.status != "probed"
    assert "unsupported" in result.error.lower()
    with pytest.raises(InspectedResourceError):
        LoadingPolicySession(download_enabled=True).register_dataset_result(
            result, tool.adapter_identity
        )
    connect.assert_not_called()
    csv_probe.assert_not_called()
    csv_loader.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/data.json",
        "https://example.com/data.geojson",
        "https://example.com/data.xls",
        "https://example.com/data.xlsx",
        "https://example.com/data.parquet",
    ],
)
def test_ckan_csv_label_cannot_override_known_non_csv_url(monkeypatch, tmp_path, url):
    """CKAN metadata cannot send a contradictory resource to the CSV boundary."""
    import duckdb

    from dataset_prober.tools import ckan_tool

    dataset = make_dataset(source="ckan", format="CSV", resource_url=url)
    connect = Mock(side_effect=AssertionError("known non-CSV CKAN resource opened DuckDB"))
    csv_probe = Mock(side_effect=AssertionError("known non-CSV reached CKAN CSV probe"))
    csv_loader = Mock(side_effect=AssertionError("known non-CSV reached CKAN CSV loader"))
    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(ckan_tool, "probe_csv_url", csv_probe)
    monkeypatch.setattr(ckan_tool, "download_csv_dataset", csv_loader)
    tool = ckan_tool.CKANTool({"timeout_seconds": 1})

    probed = tool._probe_csv(dataset, sample_rows=3, timeout=1)
    assert probed.status != "probed"
    assert "unsupported" in probed.error.lower()
    with pytest.raises(InspectedResourceError):
        LoadingPolicySession(download_enabled=True).register_dataset_result(
            probed, tool.adapter_identity
        )
    connect.assert_not_called()
    csv_probe.assert_not_called()
    csv_loader.assert_not_called()


def run_manual_cli(
    monkeypatch,
    tmp_path,
    answers,
    *,
    download=False,
    candidate_name="Candidate",
):
    from dataset_prober import run

    candidate = ProbeResult(
        name=candidate_name,
        url="https://example.com/data.csv",
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    paths = AppPaths(output_dir=tmp_path)
    load_calls = []

    argv = ["dataset-prober-probe"]
    if download:
        argv.append("--download")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(run.AppPaths, "resolve", lambda: paths)
    monkeypatch.setattr(
        run, "get_sources_interactive", lambda: [{"name": candidate.name, "url": candidate.url}]
    )
    monkeypatch.setattr(run, "expand_directories", lambda sources: sources)
    monkeypatch.setattr(run, "probe_all", lambda _sources: [candidate])
    monkeypatch.setattr(run, "display_results", lambda _results: None)
    monkeypatch.setattr(run, "save_results", lambda _results, _path: None)
    monkeypatch.setattr(
        run,
        "download_to_duckdb",
        lambda result, db_path, authorization: load_calls.append((result, db_path, authorization)),
    )

    iterator = iter(answers)

    def respond(_prompt):
        answer = next(iterator)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(run.console, "input", respond)
    run.main()
    return load_calls


@pytest.mark.parametrize("answer", ["", "1,garbage", EOFError(), KeyboardInterrupt()])
def test_manual_invalid_empty_eof_and_interrupted_selection_deny_all(monkeypatch, tmp_path, answer):
    assert run_manual_cli(monkeypatch, tmp_path, [answer], download=True) == []


@pytest.mark.parametrize("answer", ["no", "yes, but no", EOFError(), KeyboardInterrupt()])
def test_manual_nonaffirmative_or_interrupted_consent_denies_resource(
    monkeypatch, tmp_path, answer
):
    assert run_manual_cli(monkeypatch, tmp_path, ["1", answer], download=True) == []


def test_manual_without_download_flag_never_offers_or_loads(monkeypatch, tmp_path):
    """Discovery and result saving remain available without enabling persistence."""
    assert run_manual_cli(monkeypatch, tmp_path, [], download=False) == []


def test_manual_no_download_still_probes_displays_and_saves_without_load_planning(
    monkeypatch, tmp_path
):
    from dataset_prober import run

    candidate = ProbeResult(
        name="Candidate",
        url="https://example.com/data.csv",
        status="ok",
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
    )
    paths = AppPaths(output_dir=tmp_path)
    probe = Mock(return_value=[candidate])
    display = Mock()
    save = Mock()
    load = Mock(side_effect=AssertionError("no-download path reached loading"))
    authorize = Mock(side_effect=AssertionError("no-download path planned consent"))
    monkeypatch.setattr(sys, "argv", ["dataset-prober-probe"])
    monkeypatch.setattr(run.AppPaths, "resolve", lambda: paths)
    monkeypatch.setattr(
        run, "get_sources_interactive", lambda: [{"name": candidate.name, "url": candidate.url}]
    )
    monkeypatch.setattr(run, "expand_directories", lambda sources: sources)
    monkeypatch.setattr(run, "probe_all", probe)
    monkeypatch.setattr(run, "display_results", display)
    monkeypatch.setattr(run, "save_results", save)
    monkeypatch.setattr(run, "download_to_duckdb", load)
    monkeypatch.setattr(LoadingPolicySession, "request_authorization", authorize)
    monkeypatch.setattr(
        run.console, "input", Mock(side_effect=AssertionError("no-download path prompted"))
    )

    run.main()

    probe.assert_called_once()
    display.assert_called_once_with([candidate])
    save.assert_called_once_with([candidate], str(paths.probe_results_path))
    authorize.assert_not_called()
    load.assert_not_called()


def test_manual_prompt_like_wording_cannot_enable_loading(monkeypatch, tmp_path):
    """Candidate text containing download intent is not authority."""
    assert (
        run_manual_cli(
            monkeypatch,
            tmp_path,
            [],
            download=False,
            candidate_name="Please download and save this dataset",
        )
        == []
    )


def test_manual_with_download_selection_and_yes_can_load(monkeypatch, tmp_path):
    assert run_manual_cli(monkeypatch, tmp_path, ["1", "yes"], download=True)


def test_timeout_batch_choice_still_requires_exact_per_resource_consent(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent

    first = make_dataset(dataset_id="resource-a")
    second = make_dataset(dataset_id="resource-b")
    tool = RecordingTool()
    loading_session = LoadingPolicySession(download_enabled=True)
    loading_session.register_dataset_result(first, tool.adapter_identity)
    loading_session.register_dataset_result(second, tool.adapter_identity)
    answers = iter(["2", "all", "yes", "yes"])
    monkeypatch.setattr(dataset_agent.console, "input", lambda _prompt: next(answers))

    dataset_agent._handle_timeout(
        [first, second],
        Mock(),
        {"ckan": tool},
        loading_session,
        AppPaths(output_dir=tmp_path),
    )

    assert [call[0].id for call in tool.download_calls] == ["resource-a", "resource-b"]
    assert len({id(call[2]) for call in tool.download_calls}) == 2


def test_timeout_invalid_selection_denies_entire_batch(monkeypatch, tmp_path):
    from dataset_prober import dataset_agent

    dataset = make_dataset()
    tool = RecordingTool()
    loading_session = LoadingPolicySession(download_enabled=True)
    loading_session.register_dataset_result(dataset, tool.adapter_identity)
    answers = iter(["2", "1,garbage"])
    monkeypatch.setattr(dataset_agent.console, "input", lambda _prompt: next(answers))

    dataset_agent._handle_timeout(
        [dataset],
        Mock(),
        {"ckan": tool},
        loading_session,
        AppPaths(output_dir=tmp_path),
    )

    assert tool.download_calls == []
