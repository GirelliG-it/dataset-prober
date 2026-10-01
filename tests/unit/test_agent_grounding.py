"""Offline tests for agent-result grounding."""

from datetime import date
from unittest.mock import Mock

import pytest

from dataset_prober.dataset_agent import Budget, SessionCost, build_tool_definitions, execute_tool
from dataset_prober.loading_policy import LoadingPolicySession
from dataset_prober.paths import AppPaths


def test_freshness_uses_fetched_date(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
):

    # 1. Arrange fetched evidence.
    candidate = sample_dataset_result_ok
    candidate.modified = "2000-01-01"

    original_assessment = candidate.assessment
    original_eligibility = candidate.assessment.load_eligible

    # 2. Represent the candidates already returned by an adapter.
    found_datasets = [candidate]

    # 3. Arrange the exact model input contract.
    tool_input = {
        "source": candidate.source,
        "dataset_id": candidate.id,
        "max_days_old": 30,
    }

    # Supply these using existing executor-test patterns.
    result = execute_tool(
        tool_name="check_freshness",
        tool_input=tool_input,
        tool_map={candidate.source: Mock(spec=[])},  # Fake adapter mapping; no network.
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),  # Loading disabled,
        found_datasets=found_datasets,
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),  # Temporary test paths.
    )

    # 4. Assert that fetched metadata wins.
    assert result["last_updated"] == "2000-01-01"
    assert result["passes"] is False
    assert result["reason_code"] == "freshness_fail"

    # 5. Assert that freshness does not change verification.
    assert candidate.assessment == original_assessment
    assert candidate.assessment.load_eligible == original_eligibility


def test_freshness_rejects_ambiguous_candidates(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
):
    candidate = sample_dataset_result_ok

    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": candidate.source,
            "dataset_id": candidate.id,
            "max_days_old": 30,
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate, candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["reason_code"] == "candidate_identity_ambiguous"


def test_freshness_rejects_wrong_source(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
):
    candidate = sample_dataset_result_ok

    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": "ckan",
            "dataset_id": candidate.id,
            "max_days_old": 30,
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["reason_code"] == "candidate_not_inspected"


def test_freshness_rejects_unknown_dataset_id(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
):
    candidate = sample_dataset_result_ok

    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": candidate.source,
            "dataset_id": "invented-dataset-id",
            "max_days_old": 30,
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["reason_code"] == "candidate_not_inspected"


def test_freshness_schema_requires_identity_not_model_date(test_profile):
    # Schema construction only needs the available source names.
    resolved_profile = Mock(spec=["source_keys"])
    resolved_profile.source_keys = ("cbs",)
    budget = Budget.from_profile(test_profile.budget)

    definitions = build_tool_definitions(resolved_profile, budget)

    freshness_tool = next(tool for tool in definitions if tool["name"] == "check_freshness")
    schema = freshness_tool["input_schema"]

    assert set(schema["required"]) == {
        "source",
        "dataset_id",
        "max_days_old",
    }
    assert set(schema["properties"]) == {
        "source",
        "dataset_id",
        "max_days_old",
    }
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("modified", [None, "", "not-a-date"])
def test_freshness_returns_unknown_when_fetched_date_is_unusable(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
    modified,
):
    candidate = sample_dataset_result_ok
    candidate.modified = modified

    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": candidate.source,
            "dataset_id": candidate.id,
            "max_days_old": 30,
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["days_old"] is None
    assert result["reason_code"] == "freshness_unknown"


def test_freshness_pass_does_not_upgrade_report_only_candidate(
    sample_dataset_result_failed,
    test_profile,
    tmp_path,
):
    candidate = sample_dataset_result_failed
    candidate.modified = date.today().isoformat()

    original_assessment = candidate.assessment
    original_status = candidate.status
    assert candidate.assessment.load_eligible is False

    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": candidate.source,
            "dataset_id": candidate.id,
            "max_days_old": 30,
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is True
    assert result["reason_code"] == "freshness_pass"
    assert candidate.assessment == original_assessment
    assert candidate.assessment.load_eligible is False
    assert candidate.status == original_status


def test_freshness_rejects_model_supplied_last_updated(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
):
    candidate = sample_dataset_result_ok
    candidate.modified = "2000-01-01"

    original_assessment = candidate.assessment
    original_eligibility = candidate.assessment.load_eligible

    # An adversarial or legacy model date claiming the dataset is fresh.
    result = execute_tool(
        tool_name="check_freshness",
        tool_input={
            "source": candidate.source,
            "dataset_id": candidate.id,
            "max_days_old": 30,
            "last_updated": date.today().isoformat(),
        },
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["reason_code"] == "unexpected_tool_input"
    assert "last_updated" in result["reason"]
    assert result["reason"] != result["reason_code"]
    assert "last_updated" not in result
    assert "days_old" not in result
    assert candidate.assessment == original_assessment
    assert candidate.assessment.load_eligible == original_eligibility


@pytest.mark.parametrize(
    ("drop", "extra", "named_key"),
    [
        ("source", {}, "source"),
        ("dataset_id", {}, "dataset_id"),
        ("max_days_old", {}, "max_days_old"),
        (None, {"note": "fresh"}, "note"),
        ("all", {}, "dataset_id"),
    ],
)
def test_freshness_rejects_inexact_input_keys(
    sample_dataset_result_ok,
    test_profile,
    tmp_path,
    drop,
    extra,
    named_key,
):
    candidate = sample_dataset_result_ok
    candidate.modified = date.today().isoformat()

    tool_input = {
        "source": candidate.source,
        "dataset_id": candidate.id,
        "max_days_old": 30,
    }
    if drop == "all":
        tool_input = {}
    elif drop is not None:
        del tool_input[drop]
    tool_input.update(extra)

    result = execute_tool(
        tool_name="check_freshness",
        tool_input=tool_input,
        tool_map={candidate.source: Mock(spec=[])},
        budget=Budget.from_profile(test_profile.budget),
        profile=test_profile,
        loading_session=LoadingPolicySession(download_enabled=False),
        found_datasets=[candidate],
        session_cost=SessionCost(),
        paths=AppPaths(output_dir=tmp_path),
    )

    assert result["passes"] is None
    assert result["reason_code"] == "unexpected_tool_input"
    assert named_key in result["reason"]
