"""Pure policy tests with synthetic content claims, never certified provider claims."""

import json
import os
import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from dataset_prober.freshness_observation import FreshnessObservation, TimestampClaim

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
EVALUATION_ID = UUID("00000000-0000-4000-8000-000000000001")


def observation(value="2026-10-22T12:00:00Z", **changes):
    claim = TimestampClaim.capture(
        {"updated": value},
        "updated",
        resource="https://synthetic.test/metadata",
        field_path="/updated",
        semantics="DATASET_CONTENT_UPDATE",
    )
    return FreshnessObservation(
        source="synthetic",
        catalog_identity="synthetic-catalog",
        dataset_id="Exact ID",
        resource_id="resource-1",
        resource_identity="https://synthetic.test/data",
        metadata_endpoint="https://synthetic.test/metadata",
        observed_at=NOW - timedelta(days=5),
        collection_status="SUCCEEDED",
        collector_rule_version="synthetic-collector-v1",
        observation_id="observation-1",
        claims=(claim,),
        **changes,
    )


def evaluate(value="2026-10-22T12:00:00Z", *, obs=None, **kwargs):
    from dataset_prober.freshness_evaluation import ClaimReference, evaluate_freshness

    obs = obs or observation(value)
    return evaluate_freshness(
        obs,
        ClaimReference(obs.observation_id, 0) if obs.claims else None,
        evaluation_id=kwargs.pop("evaluation_id", EVALUATION_ID),
        evaluated_at=kwargs.pop("evaluated_at", NOW),
        max_days_old=kwargs.pop("max_days_old", 1),
        **kwargs,
    )


def rule(**changes):
    from dataset_prober.freshness_evaluation import InterpretationRule

    values = dict(
        rule_id="synthetic-zone",
        version="1",
        source="synthetic",
        catalog_identity="synthetic-catalog",
        resource="https://synthetic.test/metadata",
        field_path="/updated",
        offset_minutes=0,
    )
    values.update(changes)
    return InterpretationRule(**values)


@pytest.mark.parametrize(
    "stamp,verdict,age",
    [
        ("2026-10-21T12:00:00.0000000Z", "PASS", 864000000000),
        ("2026-10-21T11:59:59.9999999Z", "FAIL", 864000000001),
        ("2026-10-21T12:00:00.0000001Z", "PASS", 863999999999),
    ],
)
def test_threshold_exact_to_one_tick(stamp, verdict, age):
    result = evaluate(stamp)
    assert result.verdict == verdict
    assert result.age.lower_ticks == result.age.upper_ticks == age
    assert result.interpretation.fractional_digits == 7
    assert result.observation.observed_at != result.evaluated_at


@pytest.mark.parametrize(
    "stamp,verdict,reason",
    [
        ("2026-10-22T12:00:00Z", "PASS", "freshness_pass"),
        ("2026-10-22T11:59:59.9999999Z", "FAIL", "freshness_fail"),
        ("2026-10-22T12:00:00.0000001Z", "UNKNOWN", "future_timestamp"),
    ],
)
def test_zero_threshold(stamp, verdict, reason):
    result = evaluate(stamp, max_days_old=0)
    assert (result.verdict, result.reason) == (verdict, reason)


def test_offsets_and_seven_digits_are_exact():
    first = evaluate("2026-10-20T23:45:12.1234567+02:00", max_days_old=3)
    second = evaluate("2026-10-20T21:45:12.1234567Z", max_days_old=3)
    third = evaluate("2026-10-20T16:15:12.1234567-05:30", max_days_old=3)
    assert first.interpretation.start_ticks == second.interpretation.start_ticks
    assert first.interpretation.start_ticks == third.interpretation.start_ticks
    assert first.interpretation.start_ticks % 10_000_000 == 1234567
    assert first.age == second.age == third.age
    data = json.loads(json.dumps(first.to_dict(), allow_nan=False))
    assert data["interpretation"]["supplied_offset"] == "+02:00"
    assert data["interpretation"]["fractional_digits"] == 7
    assert data["observation"]["claims"][0]["value"].endswith(".1234567+02:00")
    assert data["interpretation"]["start_ticks"] == first.interpretation.start_ticks


@pytest.mark.parametrize(
    "stamp,reason,precision",
    [
        ("2026-10-22T12:00:00", "timezone_missing", "SECOND"),
        ("2026-10-22T12:00:00-00:00", "timezone_unknown_offset", "SECOND"),
        ("2026-10-20", "timezone_missing", "DAY"),
        ("2026", "insufficient_precision", "YEAR"),
        ("2026-02-30T12:00:00Z", "invalid_timestamp", "SECOND"),
        ("2026-10-22T12:00:00+24:00", "invalid_timestamp", "SECOND"),
        ("2026-10-22T12:00:00+00:60", "invalid_timestamp", "SECOND"),
        ("2026-10-22T12:00:60Z", "unsupported_leap_second", "SECOND"),
        ("2026-10-22T12:00:00.12345678Z", "unsupported_precision", "FRACTION"),
        ("2026-10-22T12:00:00Zgarbage", "invalid_timestamp", None),
        (" 2026-10-22T12:00:00Z", "invalid_timestamp", None),
        ("2026-10-22T12:00:00Z\n", "invalid_timestamp", None),
        ("2026-02-30", "invalid_timestamp", "DAY"),
        ("0000", "invalid_timestamp", "YEAR"),
    ],
)
def test_unusable_provider_values(stamp, reason, precision):
    result = evaluate(stamp)
    assert (result.verdict, result.reason) == ("UNKNOWN", reason)
    assert result.interpretation.precision == precision
    assert result.observation.claims[0].to_dict()["value"] == stamp


def test_explicit_scoped_rule_and_unresolved_named_zones():
    result = evaluate("2026-10-22T14:00:00", rule=rule(offset_minutes=120))
    assert result.verdict == "PASS"
    assert result.interpretation.timezone_assumption == "+02:00"
    assert result.interpretation.supplied_offset is None
    assert evaluate("2026-10-22T12:00:00-00:00", rule=rule()).reason == "timezone_unknown_offset"
    for stamp in ("2026-03-29T02:30:00", "2026-10-25T02:30:00", "2026-10-20"):
        result = evaluate(stamp, rule=rule(offset_minutes=None, named_zone="Europe/Amsterdam"))
        assert result.reason == "named_timezone_unsupported"
        assert result.interpretation.start_ticks is None
        assert result.interpretation.timezone_assumption is None
    explicit = evaluate("2026-10-22T12:00:00Z", rule=rule(offset_minutes=120))
    assert explicit.verdict == "PASS"
    assert explicit.interpretation.timezone_assumption is None


@pytest.mark.parametrize(
    "days,verdict,reason",
    [
        (1, "FAIL", "freshness_fail"),
        (2, "UNKNOWN", "interval_crosses_cutoff"),
        (3, "PASS", "freshness_pass"),
    ],
)
def test_date_interval_at_noon(days, verdict, reason):
    result = evaluate("2026-10-20", rule=rule(), max_days_old=days)
    assert (result.verdict, result.reason) == (verdict, reason)
    assert result.interpretation.precision == "DAY"
    assert result.age.lower_ticks == 36 * 3600 * 10_000_000
    assert result.age.upper_ticks == 60 * 3600 * 10_000_000
    assert not result.age.lower_inclusive and result.age.upper_inclusive


def test_interval_boundaries_and_future_overlap():
    midnight = datetime(2026, 10, 22, tzinfo=UTC)
    assert evaluate("2026-10-21", rule=rule(), evaluated_at=midnight).verdict == "PASS"
    assert evaluate("2026-10-20", rule=rule(), evaluated_at=midnight).verdict == "FAIL"
    assert evaluate("2026-10-22", rule=rule()).reason == "interval_future_overlap"
    assert evaluate("2026-10-23", rule=rule()).reason == "future_timestamp"
    shifted = evaluate("2026-10-21", rule=rule(offset_minutes=120), max_days_old=2)
    assert (
        shifted.interpretation.start_ticks
        == evaluate("2026-10-20T22:00:00Z").interpretation.start_ticks
    )


@pytest.mark.parametrize(
    "presence,value,reason",
    [
        ("ABSENT", None, "timestamp_absent"),
        ("NULL", None, "timestamp_null"),
        ("WRONG_TYPE", {"nested": [1]}, "timestamp_wrong_type"),
    ],
)
def test_presence_distinctions(presence, value, reason):
    obs = observation(value)
    obs = replace(obs, claims=(replace(obs.claims[0], presence=presence),))
    assert evaluate(obs=obs).reason == reason
    assert evaluate(obs=replace(obs, claims=())).reason == "timestamp_absent"


@pytest.mark.parametrize(
    "status,reason", [("FAILED", "collection_failed"), ("BLOCKED", "collection_blocked")]
)
def test_collection_precedes_other_limits_but_retains_parsing(status, reason):
    obs = replace(observation("bad"), collection_status=status, reason_code="fixture_failure")
    result = evaluate(obs=obs)
    assert result.reason == reason
    assert result.interpretation.reason == "invalid_timestamp"
    assert "invalid_timestamp" in result.diagnostics


@pytest.mark.parametrize("semantics", ["CATALOG_METADATA_UPDATE", "UNVERIFIED"])
@pytest.mark.parametrize("stamp", ["2026-10-22T12:00:00Z", "2020-01-01T00:00:00Z"])
def test_metadata_never_decides_content_freshness(semantics, stamp):
    obs = observation(stamp)
    obs = replace(obs, claims=(replace(obs.claims[0], semantics=semantics),))
    result = evaluate(obs=obs)
    assert (result.verdict, result.reason) == ("UNKNOWN", "semantics_not_applicable")
    assert result.interpretation.status == "PARSED"
    assert result.age is not None
    metadata = evaluate(obs=obs, target="CATALOG_METADATA")
    assert metadata.verdict == (
        "UNKNOWN" if semantics == "UNVERIFIED" else ("PASS" if stamp.startswith("2026") else "FAIL")
    )


def test_sanitization_and_parsing_diagnostics_are_not_hidden_by_semantics():
    obs = observation("bad")
    obs = replace(obs, claims=(replace(obs.claims[0], semantics="UNVERIFIED"),))
    result = evaluate(obs=obs)
    assert result.reason == "invalid_timestamp"
    assert "semantics_not_applicable" in result.diagnostics
    for stamp in ("2026-10-22T12:00:00Z", "2020-01-01T00:00:00Z", "bad"):
        obs = observation(stamp)
        obs = replace(obs, claims=(replace(obs.claims[0], sanitized=True),))
        result = evaluate(obs=obs)
        assert (result.verdict, result.reason) == ("UNKNOWN", "sanitized_evidence")


@pytest.mark.parametrize("threshold", [True, False, -1, 0.5, "30", None])
def test_invalid_threshold_is_caller_error(threshold):
    with pytest.raises(ValueError):
        evaluate(max_days_old=threshold)


def test_invalid_selection_and_rule_scope_are_caller_errors():
    from dataset_prober.freshness_evaluation import ClaimReference, evaluate_freshness

    obs = observation()
    for ref in (
        None,
        ClaimReference("other-observation", 0),
        ClaimReference(obs.observation_id, 1),
    ):
        with pytest.raises(ValueError):
            evaluate_freshness(
                obs, ref, evaluation_id=EVALUATION_ID, evaluated_at=NOW, max_days_old=1
            )
    for changes in (
        {"source": "other"},
        {"catalog_identity": "other"},
        {"field_path": "/other"},
        {"resource": "other"},
    ):
        with pytest.raises(ValueError):
            evaluate(rule=rule(**changes))
    for reference in (
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=1))),
        NOW.replace(fold=1),
    ):
        with pytest.raises(ValueError):
            evaluate(evaluated_at=reference)
    with pytest.raises(ValueError):
        evaluate(evaluation_id="not-a-uuid")


def test_repeatable_immutable_detached_records():
    result = evaluate(rule=rule())
    assert result == evaluate(rule=rule())
    assert result != evaluate(evaluation_id=UUID(int=2), rule=rule())
    before = result.to_dict()
    detached = result.to_dict()
    detached["observation"]["claims"][0]["value"] = "changed"
    detached["diagnostics"].append("changed")
    assert result.to_dict() == before
    for obj, name, value in (
        (result, "reason", "changed"),
        (result.interpretation, "start_ticks", 0),
        (result.rule, "version", "changed"),
        (result.age, "lower_ticks", 0),
        (result.claim_reference, "index", 3),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, name, value)
    assert json.loads(json.dumps(before, allow_nan=False)) == before


def test_process_timezone_independence(monkeypatch):
    previous = os.environ.get("TZ")
    try:
        records = []
        for zone in ("UTC", "Pacific/Honolulu", "Europe/Amsterdam"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            records.append(evaluate("2026-10-20", rule=rule(), max_days_old=3))
        assert records[0] == records[1] == records[2]
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("digits", range(1, 8))
def test_all_supported_fraction_lengths_and_microsecond_reference(digits):
    fraction = "1234567"[:digits]
    result = evaluate(
        f"2026-10-22T12:00:00.{fraction}Z",
        evaluated_at=NOW.replace(microsecond=999999),
        max_days_old=0,
    )
    assert result.verdict == "FAIL"
    assert result.age.lower_ticks == 9999990 - int(fraction.ljust(7, "0"))
    assert result.interpretation.fractional_digits == digits


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T24:00:00Z",
        "2026-01-01T12:60:00Z",
        "2026-01-01T12:00:61Z",
        "2026-13-01T12:00:00Z",
        "2026-01-01T12:00:00.Z",
        "2026-01-01T12:00Z",
        "2026-01-01T12:00:00+0200",
        "2026-01-01T12:00:00z",
        "2026-01-01T12:00:00Z ",
        "2026-01-01extra",
        "not a date",
    ],
)
def test_complete_grammar_and_component_validation(value):
    assert evaluate(value).reason == "invalid_timestamp"


def test_selection_is_exact_and_never_substitutes_another_claim():
    from dataset_prober.freshness_evaluation import ClaimReference, evaluate_freshness

    good = observation().claims[0]
    bad = observation("bad").claims[0]
    obs = replace(observation(), claims=(bad, good))
    results = [
        evaluate_freshness(
            obs,
            ClaimReference(obs.observation_id, index),
            evaluation_id=EVALUATION_ID,
            evaluated_at=NOW,
            max_days_old=1,
        )
        for index in (0, 1)
    ]
    assert results[0].reason == "invalid_timestamp"
    assert results[1].verdict == "PASS"
    assert results[0].to_dict()["claim_reference"]["index"] == 0
    assert results[0].observation.claims == (bad, good)


def test_normalization_origin_leap_day_and_extreme_year_boundaries():
    assert evaluate("1970-01-01T00:00:00Z").interpretation.start_ticks == 0
    assert evaluate("1969-12-31T23:59:59.9999999Z").interpretation.start_ticks == -1
    assert evaluate("2024-02-29T12:00:00Z").interpretation.status == "PARSED"
    assert evaluate("0001-01-01T00:00:00+23:59").interpretation.status == "PARSED"
    assert evaluate("9999-12-31", rule=rule()).interpretation.status == "PARSED"


def test_diagnostic_precedence_and_observation_provenance():
    obs = observation("2027-01-01T00:00:00Z")
    obs = replace(
        obs,
        collection_status="BLOCKED",
        reason_code="fixture_block",
        claims=(replace(obs.claims[0], sanitized=True, semantics="UNVERIFIED"),),
    )
    result = evaluate(obs=obs)
    assert result.diagnostics == (
        "collection_blocked",
        "sanitized_evidence",
        "future_timestamp",
        "semantics_not_applicable",
    )
    assert result.reason == "collection_blocked"
    data = result.to_dict()
    assert data["observation"] == obs.to_dict()
    assert data["observation_id"] == obs.observation_id
    assert data["evaluation_id"] == str(EVALUATION_ID)
    assert data["record_version"] == "1"
    assert data["evaluation_policy_version"] == "elapsed-freshness-v1"
    assert data["observation"]["collector_rule_version"] == "synthetic-collector-v1"
    assert evaluate(obs=replace(obs, claims=())).diagnostics == (
        "collection_blocked",
        "timestamp_absent",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"offset_minutes": True},
        {"offset_minutes": 0.5},
        {"offset_minutes": 1440},
        {"offset_minutes": -1440},
        {"offset_minutes": None},
        {"named_zone": "Europe/Amsterdam"},
        {"version": ""},
        {"offset_minutes": None, "named_zone": []},
    ],
)
def test_invalid_rules_are_caller_errors(changes):
    with pytest.raises(ValueError):
        rule(**changes)


def test_records_reject_mutable_annotations_and_invalid_caller_types():
    from dataset_prober.freshness_evaluation import ClaimReference

    result = evaluate()
    with pytest.raises(ValueError):
        replace(result, diagnostics=["mutable"])
    with pytest.raises(ValueError):
        replace(result.interpretation, precision=[])
    with pytest.raises(ValueError):
        replace(result.age, lower_ticks=True)
    with pytest.raises(ValueError):
        ClaimReference("observation-1", True)
    with pytest.raises(ValueError):
        evaluate(target="invented")


def test_evaluation_and_serialization_need_no_clock_randomness_or_io(monkeypatch):
    obs = observation()
    expected = evaluate(obs=obs).to_dict()

    def forbidden(*args, **kwargs):
        pytest.fail("Pure evaluation accessed a clock, randomness or I/O")

    # Imports and fixture creation precede the guard; only the pure call is under test.
    with monkeypatch.context() as guard:
        guard.setattr("time.time", forbidden)
        guard.setattr("uuid.uuid4", forbidden)
        guard.setattr("builtins.open", forbidden)
        assert evaluate(obs=obs).to_dict() == expected
