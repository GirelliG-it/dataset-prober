"""Immutable metadata evidence, separate from freshness policy and monitoring history."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from dataset_prober.loading_policy import sanitize_url_text


class ClaimPresence(StrEnum):
    ABSENT = "ABSENT"
    NULL = "NULL"
    WRONG_TYPE = "WRONG_TYPE"
    STRING = "STRING"


class TimestampSemantics(StrEnum):
    CATALOG_METADATA_UPDATE = "CATALOG_METADATA_UPDATE"
    DATASET_CONTENT_UPDATE = "DATASET_CONTENT_UPDATE"
    UNVERIFIED = "UNVERIFIED"


class CollectionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


def utc_now() -> datetime:
    """Clock seam used once at each metadata collection boundary."""
    return datetime.now(UTC)


def _safe_json(value):
    if isinstance(value, str):
        return sanitize_url_text(value)
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Provider JSON object keys must be strings")
        safe = {}
        for key, item in value.items():
            safe_key = sanitize_url_text(key)
            if safe_key in safe:
                raise ValueError("Provider JSON keys collide after sanitization")
            safe[safe_key] = _safe_json(item)
        return safe
    if value is None or type(value) in (bool, int, float):
        return value
    raise TypeError("Provider claim must be a JSON value")


@dataclass(frozen=True, slots=True)
class TimestampClaim:
    """A provider field plus collector annotations, with no date interpretation.

    JSON text is the immutable retained value, including wrong-type arrays/objects.
    to_dict returns a detached JSON value, never a reference to provider objects.
    """

    resource: str
    field_path: str
    presence: ClaimPresence
    value_json: str
    semantics: TimestampSemantics
    sanitized: bool = False

    def __post_init__(self):
        object.__setattr__(self, "presence", ClaimPresence(self.presence))
        object.__setattr__(self, "semantics", TimestampSemantics(self.semantics))
        if not all(
            isinstance(value, str) and value
            for value in (self.resource, self.field_path, self.value_json)
        ):
            raise ValueError("Claim identity and JSON value must be nonempty strings")
        value = json.loads(self.value_json)
        # Reject nonstandard constants and lossy/noncanonical encodings.
        if json.dumps(value, ensure_ascii=True, allow_nan=False) != self.value_json:
            raise ValueError("Claim value must use canonical JSON encoding")
        expected = (
            ClaimPresence.NULL
            if value is None
            else (ClaimPresence.STRING if isinstance(value, str) else ClaimPresence.WRONG_TYPE)
        )
        if self.presence != expected and not (
            self.presence is ClaimPresence.ABSENT and value is None
        ):
            raise ValueError("Claim presence does not match its value")
        if type(self.sanitized) is not bool:
            raise TypeError("sanitized must be a boolean")

    @classmethod
    def capture(
        cls,
        metadata: dict,
        field_name: str,
        *,
        resource: str,
        field_path: str,
        semantics: TimestampSemantics,
    ):
        value = metadata.get(field_name)
        presence = (
            ClaimPresence.ABSENT
            if field_name not in metadata
            else ClaimPresence.NULL
            if value is None
            else ClaimPresence.STRING
            if isinstance(value, str)
            else ClaimPresence.WRONG_TYPE
        )
        safe = _safe_json(value)
        encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False)
        if json.loads(encoded) != safe:
            raise ValueError("Claim cannot be represented exactly in JSON")
        return cls(resource, field_path, presence, encoded, semantics, safe != value)

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "field_path": self.field_path,
            "presence": self.presence.value,
            "value": json.loads(self.value_json),
            "semantics": self.semantics.value,
            "sanitized": self.sanitized,
        }


@dataclass(frozen=True, slots=True)
class FreshnessObservation:
    source: str
    catalog_identity: str
    dataset_id: str
    resource_id: str | None
    resource_identity: str | None
    metadata_endpoint: str
    observed_at: datetime
    collection_status: CollectionStatus
    collector_rule_version: str
    claims: tuple[TimestampClaim, ...] = ()
    reason_code: str | None = None
    observation_id: str = field(default_factory=lambda: str(uuid4()))
    record_version: str = "1"

    def __post_init__(self):
        object.__setattr__(self, "collection_status", CollectionStatus(self.collection_status))
        object.__setattr__(self, "claims", tuple(self.claims))
        if any(not isinstance(claim, TimestampClaim) for claim in self.claims):
            raise TypeError("claims must contain immutable TimestampClaim objects")
        for name in (
            "source",
            "catalog_identity",
            "dataset_id",
            "metadata_endpoint",
            "collector_rule_version",
            "observation_id",
            "record_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        for value in (self.resource_id, self.resource_identity, self.reason_code):
            if value is not None and not isinstance(value, str):
                raise TypeError("Optional identities and reason must be strings")
        if (
            type(self.observed_at) is not datetime
            or self.observed_at.tzinfo is not UTC
            or self.observed_at.fold
        ):
            raise ValueError("observed_at requires a standard UTC datetime with fold=0")
        if self.collection_status is not CollectionStatus.SUCCEEDED and not self.reason_code:
            raise ValueError("Unsuccessful metadata collection requires a reason")

    def to_dict(self) -> dict:
        return {
            "record_version": self.record_version,
            "collector_rule_version": self.collector_rule_version,
            "observation_id": self.observation_id,
            "source": self.source,
            "catalog_identity": self.catalog_identity,
            "dataset_id": self.dataset_id,
            "resource_id": self.resource_id,
            "resource_identity": self.resource_identity,
            "metadata_endpoint": self.metadata_endpoint,
            "observed_at": self.observed_at.isoformat(timespec="microseconds"),
            "collection_status": self.collection_status.value,
            "reason_code": self.reason_code,
            "claims": [claim.to_dict() for claim in self.claims],
        }
