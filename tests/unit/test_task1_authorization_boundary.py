"""Behavioral contracts for the self-enforcing Task 1 load boundary."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from dataset_prober.prober import ProbeResult
from dataset_prober.resource_classification import classify_tabular_structure
from dataset_prober.tools.base import DatasetResult
from tests.conftest import eligible_assessment_for_candidate


@contextmanager
def guarded_resource(url, path="/tmp/dataset-prober-test-resource.csv", **_kwargs):
    from dataset_prober.tools.guards import FetchedResource

    yield FetchedResource(
        source_url=url,
        final_url=url,
        path=str(path),
        headers={"Content-Type": "text/csv"},
    )


def dataset_result(
    *,
    source: str = "ckan",
    dataset_id: str = "resource-a",
    url: str = "https://example.test/resource-a.csv",
    resource_format: str = "CSV",
) -> DatasetResult:
    return DatasetResult(
        id=dataset_id,
        title=f"Dataset {dataset_id}",
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
        row_count=2,
        columns=[{"name": "value", "type": "INTEGER"}],
        sample=[[1], [2]],
        language=None,
        tags=[],
        status="probed",
        assessment=classify_tabular_structure([{"name": "value", "type": "BIGINT"}], 1),
    )


def cbs_result() -> DatasetResult:
    return dataset_result(
        source="cbs",
        dataset_id="83583NED",
        url="https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet",
        resource_format="OData",
    )


def bind_dataset_assessment(dataset, adapter_identity):
    dataset.assessment = eligible_assessment_for_candidate(
        source_key=dataset.source,
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        retrieval_url=dataset.download_url,
    )
    return dataset


def authorize_dataset(session, dataset, adapter_identity, destination, response="yes"):
    bind_dataset_assessment(dataset, adapter_identity)
    session.register_dataset_result(dataset, adapter_identity)
    return session.request_authorization(
        source_key=dataset.source,
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        destination=destination,
        input_func=lambda _prompt: response,
    )


def test_disabled_session_blocks_destination_prompt_and_issuance(monkeypatch, tmp_path):
    from dataset_prober import loading_policy
    from dataset_prober.loading_policy import DownloadDisabledError, LoadingPolicySession

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=False)
    bind_dataset_assessment(dataset, "ckan:configured")
    session.register_dataset_result(dataset, "ckan:configured")
    prompt = Mock(side_effect=AssertionError("disabled session prompted"))
    destination = Mock(side_effect=AssertionError("disabled session resolved destination"))
    monkeypatch.setattr(loading_policy, "canonicalize_destination", destination)

    with pytest.raises(DownloadDisabledError):
        session.request_authorization(
            source_key="ckan",
            adapter_identity="ckan:configured",
            resource_id=dataset.id,
            destination=tmp_path / "datasets.duckdb",
            input_func=prompt,
        )

    prompt.assert_not_called()
    destination.assert_not_called()


def test_disabled_session_cannot_be_changed_into_an_enabled_session(tmp_path):
    from dataset_prober.loading_policy import DownloadDisabledError, LoadingPolicySession

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=False)
    bind_dataset_assessment(dataset, "ckan:configured")
    session.register_dataset_result(dataset, "ckan:configured")

    with pytest.raises(AttributeError):
        session.download_enabled = True

    assert session.download_enabled is False
    prompt = Mock(side_effect=AssertionError("disabled session prompted after mutation attempt"))
    with pytest.raises(DownloadDisabledError):
        session.request_authorization(
            source_key="ckan",
            adapter_identity="ckan:configured",
            resource_id=dataset.id,
            destination=tmp_path / "datasets.duckdb",
            input_func=prompt,
        )
    prompt.assert_not_called()


def test_consent_operation_shows_plan_safely_and_binds_exact_url(tmp_path):
    from dataset_prober.loading_policy import (
        AuthorizationState,
        LoadingPolicySession,
        claims_for_dataset,
    )

    exact_url = "https://alice:secret@example.test:8443/data/file.csv?token=top-secret#private"
    dataset = dataset_result(url=exact_url)
    session = LoadingPolicySession(download_enabled=True)
    bind_dataset_assessment(dataset, "configured-ckan")
    session.register_dataset_result(dataset, "configured-ckan")
    destination = tmp_path / "nested" / "datasets.duckdb"
    expected_claims = claims_for_dataset(dataset, "configured-ckan", destination)
    prompts = []

    authorization = session.request_authorization(
        source_key="ckan",
        adapter_identity="configured-ckan",
        resource_id=dataset.id,
        destination=destination,
        input_func=lambda prompt: prompts.append(prompt) or " YeS ",
    )

    assert authorization is not None
    assert authorization.state is AuthorizationState.ISSUED
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "ckan" in prompt
    assert "configured-ckan" in prompt
    assert dataset.id in prompt
    assert "CSV" in prompt
    assert "DUCKDB_CSV" in prompt
    assert str(destination.resolve()) in prompt
    assert expected_claims.planned_table_name in prompt
    assert "https://example.test:8443/data/file.csv" in prompt
    assert hashlib.sha256(exact_url.encode()).hexdigest() in prompt
    assert "alice" not in prompt
    assert "secret" not in prompt
    assert "token" not in prompt
    assert "private" not in prompt
    with authorization.activate(expected_claims):
        pass


def test_url_shaped_resource_id_is_sanitized_in_consent(tmp_path):
    from dataset_prober.loading_policy import LoadingPolicySession

    exact_url = "https://user:password@example.test/data.csv?token=secret#fragment"
    dataset = dataset_result(source="tavily", dataset_id=exact_url, url=exact_url)
    session = LoadingPolicySession(download_enabled=True)
    bind_dataset_assessment(dataset, "configured-tavily")
    session.register_dataset_result(dataset, "configured-tavily")
    prompts = []

    session.request_authorization(
        source_key="tavily",
        adapter_identity="configured-tavily",
        resource_id=exact_url,
        destination=tmp_path / "datasets.duckdb",
        input_func=lambda prompt: prompts.append(prompt) or "no",
    )

    assert len(prompts) == 1
    assert "user" not in prompts[0]
    assert "password" not in prompts[0]
    assert "token" not in prompts[0]
    assert "secret" not in prompts[0]
    assert "fragment" not in prompts[0]


@pytest.mark.parametrize(
    ("exact_url", "safe_identity"),
    [
        (
            "ftp://alice:password@example.test/data.csv?token=query-value#fragment-value",
            "ftp://example.test/data.csv",
        ),
        (
            "custom+data://alice:password@example.test/data.csv?token=query-value#fragment-value",
            "custom+data://example.test/data.csv",
        ),
        (
            "//alice:password@example.test/data.csv?token=query-value#fragment-value",
            "//example.test/data.csv",
        ),
    ],
)
def test_arbitrary_scheme_ids_and_titles_are_sanitized_in_consent(
    tmp_path, exact_url, safe_identity
):
    from dataset_prober.loading_policy import LoadingPolicySession

    dataset = dataset_result(source="tavily", dataset_id=exact_url, url=exact_url)
    dataset.title = f"Dataset at {exact_url}"
    session = LoadingPolicySession(download_enabled=True)
    bind_dataset_assessment(dataset, "configured-tavily")
    session.register_dataset_result(dataset, "configured-tavily")
    prompts = []

    session.request_authorization(
        source_key="tavily",
        adapter_identity="configured-tavily",
        resource_id=exact_url,
        destination=tmp_path / "datasets.duckdb",
        input_func=lambda prompt: prompts.append(prompt) or "no",
    )

    assert len(prompts) == 1
    prompt = prompts[0]
    assert safe_identity in prompt
    assert hashlib.sha256(exact_url.encode()).hexdigest() in prompt
    for secret in ("alice", "password", "token", "query-value", "fragment-value"):
        assert secret not in prompt


@pytest.mark.parametrize("response", ["", "n", "true", "yes please", "1"])
def test_consent_operation_only_issues_for_exact_y_or_yes(tmp_path, response):
    from dataset_prober.loading_policy import LoadingPolicySession

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=True)
    bind_dataset_assessment(dataset, "configured-ckan")
    session.register_dataset_result(dataset, "configured-ckan")

    authorization = session.request_authorization(
        source_key="ckan",
        adapter_identity="configured-ckan",
        resource_id=dataset.id,
        destination=tmp_path / "datasets.duckdb",
        input_func=lambda _prompt: response,
    )

    assert authorization is None


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt()])
def test_consent_input_exceptions_issue_nothing(tmp_path, exception):
    from dataset_prober.loading_policy import LoadingPolicySession

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=True)
    bind_dataset_assessment(dataset, "configured-ckan")
    session.register_dataset_result(dataset, "configured-ckan")

    def interrupt(_prompt):
        raise exception

    assert (
        session.request_authorization(
            source_key="ckan",
            adapter_identity="configured-ckan",
            resource_id=dataset.id,
            destination=tmp_path / "datasets.duckdb",
            input_func=interrupt,
        )
        is None
    )


def test_registration_copies_identity_and_mutation_is_rejected(tmp_path):
    from dataset_prober.loading_policy import (
        AuthorizationMismatchError,
        AuthorizationState,
        LoadingPolicySession,
        claims_for_dataset,
    )

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(
        session, dataset, "configured-ckan", tmp_path / "datasets.duckdb"
    )
    original_claims = claims_for_dataset(dataset, "configured-ckan", tmp_path / "datasets.duckdb")

    dataset.download_url = "https://attacker.test/other.csv"
    actual_claims = claims_for_dataset(dataset, "configured-ckan", tmp_path / "datasets.duckdb")

    with pytest.raises(AuthorizationMismatchError):
        with authorization.activate(actual_claims):
            raise AssertionError("mismatched authorization became active")

    assert actual_claims != original_claims
    assert authorization.state is AuthorizationState.REJECTED


@pytest.mark.parametrize(
    "change",
    [
        {"source_key": "tavily"},
        {"adapter_identity": "different-adapter"},
        {"resource_id": "other-resource"},
        {"retrieval_url": "https://example.test/other.csv"},
        {"verified_format": "JSON"},
        {"loader_kind": "CBS_ODATA"},
        {"database_path": "/different/datasets.duckdb"},
        {"planned_table_name": "different_table"},
    ],
)
def test_every_authoritative_claim_mismatch_rejects_permanently(tmp_path, change):
    from dataset_prober.loading_policy import (
        AuthorizationMismatchError,
        AuthorizationState,
        AuthorizationStateError,
        LoaderKind,
        LoadingPolicySession,
        ResourceFormat,
        claims_for_dataset,
    )

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(
        session, dataset, "configured-ckan", tmp_path / "datasets.duckdb"
    )
    correct = claims_for_dataset(dataset, "configured-ckan", tmp_path / "datasets.duckdb")
    normalized = dict(change)
    if "verified_format" in normalized:
        normalized["verified_format"] = ResourceFormat(normalized["verified_format"])
    if "loader_kind" in normalized:
        normalized["loader_kind"] = LoaderKind(normalized["loader_kind"])
    wrong = replace(correct, **normalized)

    with pytest.raises(AuthorizationMismatchError):
        with authorization.activate(wrong):
            pass

    assert authorization.state is AuthorizationState.REJECTED
    with pytest.raises(AuthorizationStateError):
        with authorization.activate(correct):
            pass


def test_authorization_is_one_shot_after_success_and_failure(tmp_path):
    from dataset_prober.loading_policy import (
        AuthorizationState,
        AuthorizationStateError,
        LoadingPolicySession,
        claims_for_dataset,
    )

    for should_fail in (False, True):
        dataset = dataset_result(dataset_id=f"resource-{should_fail}")
        session = LoadingPolicySession(download_enabled=True)
        authorization = authorize_dataset(
            session, dataset, "configured-ckan", tmp_path / f"{should_fail}.duckdb"
        )
        claims = claims_for_dataset(dataset, "configured-ckan", tmp_path / f"{should_fail}.duckdb")
        with pytest.raises(RuntimeError) if should_fail else _does_not_raise():
            with authorization.activate(claims):
                if should_fail:
                    raise RuntimeError("load failed")
        assert authorization.state is AuthorizationState.CONSUMED
        with pytest.raises(AuthorizationStateError):
            with authorization.activate(claims):
                pass


class _does_not_raise:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_concurrent_activation_allows_exactly_one_attempt(tmp_path):
    from dataset_prober.loading_policy import (
        AuthorizationStateError,
        LoadingPolicySession,
        claims_for_dataset,
    )

    dataset = dataset_result()
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(
        session, dataset, "configured-ckan", tmp_path / "datasets.duckdb"
    )
    claims = claims_for_dataset(dataset, "configured-ckan", tmp_path / "datasets.duckdb")
    active = threading.Event()
    release = threading.Event()
    outcomes = []

    def first():
        with authorization.activate(claims):
            active.set()
            release.wait(timeout=2)
            outcomes.append("active")

    thread = threading.Thread(target=first)
    thread.start()
    assert active.wait(timeout=2)
    with pytest.raises(AuthorizationStateError):
        with authorization.activate(claims):
            pass
    release.set()
    thread.join(timeout=2)

    assert outcomes == ["active"]


def test_direct_manual_writer_rejects_before_retrieval_or_connect(monkeypatch, tmp_path):
    from dataset_prober import prober

    result = ProbeResult(
        url="https://example.test/data.csv",
        name="data",
        status="ok",
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
        assessment=classify_tabular_structure([{"name": "value", "type": "BIGINT"}], 1),
    )
    retrieval = Mock(side_effect=AssertionError("unauthorized retrieval"))
    connect = Mock(side_effect=AssertionError("unauthorized connect"))
    monkeypatch.setattr(prober, "safe_download", retrieval)
    monkeypatch.setattr("duckdb.connect", connect)

    with pytest.raises(Exception):
        prober.download_to_duckdb(result, tmp_path / "datasets.duckdb", None)

    retrieval.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("tool_name", ["ckan", "tavily", "cbs"])
def test_direct_adapter_writer_rejects_before_side_effects(monkeypatch, tmp_path, tool_name):
    from dataset_prober.tools.cbs_tool import CBSTool
    from dataset_prober.tools.ckan_tool import CKANTool
    from dataset_prober.tools.tavily_tool import TavilyTool

    if tool_name == "cbs":
        tool = CBSTool({"name": "CBS", "download_timeout_seconds": 1})
        dataset = cbs_result()
        retrieval = Mock(side_effect=AssertionError("unauthorized CBS retrieval"))
        monkeypatch.setattr(tool, "_download_odata_rows", retrieval)
    elif tool_name == "ckan":
        tool = CKANTool({"name": "CKAN", "base_url": "https://catalog.test"})
        dataset = dataset_result(source="ckan")
        retrieval = Mock(side_effect=AssertionError("unauthorized CSV retrieval"))
        monkeypatch.setattr("dataset_prober.tools.base.safe_download", retrieval)
    else:
        tool = TavilyTool({"name": "Tavily", "blocked_sources": []})
        dataset = dataset_result(source="tavily")
        retrieval = Mock(side_effect=AssertionError("unauthorized CSV retrieval"))
        monkeypatch.setattr("dataset_prober.tools.base.safe_download", retrieval)
    connect = Mock(side_effect=AssertionError("unauthorized connect"))
    monkeypatch.setattr("duckdb.connect", connect)

    with pytest.raises(Exception):
        tool.download(dataset, tmp_path / "datasets.duckdb", None)

    retrieval.assert_not_called()
    connect.assert_not_called()


def test_raw_duckdb_connection_is_rejected_by_persistent_sql_writers():
    from dataset_prober.tools.base import (
        _reject_if_html,
        load_csv_to_table,
        load_dataframe_to_table,
    )
    from dataset_prober.tools.guards import FetchedResource

    raw_connection = Mock()
    fetched = FetchedResource(
        source_url="https://example.test/data.csv",
        final_url="https://example.test/data.csv",
        path="/tmp/data.csv",
        headers={"Content-Type": "text/csv"},
    )
    with pytest.raises(TypeError):
        load_csv_to_table(raw_connection, fetched)
    with pytest.raises(TypeError):
        _reject_if_html(raw_connection)
    with pytest.raises(TypeError):
        load_dataframe_to_table(raw_connection, [])
    raw_connection.execute.assert_not_called()


def test_authorized_connection_has_no_raw_or_arbitrary_sql_escape(monkeypatch, tmp_path):
    from dataset_prober.loading_policy import LoadingPolicySession, claims_for_dataset
    from dataset_prober.tools.base import AuthorizedDuckDBConnection

    dataset = dataset_result()
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, "configured-ckan", destination)
    claims = claims_for_dataset(dataset, "configured-ckan", destination)
    raw_connection = Mock()
    monkeypatch.setattr("duckdb.connect", Mock(return_value=raw_connection))

    with authorization.activate(claims) as permit:
        with AuthorizedDuckDBConnection(permit, destination) as connection:
            assert not hasattr(connection, "connection")
            assert not hasattr(connection, "raw_connection")
            assert not hasattr(connection, "_connection")
            assert not hasattr(connection, "_read")
            assert not hasattr(connection, "_write")

            with pytest.raises(AttributeError):
                connection._read("DROP TABLE unrelated")
            with pytest.raises(AttributeError):
                connection._write("CREATE TABLE unrelated AS SELECT 1")

    assert raw_connection.execute.call_args_list == [
        call("BEGIN TRANSACTION"),
        call("COMMIT"),
    ]


@pytest.mark.parametrize("method_name", ["_create_csv_table", "_create_dataframe_table"])
def test_authorization_for_table_a_cannot_target_table_b(monkeypatch, tmp_path, method_name):
    from dataset_prober.loading_policy import LoadingPolicySession, claims_for_dataset
    from dataset_prober.tools.base import AuthorizedDuckDBConnection

    dataset = dataset_result()
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, "configured-ckan", destination)
    claims = claims_for_dataset(dataset, "configured-ckan", destination)
    assert claims.planned_table_name != "table_b"
    raw_connection = Mock()
    monkeypatch.setattr("duckdb.connect", Mock(return_value=raw_connection))

    with authorization.activate(claims) as permit:
        with AuthorizedDuckDBConnection(permit, destination) as connection:
            with pytest.raises(TypeError):
                getattr(connection, method_name)("table_b")

    assert raw_connection.execute.call_args_list == [
        call("BEGIN TRANSACTION"),
        call("COMMIT"),
    ]


def test_authorized_connection_exposes_no_drop_operation():
    from dataset_prober.tools.base import AuthorizedDuckDBConnection

    assert not hasattr(AuthorizedDuckDBConnection, "_drop_csv_table")


def test_destination_normalization_and_table_derivation_are_shared(tmp_path):
    from dataset_prober.loading_policy import (
        LoadingPolicySession,
        claims_for_dataset,
        configured_adapter_identity,
    )

    dataset = dataset_result()
    adapter_identity = configured_adapter_identity("ckan", {"name": "Configured CKAN"})
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(
        session, dataset, adapter_identity, tmp_path / "nested" / ".." / "datasets.duckdb"
    )
    writer_claims = claims_for_dataset(dataset, adapter_identity, tmp_path / "datasets.duckdb")

    with authorization.activate(writer_claims):
        pass
    assert "example" not in writer_claims.planned_table_name
    assert "resource_a" not in writer_claims.planned_table_name
    assert len(writer_claims.planned_table_name) <= 63


def test_configured_adapter_identity_distinguishes_catalog_endpoints():
    from dataset_prober.loading_policy import configured_adapter_identity

    first = configured_adapter_identity(
        "ckan", {"name": "Catalog", "base_url": "https://one.example/api/"}
    )
    second = configured_adapter_identity(
        "ckan", {"name": "Catalog", "base_url": "https://two.example/api"}
    )

    assert first != second


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/DATA.CSV", "CSV"),
        ("https://example.test/data.csv?signature=abc", "CSV"),
        ("https://example.test/data.csv#section", "CSV"),
        ("https://example.test/data%2Ecsv", "CSV"),
        ("https://example.test/download?file=data.csv", None),
        ("https://example.test/download#data.csv", None),
        ("https://example.test/download", None),
    ],
)
def test_url_format_evidence_uses_only_the_decoded_path(url, expected):
    from dataset_prober.loading_policy import detect_resource_format

    assert detect_resource_format(url) == expected


def _probe_adapter_resource(tool, source, url):
    if source == "ckan":
        result = dataset_result(source=source, url=url)
        result.status = "found"
        return tool._probe_csv(result, sample_rows=3, timeout=1)
    return tool._probe_direct(url, sample_rows=3, timeout=1)


def _probe_adapter_tool(source):
    from dataset_prober.tools.ckan_tool import CKANTool
    from dataset_prober.tools.tavily_tool import TavilyTool

    if source == "ckan":
        return CKANTool(
            {
                "name": "Configured CKAN",
                "base_url": "https://catalog.test/api/3",
                "ckan_dialect": "ckan_action",
                "landing_base_url": "https://catalog.test",
            }
        )
    return TavilyTool({"name": "Configured Tavily", "blocked_sources": []})


@pytest.mark.parametrize(
    ("source", "probe_fails"),
    [
        ("ckan", False),
        ("ckan", True),
        ("tavily", False),
        ("tavily", True),
    ],
)
def test_adapter_probe_connection_closes_after_success_or_failure(monkeypatch, source, probe_fails):
    connection = Mock()
    connect = Mock(return_value=connection)
    download = Mock(side_effect=guarded_resource)
    probe = Mock(
        side_effect=RuntimeError("probe failed") if probe_fails else None,
        return_value={
            "columns": [{"name": "value", "type": "INTEGER"}],
            "sample": [[1]],
            "row_count": 1,
            "assessment": classify_tabular_structure([{"name": "value", "type": "BIGINT"}], 1),
        },
    )
    monkeypatch.setattr("duckdb.connect", connect)
    monkeypatch.setattr(f"dataset_prober.tools.{source}_tool.safe_download", download)
    monkeypatch.setattr(f"dataset_prober.tools.{source}_tool.inspect_csv_resource", probe)
    tool = _probe_adapter_tool(source)

    result = _probe_adapter_resource(tool, source, "https://example.test/data.csv")

    expected_status = "failed" if probe_fails else "probed"
    assert result.status == expected_status
    connect.assert_called_once_with()
    download.assert_called_once()
    probe.assert_called_once()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_adapter_probe_connection_closes_when_keyboard_interrupt_propagates(monkeypatch, source):
    connection = Mock()
    monkeypatch.setattr("duckdb.connect", Mock(return_value=connection))
    monkeypatch.setattr(
        f"dataset_prober.tools.{source}_tool.safe_download",
        Mock(side_effect=guarded_resource),
    )
    monkeypatch.setattr(
        f"dataset_prober.tools.{source}_tool.inspect_csv_resource",
        Mock(side_effect=KeyboardInterrupt()),
    )
    tool = _probe_adapter_tool(source)

    with pytest.raises(KeyboardInterrupt):
        _probe_adapter_resource(tool, source, "https://example.test/data.csv")

    connection.close.assert_called_once_with()


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_adapter_probe_connection_creation_failure_does_not_attempt_cleanup(monkeypatch, source):
    connect = Mock(side_effect=RuntimeError("connect failed"))
    download = Mock(side_effect=guarded_resource)
    probe = Mock(side_effect=AssertionError("probe ran without a connection"))
    monkeypatch.setattr("duckdb.connect", connect)
    monkeypatch.setattr(f"dataset_prober.tools.{source}_tool.safe_download", download)
    monkeypatch.setattr(f"dataset_prober.tools.{source}_tool.inspect_csv_resource", probe)
    tool = _probe_adapter_tool(source)

    result = _probe_adapter_resource(tool, source, "https://example.test/data.csv")

    assert result.status != "probed"
    connect.assert_called_once_with()
    download.assert_called_once()
    probe.assert_not_called()


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_failed_real_adapter_inspection_cannot_authorize_or_persist(monkeypatch, tmp_path, source):
    from dataset_prober.dataset_agent import execute_tool
    from dataset_prober.loading_policy import LoadingPolicySession
    from dataset_prober.paths import AppPaths

    resource_url = "https://example.test/data.csv"
    tool = _probe_adapter_tool(source)
    if source == "ckan":
        response = Mock()
        response.json.return_value = {
            "success": True,
            "result": {
                "name": "resource-a",
                "title": "Resource A",
                "resources": [{"format": "CSV", "url": resource_url}],
            },
        }
        monkeypatch.setattr(
            "dataset_prober.tools.ckan_tool.safe_http_get",
            Mock(return_value=response),
        )
        resource_id = "resource-a"
    else:
        resource_id = resource_url

    connection = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr("duckdb.connect", connect)
    monkeypatch.setattr(
        f"dataset_prober.tools.{source}_tool.safe_download",
        Mock(side_effect=guarded_resource),
    )
    monkeypatch.setattr(
        f"dataset_prober.tools.{source}_tool.inspect_csv_resource",
        Mock(side_effect=RuntimeError("probe failed")),
    )
    load = Mock(side_effect=AssertionError("failed inspection reached persistent loading"))
    monkeypatch.setattr(tool, "download", load)
    session = LoadingPolicySession(download_enabled=True)
    authorization = Mock(side_effect=AssertionError("failed inspection requested consent"))
    monkeypatch.setattr(session, "request_authorization", authorization)
    found_datasets = []
    budget = Mock()
    budget.can_probe.return_value = True
    budget.probes_used = 0
    paths = AppPaths(output_dir=tmp_path / "output")

    inspected = execute_tool(
        tool_name="fetch_dataset",
        tool_input={"source": source, "dataset_id": resource_id, "sample_rows": 3},
        tool_map={source: tool},
        budget=budget,
        profile=Mock(),
        loading_session=session,
        found_datasets=found_datasets,
        session_cost=Mock(),
        paths=paths,
    )
    denied = execute_tool(
        tool_name="download_dataset",
        tool_input={"source": source, "dataset_id": resource_id, "title": "Resource A"},
        tool_map={source: tool},
        budget=budget,
        profile=Mock(),
        loading_session=session,
        found_datasets=found_datasets,
        session_cost=Mock(),
        paths=paths,
    )

    assert inspected["status"] != "probed"
    assert "has not passed inspection" in denied["error"]
    assert len(found_datasets) == 1
    assert found_datasets[0].assessment.load_eligible is False
    assert found_datasets[0].assessment.reason.value == "inspection_failed"
    authorization.assert_not_called()
    load.assert_not_called()
    connect.assert_called_once_with()
    connection.close.assert_called_once_with()
    assert not paths.output_dir.exists()


@pytest.mark.parametrize("source", ["ckan", "tavily"])
def test_real_csv_adapters_complete_one_authorized_load(monkeypatch, tmp_path, source):
    import duckdb

    from dataset_prober.loading_policy import LoadingPolicySession, claims_for_dataset
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool
    from dataset_prober.tools.tavily_tool import TavilyTool

    csv_path = tmp_path / f"{source}.csv"
    csv_path.write_text("id,value\n1,a\n2,b\n")
    dataset = dataset_result(source=source, url=str(csv_path))
    tool = (
        CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
        if source == "ckan"
        else TavilyTool({"name": "Configured Tavily", "blocked_sources": []})
    )
    destination = tmp_path / f"{source}.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    claims = claims_for_dataset(dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(
        base,
        "safe_download",
        lambda url, **_kwargs: guarded_resource(url, csv_path),
    )

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "downloaded"
    assert returned.row_count == 2
    connection = duckdb.connect(str(destination))
    assert (
        connection.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?",
            [claims.planned_table_name],
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_real_cbs_adapter_completes_one_authorized_load(monkeypatch, tmp_path):
    import duckdb

    from dataset_prober.loading_policy import LoadingPolicySession, claims_for_dataset
    from dataset_prober.tools.cbs_tool import CBSTool

    dataset = cbs_result()
    tool = CBSTool({"name": "Configured CBS"})
    destination = tmp_path / "cbs.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    claims = claims_for_dataset(dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(
        tool,
        "_download_odata_rows",
        lambda _url, timeout: [{"Period": "2025", "Value": 3}],
    )

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "downloaded"
    assert returned.row_count == 1
    connection = duckdb.connect(str(destination))
    assert (
        connection.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?",
            [claims.planned_table_name],
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_shared_csv_loader_rejects_without_live_permit(monkeypatch, tmp_path):
    from dataset_prober.tools import base

    dataset = dataset_result()
    retrieval = Mock(side_effect=AssertionError("unauthorized retrieval"))
    monkeypatch.setattr(base, "safe_download", retrieval)

    with pytest.raises(TypeError):
        base.download_csv_dataset(dataset, "configured-ckan", tmp_path / "datasets.duckdb", None)

    retrieval.assert_not_called()


def test_writer_reconstructs_mutated_resource_and_rejects_before_side_effects(
    monkeypatch, tmp_path
):
    from dataset_prober.loading_policy import (
        AuthorizationMismatchError,
        AuthorizationState,
        LoadingPolicySession,
    )
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    dataset = dataset_result(source="ckan")
    tool = CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    dataset.download_url = "https://attacker.test/replacement.csv"
    retrieval = Mock(side_effect=AssertionError("mismatch reached retrieval"))
    connect = Mock(side_effect=AssertionError("mismatch reached connect"))
    monkeypatch.setattr(base, "safe_download", retrieval)
    monkeypatch.setattr("duckdb.connect", connect)

    with pytest.raises(AuthorizationMismatchError):
        tool.download(dataset, destination, authorization)

    assert authorization.state is AuthorizationState.REJECTED
    retrieval.assert_not_called()
    connect.assert_not_called()


def test_writer_rejects_destination_changed_after_consent_before_creation(monkeypatch, tmp_path):
    from dataset_prober.loading_policy import (
        AuthorizationMismatchError,
        AuthorizationState,
        LoadingPolicySession,
    )
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    dataset = dataset_result(source="ckan")
    tool = CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
    approved = tmp_path / "approved" / "datasets.duckdb"
    changed = tmp_path / "changed" / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, approved)
    retrieval = Mock(side_effect=AssertionError("wrong destination reached retrieval"))
    connect = Mock(side_effect=AssertionError("wrong destination reached connect"))
    monkeypatch.setattr(base, "safe_download", retrieval)
    monkeypatch.setattr("duckdb.connect", connect)

    with pytest.raises(AuthorizationMismatchError):
        tool.download(dataset, changed, authorization)

    assert authorization.state is AuthorizationState.REJECTED
    assert not changed.parent.exists()
    retrieval.assert_not_called()
    connect.assert_not_called()


def test_retrieval_exception_consumes_authorization(monkeypatch, tmp_path):
    from dataset_prober.loading_policy import AuthorizationState, LoadingPolicySession
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    dataset = dataset_result(source="ckan")
    tool = CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(base, "safe_download", Mock(side_effect=RuntimeError("retrieval")))

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "failed"
    assert authorization.state is AuthorizationState.CONSUMED


def test_connection_failure_consumes_authorization(monkeypatch, tmp_path):
    from dataset_prober.loading_policy import AuthorizationState, LoadingPolicySession
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    dataset = dataset_result(source="ckan")
    tool = CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    monkeypatch.setattr(
        base,
        "safe_download",
        lambda url, **_kwargs: guarded_resource(url),
    )
    monkeypatch.setattr("duckdb.connect", Mock(side_effect=RuntimeError("connect")))

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "failed"
    assert authorization.state is AuthorizationState.CONSUMED


def test_persistent_sql_failure_closes_connection_and_consumes(monkeypatch, tmp_path):
    from dataset_prober.loading_policy import AuthorizationState, LoadingPolicySession
    from dataset_prober.tools import base
    from dataset_prober.tools.ckan_tool import CKANTool

    dataset = dataset_result(source="ckan")
    tool = CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    authorization = authorize_dataset(session, dataset, tool.adapter_identity, destination)
    connection = Mock()
    connection.execute.side_effect = [None, RuntimeError("CTAS failed"), None]
    csv_path = tmp_path / "guarded.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    monkeypatch.setattr(
        base,
        "safe_download",
        lambda url, **_kwargs: guarded_resource(url, csv_path),
    )
    monkeypatch.setattr(base, "csv_scan_expr", lambda _connection, _url: "read_csv_auto(?)")
    monkeypatch.setattr(
        base,
        "require_eligible_csv_payload",
        Mock(
            return_value={
                "assessment": classify_tabular_structure([{"name": "value", "type": "BIGINT"}], 1)
            }
        ),
    )
    monkeypatch.setattr("duckdb.connect", Mock(return_value=connection))

    returned = tool.download(dataset, destination, authorization)

    assert returned.status == "failed"
    assert authorization.state is AuthorizationState.CONSUMED
    assert connection.execute.call_args_list[0] == call("BEGIN TRANSACTION")
    assert connection.execute.call_args_list[1].args[0].startswith("CREATE TABLE ")
    assert "OR REPLACE" not in connection.execute.call_args_list[1].args[0]
    assert connection.execute.call_args_list[2] == call("ROLLBACK")
    connection.close.assert_called_once()


def test_keyboard_interrupt_consumes_before_propagating(monkeypatch, tmp_path):
    from dataset_prober import prober
    from dataset_prober.loading_policy import AuthorizationState, LoadingPolicySession

    retrieval_url = "https://example.test/data.csv"
    result = ProbeResult(
        url=retrieval_url,
        name="data",
        status="ok",
        columns=[{"name": "value", "type": "INTEGER"}],
        format="CSV",
        assessment=eligible_assessment_for_candidate(
            source_key="manual",
            adapter_identity="Manual URL",
            resource_id=retrieval_url,
            retrieval_url=retrieval_url,
        ),
    )
    destination = tmp_path / "datasets.duckdb"
    session = LoadingPolicySession(download_enabled=True)
    session.register_probe_result(result)
    authorization = session.request_authorization(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id=result.url,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    monkeypatch.setattr(prober, "safe_download", Mock(side_effect=KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        prober.download_to_duckdb(result, str(destination), authorization)

    assert authorization.state is AuthorizationState.CONSUMED


@pytest.mark.parametrize(
    ("source", "resource_format", "url"),
    [
        ("ckan", "CSV/ZIP", "https://example.test/archive.csv.zip"),
        ("ckan", "CSV", "https://example.test/extensionless"),
        ("tavily", "CSV", "https://example.test/extensionless"),
        ("tavily", "JSON", "https://example.test/data.json"),
        ("tavily", "GEOJSON", "https://example.test/data.geojson"),
        ("tavily", "XLS", "https://example.test/data.xls"),
        ("tavily", "XLSX", "https://example.test/data.xlsx"),
        ("tavily", "PARQUET", "https://example.test/data.parquet"),
    ],
)
def test_real_adapter_format_admission_denies_before_probe_or_load(
    monkeypatch, source, resource_format, url
):
    from dataset_prober.loading_policy import InspectedResourceError, LoadingPolicySession
    from dataset_prober.tools.ckan_tool import CKANTool
    from dataset_prober.tools.tavily_tool import TavilyTool

    dataset = dataset_result(source=source, url=url, resource_format=resource_format)
    probe = Mock(side_effect=AssertionError("unsupported resource reached CSV probe"))
    connect = Mock(side_effect=AssertionError("unsupported resource reached DuckDB"))
    monkeypatch.setattr("dataset_prober.tools.ckan_tool.inspect_csv_resource", probe)
    monkeypatch.setattr("dataset_prober.tools.tavily_tool.inspect_csv_resource", probe)
    monkeypatch.setattr("duckdb.connect", connect)
    tool = (
        CKANTool({"name": "Configured CKAN", "base_url": "https://catalog.test"})
        if source == "ckan"
        else TavilyTool({"name": "Configured Tavily", "blocked_sources": []})
    )
    probed = (
        tool._probe_csv(dataset, sample_rows=3, timeout=1)
        if source == "ckan"
        else tool._probe_direct(url, sample_rows=3, timeout=1)
    )

    assert probed.status != "probed"
    with pytest.raises(InspectedResourceError):
        LoadingPolicySession(download_enabled=True).register_dataset_result(
            probed, tool.adapter_identity
        )
    probe.assert_not_called()
    connect.assert_not_called()
