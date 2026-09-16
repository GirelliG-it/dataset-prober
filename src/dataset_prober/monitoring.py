"""Pure monitoring comparisons, without collection, storage, or loading authority.

Values are compared exactly as supplied. Collectors own provider semantics,
sanitization, and versioned normalization; this module does not parse timestamps
inside evidence or infer dataset freshness from matching metadata.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CheckStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ChangeStatus(StrEnum):
    BASELINE = "BASELINE"
    NO_CHANGE_DETECTED = "NO_CHANGE_DETECTED"
    POSSIBLY_UPDATED = "POSSIBLY_UPDATED"
    UNKNOWN = "UNKNOWN"


class Availability(StrEnum):
    PRESENT = "PRESENT"
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_CHECKED = "NOT_CHECKED"
    INVALID = "INVALID"
    CHECK_FAILED = "CHECK_FAILED"


class EvidenceKind(StrEnum):
    REVISION = "revision"
    MODIFIED = "modified"
    ETAG = "etag"
    SCHEMA = "schema"
    TITLE = "title"


def _require_string(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} requires a nonempty string")


@dataclass(frozen=True, slots=True)
class Evidence:
    """One field, scoped to an exact resource and collection/normalization rule.

    Collectors must supply sanitized resource identifiers, preserve timestamp
    precision, and identify fingerprint algorithms in rule_version.
    """

    kind: EvidenceKind
    resource: str
    rule_version: str
    availability: Availability
    value: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        object.__setattr__(self, "availability", Availability(self.availability))
        _require_string(self.resource, "resource")
        _require_string(self.rule_version, "rule_version")
        if self.availability is Availability.PRESENT:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("Present evidence requires a nonempty string value")
        elif self.value is not None:
            raise ValueError("Unavailable evidence cannot carry a value")

    @property
    def key(self) -> tuple[EvidenceKind, str, str]:
        return self.kind, self.resource, self.rule_version


@dataclass(frozen=True, slots=True)
class Observation:
    """An attempt retained independently of its eventual comparison result.

    checked_at is an aware observation instant. Provider modification timestamps
    belong in evidence as precision-preserving strings, not in this field.
    """

    observation_id: str
    source_id: str
    checked_at: datetime
    check_status: CheckStatus
    evidence: tuple[Evidence, ...] = ()
    failure_reason: str | None = None
    contract_version: str = "1"
    checker_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_status", CheckStatus(self.check_status))
        if isinstance(self.evidence, (str, bytes, bytearray)) or not isinstance(
            self.evidence, Iterable
        ):
            raise ValueError("evidence requires a collection of Evidence objects")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if any(not isinstance(item, Evidence) for item in self.evidence):
            raise ValueError("evidence requires Evidence objects")
        for name in ("observation_id", "source_id", "contract_version", "checker_version"):
            _require_string(getattr(self, name), name)
        if not isinstance(self.checked_at, datetime) or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be a datetime with a timezone")
        if self.failure_reason is not None:
            _require_string(self.failure_reason, "failure_reason")
        if self.check_status is not CheckStatus.SUCCEEDED and not self.failure_reason:
            raise ValueError("Failed or blocked observations require a reason")
        if len({item.key for item in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence keys")


@dataclass(frozen=True, slots=True)
class EvidenceComparison:
    kind: EvidenceKind
    resource: str
    rule_version: str
    previous: str
    current: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        for name in ("resource", "rule_version", "previous", "current"):
            _require_string(getattr(self, name), name)

    @property
    def changed(self) -> bool:
        return self.previous != self.current


@dataclass(frozen=True, slots=True)
class Comparison:
    observation_id: str
    baseline_observation_id: str | None
    change_status: ChangeStatus
    reason: str
    evidence: tuple[EvidenceComparison, ...] = ()
    comparison_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "change_status", ChangeStatus(self.change_status))
        for name in ("observation_id", "reason", "comparison_version"):
            _require_string(getattr(self, name), name)
        if self.baseline_observation_id is not None:
            _require_string(self.baseline_observation_id, "baseline_observation_id")
        if isinstance(self.evidence, (str, bytes, bytearray)) or not isinstance(
            self.evidence, Iterable
        ):
            raise ValueError("evidence requires a collection of EvidenceComparison objects")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if any(not isinstance(item, EvidenceComparison) for item in self.evidence):
            raise ValueError("evidence requires EvidenceComparison objects")


def compare_observation(current: Observation, history: Iterable[Observation]) -> Comparison:
    """Compare with the latest strictly earlier, successful, compatible observation.

    History is never mutated. Equal-time candidate baselines are ambiguous rather
    than ordered by incidental input order. Missing or incomparable evidence is
    never interpreted as equality. The latest successful attempt is not skipped
    merely because it lacks evidence. All conclusions concern checked evidence
    only; even matching signals cannot exclude an intervening, reverted change.
    """
    candidates = [
        item
        for item in history
        if item.source_id == current.source_id
        and item.observation_id != current.observation_id
        and item.checked_at < current.checked_at
        and item.check_status is CheckStatus.SUCCEEDED
        and item.contract_version == current.contract_version
        and item.checker_version == current.checker_version
    ]
    latest = max((item.checked_at for item in candidates), default=None)
    newest = [item for item in candidates if item.checked_at == latest]
    baseline = newest[0] if len(newest) == 1 else None

    def result(
        status: ChangeStatus,
        reason: str,
        evidence: tuple[EvidenceComparison, ...] = (),
    ) -> Comparison:
        return Comparison(
            current.observation_id,
            baseline.observation_id if baseline else None,
            status,
            reason,
            evidence,
        )

    if current.check_status is not CheckStatus.SUCCEEDED:
        return result(
            ChangeStatus.UNKNOWN, "Check failed or was blocked; source change is unknown."
        )
    if len(newest) > 1:
        return result(ChangeStatus.UNKNOWN, "Multiple latest observations have the same timestamp.")
    present_signals = [
        item
        for item in current.evidence
        if item.kind is not EvidenceKind.TITLE and item.availability is Availability.PRESENT
    ]
    if baseline is None:
        if not present_signals:
            return result(ChangeStatus.UNKNOWN, "No usable change evidence or comparable baseline.")
        return result(
            ChangeStatus.BASELINE, "First usable observation under compatible collection rules."
        )

    old = {item.key: item for item in baseline.evidence}
    new = {item.key: item for item in current.evidence}
    compared = tuple(
        EvidenceComparison(
            item.kind, item.resource, item.rule_version, old[item.key].value, item.value
        )
        for item in current.evidence
        if item.availability is Availability.PRESENT
        and item.key in old
        and old[item.key].availability is Availability.PRESENT
    )
    signals = tuple(item for item in compared if item.kind is not EvidenceKind.TITLE)
    if any(item.changed for item in signals):
        return result(
            ChangeStatus.POSSIBLY_UPDATED,
            "Comparable metadata changed; this does not establish a dataset content update.",
            compared,
        )
    # A disappearing or incomparable previously available signal prevents a
    # partial match from concealing loss of evidence. Absent in both is harmless.
    available_keys = {
        key
        for key, item in (*old.items(), *new.items())
        if item.kind is not EvidenceKind.TITLE and item.availability is Availability.PRESENT
    }
    compared_keys = {(item.kind, item.resource, item.rule_version) for item in signals}
    if signals and available_keys == compared_keys:
        return result(
            ChangeStatus.NO_CHANGE_DETECTED,
            "No change detected in comparable metadata; dataset contents and freshness are unverified.",
            compared,
        )
    return result(
        ChangeStatus.UNKNOWN,
        "Insufficient comparable change evidence; descriptive changes alone do not establish data changes.",
        compared,
    )
