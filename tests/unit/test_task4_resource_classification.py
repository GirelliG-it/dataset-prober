"""Task 4 deterministic resource-classification contracts."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from dataset_prober.loading_policy import (
    canonical_candidate_identity,
    configured_adapter_identity,
)
from dataset_prober.resource_classification import (
    AssessmentReason,
    FormatSupport,
    InspectionOutcome,
    QueryabilityOutcome,
    ResourceAssessment,
    ResourceKind,
    inspection_failed_assessment,
    unknown_assessment,
)
from dataset_prober.tools.guards import FetchedResource
from tests.conftest import eligible_assessment_for_candidate


def verified_assessment() -> ResourceAssessment:
    return eligible_assessment_for_candidate(
        source_key="manual",
        adapter_identity="Manual URL",
        resource_id="https://public.example/data.csv",
        retrieval_url="https://public.example/data.csv",
    )


def caller_assembled_eligible_assessment() -> ResourceAssessment:
    return ResourceAssessment(
        resource_kind=ResourceKind.DATASET,
        inspection_outcome=InspectionOutcome.SUCCEEDED,
        queryability_outcome=QueryabilityOutcome.VERIFIED_NON_EMPTY,
        format_support=FormatSupport.SUPPORTED,
        reason=AssessmentReason.VERIFIED_TABULAR_DATA,
    )


def test_eligibility_is_derived_from_complete_assessment():
    verified = verified_assessment()
    empty = ResourceAssessment(
        resource_kind=ResourceKind.DATASET,
        inspection_outcome=InspectionOutcome.SUCCEEDED,
        queryability_outcome=QueryabilityOutcome.STRUCTURED_EMPTY,
        format_support=FormatSupport.SUPPORTED,
        reason=AssessmentReason.STRUCTURED_EMPTY,
    )

    assert verified.load_eligible is True
    assert empty.load_eligible is False
    assert "load_eligible" not in ResourceAssessment.__dataclass_fields__


def test_direct_construction_cannot_establish_classifier_issued_eligibility():
    assessment = caller_assembled_eligible_assessment()

    assert assessment.load_eligible is False
    assert assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA


def test_copy_replace_and_reconstruction_do_not_copy_classifier_evidence():
    classified = verified_assessment()
    reconstructed = ResourceAssessment(
        resource_kind=classified.resource_kind,
        inspection_outcome=classified.inspection_outcome,
        queryability_outcome=classified.queryability_outcome,
        format_support=classified.format_support,
        reason=classified.reason,
        explanation=classified.explanation,
    )

    assert classified.load_eligible is True
    assert copy(classified).load_eligible is False
    assert replace(classified).load_eligible is False
    assert reconstructed.load_eligible is False


@pytest.mark.parametrize(
    "values",
    [
        {
            "resource_kind": ResourceKind.DOCUMENT,
            "inspection_outcome": InspectionOutcome.SUCCEEDED,
            "queryability_outcome": QueryabilityOutcome.VERIFIED_NON_EMPTY,
            "format_support": FormatSupport.UNSUPPORTED,
            "reason": AssessmentReason.DOCUMENT_SIGNATURE,
        },
        {
            "resource_kind": ResourceKind.DATASET,
            "inspection_outcome": InspectionOutcome.FAILED,
            "queryability_outcome": QueryabilityOutcome.VERIFIED_NON_EMPTY,
            "format_support": FormatSupport.SUPPORTED,
            "reason": AssessmentReason.INSPECTION_FAILED,
        },
        {
            "resource_kind": ResourceKind.DATASET,
            "inspection_outcome": InspectionOutcome.SUCCEEDED,
            "queryability_outcome": QueryabilityOutcome.STRUCTURED_EMPTY,
            "format_support": FormatSupport.SUPPORTED,
            "reason": AssessmentReason.VERIFIED_TABULAR_DATA,
        },
        {
            "resource_kind": ResourceKind.UNKNOWN,
            "inspection_outcome": InspectionOutcome.NOT_INSPECTED,
            "queryability_outcome": QueryabilityOutcome.UNVERIFIED,
            "format_support": FormatSupport.SUPPORTED,
            "reason": AssessmentReason.UNSUPPORTED_FORMAT,
        },
        {
            "resource_kind": ResourceKind.DATASET,
            "inspection_outcome": InspectionOutcome.SUCCEEDED,
            "queryability_outcome": QueryabilityOutcome.VERIFIED_NON_EMPTY,
            "format_support": FormatSupport.SUPPORTED,
            "reason": AssessmentReason.CONTRADICTORY_EVIDENCE,
        },
    ],
)
def test_invalid_assessment_combinations_are_refused(values):
    with pytest.raises(ValueError):
        ResourceAssessment(**values)


def test_reason_codes_and_assessment_serialize_predictably():
    serialized = verified_assessment().to_dict()

    assert serialized == {
        "resource_kind": "dataset",
        "inspection_outcome": "succeeded",
        "queryability_outcome": "verified_non_empty",
        "format_support": "supported",
        "load_eligible": True,
        "assessment_reason": "verified_tabular_data",
        "assessment_explanation": (
            "Object-record envelope verifies non-empty structured observations"
        ),
    }


def test_assessment_is_immutable():
    assessment = verified_assessment()

    with pytest.raises(FrozenInstanceError):
        assessment.reason = AssessmentReason.UNKNOWN_UNVERIFIED


def test_lifecycle_status_cannot_create_eligibility():
    assessment = unknown_assessment()

    assert assessment.load_eligible is False
    assert assessment.reason is AssessmentReason.UNKNOWN_UNVERIFIED


def test_failed_inspection_does_not_claim_verified_format_support():
    assessment = inspection_failed_assessment("Inspection did not complete")

    assert assessment.resource_kind is ResourceKind.UNKNOWN
    assert assessment.inspection_outcome is InspectionOutcome.FAILED
    assert assessment.queryability_outcome is QueryabilityOutcome.UNVERIFIED
    assert assessment.format_support is FormatSupport.UNVERIFIED
    assert assessment.reason is AssessmentReason.INSPECTION_FAILED
    assert assessment.load_eligible is False


@pytest.mark.parametrize(
    ("inspection_outcome", "reason"),
    [
        (InspectionOutcome.FAILED, AssessmentReason.INSPECTION_FAILED),
        (InspectionOutcome.NOT_INSPECTED, AssessmentReason.UNKNOWN_UNVERIFIED),
    ],
)
def test_supported_format_requires_successful_inspection(inspection_outcome, reason):
    with pytest.raises(ValueError, match="Supported format requires successful inspection"):
        ResourceAssessment(
            resource_kind=ResourceKind.UNKNOWN,
            inspection_outcome=inspection_outcome,
            queryability_outcome=QueryabilityOutcome.UNVERIFIED,
            format_support=FormatSupport.SUPPORTED,
            reason=reason,
        )


def test_unknown_assessment_rejects_content_specific_reason():
    with pytest.raises(ValueError, match="Unknown assessment reason must be unknown_unverified"):
        unknown_assessment(AssessmentReason.PDF_CONTENT)


def test_direct_construction_cannot_attach_content_reason_without_inspection():
    with pytest.raises(ValueError, match="pdf_content is incompatible"):
        ResourceAssessment(
            resource_kind=ResourceKind.UNKNOWN,
            inspection_outcome=InspectionOutcome.NOT_INSPECTED,
            queryability_outcome=QueryabilityOutcome.UNVERIFIED,
            format_support=FormatSupport.UNVERIFIED,
            reason=AssessmentReason.PDF_CONTENT,
        )


_VERIFIED_STATE = (
    ResourceKind.DATASET,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.VERIFIED_NON_EMPTY,
    FormatSupport.SUPPORTED,
)
_DOCUMENT_STATE = (
    ResourceKind.DOCUMENT,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_LANDING_STATE = (
    ResourceKind.LANDING_PAGE,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_ERROR_STATE = (
    ResourceKind.ERROR_RESPONSE,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_EMPTY_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.SUPPORTED,
)
_STRUCTURED_EMPTY_STATE = (
    ResourceKind.DATASET,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.STRUCTURED_EMPTY,
    FormatSupport.SUPPORTED,
)
_UNINSPECTED_UNSUPPORTED_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.NOT_INSPECTED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNSUPPORTED,
)
_INSPECTED_UNSUPPORTED_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.NOT_QUERYABLE,
    FormatSupport.UNSUPPORTED,
)
_FAILED_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.FAILED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNVERIFIED,
)
_AMBIGUOUS_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.SUCCEEDED,
    QueryabilityOutcome.AMBIGUOUS,
    FormatSupport.SUPPORTED,
)
_UNKNOWN_STATE = (
    ResourceKind.UNKNOWN,
    InspectionOutcome.NOT_INSPECTED,
    QueryabilityOutcome.UNVERIFIED,
    FormatSupport.UNVERIFIED,
)


@pytest.mark.parametrize(
    ("reason", "state"),
    [
        (AssessmentReason.VERIFIED_TABULAR_DATA, _VERIFIED_STATE),
        (AssessmentReason.DOCUMENT_SIGNATURE, _DOCUMENT_STATE),
        (AssessmentReason.PDF_CONTENT, _DOCUMENT_STATE),
        (AssessmentReason.HTML_CONTENT, _LANDING_STATE),
        (AssessmentReason.ERROR_RESPONSE, _ERROR_STATE),
        (AssessmentReason.EMPTY_CONTENT, _EMPTY_STATE),
        (AssessmentReason.STRUCTURED_EMPTY, _STRUCTURED_EMPTY_STATE),
        (AssessmentReason.UNSUPPORTED_FORMAT, _UNINSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.UNSUPPORTED_FORMAT, _INSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.INSPECTION_FAILED, _FAILED_STATE),
        (AssessmentReason.AMBIGUOUS_SINGLE_COLUMN, _AMBIGUOUS_STATE),
        (AssessmentReason.AMBIGUOUS_STRUCTURE, _AMBIGUOUS_STATE),
        (AssessmentReason.CONTRADICTORY_EVIDENCE, _INSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.UNKNOWN_UNVERIFIED, _UNKNOWN_STATE),
    ],
)
def test_every_assessment_reason_accepts_its_canonical_state(reason, state):
    assessment = ResourceAssessment(*state, reason)

    assert assessment.reason is reason


@pytest.mark.parametrize(
    ("reason", "incompatible_state"),
    [
        (AssessmentReason.VERIFIED_TABULAR_DATA, _INSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.DOCUMENT_SIGNATURE, _INSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.PDF_CONTENT, _LANDING_STATE),
        (AssessmentReason.HTML_CONTENT, _DOCUMENT_STATE),
        (AssessmentReason.ERROR_RESPONSE, _LANDING_STATE),
        (AssessmentReason.EMPTY_CONTENT, _AMBIGUOUS_STATE),
        (AssessmentReason.STRUCTURED_EMPTY, _EMPTY_STATE),
        (AssessmentReason.UNSUPPORTED_FORMAT, _EMPTY_STATE),
        (AssessmentReason.INSPECTION_FAILED, _UNKNOWN_STATE),
        (AssessmentReason.AMBIGUOUS_SINGLE_COLUMN, _EMPTY_STATE),
        (AssessmentReason.AMBIGUOUS_STRUCTURE, _INSPECTED_UNSUPPORTED_STATE),
        (AssessmentReason.CONTRADICTORY_EVIDENCE, _EMPTY_STATE),
        (AssessmentReason.UNKNOWN_UNVERIFIED, _FAILED_STATE),
    ],
)
def test_every_assessment_reason_rejects_an_incompatible_state(reason, incompatible_state):
    with pytest.raises(ValueError):
        ResourceAssessment(*incompatible_state, reason)


def test_replacement_and_reconstruction_cannot_create_contradictory_assessment():
    canonical = unknown_assessment()
    contradictory_fields = {
        "resource_kind": ResourceKind.UNKNOWN,
        "inspection_outcome": InspectionOutcome.NOT_INSPECTED,
        "queryability_outcome": QueryabilityOutcome.UNVERIFIED,
        "format_support": FormatSupport.UNVERIFIED,
        "reason": AssessmentReason.PDF_CONTENT,
    }

    with pytest.raises(ValueError):
        replace(canonical, reason=AssessmentReason.PDF_CONTENT)
    with pytest.raises(ValueError):
        ResourceAssessment(**contradictory_fields)


def _inspect(tmp_path: Path, body: bytes, *, content_type="text/csv", final_url=None):
    import duckdb

    from dataset_prober.tools.base import inspect_csv_resource

    source_url = "https://public.example/data.csv"
    path = tmp_path / "resource.csv"
    path.write_bytes(body)
    fetched = FetchedResource(
        source_url=source_url,
        final_url=final_url or source_url,
        path=str(path),
        headers={"Content-Type": content_type},
    )
    connection = duckdb.connect()
    try:
        return inspect_csv_resource(
            connection,
            fetched,
            sample_rows=3,
            candidate_identity=canonical_candidate_identity(
                "manual",
                configured_adapter_identity("manual", {}),
                source_url,
                source_url,
            ),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("body", "eligible", "reason", "queryability"),
    [
        (
            b"id,name\n1,Alice\n2,Bob\n",
            True,
            AssessmentReason.VERIFIED_TABULAR_DATA,
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
        ),
        (
            b"value\n1\n2\n",
            True,
            AssessmentReason.VERIFIED_TABULAR_DATA,
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
        ),
        (
            b"code\nA\nB\n",
            False,
            AssessmentReason.AMBIGUOUS_SINGLE_COLUMN,
            QueryabilityOutcome.AMBIGUOUS,
        ),
        (
            b"This is a report\nIt explains results\nNothing tabular here\n",
            False,
            AssessmentReason.AMBIGUOUS_SINGLE_COLUMN,
            QueryabilityOutcome.AMBIGUOUS,
        ),
        (
            b"id,name\n",
            False,
            AssessmentReason.STRUCTURED_EMPTY,
            QueryabilityOutcome.STRUCTURED_EMPTY,
        ),
        (
            b"",
            False,
            AssessmentReason.EMPTY_CONTENT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b" \r\n\t\n",
            False,
            AssessmentReason.EMPTY_CONTENT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b"\xef\xbb\xbf  <!DoCtYpE HTML><html><body>login</body></html>",
            False,
            AssessmentReason.HTML_CONTENT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b" \n%PDF-1.7\n1 0 obj\n",
            False,
            AssessmentReason.PDF_CONTENT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b'{"error":"rate limited","status":429}',
            False,
            AssessmentReason.ERROR_RESPONSE,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b'{"items":[{"id":1}]}',
            False,
            AssessmentReason.CONTRADICTORY_EVIDENCE,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b"PAR1binary parquet content",
            False,
            AssessmentReason.UNSUPPORTED_FORMAT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b"PK\x03\x04binary spreadsheet or archive",
            False,
            AssessmentReason.UNSUPPORTED_FORMAT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b'<?xml version="1.0"?><records><record /></records>',
            False,
            AssessmentReason.UNSUPPORTED_FORMAT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
        (
            b"<script>window.location='/login'</script>",
            False,
            AssessmentReason.HTML_CONTENT,
            QueryabilityOutcome.NOT_QUERYABLE,
        ),
    ],
)
def test_csv_classifier_uses_content_and_structure(tmp_path, body, eligible, reason, queryability):
    inspection = _inspect(tmp_path, body)
    assessment = inspection["assessment"]

    assert assessment.load_eligible is eligible
    assert assessment.reason is reason
    assert assessment.queryability_outcome is queryability


@pytest.mark.parametrize(
    "body",
    [
        b"<div>,<span>\nrow,more\n",
        b"\xef\xbb\xbf \r\n<CuStOm-Element data-kind='report'>,<aside>\nrow,value\n",
        b"<report-widget\n data-kind='summary'>,<report-section>\nrow,value\n",
        b"< custom>,value\nrow,1\n",
        b'<?report version="1.0"?>,value\nrow,1\n',
        b'<!ENTITY report "summary">,value\nrow,1\n',
        b"<custom,value\nrow,1\n",
        b"</custom,value\nrow,1\n",
    ],
)
def test_generic_document_markup_cannot_become_csv_through_parser_success(tmp_path, body):
    assessment = _inspect(tmp_path, body, content_type="text/csv")["assessment"]

    assert assessment.load_eligible is False
    assert assessment.resource_kind in {ResourceKind.DOCUMENT, ResourceKind.LANDING_PAGE}
    assert assessment.queryability_outcome is QueryabilityOutcome.NOT_QUERYABLE
    assert assessment.reason in {
        AssessmentReason.DOCUMENT_SIGNATURE,
        AssessmentReason.HTML_CONTENT,
    }


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<_custom>,value\nrow,1\n", id="underscore-name-start"),
        pytest.param(
            "<élement>,value\nrow,1\n".encode(),
            id="unicode-name-start",
        ),
        pytest.param(b"<:section>,value\nrow,1\n", id="colon-name-start"),
        pytest.param(b"</_custom,value\nrow,1\n", id="closing-underscore"),
        pytest.param(
            "</élement,value\nrow,1\n".encode(),
            id="closing-unicode",
        ),
        pytest.param(
            b"  \n\xef\xbb\xbf<custom,value\nrow,1\n",
            id="whitespace-before-utf8-bom",
        ),
        pytest.param(
            b"\xef\xbb\xbf  \n<custom,value\nrow,1\n",
            id="utf8-bom-before-whitespace",
        ),
        pytest.param(
            "\ufeff<custom,value\nrow,1\n".encode(),
            id="encoded-leading-ufeff",
        ),
        pytest.param(
            "\ufeff  \n<_custom data-kind='summary'\n lang='nl',value\nrow,1\n".encode(),
            id="bom-whitespace-attributes-multiline",
        ),
    ],
)
def test_structural_markup_name_starts_remain_report_only(tmp_path, body):
    assessment = _inspect(tmp_path, body, content_type="text/csv")["assessment"]

    assert assessment.load_eligible is False
    assert assessment.resource_kind in {ResourceKind.DOCUMENT, ResourceKind.LANDING_PAGE}
    assert assessment.queryability_outcome is QueryabilityOutcome.NOT_QUERYABLE
    assert assessment.reason in {
        AssessmentReason.DOCUMENT_SIGNATURE,
        AssessmentReason.HTML_CONTENT,
    }


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(
            b" " * 8191 + b"\xef\xbb\xbf" + b"<_custom>,value\nrow,1\n",
            AssessmentReason.HTML_CONTENT,
            id="utf8-bom-split-after-byte-one",
        ),
        pytest.param(
            b" " * 8190 + b"\xef\xbb\xbf" + "<élement>,value\nrow,1\n".encode(),
            AssessmentReason.HTML_CONTENT,
            id="utf8-bom-split-after-byte-two",
        ),
        pytest.param(
            b" " * 8192 + b"\xef\xbb\xbf" + b"<custom,value\nrow,1\n",
            AssessmentReason.HTML_CONTENT,
            id="utf8-bom-begins-in-second-chunk",
        ),
        pytest.param(
            b"\xef\xbb\xbf"
            + b" " * 8188
            + b"\xef\xbb\xbf\n\t\xef\xbb\xbf"
            + b"<:custom>,value\nrow,1\n",
            AssessmentReason.HTML_CONTENT,
            id="repeated-whitespace-and-boms-across-chunks",
        ),
        pytest.param(
            b" " * 8191 + b"\xef\xbb\xbf\n\t\xef\xbb\xbf  ",
            AssessmentReason.EMPTY_CONTENT,
            id="split-bom-padding-only",
        ),
    ],
)
def test_leading_padding_chunk_boundaries_use_the_real_csv_inspection_path(tmp_path, body, reason):
    assessment = _inspect(tmp_path, body, content_type="text/csv")["assessment"]

    assert assessment.load_eligible is False
    assert assessment.queryability_outcome is QueryabilityOutcome.NOT_QUERYABLE
    assert assessment.reason is reason


@pytest.mark.parametrize(
    "body",
    [
        b"<5,value\n1,2\n",
        b"< 5,value\n1,2\n",
        b'expression,label\n"a < b","<comparison>"\n"c > d","plain"\n',
        b'"<custom>",value\nrow,1\n',
        '"<élement>",value\nrow,1\n'.encode(),
        b"name,value\n<custom>,1\n",
        "naam,value\n<élement>,1\n".encode(),
        "élément,value\nrow,1\n".encode(),
    ],
)
def test_isolated_angle_brackets_inside_tabular_values_do_not_imply_markup(tmp_path, body):
    assessment = _inspect(tmp_path, body)["assessment"]

    assert assessment.load_eligible is True
    assert assessment.resource_kind is ResourceKind.DATASET
    assert assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA


def test_declared_pdf_mime_without_pdf_signature_fails_closed(tmp_path):
    assessment = _inspect(
        tmp_path,
        b"id,name\n1,Alice\n",
        content_type="application/pdf",
    )["assessment"]

    assert assessment.load_eligible is False
    assert assessment.reason is AssessmentReason.CONTRADICTORY_EVIDENCE


@pytest.mark.parametrize(
    "content_type",
    [
        "application/xml",
        "application/zip",
        "application/vnd.apache.parquet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ],
)
def test_declared_unsupported_machine_format_contradicts_csv(tmp_path, content_type):
    assessment = _inspect(
        tmp_path,
        b"id,name\n1,Alice\n",
        content_type=content_type,
    )["assessment"]

    assert assessment.load_eligible is False
    assert assessment.reason is AssessmentReason.CONTRADICTORY_EVIDENCE


def test_final_non_csv_extension_contradicts_csv_admission(tmp_path):
    assessment = _inspect(
        tmp_path,
        b"id,name\n1,Alice\n",
        final_url="https://public.example/report.pdf",
    )["assessment"]

    assert assessment.load_eligible is False
    assert assessment.reason is AssessmentReason.CONTRADICTORY_EVIDENCE


def test_parser_failure_is_an_inspection_failure(monkeypatch, tmp_path):
    from dataset_prober.tools import base

    monkeypatch.setattr(
        base,
        "probe_csv_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("malformed CSV")),
    )

    assessment = _inspect(tmp_path, b'id,name\n1,"unterminated\n')["assessment"]

    assert assessment.load_eligible is False
    assert assessment.inspection_outcome is InspectionOutcome.FAILED
    assert assessment.reason is AssessmentReason.INSPECTION_FAILED


@pytest.mark.parametrize(
    ("payload", "reason", "queryability", "eligible"),
    [
        (
            {"value": [{"value": "one"}, {"value": "two"}]},
            AssessmentReason.VERIFIED_TABULAR_DATA,
            QueryabilityOutcome.VERIFIED_NON_EMPTY,
            True,
        ),
        (
            {"value": []},
            AssessmentReason.STRUCTURED_EMPTY,
            QueryabilityOutcome.STRUCTURED_EMPTY,
            False,
        ),
        (
            {"error": {"code": "Denied"}},
            AssessmentReason.ERROR_RESPONSE,
            QueryabilityOutcome.NOT_QUERYABLE,
            False,
        ),
        (
            {"message": "not a record envelope"},
            AssessmentReason.INSPECTION_FAILED,
            QueryabilityOutcome.UNVERIFIED,
            False,
        ),
    ],
)
def test_object_record_classifier_returns_same_assessment_contract(
    payload, reason, queryability, eligible
):
    from dataset_prober.resource_classification import classify_record_payload

    identity = canonical_candidate_identity(
        "cbs",
        "CBS Statistics Netherlands",
        "83583NED",
        "https://opendata.cbs.nl/ODataApi/odata/83583NED/TypedDataSet",
    )
    assessment, records = classify_record_payload(payload, candidate_identity=identity)

    assert assessment.reason is reason
    assert assessment.queryability_outcome is queryability
    assert assessment.load_eligible is eligible
    assert bool(records) is eligible
