"""Behavioral examples for the first monitoring contract, without HTTP or ingestion."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import permutations

import pytest

from dataset_prober.monitoring import (
    Availability,
    ChangeStatus,
    CheckStatus,
    Comparison,
    Evidence,
    EvidenceComparison,
    EvidenceKind,
    Observation,
    compare_observation,
)


def evidence(value="2026-09-14T09:00:00Z", kind=EvidenceKind.MODIFIED, **kwargs):
    return Evidence(
        kind, "https://example.test/TableInfos", "1", Availability.PRESENT, value, **kwargs
    )


def observation(day, *items, **kwargs):
    return Observation(
        str(day),
        "cbs:example",
        datetime(2026, 9, day, tzinfo=UTC),
        kwargs.pop("check_status", CheckStatus.SUCCEEDED),
        tuple(items),
        **kwargs,
    )


def test_first_observation_establishes_baseline():
    result = compare_observation(observation(14, evidence()), [])
    assert result.change_status is ChangeStatus.BASELINE
    assert result.baseline_observation_id is None


def test_timeout_is_retained_but_wednesday_compares_against_monday():
    monday = observation(14, evidence())
    tuesday = observation(15, check_status=CheckStatus.FAILED, failure_reason="timeout")
    history = [tuesday, monday]
    assert compare_observation(tuesday, [monday]).change_status is ChangeStatus.UNKNOWN
    result = compare_observation(observation(16, evidence()), history)
    assert result.baseline_observation_id == monday.observation_id
    assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert history == [tuesday, monday]


@pytest.mark.parametrize("availability", list(Availability)[1:])
def test_missing_evidence_is_unknown(availability):
    missing = replace(evidence(), value=None, availability=availability)
    result = compare_observation(observation(16, missing), [observation(14, evidence())])
    assert result.change_status is ChangeStatus.UNKNOWN
    assert compare_observation(observation(16, missing), []).change_status is ChangeStatus.UNKNOWN


def test_within_day_timestamp_change_is_not_truncated():
    result = compare_observation(
        observation(16, evidence("2026-09-14T10:00:00Z")),
        [observation(14, evidence())],
    )
    assert result.change_status is ChangeStatus.POSSIBLY_UPDATED
    assert result.evidence[0].changed


def test_title_change_is_reported_without_claiming_content_change():
    result = compare_observation(
        observation(16, evidence("New title", EvidenceKind.TITLE)),
        [observation(14, evidence("Old title", EvidenceKind.TITLE))],
    )
    assert result.change_status is ChangeStatus.UNKNOWN
    assert result.evidence[0].changed
    assert result.evidence[0].kind is EvidenceKind.TITLE


@pytest.mark.parametrize(
    "overrides", [{"resource": "https://example.test/catalog"}, {"rule_version": "2"}]
)
def test_different_endpoints_or_normalization_rules_are_not_compared(overrides):
    result = compare_observation(
        observation(16, replace(evidence(), **overrides)),
        [observation(14, evidence())],
    )
    assert result.change_status is ChangeStatus.UNKNOWN
    assert not result.evidence


def test_lost_timestamp_is_not_hidden_by_unchanged_revision():
    revision = evidence("r1", EvidenceKind.REVISION)
    result = compare_observation(
        observation(16, revision),
        [observation(14, evidence(), revision)],
    )
    assert result.change_status is ChangeStatus.UNKNOWN


def test_changed_revision_is_useful_even_when_timestamp_disappears():
    result = compare_observation(
        observation(16, evidence("r2", EvidenceKind.REVISION)),
        [observation(14, evidence(), evidence("r1", EvidenceKind.REVISION))],
    )
    assert result.change_status is ChangeStatus.POSSIBLY_UPDATED


def test_new_revision_cannot_be_compared_retroactively():
    result = compare_observation(
        observation(16, evidence("r1", EvidenceKind.REVISION)),
        [observation(14, evidence())],
    )
    assert result.change_status is ChangeStatus.UNKNOWN


def test_latest_success_with_missing_evidence_is_not_skipped():
    result = compare_observation(
        observation(16, evidence()),
        [observation(14, evidence()), observation(15)],
    )
    assert result.baseline_observation_id == "15"
    assert result.change_status is ChangeStatus.UNKNOWN


def test_baseline_excludes_other_sources_future_and_incompatible_versions():
    monday = observation(14, evidence())
    history = [
        replace(observation(15, evidence()), source_id="other"),
        observation(17, evidence()),
        observation(15, evidence(), contract_version="other"),
        observation(15, evidence(), checker_version="other"),
        monday,
    ]
    assert compare_observation(observation(16, evidence()), history).baseline_observation_id == "14"


def test_equal_time_baselines_are_ambiguous():
    first = observation(14, evidence())
    second = replace(first, observation_id="second")
    result = compare_observation(observation(16, evidence()), [first, second])
    assert result.change_status is ChangeStatus.UNKNOWN
    assert result.baseline_observation_id is None


def test_blocked_check_cannot_produce_a_change_conclusion():
    blocked = observation(
        16, evidence("different"), check_status=CheckStatus.BLOCKED, failure_reason="budget"
    )
    assert (
        compare_observation(blocked, [observation(14, evidence())]).change_status
        is ChangeStatus.UNKNOWN
    )


def test_naive_time_and_duplicate_evidence_are_rejected():
    with pytest.raises(ValueError, match="timezone"):
        replace(observation(14), checked_at=datetime(2026, 9, 14))
    with pytest.raises(ValueError, match="Duplicate"):
        observation(14, evidence(), evidence())


def test_failed_check_requires_reason():
    with pytest.raises(ValueError, match="reason"):
        observation(14, check_status=CheckStatus.FAILED)


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": ""},
        {"value": None},
        {"availability": Availability.NOT_PROVIDED},
        {"resource": ""},
        {"rule_version": ""},
    ],
)
def test_invalid_evidence_is_rejected(overrides):
    with pytest.raises(ValueError):
        replace(evidence(), **overrides)


@pytest.mark.parametrize(
    "kind",
    [EvidenceKind.REVISION, EvidenceKind.MODIFIED, EvidenceKind.ETAG, EvidenceKind.SCHEMA],
)
def test_each_change_signal_can_establish_match_or_change_a_baseline(kind):
    first = observation(14, evidence("original", kind))
    assert compare_observation(first, []).change_status is ChangeStatus.BASELINE
    matched = compare_observation(observation(15, evidence("original", kind)), [first])
    changed = compare_observation(observation(16, evidence("changed", kind)), [first])

    assert matched.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert not matched.evidence[0].changed
    assert changed.change_status is ChangeStatus.POSSIBLY_UPDATED
    assert changed.evidence[0].previous == "original"
    assert changed.evidence[0].current == "changed"
    assert changed.comparison_version == "1"


def test_title_alone_cannot_establish_a_baseline():
    assert (
        compare_observation(
            observation(14, evidence("Title", EvidenceKind.TITLE)), []
        ).change_status
        is ChangeStatus.UNKNOWN
    )


def test_changed_title_is_recorded_alongside_unchanged_change_evidence():
    first = observation(14, evidence(), evidence("Old title", EvidenceKind.TITLE))
    current = observation(16, evidence(), evidence("New title", EvidenceKind.TITLE))
    result = compare_observation(current, [first])

    assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert [(item.kind, item.changed) for item in result.evidence] == [
        (EvidenceKind.MODIFIED, False),
        (EvidenceKind.TITLE, True),
    ]


@pytest.mark.parametrize("availability", list(Availability)[1:])
def test_two_missing_values_never_become_matching_evidence(availability):
    missing = replace(evidence(), availability=availability, value=None)
    result = compare_observation(observation(16, missing), [observation(14, missing)])
    assert result.change_status is ChangeStatus.UNKNOWN
    assert result.evidence == ()


@pytest.mark.parametrize("availability", list(Availability)[1:])
@pytest.mark.parametrize("new_value", ["r1", "r2"])
def test_explicit_loss_of_timestamp_is_unknown_unless_revision_changes(availability, new_value):
    missing = replace(evidence(), availability=availability, value=None)
    baseline = observation(14, evidence(), evidence("r1", EvidenceKind.REVISION))
    current = observation(16, missing, evidence(new_value, EvidenceKind.REVISION))

    result = compare_observation(current, [baseline])

    expected = ChangeStatus.UNKNOWN if new_value == "r1" else ChangeStatus.POSSIBLY_UPDATED
    assert result.change_status is expected
    assert len(result.evidence) == 1
    assert result.evidence[0].kind is EvidenceKind.REVISION


def test_missing_slot_on_both_sides_does_not_hide_a_matching_present_signal():
    missing = replace(evidence(), availability=Availability.NOT_CHECKED, value=None)
    revision = evidence("r1", EvidenceKind.REVISION)
    result = compare_observation(
        observation(16, missing, revision), [observation(14, missing, revision)]
    )
    assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert len(result.evidence) == 1


@pytest.mark.parametrize("new_signal", [False, True])
def test_unmatched_available_signal_on_either_side_blocks_partial_equality(new_signal):
    revision = evidence("r1", EvidenceKind.REVISION)
    previous = (revision,) if new_signal else (revision, evidence())
    current = (revision, evidence()) if new_signal else (revision,)
    result = compare_observation(observation(16, *current), [observation(14, *previous)])
    assert result.change_status is ChangeStatus.UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"resource": "https://example.test/catalog"},
        {"rule_version": "fingerprint-v2"},
        {"kind": EvidenceKind.ETAG},
    ],
)
def test_incomparable_signal_cannot_be_hidden_by_another_matching_signal(overrides):
    revision = evidence("r1", EvidenceKind.REVISION)
    result = compare_observation(
        observation(16, revision, replace(evidence(), **overrides)),
        [observation(14, revision, evidence())],
    )
    assert result.change_status is ChangeStatus.UNKNOWN
    assert len(result.evidence) == 1


@pytest.mark.parametrize("status", [CheckStatus.FAILED, CheckStatus.BLOCKED])
def test_unsuccessful_attempts_never_replace_successful_baseline(status):
    first = observation(14, evidence())
    failed = observation(15, evidence("changed"), check_status=status, failure_reason="unavailable")
    history = [first, failed]
    current = observation(16, evidence())

    assert compare_observation(failed, [first]).change_status is ChangeStatus.UNKNOWN
    result = compare_observation(current, history)
    assert result.baseline_observation_id == first.observation_id
    assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    assert history == [first, failed]


def test_latest_baseline_selection_is_independent_of_history_order():
    first = observation(13, evidence("old"))
    latest = observation(15, evidence())
    failed = observation(14, check_status=CheckStatus.FAILED, failure_reason="timeout")
    current = observation(16, evidence())
    results = [
        compare_observation(current, iter(history))
        for history in permutations([first, latest, failed])
    ]

    assert all(result == results[0] for result in results)
    assert results[0].baseline_observation_id == latest.observation_id
    assert results[0].change_status is ChangeStatus.NO_CHANGE_DETECTED


def test_same_time_and_future_observations_are_not_baselines():
    current = observation(16, evidence())
    same_time = replace(current, observation_id="same-time")
    result = compare_observation(current, [same_time, current, observation(17, evidence())])
    assert result.change_status is ChangeStatus.BASELINE
    assert result.baseline_observation_id is None


@pytest.mark.parametrize("field", ["source_id", "contract_version", "checker_version"])
def test_no_compatible_history_establishes_a_new_baseline(field):
    previous = replace(observation(14, evidence()), **{field: "different"})
    result = compare_observation(observation(16, evidence()), [previous])
    assert result.change_status is ChangeStatus.BASELINE
    assert result.baseline_observation_id is None


def test_only_ties_at_latest_eligible_timestamp_are_ambiguous():
    first = observation(14, evidence("old"))
    tied = replace(first, observation_id="tied")
    latest = observation(15, evidence())
    for history in permutations([first, tied, latest]):
        result = compare_observation(observation(16, evidence()), history)
        assert result.baseline_observation_id == latest.observation_id
        assert result.change_status is ChangeStatus.NO_CHANGE_DETECTED
    for history in ([first, tied], [tied, first]):
        result = compare_observation(observation(16, evidence()), history)
        assert result.change_status is ChangeStatus.UNKNOWN
        assert result.baseline_observation_id is None


def test_timezone_offsets_and_subsecond_observation_precision_are_respected():
    first = observation(14, evidence())
    same_instant = replace(
        first,
        observation_id="same-instant",
        checked_at=first.checked_at.astimezone(timezone(timedelta(hours=2))),
    )
    current = replace(
        first, observation_id="later", checked_at=first.checked_at + timedelta(microseconds=1)
    )
    assert compare_observation(current, [same_instant]).baseline_observation_id == "same-instant"
    assert compare_observation(current, [first, same_instant]).change_status is ChangeStatus.UNKNOWN


def test_provider_values_are_compared_verbatim_without_normalization():
    original = "2026-09-14T09:00:00.000Z"
    different_precision = "2026-09-14T09:00:00Z"
    result = compare_observation(
        observation(16, evidence(different_precision)), [observation(14, evidence(original))]
    )
    assert result.change_status is ChangeStatus.POSSIBLY_UPDATED
    assert result.evidence[0].previous == original
    assert result.evidence[0].current == different_precision


def test_observations_and_results_detach_mutable_collections_and_are_frozen():
    items = [evidence()]
    first = replace(observation(14), evidence=items)
    items.clear()
    assert first.evidence == (evidence(),)
    compared = [EvidenceComparison(EvidenceKind.MODIFIED, "resource", "1", "a", "b")]
    result = Comparison("current", "previous", ChangeStatus.POSSIBLY_UPDATED, "signal", compared)
    compared.clear()
    assert len(result.evidence) == 1
    for obj, field, value in (
        (first, "source_id", "other"),
        (first.evidence[0], "value", "other"),
        (result, "reason", "other"),
        (result.evidence[0], "current", "other"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)
        assert not hasattr(obj, "__dict__")


@pytest.mark.parametrize("field", ["resource", "rule_version"])
@pytest.mark.parametrize("value", [None, "", " \t", 1, ["mutable"]])
def test_evidence_identity_requires_nonblank_strings(field, value):
    with pytest.raises(ValueError, match=field):
        replace(evidence(), **{field: value})


@pytest.mark.parametrize(
    "field",
    ["observation_id", "source_id", "contract_version", "checker_version", "failure_reason"],
)
@pytest.mark.parametrize("value", ["", " \t", 1, ["mutable"]])
def test_observation_identity_versions_and_supplied_reasons_require_strings(field, value):
    with pytest.raises(ValueError, match=field):
        replace(observation(14), **{field: value})


@pytest.mark.parametrize("value", [None, "2026-09-14", 123])
def test_observation_time_requires_aware_datetime(value):
    with pytest.raises(ValueError, match="datetime"):
        replace(observation(14), checked_at=value)


@pytest.mark.parametrize("value", [None, "invalid", ["invalid"], [{}], bytearray()])
def test_collections_cannot_contain_unvalidated_or_mutable_evidence(value):
    with pytest.raises(ValueError, match="evidence"):
        replace(observation(14), evidence=value)
    with pytest.raises(ValueError, match="evidence"):
        Comparison("current", None, ChangeStatus.UNKNOWN, "no evidence", value)
