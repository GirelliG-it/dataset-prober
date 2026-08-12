"""Deterministic resource assessment for v0.1 loading safety."""

from __future__ import annotations

import threading
import unicodedata
import weakref
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ResourceKind(StrEnum):
    """What the inspected resource is, independently of pipeline status."""

    DATASET = "dataset"
    DOCUMENT = "document"
    LANDING_PAGE = "landing_page"
    ERROR_RESPONSE = "error_response"
    UNKNOWN = "unknown"


class InspectionOutcome(StrEnum):
    """Whether deterministic content inspection completed."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_INSPECTED = "not_inspected"


class QueryabilityOutcome(StrEnum):
    """What deterministic inspection established about records and structure."""

    VERIFIED_NON_EMPTY = "verified_non_empty"
    STRUCTURED_EMPTY = "structured_empty"
    AMBIGUOUS = "ambiguous"
    NOT_QUERYABLE = "not_queryable"
    UNVERIFIED = "unverified"


class FormatSupport(StrEnum):
    """Whether the detected format has an implemented v0.1 loader."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


class AssessmentReason(StrEnum):
    """Stable machine-readable reasons for assessment outcomes."""

    VERIFIED_TABULAR_DATA = "verified_tabular_data"
    DOCUMENT_SIGNATURE = "document_signature"
    PDF_CONTENT = "pdf_content"
    HTML_CONTENT = "html_content"
    ERROR_RESPONSE = "error_response"
    EMPTY_CONTENT = "empty_content"
    STRUCTURED_EMPTY = "structured_empty"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INSPECTION_FAILED = "inspection_failed"
    AMBIGUOUS_SINGLE_COLUMN = "ambiguous_single_column"
    AMBIGUOUS_STRUCTURE = "ambiguous_structure"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    UNKNOWN_UNVERIFIED = "unknown_unverified"


class ResourceClassificationError(ValueError):
    """A report-only resource attempted to cross a loading boundary."""


_AssessmentState = tuple[
    ResourceKind,
    InspectionOutcome,
    QueryabilityOutcome,
    FormatSupport,
]

_VERIFIED_DATA: _AssessmentState = (
    ResourceKind.DATASET,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.VERIFIED_NON_EMPTY,
    FormatSupport.SUPPORTED,
)
_DOCUMENT: _AssessmentState = (
    ResourceKind.DOCUMENT,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_LANDING_PAGE: _AssessmentState = (
    ResourceKind.LANDING_PAGE,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_ERROR_RESPONSE: _AssessmentState = (
    ResourceKind.ERROR_RESPONSE,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_EMPTY: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.SUPPORTED,
)
_STRUCTURED_EMPTY: _AssessmentState = (
    ResourceKind.DATASET,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.STRUCTURED_EMPTY,
    FormatSupport.SUPPORTED,
)
_UNINSPECTED_UNSUPPORTED: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.NOT_INSPECTED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNSUPPORTED,
)
_INSPECTED_UNSUPPORTED: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_INSPECTION_FAILED: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.FAILED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNVERIFIED,
)
_AMBIGUOUS: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.AMBIGUOUS,
    FormatSupport.SUPPORTED,
)
_UNKNOWN: _AssessmentState = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.NOT_INSPECTED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNVERIFIED,
)

_REASON_STATE_COMPATIBILITY: dict[AssessmentReason, frozenset[_AssessmentState]] = {
    AssessmentReason.VERIFIED_TABULAR_DATA: frozenset({_VERIFIED_DATA}),
    AssessmentReason.DOCUMENT_SIGNATURE: frozenset({_DOCUMENT}),
    AssessmentReason.PDF_CONTENT: frozenset({_DOCUMENT}),
    AssessmentReason.HTML_CONTENT: frozenset({_LANDING_PAGE}),
    AssessmentReason.ERROR_RESPONSE: frozenset({_ERROR_RESPONSE}),
    AssessmentReason.EMPTY_CONTENT: frozenset({_EMPTY}),
    AssessmentReason.STRUCTURED_EMPTY: frozenset({_STRUCTURED_EMPTY}),
    AssessmentReason.UNSUPPORTED_FORMAT: frozenset(
        {_UNINSPECTED_UNSUPPORTED, _INSPECTED_UNSUPPORTED}
    ),
    AssessmentReason.INSPECTION_FAILED: frozenset({_INSPECTION_FAILED}),
    AssessmentReason.AMBIGUOUS_SINGLE_COLUMN: frozenset({_AMBIGUOUS}),
    AssessmentReason.AMBIGUOUS_STRUCTURE: frozenset({_AMBIGUOUS}),
    AssessmentReason.CONTRADICTORY_EVIDENCE: frozenset({_INSPECTED_UNSUPPORTED}),
    AssessmentReason.UNKNOWN_UNVERIFIED: frozenset({_UNKNOWN}),
}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ResourceAssessment:
    """Immutable, internally consistent facts from deterministic inspection."""

    resource_kind: ResourceKind
    inspection_outcome: InspectionOutcome
    queryability_outcome: QueryabilityOutcome
    format_support: FormatSupport
    reason: AssessmentReason
    explanation: str = ""

    def __post_init__(self) -> None:
        queryability = self.queryability_outcome
        if (
            self.format_support is FormatSupport.SUPPORTED
            and self.inspection_outcome is not InspectionOutcome.SUCCEEDED
        ):
            raise ValueError("Supported format requires successful inspection")
        if queryability in {
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
            QueryabilityOutcome.STRUCTURED_EMPTY,
        }:
            if self.inspection_outcome is not InspectionOutcome.SUCCEEDED:
                raise ValueError("Verified structure requires successful inspection")
            if self.resource_kind is not ResourceKind.DATASET:
                raise ValueError("Verified structure requires dataset resource kind")
            if self.format_support is not FormatSupport.SUPPORTED:
                raise ValueError("Verified structure requires supported format")

        if self.inspection_outcome is InspectionOutcome.FAILED and queryability is not (
            QueryabilityOutcome.UNVERIFIED
        ):
            raise ValueError("Failed inspection cannot establish queryability")
        if self.inspection_outcome is InspectionOutcome.NOT_INSPECTED and queryability is not (
            QueryabilityOutcome.UNVERIFIED
        ):
            raise ValueError("Uninspected content cannot establish queryability")
        if self.resource_kind in {
            ResourceKind.DOCUMENT,
            ResourceKind.LANDING_PAGE,
            ResourceKind.ERROR_RESPONSE,
        } and queryability in {
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
            QueryabilityOutcome.STRUCTURED_EMPTY,
        }:
            raise ValueError("Non-dataset resources cannot be verified as tabular")

        state = (
            self.resource_kind,
            self.inspection_outcome,
            queryability,
            self.format_support,
        )
        compatible_states = _REASON_STATE_COMPATIBILITY.get(self.reason)
        if compatible_states is None or state not in compatible_states:
            reason = getattr(self.reason, "value", str(self.reason))
            raise ValueError(f"Assessment reason {reason} is incompatible with assessment state")

    @property
    def _meets_eligibility_facts(self) -> bool:
        return (
            self.resource_kind is ResourceKind.DATASET
            and self.inspection_outcome is InspectionOutcome.SUCCEEDED
            and self.queryability_outcome is QueryabilityOutcome.VERIFIED_NON_EMPTY
            and self.format_support is FormatSupport.SUPPORTED
            and self.reason is AssessmentReason.VERIFIED_TABULAR_DATA
        )

    @property
    def load_eligible(self) -> bool:
        """Derive eligibility from facts plus classifier-issued inspection evidence."""
        return self._meets_eligibility_facts and _has_classifier_evidence(self)

    def to_dict(self) -> dict[str, str | bool]:
        """Return stable additive fields for reports and serialized results."""
        return {
            "resource_kind": self.resource_kind.value,
            "inspection_outcome": self.inspection_outcome.value,
            "queryability_outcome": self.queryability_outcome.value,
            "format_support": self.format_support.value,
            "load_eligible": self.load_eligible,
            "assessment_reason": self.reason.value,
            "assessment_explanation": self.explanation,
        }


_classifier_evidence_lock = threading.Lock()


@dataclass(slots=True)
class _ClassifierEvidence:
    reference: weakref.ReferenceType[ResourceAssessment]
    token: object
    candidate_identity: object | None = None


_classifier_evidence: dict[int, _ClassifierEvidence] = {}


def _forget_classifier_evidence(
    key: int,
    reference: weakref.ReferenceType[ResourceAssessment],
) -> None:
    with _classifier_evidence_lock:
        current = _classifier_evidence.get(key)
        if current is not None and current.reference is reference:
            _classifier_evidence.pop(key, None)


def _issue_classifier_evidence(assessment: ResourceAssessment) -> ResourceAssessment:
    """Mark one exact classifier result as eligible without making the token public."""
    if not assessment._meets_eligibility_facts:
        raise ValueError("Classifier evidence can only be issued for verified tabular facts")
    key = id(assessment)
    token = object()
    reference = weakref.ref(
        assessment,
        lambda caught, evidence_key=key: _forget_classifier_evidence(evidence_key, caught),
    )
    with _classifier_evidence_lock:
        _classifier_evidence[key] = _ClassifierEvidence(reference, token)
    return assessment


def _bind_classifier_evidence(
    assessment: ResourceAssessment,
    candidate_identity: object,
) -> ResourceAssessment:
    """Bind exact issued evidence to one immutable inspected candidate identity."""
    if candidate_identity is None:
        raise ValueError("Classifier evidence requires a candidate identity")
    if not assessment._meets_eligibility_facts:
        return assessment
    with _classifier_evidence_lock:
        evidence = _classifier_evidence.get(id(assessment))
        if evidence is None or evidence.reference() is not assessment:
            raise ResourceClassificationError("Assessment has no classifier-issued evidence")
        if evidence.candidate_identity is not None and evidence.candidate_identity != (
            candidate_identity
        ):
            raise ResourceClassificationError(
                "Classifier evidence is already bound to another candidate"
            )
        evidence.candidate_identity = candidate_identity
    return assessment


def _classifier_evidence_token_for_candidate(
    assessment: ResourceAssessment,
    candidate_identity: object,
) -> object | None:
    """Return evidence only when the exact live assessment is bound to this candidate."""
    if not isinstance(assessment, ResourceAssessment):
        return None
    with _classifier_evidence_lock:
        evidence = _classifier_evidence.get(id(assessment))
        if (
            evidence is None
            or evidence.reference() is not assessment
            or evidence.candidate_identity != candidate_identity
        ):
            return None
        return evidence.token


def _has_classifier_evidence(assessment: ResourceAssessment) -> bool:
    if not isinstance(assessment, ResourceAssessment):
        return False
    with _classifier_evidence_lock:
        evidence = _classifier_evidence.get(id(assessment))
        return (
            evidence is not None
            and evidence.reference() is assessment
            and evidence.candidate_identity is not None
        )


def unknown_assessment(
    reason: AssessmentReason = AssessmentReason.UNKNOWN_UNVERIFIED,
    explanation: str = "Resource has not been deterministically verified",
) -> ResourceAssessment:
    """Return the safe default used by legacy constructors and discovery-only results."""
    if reason is not AssessmentReason.UNKNOWN_UNVERIFIED:
        raise ValueError("Unknown assessment reason must be unknown_unverified")
    return ResourceAssessment(
        resource_kind=ResourceKind.UNKNOWN,
        inspection_outcome=InspectionOutcome.NOT_INSPECTED,
        queryability_outcome=QueryabilityOutcome.UNVERIFIED,
        format_support=FormatSupport.UNVERIFIED,
        reason=reason,
        explanation=explanation,
    )


def _assessment(
    *,
    kind: ResourceKind,
    inspection: InspectionOutcome,
    queryability: QueryabilityOutcome,
    support: FormatSupport,
    reason: AssessmentReason,
    explanation: str,
) -> ResourceAssessment:
    assessment = ResourceAssessment(
        resource_kind=kind,
        inspection_outcome=inspection,
        queryability_outcome=queryability,
        format_support=support,
        reason=reason,
        explanation=explanation,
    )
    if assessment._meets_eligibility_facts:
        return _issue_classifier_evidence(assessment)
    return assessment


def unsupported_format_assessment(format_name: str | None) -> ResourceAssessment:
    """Classify recognized or unproven formats without pretending to inspect them."""
    detail = format_name or "unknown"
    return _assessment(
        kind=ResourceKind.UNKNOWN,
        inspection=InspectionOutcome.NOT_INSPECTED,
        queryability=QueryabilityOutcome.UNVERIFIED,
        support=FormatSupport.UNSUPPORTED,
        reason=AssessmentReason.UNSUPPORTED_FORMAT,
        explanation=f"No v0.1 loader is implemented for format {detail}",
    )


def inspection_failed_assessment(explanation: str) -> ResourceAssessment:
    """Classify a resource whose deterministic inspection did not complete."""
    return _assessment(
        kind=ResourceKind.UNKNOWN,
        inspection=InspectionOutcome.FAILED,
        queryability=QueryabilityOutcome.UNVERIFIED,
        support=FormatSupport.UNVERIFIED,
        reason=AssessmentReason.INSPECTION_FAILED,
        explanation=explanation,
    )


def error_response_assessment(explanation: str) -> ResourceAssessment:
    """Classify an explicitly inspected source or API error envelope."""
    return _assessment(
        kind=ResourceKind.ERROR_RESPONSE,
        inspection=InspectionOutcome.SUCCEEDED,
        queryability=QueryabilityOutcome.NOT_QUERYABLE,
        support=FormatSupport.UNSUPPORTED,
        reason=AssessmentReason.ERROR_RESPONSE,
        explanation=explanation,
    )


def inspection_error_assessment(error: BaseException) -> ResourceAssessment:
    """Map typed deterministic retrieval evidence without parsing error text."""
    if str(getattr(error, "content_type", "")).upper() == "HTML":
        return _assessment(
            kind=ResourceKind.LANDING_PAGE,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
            explanation="Declared HTML content is report-only",
        )
    return inspection_failed_assessment(f"Resource inspection failed ({type(error).__name__})")


def is_error_envelope(value: Any) -> bool:
    """Return True only for explicit, conventional machine-readable error evidence."""
    if not isinstance(value, dict):
        return False
    keys = {str(key).strip().lower() for key in value}
    if keys.intersection({"error", "errors", "exception"}):
        return True
    status = value.get("status")
    return isinstance(status, int) and not isinstance(status, bool) and status >= 400


def _is_markup_name_start(character: str) -> bool:
    """Return whether one character can conservatively start an XML-like name."""
    return character in {"_", ":"} or unicodedata.category(character) in {
        "Lu",
        "Ll",
        "Lt",
        "Lm",
        "Lo",
        "Nl",
    }


def _looks_like_document_markup(leading: bytes) -> bool:
    """Recognize structural markup prefixes without enumerating element names.

    An unquoted first header beginning with an element-like prefix is refused as
    ambiguous even when a CSV parser could interpret it as a column name.
    """
    text = leading.decode("utf-8", errors="replace")
    if not text.startswith("<"):
        return False

    position = 1
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text):
        return False

    marker = text[position]
    if marker in {"?", "!"}:
        return True
    if marker == "/":
        position += 1
        while position < len(text) and text[position].isspace():
            position += 1
        return position >= len(text) or _is_markup_name_start(text[position])
    return _is_markup_name_start(marker)


def classify_blocking_content(
    *,
    empty: bool,
    leading_bytes: bytes,
    content_type: str | None,
    json_detected: bool,
    json_value: Any = None,
    format_conflict: bool = False,
) -> ResourceAssessment | None:
    """Apply strong content and contradiction rules before any CSV parser result."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    leading = leading_bytes.lower()

    if leading.startswith(b"%pdf-"):
        return _assessment(
            kind=ResourceKind.DOCUMENT,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.PDF_CONTENT,
            explanation="Inspected payload has a PDF document signature",
        )
    if leading.startswith(
        (
            b"par1",
            b"pk\x03\x04",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        )
    ):
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.UNSUPPORTED_FORMAT,
            explanation=("Inspected payload has a machine-readable format without a v0.1 loader"),
        )
    if leading.startswith((b"<?xml", b"<rss", b"<feed")):
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.UNSUPPORTED_FORMAT,
            explanation="Inspected payload is XML, for which no v0.1 loader is implemented",
        )
    if _looks_like_document_markup(leading_bytes):
        return _assessment(
            kind=ResourceKind.LANDING_PAGE,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
            explanation="Inspected payload begins with document-like markup",
        )
    if json_detected:
        if is_error_envelope(json_value):
            return error_response_assessment(
                "Inspected payload contains a machine-readable error envelope"
            )
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.CONTRADICTORY_EVIDENCE,
            explanation="JSON-like content contradicts the admitted CSV resource",
        )
    if empty:
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.SUPPORTED,
            reason=AssessmentReason.EMPTY_CONTENT,
            explanation="Inspected payload is empty or whitespace-only",
        )
    if mime in {"text/html", "application/xhtml+xml"}:
        return _assessment(
            kind=ResourceKind.LANDING_PAGE,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.HTML_CONTENT,
            explanation="Declared HTML content is report-only",
        )
    if mime in {
        "application/pdf",
        "application/json",
        "application/geo+json",
        "application/xml",
        "text/xml",
        "application/zip",
        "application/vnd.apache.parquet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.CONTRADICTORY_EVIDENCE,
            explanation=f"Declared content type {mime} contradicts the admitted CSV resource",
        )
    if format_conflict:
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.NOT_QUERYABLE,
            support=FormatSupport.UNSUPPORTED,
            reason=AssessmentReason.CONTRADICTORY_EVIDENCE,
            explanation="Final resource format contradicts the admitted CSV resource",
        )
    return None


def classify_tabular_structure(columns: list[dict], row_count: int | None) -> ResourceAssessment:
    """Classify a parsed CSV relation using deterministic schema and row evidence."""
    if row_count is None:
        return inspection_failed_assessment("Exact row counting did not complete")
    if not columns:
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.AMBIGUOUS,
            support=FormatSupport.SUPPORTED,
            reason=AssessmentReason.AMBIGUOUS_STRUCTURE,
            explanation="CSV parser produced no coherent columns",
        )

    names = [str(column.get("name", "")) for column in columns]
    generic = [name.startswith("column") and name[6:].isdigit() for name in names]
    if not all(names) or all(generic):
        return _assessment(
            kind=ResourceKind.UNKNOWN,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.AMBIGUOUS,
            support=FormatSupport.SUPPORTED,
            reason=AssessmentReason.AMBIGUOUS_STRUCTURE,
            explanation="CSV parser produced only generic or missing column identity",
        )

    if row_count == 0:
        return _assessment(
            kind=ResourceKind.DATASET,
            inspection=InspectionOutcome.SUCCEEDED,
            queryability=QueryabilityOutcome.STRUCTURED_EMPTY,
            support=FormatSupport.SUPPORTED,
            reason=AssessmentReason.STRUCTURED_EMPTY,
            explanation="Tabular columns were verified but no observations were present",
        )

    if len(columns) == 1:
        column_type = str(columns[0].get("type", "")).strip().upper()
        if column_type in {"", "VARCHAR", "CHAR", "TEXT", "BLOB"}:
            return _assessment(
                kind=ResourceKind.UNKNOWN,
                inspection=InspectionOutcome.SUCCEEDED,
                queryability=QueryabilityOutcome.AMBIGUOUS,
                support=FormatSupport.SUPPORTED,
                reason=AssessmentReason.AMBIGUOUS_SINGLE_COLUMN,
                explanation=(
                    "A string-only one-column parse cannot distinguish records from prose"
                ),
            )

    return _assessment(
        kind=ResourceKind.DATASET,
        inspection=InspectionOutcome.SUCCEEDED,
        queryability=QueryabilityOutcome.VERIFIED_NON_EMPTY,
        support=FormatSupport.SUPPORTED,
        reason=AssessmentReason.VERIFIED_TABULAR_DATA,
        explanation="Deterministic inspection verified non-empty tabular records",
    )


def classify_record_payload(
    payload: Any,
    *,
    candidate_identity: object | None = None,
) -> tuple[ResourceAssessment, list[dict]]:
    """Classify a CBS/OData-style object-record envelope."""
    if is_error_envelope(payload):
        return (
            error_response_assessment("Object response contains an explicit error envelope"),
            [],
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        return (
            inspection_failed_assessment("Object response has no record collection"),
            [],
        )

    records = payload["value"]
    if not records:
        return (
            _assessment(
                kind=ResourceKind.DATASET,
                inspection=InspectionOutcome.SUCCEEDED,
                queryability=QueryabilityOutcome.STRUCTURED_EMPTY,
                support=FormatSupport.SUPPORTED,
                reason=AssessmentReason.STRUCTURED_EMPTY,
                explanation="Object-record envelope is structurally valid but empty",
            ),
            [],
        )

    if not all(isinstance(record, dict) and record for record in records):
        return (
            _assessment(
                kind=ResourceKind.UNKNOWN,
                inspection=InspectionOutcome.SUCCEEDED,
                queryability=QueryabilityOutcome.AMBIGUOUS,
                support=FormatSupport.SUPPORTED,
                reason=AssessmentReason.AMBIGUOUS_STRUCTURE,
                explanation="Record collection contains non-object or empty observations",
            ),
            [],
        )
    first_keys = tuple(records[0].keys())
    if any(tuple(record.keys()) != first_keys for record in records[1:]):
        return (
            _assessment(
                kind=ResourceKind.UNKNOWN,
                inspection=InspectionOutcome.SUCCEEDED,
                queryability=QueryabilityOutcome.AMBIGUOUS,
                support=FormatSupport.SUPPORTED,
                reason=AssessmentReason.AMBIGUOUS_STRUCTURE,
                explanation="Object observations do not share one coherent field structure",
            ),
            [],
        )

    assessment = _assessment(
        kind=ResourceKind.DATASET,
        inspection=InspectionOutcome.SUCCEEDED,
        queryability=QueryabilityOutcome.VERIFIED_NON_EMPTY,
        support=FormatSupport.SUPPORTED,
        reason=AssessmentReason.VERIFIED_TABULAR_DATA,
        explanation="Object-record envelope verifies non-empty structured observations",
    )
    if candidate_identity is not None:
        _bind_classifier_evidence(assessment, candidate_identity)
    return assessment, records
