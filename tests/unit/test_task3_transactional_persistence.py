"""Task 3 contracts for non-destructive, transactional DuckDB persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

import duckdb
import pandas as pd
import pytest

from dataset_prober.loading_policy import (
    AuthorizationState,
    LoadingPolicySession,
    claims_for_dataset,
    claims_for_probe,
)
from dataset_prober.prober import ProbeResult
from dataset_prober.tools.base import (
    AuthorizedDuckDBConnection,
    DatasetResult,
    load_csv_to_table,
    load_dataframe_to_table,
)
from dataset_prober.tools.guards import FetchedResource
from tests.conftest import eligible_assessment_for_candidate


def _manual_result() -> ProbeResult:
    retrieval_url = "https://example.test/authorized.csv"
    return ProbeResult(
        url=retrieval_url,
        name="Authorized CSV",
        status="ok",
        row_count=2,
        columns=[{"name": "id", "type": "BIGINT"}],
        sample=[[1], [2]],
        format="CSV",
        assessment=eligible_assessment_for_candidate(
            source_key="manual",
            adapter_identity="Manual URL",
            resource_id=retrieval_url,
            retrieval_url=retrieval_url,
        ),
    )


def _cbs_result() -> DatasetResult:
    retrieval_url = "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet"
    return DatasetResult(
        id="83583NED",
        title="CBS test table",
        description="",
        source="cbs",
        source_name="CBS Statistics Netherlands",
        url=retrieval_url,
        download_url=retrieval_url,
        format="OData",
        modified=None,
        frequency=None,
        license=None,
        license_url=None,
        row_count=2,
        columns=[{"name": "Period", "type": "VARCHAR"}],
        sample=[["2025"]],
        language="nl",
        tags=[],
        status="probed",
        assessment=eligible_assessment_for_candidate(
            source_key="cbs",
            adapter_identity="CBS Statistics Netherlands",
            resource_id="83583NED",
            retrieval_url=retrieval_url,
        ),
    )


def _authorize_probe(result: ProbeResult, destination: Path):
    session = LoadingPolicySession(download_enabled=True)
    session.register_probe_result(result)
    authorization = session.request_authorization(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id=result.url,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    assert authorization is not None
    return authorization, claims_for_probe(result, destination)


def _authorize_cbs(dataset: DatasetResult, destination: Path):
    adapter_identity = "CBS Statistics Netherlands"
    session = LoadingPolicySession(download_enabled=True)
    session.register_dataset_result(dataset, adapter_identity)
    authorization = session.request_authorization(
        source_key="cbs",
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        destination=destination,
        input_func=lambda _prompt: "yes",
    )
    assert authorization is not None
    return authorization, claims_for_dataset(dataset, adapter_identity, destination)


def _csv_resource(result: ProbeResult, path: Path) -> FetchedResource:
    return FetchedResource(
        source_url=result.url,
        final_url=result.url,
        path=str(path),
        headers={"Content-Type": "text/csv"},
    )


def _create_sentinel(destination: Path, table_name: str) -> None:
    connection = duckdb.connect(str(destination))
    try:
        connection.execute(f'CREATE TABLE "{table_name}" (sentinel VARCHAR, ordinal INTEGER)')
        connection.execute(
            f'INSERT INTO "{table_name}" VALUES (?, ?), (?, ?)',
            ["keep-first", 2, "keep-second", 1],
        )
    finally:
        connection.close()


def _sentinel_snapshot(destination: Path, table_name: str):
    connection = duckdb.connect(str(destination))
    try:
        schema = [
            (row[0], row[1]) for row in connection.execute(f'DESCRIBE "{table_name}"').fetchall()
        ]
        rows = connection.execute(f'SELECT * FROM "{table_name}" ORDER BY ordinal').fetchall()
        return schema, rows
    finally:
        connection.close()


def _persistent_tables(destination: Path) -> list[str]:
    connection = duckdb.connect(str(destination))
    try:
        connection.execute("SELECT 1").fetchone()
        return [
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
    finally:
        connection.close()


def test_csv_collision_preserves_existing_schema_and_rows(tmp_path):
    destination = tmp_path / "csv-collision.duckdb"
    csv_path = tmp_path / "different.csv"
    csv_path.write_text("new_id,new_value\n1,replacement\n", encoding="utf-8")
    result = _manual_result()
    authorization, claims = _authorize_probe(result, destination)
    _create_sentinel(destination, claims.planned_table_name)
    before = _sentinel_snapshot(destination, claims.planned_table_name)

    with pytest.raises(duckdb.CatalogException, match="already exists"):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                load_csv_to_table(connection, _csv_resource(result, csv_path))

    assert authorization.state is AuthorizationState.CONSUMED
    assert _sentinel_snapshot(destination, claims.planned_table_name) == before


def test_cbs_collision_preserves_existing_schema_and_rows(tmp_path):
    destination = tmp_path / "cbs-collision.duckdb"
    dataset = _cbs_result()
    authorization, claims = _authorize_cbs(dataset, destination)
    _create_sentinel(destination, claims.planned_table_name)
    before = _sentinel_snapshot(destination, claims.planned_table_name)

    with pytest.raises(duckdb.CatalogException, match="already exists"):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                load_dataframe_to_table(
                    connection,
                    pd.DataFrame([{"new_id": 1, "new_value": "replacement"}]),
                )

    assert authorization.state is AuthorizationState.CONSUMED
    assert _sentinel_snapshot(destination, claims.planned_table_name) == before


def test_csv_row_count_failure_rolls_back_created_table(monkeypatch, tmp_path):
    destination = tmp_path / "csv-row-count.duckdb"
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("id,value\n1,a\n2,b\n", encoding="utf-8")
    result = _manual_result()
    authorization, claims = _authorize_probe(result, destination)
    monkeypatch.setattr(
        AuthorizedDuckDBConnection,
        "_csv_row_count",
        Mock(side_effect=RuntimeError("row-count verification failed")),
    )

    with pytest.raises(RuntimeError, match="row-count verification failed"):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                load_csv_to_table(connection, _csv_resource(result, csv_path))

    assert _persistent_tables(destination) == []


def test_cbs_row_count_failure_rolls_back_created_table(monkeypatch, tmp_path):
    destination = tmp_path / "cbs-row-count.duckdb"
    dataset = _cbs_result()
    authorization, claims = _authorize_cbs(dataset, destination)
    monkeypatch.setattr(
        AuthorizedDuckDBConnection,
        "_dataframe_row_count",
        Mock(side_effect=RuntimeError("row-count verification failed")),
    )

    with pytest.raises(RuntimeError, match="row-count verification failed"):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                load_dataframe_to_table(connection, pd.DataFrame([{"Period": "2025"}]))

    assert _persistent_tables(destination) == []


def test_html_validation_failure_rolls_back_without_cleanup_table(tmp_path):
    destination = tmp_path / "html.duckdb"
    html_path = tmp_path / "landing.csv"
    html_path.write_text("<!DOCTYPE html>\n<html><body>not data</body></html>\n")
    result = _manual_result()
    authorization, claims = _authorize_probe(result, destination)

    with pytest.raises(ValueError, match="HTML page"):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                load_csv_to_table(connection, _csv_resource(result, html_path))

    assert _persistent_tables(destination) == []


def test_keyboard_interrupt_after_creation_rolls_back(tmp_path):
    destination = tmp_path / "interrupted.duckdb"
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("id,value\n1,a\n", encoding="utf-8")
    result = _manual_result()
    authorization, claims = _authorize_probe(result, destination)

    with pytest.raises(KeyboardInterrupt):
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination) as connection:
                connection._create_csv_table(_csv_resource(result, csv_path))
                raise KeyboardInterrupt()

    assert authorization.state is AuthorizationState.CONSUMED
    assert _persistent_tables(destination) == []


def test_successful_csv_and_cbs_loads_commit_expected_tables(tmp_path):
    csv_destination = tmp_path / "csv-success.duckdb"
    csv_path = tmp_path / "success.csv"
    csv_path.write_text("id,value\n1,a\n2,b\n", encoding="utf-8")
    result = _manual_result()
    csv_authorization, csv_claims = _authorize_probe(result, csv_destination)

    with csv_authorization.activate(csv_claims) as permit:
        with AuthorizedDuckDBConnection(permit, csv_destination) as connection:
            assert load_csv_to_table(connection, _csv_resource(result, csv_path)) == 2

    assert csv_authorization.state is AuthorizationState.CONSUMED
    assert _persistent_tables(csv_destination) == [csv_claims.planned_table_name]
    csv_connection = duckdb.connect(str(csv_destination))
    try:
        assert csv_connection.execute(
            f'SELECT * FROM "{csv_claims.planned_table_name}" ORDER BY id'
        ).fetchall() == [(1, "a"), (2, "b")]
    finally:
        csv_connection.close()

    cbs_destination = tmp_path / "cbs-success.duckdb"
    dataset = _cbs_result()
    cbs_authorization, cbs_claims = _authorize_cbs(dataset, cbs_destination)
    dataframe = pd.DataFrame([{"Period": "2024", "Value": 1}, {"Period": "2025", "Value": 2}])

    with cbs_authorization.activate(cbs_claims) as permit:
        with AuthorizedDuckDBConnection(permit, cbs_destination) as connection:
            assert load_dataframe_to_table(connection, dataframe) == 2

    assert cbs_authorization.state is AuthorizationState.CONSUMED
    assert _persistent_tables(cbs_destination) == [cbs_claims.planned_table_name]
    cbs_connection = duckdb.connect(str(cbs_destination))
    try:
        assert cbs_connection.execute(
            f'SELECT * FROM "{cbs_claims.planned_table_name}" ORDER BY Period'
        ).fetchall() == [("2024", 1), ("2025", 2)]
    finally:
        cbs_connection.close()


def test_unauthorized_connection_cannot_open_database_or_begin_transaction(monkeypatch, tmp_path):
    connect = Mock(side_effect=AssertionError("unauthorized access reached DuckDB"))
    monkeypatch.setattr("duckdb.connect", connect)

    with pytest.raises(TypeError, match="live active permit"):
        AuthorizedDuckDBConnection(None, tmp_path / "unauthorized.duckdb")

    connect.assert_not_called()


def test_rollback_failure_keeps_original_load_error_primary(monkeypatch, tmp_path):
    result = _manual_result()
    destination = tmp_path / "rollback-failure.duckdb"
    authorization, claims = _authorize_probe(result, destination)
    connection = Mock()
    rollback_error = RuntimeError("rollback also failed")
    connection.execute.side_effect = [None, rollback_error]
    monkeypatch.setattr("duckdb.connect", Mock(return_value=connection))

    with pytest.raises(ValueError, match="original load failure") as caught:
        with authorization.activate(claims) as permit:
            with AuthorizedDuckDBConnection(permit, destination):
                raise ValueError("original load failure")

    assert caught.value.__cause__ is rollback_error
    assert connection.execute.call_args_list == [
        call("BEGIN TRANSACTION"),
        call("ROLLBACK"),
    ]
    connection.close.assert_called_once()
    assert authorization.state is AuthorizationState.CONSUMED
