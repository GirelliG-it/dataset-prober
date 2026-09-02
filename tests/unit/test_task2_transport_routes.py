"""Task 2 routing contracts: source fetches must traverse the guarded transport."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dataset_prober.tools.base import DatasetResult, RunDeadlineExceeded
from tests.conftest import eligible_assessment_for_candidate


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def remaining_until(clock: FakeClock, deadline: float):
    return lambda: max(0.0, deadline - clock())


class FakeHttpResult:
    def __init__(
        self,
        *,
        url="https://public.example/",
        data=None,
        text="",
        headers=None,
        content=b"",
    ):
        self.url = url
        self._data = data or {}
        self.text = text
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}
        self.status_code = 200

    def json(self):
        return self._data


def dataset_result(*, source, url, resource_format, dataset_id="resource-a"):
    return DatasetResult(
        id=dataset_id,
        title="Resource A",
        description="",
        source=source,
        source_name=source.upper(),
        url=url,
        download_url=url,
        format=resource_format,
        modified=None,
        frequency=None,
        license=None,
        license_url=None,
        row_count=1,
        columns=[{"name": "value", "type": "INTEGER"}],
        sample=[[1]],
        language=None,
        tags=[],
        status="probed",
    )


def fake_download(local_path: Path, *, source_url="https://public.example/data.csv"):
    from dataset_prober.tools.guards import FetchedResource

    @contextmanager
    def download(url, **_kwargs):
        assert url == source_url
        yield FetchedResource(
            source_url=url,
            final_url=url,
            path=str(local_path),
            headers={"Content-Type": "text/csv"},
        )

    return download


def ckan_route_config(
    *,
    base_url="https://catalog.public.example/api/3",
    landing_base_url="https://catalog.public.example",
    ckan_dialect="ckan_action",
    ckan_search_mode="server_literal_csv",
):
    return {
        "name": "CKAN",
        "base_url": base_url,
        "landing_base_url": landing_base_url,
        "ckan_dialect": ckan_dialect,
        "ckan_search_mode": ckan_search_mode,
    }


def authorize(dataset, adapter_identity, destination):
    from dataset_prober.loading_policy import LoadingPolicySession

    if not dataset.assessment.load_eligible:
        dataset.assessment = eligible_assessment_for_candidate(
            source_key=dataset.source,
            adapter_identity=adapter_identity,
            resource_id=dataset.id,
            retrieval_url=dataset.download_url,
        )
    session = LoadingPolicySession(download_enabled=True)
    session.register_dataset_result(dataset, adapter_identity)
    authorization = session.request_authorization(
        source_key=dataset.source,
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    assert authorization is not None
    return authorization


def test_manual_unsafe_url_fails_before_duckdb_or_authorization(monkeypatch, tmp_path):
    from dataset_prober import prober
    from dataset_prober.loading_policy import InspectedResourceError, LoadingPolicySession
    from dataset_prober.tools.guards import UnsafeURLError

    @contextmanager
    def reject(*_args, **_kwargs):
        raise UnsafeURLError("resolves to non-public address")
        yield

    monkeypatch.setattr(prober, "safe_download", reject, raising=False)
    monkeypatch.setattr(
        "duckdb.connect", Mock(side_effect=AssertionError("unsafe URL opened DuckDB"))
    )

    result = prober.probe_url("unsafe", "https://unsafe.example/data.csv")

    assert result.status == "error"
    assert "non-public" in result.error
    session = LoadingPolicySession(download_enabled=True)
    with pytest.raises(InspectedResourceError):
        session.register_probe_result(result)
    assert not (tmp_path / "datasets.duckdb").exists()


def test_manual_probe_reads_guarded_local_copy_without_httpfs(monkeypatch, tmp_path):
    from dataset_prober import prober

    csv_path = tmp_path / "fetched.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.setattr(prober, "safe_download", fake_download(csv_path), raising=False)
    assert not hasattr(prober, "ensure_httpfs")

    result = prober.probe_url("safe", "https://public.example/data.csv")

    assert result.status == "ok"
    assert result.row_count == 1


def test_ckan_catalogue_and_resource_use_guarded_transport(monkeypatch, tmp_path):
    from dataset_prober.tools import ckan_tool

    resource_url = "https://files.public.example/data.csv"
    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [{"format": "CSV", "url": resource_url}],
    }
    responses = [
        FakeHttpResult(data={"success": True, "result": {"results": [package]}}),
        FakeHttpResult(data={"success": True, "result": package}),
    ]
    safe_get = Mock(side_effect=responses)
    csv_path = tmp_path / "ckan.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    safe_fetch = Mock(side_effect=fake_download(csv_path, source_url=resource_url))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get, raising=False)
    monkeypatch.setattr(ckan_tool, "safe_download", safe_fetch, raising=False)
    assert not hasattr(ckan_tool, "ensure_httpfs")
    tool = ckan_tool.CKANTool(ckan_route_config())

    found = tool.search("resource", max_results=1)
    result = tool.fetch(found[0].id, sample_rows=3)

    assert result.status == "probed"
    assert result.url == "https://catalog.public.example/dataset/resource-a"
    assert result.download_url == resource_url
    assert safe_get.call_args_list[0].args[0] == (
        "https://catalog.public.example/api/3/action/package_search"
    )
    assert safe_get.call_args_list[0].kwargs["params"] == {
        "q": "resource",
        "rows": 2,
        "sort": "metadata_modified desc",
        "fq": "res_format:CSV",
    }
    assert safe_get.call_args_list[1].args[0] == (
        "https://catalog.public.example/api/3/action/package_show"
    )
    assert safe_get.call_count == 2
    safe_fetch.assert_called_once()


@pytest.mark.parametrize("remaining_seconds", [2, 60])
@pytest.mark.parametrize("source", ["cbs", "ckan"])
def test_catalog_search_timeout_is_capped_by_fresh_run_allowance(
    monkeypatch,
    source,
    remaining_seconds,
):
    clock = FakeClock()
    remaining_time = remaining_until(clock, remaining_seconds)

    if source == "cbs":
        from dataset_prober.tools import cbs_tool

        transport = Mock(return_value=FakeHttpResult(data={"value": []}))
        monkeypatch.setattr(cbs_tool, "safe_http_get", transport)
        tool = cbs_tool.CBSTool(
            {
                "name": "CBS",
                "base_url": "https://opendata.cbs.nl/ODataCatalog",
                "timeout_seconds": 30,
            }
        )
    else:
        from dataset_prober.tools import ckan_tool

        transport = Mock(
            return_value=FakeHttpResult(data={"success": True, "result": {"results": []}})
        )
        monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
        tool = ckan_tool.CKANTool(
            {
                **ckan_route_config(),
                "timeout_seconds": 30,
            }
        )

    tool.search("population", max_results=1, remaining_time=remaining_time)

    expected_timeout = min(30, remaining_seconds)
    assert transport.call_count == 1
    assert transport.call_args.kwargs["timeout"] == expected_timeout


@pytest.mark.parametrize("source", ["cbs", "ckan"])
def test_catalog_search_preserves_exhausted_run_deadline_before_transport(
    monkeypatch,
    source,
):
    remaining_time = Mock(return_value=0)
    transport = Mock(side_effect=AssertionError("expired search reached transport"))

    if source == "cbs":
        from dataset_prober.tools import cbs_tool

        monkeypatch.setattr(cbs_tool, "safe_http_get", transport)
        tool = cbs_tool.CBSTool(
            {
                "name": "CBS",
                "base_url": "https://opendata.cbs.nl/ODataCatalog",
                "timeout_seconds": 30,
            }
        )
    else:
        from dataset_prober.tools import ckan_tool

        monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
        tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    with pytest.raises(RunDeadlineExceeded, match="deadline exhausted"):
        tool.search("population", max_results=1, remaining_time=remaining_time)

    remaining_time.assert_called_once_with()
    transport.assert_not_called()


def test_cbs_fetch_recalculates_run_allowance_before_sample_request(monkeypatch):
    from dataset_prober.tools import cbs_tool

    clock = FakeClock()
    remaining_time = remaining_until(clock, 60)
    timeouts = []

    def transport(_url, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            clock.advance(58)
            return FakeHttpResult(data={"value": [{"Title": "Population"}]})
        return FakeHttpResult(data={"value": [{"Period": "2025", "Value": 1}]})

    monkeypatch.setattr(cbs_tool, "safe_http_get", transport)
    tool = cbs_tool.CBSTool({"name": "CBS", "timeout_seconds": 30})

    result = tool.fetch("83583NED", sample_rows=1, remaining_time=remaining_time)

    assert result.status == "probed"
    assert timeouts == [30, 2]


def test_cbs_fetch_does_not_start_sample_after_metadata_exhausts_deadline(monkeypatch):
    from dataset_prober.tools import cbs_tool

    clock = FakeClock()
    remaining_time = remaining_until(clock, 60)
    metadata = FakeHttpResult(data={"value": [{"Title": "Population"}]})

    def transport(_url, **_kwargs):
        clock.advance(60)
        return metadata

    safe_get = Mock(side_effect=transport)
    monkeypatch.setattr(cbs_tool, "safe_http_get", safe_get)
    tool = cbs_tool.CBSTool({"name": "CBS", "timeout_seconds": 30})

    with pytest.raises(RunDeadlineExceeded, match="deadline exhausted"):
        tool.fetch("83583NED", sample_rows=1, remaining_time=remaining_time)

    assert safe_get.call_count == 1


def test_ckan_fetch_recalculates_run_allowance_before_csv_retrieval(monkeypatch, tmp_path):
    from dataset_prober.tools import ckan_tool
    from dataset_prober.tools.guards import FetchedResource

    resource_url = "https://files.public.example/data.csv"
    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [{"format": "CSV", "url": resource_url}],
    }
    clock = FakeClock()
    remaining_time = remaining_until(clock, 60)
    metadata_timeouts = []
    retrieval_timeouts = []
    csv_path = tmp_path / "ckan-deadline.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")

    def package_show(_url, **kwargs):
        metadata_timeouts.append(kwargs["timeout"])
        clock.advance(58)
        return FakeHttpResult(data={"success": True, "result": package})

    @contextmanager
    def retrieve(url, **kwargs):
        retrieval_timeouts.append(kwargs["timeout"])
        yield FetchedResource(
            source_url=url,
            final_url=url,
            path=str(csv_path),
            headers={"Content-Type": "text/csv"},
        )

    monkeypatch.setattr(ckan_tool, "safe_http_get", package_show)
    monkeypatch.setattr(ckan_tool, "safe_download", retrieve)
    tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    result = tool.fetch("resource-a", sample_rows=1, remaining_time=remaining_time)

    assert result.status == "probed"
    assert metadata_timeouts == [30]
    assert retrieval_timeouts == [2]


def test_ckan_fetch_does_not_start_csv_probe_after_metadata_exhausts_deadline(monkeypatch):
    from dataset_prober.tools import ckan_tool

    resource_url = "https://files.public.example/data.csv"
    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [{"format": "CSV", "url": resource_url}],
    }
    clock = FakeClock()
    remaining_time = remaining_until(clock, 60)

    def package_show(_url, **_kwargs):
        clock.advance(60)
        return FakeHttpResult(data={"success": True, "result": package})

    safe_fetch = Mock(side_effect=AssertionError("expired fetch reached CSV retrieval"))
    monkeypatch.setattr(ckan_tool, "safe_http_get", package_show)
    monkeypatch.setattr(ckan_tool, "safe_download", safe_fetch)
    tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    with pytest.raises(RunDeadlineExceeded, match="deadline exhausted"):
        tool.fetch("resource-a", sample_rows=1, remaining_time=remaining_time)

    safe_fetch.assert_not_called()


def test_ckan_fetch_preserves_deadline_after_retrieval_before_duckdb(
    monkeypatch,
    tmp_path,
):
    from dataset_prober.tools import ckan_tool
    from dataset_prober.tools.guards import FetchedResource

    resource_url = "https://files.public.example/data.csv"
    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [{"format": "CSV", "url": resource_url}],
    }
    clock = FakeClock()
    remaining_time = remaining_until(clock, 60)
    csv_path = tmp_path / "ckan-deadline-cleanup.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    cleanup_observed = []
    retrieval_calls = []

    @contextmanager
    def retrieve(url, **kwargs):
        retrieval_calls.append((url, kwargs["timeout"]))
        try:
            clock.advance(60)
            yield FetchedResource(
                source_url=url,
                final_url=url,
                path=str(csv_path),
                headers={"Content-Type": "text/csv"},
            )
        finally:
            csv_path.unlink(missing_ok=True)
            cleanup_observed.append(True)

    package_show = Mock(return_value=FakeHttpResult(data={"success": True, "result": package}))
    duckdb_connect = Mock(side_effect=AssertionError("expired probe opened DuckDB"))
    monkeypatch.setattr(ckan_tool, "safe_http_get", package_show)
    monkeypatch.setattr(ckan_tool, "safe_download", retrieve)
    monkeypatch.setattr("duckdb.connect", duckdb_connect)
    tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})
    candidate = tool._package_to_result(package)
    monkeypatch.setattr(tool, "_package_to_result", Mock(return_value=candidate))

    with pytest.raises(RunDeadlineExceeded, match="deadline exhausted"):
        tool.fetch("resource-a", sample_rows=1, remaining_time=remaining_time)

    assert package_show.call_count == 1
    assert retrieval_calls == [(resource_url, 30)]
    duckdb_connect.assert_not_called()
    assert cleanup_observed == [True]
    assert not csv_path.exists()
    assert candidate.status == "found"
    assert candidate.error is None


@pytest.mark.parametrize("source", ["cbs", "ckan"])
def test_catalog_search_transport_failure_remains_error_result(monkeypatch, source):
    transport = Mock(side_effect=OSError("source transport failed"))

    if source == "cbs":
        from dataset_prober.tools import cbs_tool

        monkeypatch.setattr(cbs_tool, "safe_http_get", transport)
        tool = cbs_tool.CBSTool(
            {
                "name": "CBS",
                "base_url": "https://opendata.cbs.nl/ODataCatalog",
                "timeout_seconds": 30,
            }
        )
    else:
        from dataset_prober.tools import ckan_tool

        monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
        tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    results = tool.search("population", max_results=1, remaining_time=lambda: 60)

    assert transport.call_count == 1
    assert len(results) == 1
    assert results[0].status == "failed"
    assert "source transport failed" in results[0].error


@pytest.mark.parametrize("source", ["cbs", "ckan"])
def test_fetch_transport_failure_remains_failed_dataset_result(monkeypatch, source):
    transport = Mock(side_effect=OSError("source transport failed"))

    if source == "cbs":
        from dataset_prober.tools import cbs_tool

        monkeypatch.setattr(cbs_tool, "safe_http_get", transport)
        tool = cbs_tool.CBSTool({"name": "CBS", "timeout_seconds": 30})
    else:
        from dataset_prober.tools import ckan_tool

        monkeypatch.setattr(ckan_tool, "safe_http_get", transport)
        tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    dataset_id = "83583NED" if source == "cbs" else "resource-a"
    result = tool.fetch(dataset_id, sample_rows=1, remaining_time=lambda: 60)

    assert transport.call_count == 1
    assert result.status == "failed"
    assert "source transport failed" in result.error


def test_ckan_csv_inspection_transport_failure_remains_failed_dataset_result(monkeypatch):
    from dataset_prober.tools import ckan_tool

    resource_url = "https://files.public.example/data.csv"
    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [{"format": "CSV", "url": resource_url}],
    }
    package_show = Mock(return_value=FakeHttpResult(data={"success": True, "result": package}))

    @contextmanager
    def failed_retrieval(_url, **_kwargs):
        raise OSError("CSV retrieval failed")
        yield

    monkeypatch.setattr(ckan_tool, "safe_http_get", package_show)
    monkeypatch.setattr(ckan_tool, "safe_download", failed_retrieval)
    tool = ckan_tool.CKANTool({**ckan_route_config(), "timeout_seconds": 30})

    result = tool.fetch("resource-a", sample_rows=1, remaining_time=lambda: 60)

    assert package_show.call_count == 1
    assert result.status == "failed"
    assert "CSV retrieval failed" in result.error


@pytest.mark.parametrize(
    ("config", "search_url", "show_url", "landing_url"),
    [
        (
            ckan_route_config(),
            "https://catalog.public.example/api/3/action/package_search",
            "https://catalog.public.example/api/3/action/package_show",
            "https://catalog.public.example/dataset/resource-a",
        ),
        (
            ckan_route_config(
                base_url="https://api.gsa.gov/technology/datagov/v3",
                landing_base_url="https://catalog.data.gov",
            ),
            "https://api.gsa.gov/technology/datagov/v3/action/package_search",
            "https://api.gsa.gov/technology/datagov/v3/action/package_show",
            "https://catalog.data.gov/dataset/resource-a",
        ),
        (
            ckan_route_config(
                base_url="https://data.europa.eu/api/hub/search",
                landing_base_url="https://data.europa.eu",
                ckan_dialect="eu_hub",
            ),
            "https://data.europa.eu/api/hub/search/ckan/package_search",
            "https://data.europa.eu/api/hub/search/ckan/package_show",
            "https://data.europa.eu/data/datasets/resource-a",
        ),
        (
            ckan_route_config(
                base_url="https://catalog.public.example/api/3/",
                landing_base_url="https://catalog.public.example/",
            ),
            "https://catalog.public.example/api/3/action/package_search",
            "https://catalog.public.example/api/3/action/package_show",
            "https://catalog.public.example/dataset/resource-a",
        ),
    ],
)
def test_ckan_dialects_construct_exact_api_and_landing_urls(
    monkeypatch,
    config,
    search_url,
    show_url,
    landing_url,
):
    from dataset_prober.tools import ckan_tool

    package = {
        "name": "resource-a",
        "id": "ignored-id",
        "title": "Resource A",
        "url": "https://package-controlled.example/wrong",
        "resources": [],
    }
    safe_get = Mock(
        side_effect=[
            FakeHttpResult(data={"success": True, "result": {"results": [package]}}),
            FakeHttpResult(data={"success": True, "result": package}),
        ]
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(config)

    search_result = tool.search("resource", max_results=1)[0]
    fetch_result = tool.fetch("resource-a", sample_rows=1)

    assert safe_get.call_args_list[0].args[0] == search_url
    assert safe_get.call_args_list[1].args[0] == show_url
    assert search_result.url == landing_url
    assert fetch_result.url == landing_url
    assert "package-controlled.example" not in search_result.url
    assert "package-controlled.example" not in fetch_result.url


def test_dutch_data_overheid_uses_official_routes_bounded_query_and_no_api_key(monkeypatch):
    from dataset_prober.config_loader import ConfigLoader
    from dataset_prober.profile_resolution import resolve_profile
    from dataset_prober.tools import TOOL_REGISTRY, ckan_tool

    csv_uri = "http://publications.europa.eu/resource/authority/file-type/CSV"
    packages = [
        {
            "name": "resource-a",
            "title": "Resource A",
            "resources": [{"format": csv_uri, "url": "https://files.public.example/a.csv"}],
        },
        {
            "name": "resource-b",
            "title": "Resource B",
            "resources": [
                {
                    "format_displayname": "CSV",
                    "url": "https://files.public.example/b.csv",
                }
            ],
        },
        {
            "name": "resource-c",
            "title": "Resource C",
            "resources": [
                {
                    "format": csv_uri,
                    "format_displayname": "CSV",
                    "url": "https://files.public.example/c.csv",
                }
            ],
        },
        {
            "name": "resource-d",
            "title": "Resource D",
            "resources": [{"format": csv_uri, "url": "https://files.public.example/d.csv"}],
        },
    ]
    show_package = {"name": "resource-a", "title": "Resource A", "resources": []}
    safe_get = Mock(
        side_effect=[
            FakeHttpResult(data={"success": True, "result": {"results": packages}}),
            FakeHttpResult(data={"success": True, "result": show_package}),
        ]
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    profile = ConfigLoader().load("dutch_government")
    resolved = resolve_profile(profile, registry=TOOL_REGISTRY)
    tool = resolved.execution_map["ckan"]

    search_results = tool.search("water quality", max_results=3)
    fetch_result = tool.fetch("resource-a", sample_rows=1)

    assert [result.id for result in search_results] == [
        "resource-a",
        "resource-b",
        "resource-c",
    ]
    search_result = search_results[0]
    search_call, show_call = safe_get.call_args_list
    assert search_call.args[0] == "https://data.overheid.nl/data/api/3/action/package_search"
    assert search_call.kwargs["params"] == {
        "q": "water quality",
        "rows": 6,
        "sort": "metadata_modified desc",
    }
    assert show_call.args[0] == "https://data.overheid.nl/data/api/3/action/package_show"
    assert show_call.kwargs["params"] == {"id": "resource-a"}
    assert search_call.kwargs["headers"] == {"Content-Type": "application/json"}
    assert show_call.kwargs["headers"] == {"Content-Type": "application/json"}
    assert search_call.kwargs["timeout"] == 30
    assert show_call.kwargs["timeout"] == 30
    assert search_result.url == "https://data.overheid.nl/dataset/resource-a"
    assert fetch_result.url == "https://data.overheid.nl/dataset/resource-a"


def test_local_resource_metadata_search_rejects_unsupported_ambiguous_and_missing_formats(
    monkeypatch,
):
    from dataset_prober.tools import ckan_tool

    csv_uri = "http://publications.europa.eu/resource/authority/file-type/CSV"
    packages = [
        {
            "name": "zip-only",
            "resources": [
                {
                    "format": "http://publications.europa.eu/resource/authority/file-type/ZIP",
                    "url": "https://files.public.example/data.zip",
                }
            ],
        },
        {
            "name": "contradictory",
            "resources": [
                {
                    "format": csv_uri,
                    "format_displayname": "ZIP",
                    "url": "https://files.public.example/data.csv",
                }
            ],
        },
        {
            "name": "unknown",
            "resources": [
                {
                    "format": "custom-tabular",
                    "url": "https://files.public.example/data.csv",
                }
            ],
        },
        {
            "name": "missing",
            "resources": [{"url": "https://files.public.example/data.csv"}],
        },
        {
            "name": "supported",
            "resources": [
                {
                    "format_displayname": "CSV",
                    "url": "https://files.public.example/data",
                }
            ],
        },
    ]
    safe_get = Mock(
        return_value=FakeHttpResult(data={"success": True, "result": {"results": packages}})
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))

    results = tool.search("resource", max_results=3)

    assert [result.id for result in results] == ["supported"]
    assert results[0].format == "CSV"
    assert results[0].download_url == "https://files.public.example/data"
    assert "fq" not in safe_get.call_args.kwargs["params"]


@pytest.mark.parametrize(
    "earlier_resource",
    [
        {"format": "CSV/ZIP", "url": "https://files.public.example/data.zip"},
        {"format": "CSV"},
        {"format": "CSV", "url": ""},
        {"format": "CSV", "url": "   "},
        {"format": "CSV", "url": 7},
        {
            "format": "http://publications.europa.eu/resource/authority/file-type/CSV",
            "format_displayname": "ZIP",
            "url": "https://files.public.example/contradictory.csv",
        },
        {"format": "JSON", "url": "https://files.public.example/data.json"},
        "not-a-resource",
    ],
)
def test_local_resource_metadata_selects_later_admissible_plain_csv(earlier_resource):
    from dataset_prober.tools.ckan_tool import CKANTool

    expected_url = "https://files.public.example/exact-resource.csv"
    package = {
        "name": "resource-a",
        "resources": [
            earlier_resource,
            {"format": "CSV", "url": expected_url},
        ],
    }
    tool = CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))

    result = tool._package_to_result(package)

    assert result.format == "CSV"
    assert result.download_url == expected_url


def test_local_resource_metadata_search_returns_later_admissible_plain_csv(monkeypatch):
    from dataset_prober.tools import ckan_tool

    expected_url = "https://files.public.example/exact-resource.csv"
    package = {
        "name": "resource-a",
        "resources": [
            {"format": "CSV/ZIP", "url": "https://files.public.example/data.zip"},
            {"format": "CSV", "url": expected_url},
        ],
    }
    safe_get = Mock(
        return_value=FakeHttpResult(data={"success": True, "result": {"results": [package]}})
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))

    results = tool.search("resource", max_results=1)

    assert len(results) == 1
    assert results[0].format == "CSV"
    assert results[0].download_url == expected_url
    assert safe_get.call_args.kwargs["params"] == {
        "q": "resource",
        "rows": 2,
        "sort": "metadata_modified desc",
    }


def test_local_resource_metadata_fetch_probes_later_admissible_plain_csv(monkeypatch):
    from dataset_prober.tools import ckan_tool

    expected_url = "https://files.public.example/exact-resource.csv"
    package = {
        "name": "resource-a",
        "resources": [
            {"format": "CSV", "url": ""},
            {"format": "CSV", "url": expected_url},
        ],
    }
    safe_get = Mock(return_value=FakeHttpResult(data={"success": True, "result": package}))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))
    probe = Mock(side_effect=lambda result, *_args, **_kwargs: result)
    monkeypatch.setattr(tool, "_probe_csv", probe)

    result = tool.fetch("resource-a", sample_rows=2)

    assert result.format == "CSV"
    assert result.download_url == expected_url
    probe.assert_called_once()
    assert probe.call_args.args[0] is result
    assert probe.call_args.kwargs["remaining_time"] is None


def test_local_resource_metadata_query_only_csv_stops_before_guarded_retrieval(
    monkeypatch,
):
    from dataset_prober.resource_classification import AssessmentReason
    from dataset_prober.tools import ckan_tool

    resource_url = "https://files.public.example/download?format=csv"
    package = {
        "name": "resource-a",
        "resources": [
            {
                "format_displayname": "CSV",
                "url": resource_url,
            }
        ],
    }
    safe_get = Mock(
        return_value=FakeHttpResult(
            data={
                "success": True,
                "result": package,
            }
        )
    )
    guarded_retrieval = Mock(
        side_effect=AssertionError("query-only CSV evidence reached guarded retrieval")
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    monkeypatch.setattr(ckan_tool, "safe_download", guarded_retrieval)
    tool = ckan_tool.CKANTool(
        ckan_route_config(
            ckan_search_mode="local_resource_metadata",
        )
    )

    result = tool.fetch("resource-a", sample_rows=2)

    assert result.status == "found"
    assert result.format == "CSV"
    assert result.download_url == resource_url
    assert result.error == "Unsupported or unproven CKAN resource format: CSV"
    assert result.assessment.reason is AssessmentReason.UNSUPPORTED_FORMAT
    guarded_retrieval.assert_not_called()


def test_local_resource_metadata_fetch_without_admissible_resource_stays_report_only(
    monkeypatch,
):
    from dataset_prober.resource_classification import AssessmentReason
    from dataset_prober.tools import ckan_tool

    package = {
        "name": "resource-a",
        "resources": [
            "not-a-resource",
            {"format": "CSV/ZIP", "url": "https://files.public.example/data.zip"},
            {"format": "CSV", "url": "   "},
            {
                "format": "CSV",
                "format_displayname": "ZIP",
                "url": "https://files.public.example/contradictory.csv",
            },
        ],
    }
    safe_get = Mock(return_value=FakeHttpResult(data={"success": True, "result": package}))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))
    probe = Mock(side_effect=AssertionError("unsupported resource reached CSV probing"))
    monkeypatch.setattr(tool, "_probe_csv", probe)

    result = tool.fetch("resource-a", sample_rows=2)

    assert result.status == "found"
    assert result.format is None
    assert result.download_url is None
    assert result.error == "No supported CSV resources found in package"
    assert result.assessment.reason is AssessmentReason.UNSUPPORTED_FORMAT
    probe.assert_not_called()


def test_server_literal_csv_resource_selection_remains_first_recognized_resource(monkeypatch):
    from dataset_prober.tools import ckan_tool

    packages = [
        {
            "name": "zip-first",
            "resources": [
                {"format": "CSV/ZIP", "url": "https://files.public.example/data.zip"},
                {"format": "CSV", "url": "https://files.public.example/data.csv"},
            ],
        },
        {
            "name": "empty-url-first",
            "resources": [
                {"format": "CSV", "url": ""},
                {"format": "CSV", "url": "https://files.public.example/data.csv"},
            ],
        },
    ]
    safe_get = Mock(
        return_value=FakeHttpResult(data={"success": True, "result": {"results": packages}})
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="server_literal_csv"))

    results = tool.search("resource", max_results=2)

    assert [(result.format, result.download_url) for result in results] == [
        ("CSV/ZIP", "https://files.public.example/data.zip"),
        ("CSV", ""),
    ]
    assert safe_get.call_args.kwargs["params"]["fq"] == "res_format:CSV"


def test_local_resource_metadata_inspection_stays_within_bounded_overfetch(monkeypatch):
    from dataset_prober.tools import ckan_tool

    unsupported = [
        {
            "name": f"unsupported-{index}",
            "resources": [{"format": "ZIP", "url": "https://files.public.example/data"}],
        }
        for index in range(4)
    ]
    beyond_bound = {
        "name": "beyond-bound",
        "resources": [
            {
                "format": "CSV",
                "url": "https://files.public.example/data",
            }
        ],
    }
    safe_get = Mock(
        return_value=FakeHttpResult(
            data={
                "success": True,
                "result": {"results": [*unsupported, beyond_bound]},
            }
        )
    )
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))

    results = tool.search("resource", max_results=2)

    assert results == []
    assert safe_get.call_args.kwargs["params"]["rows"] == 4


@pytest.mark.parametrize(
    ("resource", "expected_format"),
    [
        (
            {
                "format": "http://publications.europa.eu/resource/authority/file-type/CSV",
                "format_displayname": "CSV",
                "url": "https://files.public.example/data",
            },
            "CSV",
        ),
        (
            {
                "format": "http://publications.europa.eu/resource/authority/file-type/CSV",
                "format_displayname": "ZIP",
                "url": "https://files.public.example/data.csv",
            },
            None,
        ),
    ],
)
def test_ckan_package_show_uses_closed_resource_format_normalization(
    monkeypatch, resource, expected_format
):
    from dataset_prober.tools import ckan_tool

    package = {
        "name": "resource-a",
        "title": "Resource A",
        "resources": [resource],
    }
    safe_get = Mock(return_value=FakeHttpResult(data={"success": True, "result": package}))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)
    tool = ckan_tool.CKANTool(ckan_route_config(ckan_search_mode="local_resource_metadata"))
    probe = Mock(side_effect=lambda result, *_args, **_kwargs: result)
    monkeypatch.setattr(tool, "_probe_csv", probe)

    result = tool.fetch("resource-a", sample_rows=1)

    assert safe_get.call_args.args[0].endswith("/action/package_show")
    assert result.format == expected_format
    if expected_format is None:
        probe.assert_not_called()
        assert result.error == "No supported CSV resources found in package"
    else:
        probe.assert_called_once()
        assert result.download_url == "https://files.public.example/data"


@pytest.mark.parametrize(
    ("package", "expected_id", "expected_url"),
    [
        (
            {"name": "preferred-name", "id": "fallback-id"},
            "preferred-name",
            "https://catalog.public.example/dataset/preferred-name",
        ),
        (
            {"name": " ", "id": "fallback-id"},
            "fallback-id",
            "https://catalog.public.example/dataset/fallback-id",
        ),
        (
            {"name": 7, "id": "fallback-id"},
            "fallback-id",
            "https://catalog.public.example/dataset/fallback-id",
        ),
        (
            {"name": "folder/na?me#part%é", "id": "fallback-id"},
            "folder/na?me#part%é",
            ("https://catalog.public.example/dataset/folder%2Fna%3Fme%23part%25%C3%A9"),
        ),
        (
            {"name": " resource ", "id": "fallback-id"},
            " resource ",
            "https://catalog.public.example/dataset/%20resource%20",
        ),
    ],
)
def test_ckan_landing_uses_stable_opaque_identifier(package, expected_id, expected_url):
    from dataset_prober.tools.ckan_tool import CKANTool

    package = {
        **package,
        "title": "Resource",
        "url": "https://package-controlled.example/wrong",
        "resources": [],
    }

    result = CKANTool(ckan_route_config())._package_to_result(package)

    assert result.id == expected_id
    assert result.url == expected_url
    assert "package-controlled.example" not in result.url


@pytest.mark.parametrize(
    "package",
    [
        {},
        {"name": "", "id": ""},
        {"name": ".", "id": "fallback-id"},
        {"name": "..", "id": "fallback-id"},
        {"id": "."},
        {"name": " ", "id": ".."},
    ],
)
def test_ckan_package_requires_non_special_stable_identifier(package):
    from dataset_prober.tools.ckan_tool import CKANTool

    with pytest.raises(ValueError, match="identifier"):
        CKANTool(ckan_route_config())._package_to_result(package)


@pytest.mark.parametrize(
    "config",
    [
        {
            "base_url": "https://catalog.public.example/api/3",
            "landing_base_url": "https://catalog.public.example",
            "ckan_search_mode": "server_literal_csv",
        },
        ckan_route_config(ckan_dialect="unknown"),
        ckan_route_config(base_url=None),
        ckan_route_config(landing_base_url=None),
    ],
)
def test_invalid_ckan_route_configuration_stops_before_guarded_transport(monkeypatch, config):
    from dataset_prober.tools import ckan_tool

    safe_get = Mock(side_effect=AssertionError("invalid route reached guarded transport"))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)

    with pytest.raises(ValueError, match="route configuration"):
        ckan_tool.CKANTool(config).search("resource", max_results=1)

    safe_get.assert_not_called()


@pytest.mark.parametrize("mode", [None, "", "unknown", 1])
def test_invalid_ckan_search_mode_stops_before_guarded_transport(monkeypatch, mode):
    from dataset_prober.tools import ckan_tool

    safe_get = Mock(side_effect=AssertionError("invalid search mode reached guarded transport"))
    monkeypatch.setattr(ckan_tool, "safe_http_get", safe_get)

    with pytest.raises(ValueError, match="search configuration"):
        ckan_tool.CKANTool(ckan_route_config(ckan_search_mode=mode)).search(
            "resource", max_results=1
        )

    safe_get.assert_not_called()


def test_ckan_route_fields_reach_tool_factory_without_type_loss(test_profile):
    from dataclasses import replace

    from dataset_prober.profile_contract import (
        CKANDialect,
        CKANSearchMode,
        build_profile_contract,
    )
    from dataset_prober.profile_resolution import resolve_profile
    from dataset_prober.tools import TOOL_REGISTRY
    from dataset_prober.tools.ckan_tool import CKANTool

    source_contract = test_profile.contract
    contract = build_profile_contract(
        profile_id="synthetic_ckan",
        status="enabled",
        reason=None,
        catalogs=[
            {
                "catalog_id": "synthetic_ckan",
                "adapter": "ckan",
                "name": "Synthetic CKAN",
                "base_url": "https://api.public.example/api/3",
                "api_key_env": None,
                "timeout_seconds": 10,
                "priority": 1,
                "required": True,
                "ckan_dialect": "ckan_action",
                "ckan_search_mode": "server_literal_csv",
                "landing_base_url": "https://catalog.public.example",
            }
        ],
        budget={
            field: getattr(source_contract.budget, field)
            for field in (
                "max_searches",
                "max_results",
                "max_probes",
                "max_model_calls",
                "max_tokens",
                "max_total_tokens",
                "timeout_minutes",
                "sample_rows",
                "download_timeout_seconds",
            )
        },
        supported_adapters={"ckan"},
        trusted_hosts=[],
        blocked_hosts=[],
    )
    profile = replace(test_profile, contract=contract)

    resolved = resolve_profile(profile, registry=TOOL_REGISTRY)
    [tool] = resolved.tools

    assert isinstance(tool, CKANTool)
    assert profile.catalogs[0].ckan_dialect is CKANDialect.CKAN_ACTION
    assert profile.catalogs[0].ckan_search_mode is CKANSearchMode.SERVER_LITERAL_CSV
    assert tool.config["ckan_dialect"] is CKANDialect.CKAN_ACTION
    assert tool.config["ckan_search_mode"] is profile.catalogs[0].ckan_search_mode
    assert tool.config["landing_base_url"] == "https://catalog.public.example"


def test_cbs_catalogue_sample_and_bulk_pages_use_guarded_transport(monkeypatch, tmp_path):
    from dataset_prober.tools import cbs_tool

    table_id = "83583NED"
    data_url = f"https://opendata.cbs.nl/ODataApi/odata/{table_id}/TypedDataSet?$format=json"
    responses = [
        FakeHttpResult(
            data={
                "value": [
                    {
                        "Identifier": table_id,
                        "Title": "Population",
                        "ShortDescription": "Population",
                        "OutputStatus": "active",
                        "Modified": "2026-01-01",
                    }
                ]
            }
        ),
        FakeHttpResult(
            url=f"https://opendata.cbs.nl/ODataApi/odata/{table_id}/TableInfos?$format=json",
            data={"value": [{"Title": "Population"}]},
        ),
        FakeHttpResult(
            url=(
                f"https://opendata.cbs.nl/ODataApi/odata/{table_id}/"
                "TypedDataSet?%24top=3&%24format=json"
            ),
            data={"value": [{"Period": "2025", "Value": 1}]},
        ),
        FakeHttpResult(
            url=data_url,
            data={"value": [{"Period": "2025", "Value": 1}]},
        ),
    ]
    safe_get = Mock(side_effect=responses)
    monkeypatch.setattr(cbs_tool, "safe_http_get", safe_get, raising=False)
    monkeypatch.setattr(
        "cbsodata.get_data",
        Mock(side_effect=AssertionError("CBS used opaque cbsodata transport")),
    )
    tool = cbs_tool.CBSTool({"name": "CBS", "base_url": "https://opendata.cbs.nl/ODataCatalog"})

    found = tool.search("population", max_results=1)
    result = tool.fetch(found[0].id, sample_rows=3)
    assert result.download_url == data_url
    destination = tmp_path / "cbs.duckdb"
    authorization = authorize(result, tool.adapter_identity, destination)
    loaded = tool.download(result, destination, authorization)

    assert loaded.status == "downloaded"
    assert loaded.row_count == 1
    assert safe_get.call_count == 4


def test_tavily_provider_side_search_and_extraction_are_disabled(monkeypatch):
    from dataset_prober.tools.tavily_tool import TavilyTool

    client = Mock()
    client.search.side_effect = AssertionError("disabled Tavily search reached provider")
    client.extract.side_effect = AssertionError("disabled Tavily extraction reached provider")
    tool = TavilyTool({"name": "Tavily", "blocked_sources": [], "timeout_seconds": 1})
    monkeypatch.setattr(tool, "_client", Mock(return_value=client), raising=False)

    search_result = tool.search("population", max_results=1)
    extract_result = tool.fetch("https://public.example/landing", sample_rows=3)

    assert search_result[0].status == "failed"
    assert "disabled" in search_result[0].error.lower()
    assert extract_result.status == "failed"
    assert "disabled" in extract_result.error.lower()
    client.search.assert_not_called()
    client.extract.assert_not_called()


def test_agent_model_context_and_schema_exclude_disabled_tavily_provider(
    monkeypatch,
    tmp_path,
    test_profile,
):
    import json
    from dataclasses import replace

    from dataset_prober import dataset_agent
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.paths import AppPaths
    from dataset_prober.profile_contract import build_profile_contract
    from dataset_prober.profile_resolution import resolve_profile
    from dataset_prober.tools.cbs_tool import CBSTool

    contract = test_profile.contract
    catalogs = [
        {
            "catalog_id": catalog.catalog_id,
            "adapter": catalog.adapter,
            "name": catalog.name,
            "base_url": catalog.base_url,
            "api_key_env": catalog.api_key_env,
            "timeout_seconds": catalog.timeout_seconds,
            "priority": catalog.priority,
            "required": catalog.required,
        }
        for catalog in contract.catalogs
    ]
    catalogs.append(
        {
            "catalog_id": "disabled_tavily",
            "adapter": "tavily",
            "name": "Disabled Tavily discovery",
            "base_url": "https://api.tavily.com",
            "api_key_env": "TAVILY_API_KEY",
            "timeout_seconds": 10,
            "priority": 2,
            "required": False,
        }
    )
    mixed_contract = build_profile_contract(
        profile_id="mixed_profile",
        status="enabled",
        reason=None,
        catalogs=catalogs,
        budget={
            field: getattr(contract.budget, field)
            for field in (
                "max_searches",
                "max_results",
                "max_probes",
                "max_model_calls",
                "max_tokens",
                "max_total_tokens",
                "timeout_minutes",
                "sample_rows",
                "download_timeout_seconds",
            )
        },
        supported_adapters={"cbs", "ckan", "tavily"},
        trusted_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in contract.trusted_hosts
        ],
        blocked_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in contract.blocked_hosts
        ],
    )
    profile = replace(test_profile, contract=mixed_contract)
    assert [catalog.adapter for catalog in profile.catalogs] == ["cbs", "tavily"]
    assert [catalog.adapter for catalog in profile.agent_usable_catalogs] == ["cbs"]

    tavily_factory = Mock(side_effect=AssertionError("policy-excluded Tavily was constructed"))
    resolved = resolve_profile(
        profile,
        registry={"cbs": CBSTool, "tavily": tavily_factory},
    )
    monkeypatch.setattr(dataset_agent, "get_anthropic_api_key", Mock(return_value="offline-key"))
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Done")],
        usage=usage,
    )
    client = Mock()
    client.messages.create.return_value = response
    anthropic_factory = Mock(return_value=client)
    monkeypatch.setattr(dataset_agent.anthropic, "Anthropic", anthropic_factory)

    dataset_agent.run_profile(
        user_prompt="Find population data",
        resolved_profile=resolved,
        budget=dataset_agent.Budget.from_profile(profile.budget),
        loading_session=LoadingPolicySession(download_enabled=False),
        session_cost=dataset_agent.SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    call = client.messages.create.call_args.kwargs
    system = call["system"].lower()
    definitions = call["tools"]
    rendered = json.dumps(definitions).lower()

    assert "cbs" in system
    assert "tavily" not in system
    assert "disabled tavily discovery" not in system
    assert "api.tavily.com" not in system
    for definition in definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source:
            assert source["enum"] == ["cbs"]
            assert "tavily" not in source["enum"]
            assert "tavily" not in source["description"].lower()
        assert "tavily" not in definition["description"].lower()
    assert "disabled tavily discovery" not in rendered
    assert resolved.source_keys == tuple(resolved.execution_map) == ("cbs",)
    client.messages.create.assert_called_once()
    anthropic_factory.assert_called_once_with(api_key="offline-key", max_retries=0)
    tavily_factory.assert_not_called()


def test_tavily_direct_resource_uses_guarded_local_copy(monkeypatch, tmp_path):
    from dataset_prober.tools import tavily_tool

    resource_url = "https://public.example/data.csv"
    csv_path = tmp_path / "tavily.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    safe_fetch = Mock(side_effect=fake_download(csv_path, source_url=resource_url))
    monkeypatch.setattr(tavily_tool, "safe_download", safe_fetch, raising=False)
    assert not hasattr(tavily_tool, "ensure_httpfs")
    tool = tavily_tool.TavilyTool({"name": "Tavily", "blocked_sources": []})

    result = tool.fetch(resource_url, sample_rows=3)

    assert result.status == "probed"
    safe_fetch.assert_called_once()


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_unsafe_adapter_resource_stops_before_probe_connection(monkeypatch, source):
    from dataset_prober.tools import guards
    from dataset_prober.tools.ckan_tool import CKANTool
    from dataset_prober.tools.tavily_tool import TavilyTool

    monkeypatch.setattr(
        guards.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("169.254.169.254", 443))],
    )
    connect = Mock(side_effect=AssertionError("unsafe resource opened DuckDB"))
    monkeypatch.setattr("duckdb.connect", connect)
    url = "https://unsafe.public-name.example/data.csv"
    if source == "ckan":
        tool = CKANTool({"name": "CKAN", "base_url": "https://catalog.public.example"})
        result = dataset_result(source="ckan", url=url, resource_format="CSV")
        result.status = "found"
        result = tool._probe_csv(result, sample_rows=3, timeout=1)
    else:
        tool = TavilyTool({"name": "Tavily", "blocked_sources": []})
        result = tool._probe_direct(url, sample_rows=3, timeout=1)

    assert result.status != "probed"
    assert "non-public" in result.error
    connect.assert_not_called()


def test_unsafe_configured_cbs_catalogue_fails_before_connection(monkeypatch):
    from dataset_prober.tools import guards
    from dataset_prober.tools.cbs_tool import CBSTool

    monkeypatch.setattr(
        guards.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    tool = CBSTool({"name": "CBS", "base_url": "http://localhost/catalog"})

    results = tool.search("population", max_results=1)

    assert results[0].status == "failed"
    assert "non-public" in results[0].error


def test_cbs_odata_pagination_cannot_change_source_origin(monkeypatch):
    from dataset_prober.tools import cbs_tool

    initial_url = "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet?$format=json"
    first_page = FakeHttpResult(
        url=initial_url,
        data={
            "value": [{"Period": "2025"}],
            "@odata.nextLink": "https://other-public.example/next",
        },
    )
    safe_get = Mock(return_value=first_page)
    monkeypatch.setattr(cbs_tool, "safe_http_get", safe_get)
    tool = cbs_tool.CBSTool({"name": "CBS"})

    with pytest.raises(cbs_tool.UnsafeURLError, match="changed source origin"):
        tool._download_odata_rows(initial_url, timeout=1)

    safe_get.assert_called_once_with(initial_url, timeout=1)


def test_crawler_and_directory_listing_use_guarded_transport(monkeypatch):
    from dataset_prober import crawler

    html = '<a href="data.csv">Data</a>'
    safe_get = Mock(side_effect=lambda url, **_kwargs: FakeHttpResult(url=url, text=html))
    monkeypatch.setattr(crawler, "safe_http_get", safe_get, raising=False)

    crawled = crawler.crawl("https://public.example/catalog", max_depth=0)
    listing = crawler.resolve_directory("https://public.example/catalog/")

    assert crawled == [{"name": "Data", "url": "https://public.example/data.csv"}]
    assert listing["files"][0]["url"] == "https://public.example/catalog/data.csv"
    assert safe_get.call_count == 2


def test_csv_writer_downloads_guarded_copy_before_persistent_connect(monkeypatch, tmp_path):
    from dataset_prober.tools import base
    from dataset_prober.tools.guards import FetchedResource

    resource_url = "https://public.example/data.csv"
    dataset = dataset_result(source="ckan", url=resource_url, resource_format="CSV")
    destination = tmp_path / "datasets.duckdb"
    csv_path = tmp_path / "guarded.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    events = []

    @contextmanager
    def guarded_download(url, **_kwargs):
        events.append(("retrieval", url))
        yield FetchedResource(
            source_url=url,
            final_url=url,
            path=str(csv_path),
            headers={"Content-Type": "text/csv"},
        )

    real_connect = __import__("duckdb").connect

    def tracked_connect(path=None, *args, **kwargs):
        events.append(("connect", path))
        return real_connect(path, *args, **kwargs) if path else real_connect(*args, **kwargs)

    monkeypatch.setattr(base, "safe_download", guarded_download, raising=False)
    monkeypatch.setattr("duckdb.connect", tracked_connect)
    tool_identity = "CKAN (https://catalog.public.example)"
    authorization = authorize(dataset, tool_identity, destination)
    actual_claims = __import__(
        "dataset_prober.loading_policy", fromlist=["claims_for_dataset"]
    ).claims_for_dataset(dataset, tool_identity, destination)

    with authorization.activate(actual_claims) as permit:
        loaded = base.download_csv_dataset(dataset, tool_identity, destination, permit)

    assert loaded.status == "downloaded"
    assert events[0] == ("retrieval", resource_url)
    assert events[1] == ("connect", None)
    assert events[2] == ("connect", str(destination.resolve()))


def test_remote_url_never_reaches_duckdb_csv_scanner(monkeypatch, tmp_path):
    from dataset_prober.tools import base

    local_path = tmp_path / "guarded.csv"
    local_path.write_text("value\n1\n", encoding="utf-8")
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [("value", "BIGINT")]
    connection.execute.return_value.fetchone.return_value = (1,)
    scanner = Mock(return_value="read_csv_auto(?)")
    monkeypatch.setattr(base, "csv_scan_expr", scanner)

    fetched = SimpleNamespace(
        source_url="https://public.example/data.csv",
        final_url="https://public.example/data.csv",
        path=str(local_path),
        headers={"Content-Type": "text/csv"},
    )
    base.probe_csv_url(connection, fetched.path, sample_rows=3)

    assert scanner.call_args.args[1] == str(local_path)
    assert not scanner.call_args.args[1].startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "helper",
    [
        lambda base, connection: base.csv_scan_expr(connection, "https://public.example/data.csv"),
        lambda base, connection: base.probe_csv_url(
            connection, "https://public.example/data.csv", sample_rows=3
        ),
    ],
)
def test_shared_csv_probe_helpers_reject_remote_urls_before_duckdb(helper):
    from dataset_prober.tools import base

    connection = Mock()

    with pytest.raises(ValueError, match="existing local file"):
        helper(base, connection)

    connection.execute.assert_not_called()


def test_dataset_result_public_dict_redacts_sensitive_url_components():
    sensitive = "https://user:password@public.example/data.csv?token=secret#private"
    result = dataset_result(source="ckan", url=sensitive, resource_format="CSV")
    result.id = sensitive
    result.title = f"Dataset at {sensitive}"
    result.description = f"Retrieved from {sensitive}"
    result.error = f"Failed while reading {sensitive}"

    public = result.to_dict()
    rendered = str(public)

    assert "user" not in rendered
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "private" not in rendered
    assert rendered.count("SHA-256:") >= 4
    assert result.download_url == sensitive


def test_manual_saved_report_redacts_url_and_error_but_retains_internal_identity(tmp_path):
    import json

    from dataset_prober.prober import ProbeResult, save_results

    sensitive = "https://user:password@public.example/data.csv?token=secret#private"
    result = ProbeResult(
        url=sensitive,
        name=f"Dataset at {sensitive}",
        status="error",
        error=f"Failed while reading {sensitive}",
        format="CSV",
    )
    output = tmp_path / "results.json"

    save_results([result], str(output))
    rendered = output.read_text(encoding="utf-8")
    saved = json.loads(rendered)[0]

    assert "user" not in rendered
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "private" not in rendered
    assert "SHA-256:" in saved["url"]
    assert result.url == sensitive


def test_probe_error_redacts_sensitive_url_from_dependency_exception(monkeypatch):
    from contextlib import contextmanager

    from dataset_prober import prober

    sensitive = "https://public.example/data.csv?token=secret#private"

    @contextmanager
    def fail(_url, **_kwargs):
        raise RuntimeError(f"transport failed for {sensitive}")
        yield

    monkeypatch.setattr(prober, "safe_download", fail)

    result = prober.probe_url("dataset", sensitive)

    assert result.url == sensitive
    assert "secret" not in result.error
    assert "private" not in result.error
    assert "SHA-256:" in result.error


def test_failed_dataset_error_is_safe_for_direct_console_use():
    sensitive = "https://user:password@public.example/data.csv?token=secret#private"

    result = DatasetResult.failed(
        id=sensitive,
        title=sensitive,
        source="ckan",
        source_name="CKAN",
        error=f"Failure at {sensitive}",
    )

    assert "user" not in result.error
    assert "password" not in result.error
    assert "secret" not in result.error
    assert "private" not in result.error
    assert "SHA-256:" in result.error


def test_crawler_console_redacts_urls_but_keeps_exact_internal_link(monkeypatch):
    from dataset_prober import crawler

    sensitive_base = "https://public.example/catalog?session=secret#private"
    sensitive_link = "data.csv?signature=hidden#fragment"
    safe_get = Mock(
        return_value=FakeHttpResult(
            url=sensitive_base,
            text=f'<a href="{sensitive_link}">Data</a>',
        )
    )
    monkeypatch.setattr(crawler, "safe_http_get", safe_get)

    with crawler.console.capture() as capture:
        results = crawler.crawl(sensitive_base, max_depth=0)

    rendered = capture.get()
    assert "secret" not in rendered
    assert "private" not in rendered
    assert "hidden" not in rendered
    assert "fragment" not in rendered
    assert "SHA-256:" in rendered
    assert results[0]["url"].endswith(sensitive_link)


def test_authorized_html_rejection_does_not_expose_signed_url(monkeypatch, tmp_path):
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    sensitive = "https://public.example/data.csv?signature=secret#private"
    local_path = tmp_path / "landing.csv"
    local_path.write_text("<!DOCTYPE html>\n<html><body>login</body></html>\n", encoding="utf-8")
    dataset = dataset_result(source="ckan", url=sensitive, resource_format="CSV")
    tool = CKANTool({"name": "CKAN", "base_url": "https://catalog.public.example"})
    destination = tmp_path / "datasets.duckdb"
    authorization = authorize(dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(
        base,
        "safe_download",
        fake_download(local_path, source_url=sensitive),
    )

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "failed"
    assert "secret" not in returned.error
    assert "private" not in returned.error
    assert "SHA-256:" in returned.error


def test_cbs_pagination_has_one_cumulative_download_budget(monkeypatch):
    from dataset_prober.tools import cbs_tool

    initial = "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet"
    responses = [
        FakeHttpResult(
            url=initial,
            data={"value": [{"Period": "2025"}], "@odata.nextLink": "/page-2"},
            content=b"123456",
        ),
        FakeHttpResult(
            url="https://opendata.cbs.nl/page-2",
            data={"value": [{"Period": "2026"}]},
            content=b"123456",
        ),
    ]
    monkeypatch.setattr(cbs_tool, "safe_http_get", Mock(side_effect=responses))
    monkeypatch.setattr(cbs_tool, "_MAX_ODATA_DOWNLOAD_BYTES", 10, raising=False)
    tool = cbs_tool.CBSTool({"name": "CBS"})

    with pytest.raises(cbs_tool.UnsafeResourceError, match="size limit"):
        tool._download_odata_rows(initial, timeout=1)
