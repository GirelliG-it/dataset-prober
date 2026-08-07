"""Task 2 routing contracts: source fetches must traverse the guarded transport."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dataset_prober.tools.base import DatasetResult


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


def authorize(dataset, adapter_identity, destination):
    from dataset_prober.loading_policy import LoadingPolicySession

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
    tool = ckan_tool.CKANTool({"name": "CKAN", "base_url": "https://catalog.public.example/api/3"})

    found = tool.search("resource", max_results=1)
    result = tool.fetch(found[0].id, sample_rows=3)

    assert result.status == "probed"
    assert result.download_url == resource_url
    assert safe_get.call_count == 2
    safe_fetch.assert_called_once()


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


def test_agent_tool_schema_does_not_advertise_disabled_tavily_provider():
    from types import SimpleNamespace

    from dataset_prober.dataset_agent import build_tool_definitions

    profile = SimpleNamespace(
        catalogs=[SimpleNamespace(type="ckan"), SimpleNamespace(type="tavily")]
    )

    definitions = build_tool_definitions(profile)

    for definition in definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source:
            assert "tavily" not in source["enum"]
            assert "tavily" not in source["description"].lower()
        assert "tavily" not in definition["description"].lower()


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
    assert events[1] == ("connect", str(destination.resolve()))


def test_remote_url_never_reaches_duckdb_csv_scanner(monkeypatch, tmp_path):
    from dataset_prober.tools import base

    local_path = tmp_path / "guarded.csv"
    local_path.write_text("value\n1\n", encoding="utf-8")
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [("value", "BIGINT")]
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
