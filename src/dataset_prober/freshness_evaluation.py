"""Pure, standalone freshness policy. No operational caller or persistence integration."""

import json
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from dataset_prober.freshness_observation import FreshnessObservation, TimestampClaim

POLICY_VERSION = "elapsed-freshness-v1"
TICKS_PER_SECOND = 10_000_000
TICKS_PER_DAY = 86_400 * TICKS_PER_SECOND
TICK_ORIGIN = "1970-01-01T00:00:00Z"
_ORIGIN_ORDINAL = 719163
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})?"
)


class EvaluationTarget(StrEnum):
    DATASET_CONTENT = "DATASET_CONTENT"
    CATALOG_METADATA = "CATALOG_METADATA"


def _nonempty(*values):
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValueError("Identity fields must be nonempty strings")


def _utc_reference(value):
    if type(value) is not datetime or value.tzinfo is not UTC or value.fold:
        raise ValueError("Evaluation reference must be a standard UTC datetime with fold=0")


def _threshold(value):
    if type(value) is not int or value < 0:
        raise ValueError("max_days_old must be a nonnegative integer, excluding booleans")


def _ticks(value):
    # Calendar arithmetic only: no float timestamps or machine timezone consultation.
    seconds = value.hour * 3600 + value.minute * 60 + value.second
    return (
        (value.toordinal() - _ORIGIN_ORDINAL) * TICKS_PER_DAY
        + seconds * TICKS_PER_SECOND
        + value.microsecond * 10
    )


@dataclass(frozen=True, slots=True)
class ClaimReference:
    observation_id: str
    index: int

    def __post_init__(self):
        _nonempty(self.observation_id)
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Claim index must be a nonnegative integer")

    def to_dict(self):
        return {"observation_id": self.observation_id, "index": self.index}


@dataclass(frozen=True, slots=True)
class InterpretationRule:
    """Caller-reviewed timezone rule; no built-in certification of any provider.

    Exactly one timezone representation is required. Named zones are recorded but
    deliberately unresolved in policy v1; no timezone database is read.
    """

    rule_id: str
    version: str
    source: str
    catalog_identity: str
    resource: str
    field_path: str
    offset_minutes: int | None = None
    named_zone: str | None = None

    def __post_init__(self):
        _nonempty(
            self.rule_id,
            self.version,
            self.source,
            self.catalog_identity,
            self.resource,
            self.field_path,
        )
        if (self.offset_minutes is None) == (self.named_zone is None):
            raise ValueError("Rule requires exactly one timezone representation")
        if self.offset_minutes is not None and (
            type(self.offset_minutes) is not int or not -1439 <= self.offset_minutes <= 1439
        ):
            raise ValueError("Fixed offset must be integer minutes between -1439 and 1439")
        if self.named_zone is not None:
            _nonempty(self.named_zone)

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class Interpretation:
    status: str
    reason: str | None = None
    precision: str | None = None
    fractional_digits: int | None = None
    supplied_offset: str | None = None
    timezone_assumption: str | None = None
    start_ticks: int | None = None
    end_ticks: int | None = None

    def __post_init__(self):
        for name in ("status", "reason", "precision", "supplied_offset", "timezone_assumption"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise ValueError("Interpretation annotations must be strings")
        for value in (self.fractional_digits, self.start_ticks, self.end_ticks):
            if value is not None and type(value) is not int:
                raise ValueError("Interpretation counts must be integers")
        if self.status not in ("PARSED", "INVALID", "UNSUPPORTED", "UNRESOLVED", "NO_EVIDENCE"):
            raise ValueError("Unsupported parsing status")
        if (self.status == "PARSED") != (self.start_ticks is not None):
            raise ValueError("Only parsed interpretations have normalized values")
        if self.end_ticks is not None and (
            self.start_ticks is None or self.end_ticks <= self.start_ticks
        ):
            raise ValueError("Interval end must follow its start")

    def to_dict(self):
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result.update(
            unit="100ns",
            utc_origin=TICK_ORIGIN,
            kind=None
            if self.start_ticks is None
            else "instant"
            if self.end_ticks is None
            else "interval",
            start_inclusive=True if self.start_ticks is not None else None,
            end_exclusive=True if self.end_ticks is not None else None,
        )
        return result


@dataclass(frozen=True, slots=True)
class AgeBounds:
    lower_ticks: int
    upper_ticks: int
    lower_inclusive: bool
    upper_inclusive: bool

    def __post_init__(self):
        if type(self.lower_ticks) is not int or type(self.upper_ticks) is not int:
            raise ValueError("Age bounds must be integer ticks")
        if type(self.lower_inclusive) is not bool or type(self.upper_inclusive) is not bool:
            raise ValueError("Age boundary flags must be booleans")
        if self.lower_ticks > self.upper_ticks:
            raise ValueError("Age bounds are reversed")

    def to_dict(self):
        return {"unit": "100ns", **{f.name: getattr(self, f.name) for f in fields(self)}}


@dataclass(frozen=True, slots=True)
class FreshnessEvaluation:
    evaluation_id: UUID
    observation: FreshnessObservation
    claim_reference: ClaimReference | None
    evaluated_at: datetime
    max_days_old: int
    target: EvaluationTarget
    rule: InterpretationRule | None
    interpretation: Interpretation
    age: AgeBounds | None
    verdict: str
    reason: str
    diagnostics: tuple[str, ...]
    record_version: str = "1"
    evaluation_policy_version: str = POLICY_VERSION

    def __post_init__(self):
        _validate_inputs(
            self.observation,
            self.claim_reference,
            self.evaluation_id,
            self.evaluated_at,
            self.max_days_old,
            self.rule,
        )
        object.__setattr__(self, "target", EvaluationTarget(self.target))
        if type(self.interpretation) is not Interpretation or (
            self.age is not None and type(self.age) is not AgeBounds
        ):
            raise ValueError("Evaluation requires immutable interpretation and age records")
        if type(self.diagnostics) is not tuple or any(type(d) is not str for d in self.diagnostics):
            raise ValueError("Diagnostics must be an immutable tuple of strings")
        _nonempty(self.reason, self.record_version, self.evaluation_policy_version)
        if type(self.verdict) is not str or self.verdict not in ("PASS", "FAIL", "UNKNOWN"):
            raise ValueError("Unsupported verdict")

    def to_dict(self):
        return {
            "evaluation_id": str(self.evaluation_id),
            "record_version": self.record_version,
            "evaluation_policy_version": self.evaluation_policy_version,
            "observation_id": self.observation.observation_id,
            "observation": self.observation.to_dict(),
            "claim_reference": self.claim_reference.to_dict() if self.claim_reference else None,
            "evaluated_at": self.evaluated_at.isoformat(timespec="microseconds"),
            "max_days_old": self.max_days_old,
            "target": self.target.value,
            "rule": self.rule.to_dict() if self.rule else None,
            "interpretation": self.interpretation.to_dict(),
            "age": self.age.to_dict() if self.age else None,
            "verdict": self.verdict,
            "reason": self.reason,
            "diagnostics": list(self.diagnostics),
        }


def _validate_inputs(observation, reference, evaluation_id, evaluated_at, max_days_old, rule):
    if type(observation) is not FreshnessObservation:
        raise ValueError("Expected an immutable freshness observation")
    if type(evaluation_id) is not UUID:
        raise ValueError("evaluation_id must be a caller-supplied UUID")
    _utc_reference(evaluated_at)
    _threshold(max_days_old)
    claim = None
    if reference is None:
        if observation.claims:
            raise ValueError("Select a claim explicitly when claims are present")
    elif (
        type(reference) is not ClaimReference
        or reference.observation_id != observation.observation_id
        or reference.index >= len(observation.claims)
    ):
        raise ValueError("Claim reference does not belong to this observation")
    else:
        claim = observation.claims[reference.index]
    if rule is not None:
        if (
            type(rule) is not InterpretationRule
            or claim is None
            or (
                (rule.source, rule.catalog_identity, rule.resource, rule.field_path)
                != (
                    observation.source,
                    observation.catalog_identity,
                    claim.resource,
                    claim.field_path,
                )
            )
        ):
            raise ValueError("Interpretation rule does not match the selected claim's scope")
    return claim


def _interpret(claim: TimestampClaim | None, rule: InterpretationRule | None):
    if claim is None or claim.presence != "STRING":
        presence = claim.presence.value.lower() if claim else "absent"
        return Interpretation("NO_EVIDENCE", f"timestamp_{presence}")
    value = json.loads(claim.value_json)
    match = _TIMESTAMP.fullmatch(value)
    annotations = {}
    is_day = bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    if re.fullmatch(r"[0-9]{4}", value):
        return Interpretation(
            "UNSUPPORTED" if int(value) else "INVALID",
            "insufficient_precision" if int(value) else "invalid_timestamp",
            precision="YEAR",
        )
    if match:
        year, month, day, hour, minute, second = map(int, match.groups()[:6])
        fraction, offset = match.groups()[6:]
        annotations = dict(
            precision="FRACTION" if fraction else "SECOND",
            fractional_digits=len(fraction) if fraction else 0,
            supplied_offset=offset,
        )
    elif is_day:
        year, month, day = map(int, value.split("-"))
        hour = minute = second = 0
        fraction = offset = None
        annotations = dict(precision="DAY")
    else:
        return Interpretation("INVALID", "invalid_timestamp")

    # Validate all calendar and offset components before precision/zone handling.
    try:
        local = datetime(year, month, day, hour, minute, 59 if second == 60 else second)
    except ValueError:
        return Interpretation("INVALID", "invalid_timestamp", **annotations)
    offset_minutes = None
    if offset and offset != "Z":
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 23 or minutes > 59:
            return Interpretation("INVALID", "invalid_timestamp", **annotations)
        offset_minutes = (hours * 60 + minutes) * (-1 if offset[0] == "-" else 1)
    elif offset == "Z":
        offset_minutes = 0
    if second == 60:
        return Interpretation("UNSUPPORTED", "unsupported_leap_second", **annotations)
    if fraction and len(fraction) > 7:
        return Interpretation("UNSUPPORTED", "unsupported_precision", **annotations)
    if offset == "-00:00":
        return Interpretation("UNRESOLVED", "timezone_unknown_offset", **annotations)
    if offset_minutes is None:
        if rule is None:
            return Interpretation("UNRESOLVED", "timezone_missing", **annotations)
        if rule.named_zone is not None:
            return Interpretation("UNSUPPORTED", "named_timezone_unsupported", **annotations)
        offset_minutes = rule.offset_minutes
        magnitude = abs(offset_minutes)
        annotations["timezone_assumption"] = (
            f"{'-' if offset_minutes < 0 else '+'}{magnitude // 60:02}:{magnitude % 60:02}"
        )
    offset_ticks = offset_minutes * 60 * TICKS_PER_SECOND
    start = _ticks(local) - offset_ticks
    if is_day:
        # Both local boundaries converted using the reviewed fixed offset. Named
        # zones are unresolved above: this does not assume 24h for DST local days.
        end = (local.toordinal() + 1 - _ORIGIN_ORDINAL) * TICKS_PER_DAY - offset_ticks
        return Interpretation("PARSED", start_ticks=start, end_ticks=end, **annotations)
    start += int(fraction.ljust(7, "0")) if fraction else 0
    return Interpretation("PARSED", start_ticks=start, **annotations)


def _compare(interpretation, reference_ticks, max_days_old):
    start, end = interpretation.start_ticks, interpretation.end_ticks
    if start is None:
        return None, None
    cutoff = reference_ticks - max_days_old * TICKS_PER_DAY
    if end is None:
        age = reference_ticks - start
        bounds = AgeBounds(age, age, True, True)
        reason = (
            "future_timestamp"
            if age < 0
            else "freshness_pass"
            if start >= cutoff
            else "freshness_fail"
        )
    else:
        bounds = AgeBounds(reference_ticks - end, reference_ticks - start, False, True)
        reason = (
            "future_timestamp"
            if start > reference_ticks
            else "interval_future_overlap"
            if end > reference_ticks
            else "freshness_pass"
            if start >= cutoff
            else "freshness_fail"
            if end <= cutoff
            else "interval_crosses_cutoff"
        )
    return bounds, reason


def evaluate_freshness(
    observation: FreshnessObservation,
    claim_reference: ClaimReference | None,
    *,
    evaluation_id: UUID,
    evaluated_at: datetime,
    max_days_old: int,
    target: EvaluationTarget = EvaluationTarget.DATASET_CONTENT,
    rule: InterpretationRule | None = None,
) -> FreshnessEvaluation:
    """Evaluate exactly one selected claim, or explicit absence on an empty observation."""
    claim = _validate_inputs(
        observation, claim_reference, evaluation_id, evaluated_at, max_days_old, rule
    )
    target = EvaluationTarget(target)
    interpretation = _interpret(claim, rule)
    age, temporal_reason = _compare(interpretation, _ticks(evaluated_at), max_days_old)
    diagnostics = []
    if observation.collection_status != "SUCCEEDED":
        diagnostics.append(f"collection_{observation.collection_status.value.lower()}")
    if interpretation.status == "NO_EVIDENCE":
        diagnostics.append(interpretation.reason)
    if claim is not None and claim.sanitized:
        diagnostics.append("sanitized_evidence")
    if interpretation.reason and interpretation.status != "NO_EVIDENCE":
        diagnostics.append(interpretation.reason)
    if temporal_reason and temporal_reason not in ("freshness_pass", "freshness_fail"):
        diagnostics.append(temporal_reason)
    expected = (
        "DATASET_CONTENT_UPDATE"
        if target == EvaluationTarget.DATASET_CONTENT
        else "CATALOG_METADATA_UPDATE"
    )
    if claim is not None and claim.semantics != expected:
        diagnostics.append("semantics_not_applicable")
    reason = diagnostics[0] if diagnostics else temporal_reason
    verdict = "UNKNOWN" if diagnostics else "PASS" if reason == "freshness_pass" else "FAIL"
    return FreshnessEvaluation(
        evaluation_id,
        observation,
        claim_reference,
        evaluated_at,
        max_days_old,
        target,
        rule,
        interpretation,
        age,
        verdict,
        reason,
        tuple(diagnostics),
    )
