"""Run-local freshness grounding and reporting; no provider collection or history store."""

from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlencode
from uuid import UUID, uuid4

from dataset_prober.freshness_evaluation import ClaimReference, FreshnessEvaluation
from dataset_prober.freshness_observation import FreshnessObservation
from dataset_prober.loading_policy import safe_url_identity, sanitize_url_text
from dataset_prober.tools.cbs_tool import _CBS_ODATA_BASE


@dataclass(frozen=True)
class SelectionRule:
    version: str
    collector_version: str
    field_path: str
    semantics: str


SELECTION_RULES = MappingProxyType(
    {
        "cbs": SelectionRule(
            "cbs-modified-selection-v1",
            "cbs-tableinfos-freshness-v1",
            "/value/0/Modified",
            "UNVERIFIED",
        ),
        "ckan": SelectionRule(
            "ckan-modified-selection-v1",
            "ckan-package-freshness-v1",
            "/result/metadata_modified",
            "CATALOG_METADATA_UPDATE",
        ),
    }
)

_GROUNDING_EXPLANATIONS = MappingProxyType(
    {
        "candidate_not_inspected": "No fetched candidate matches this exact source and dataset ID.",
        "candidate_identity_ambiguous": "More than one fetched candidate matches this exact identity.",
        "observation_missing": "The candidate has no captured freshness observation; its legacy date is not evidence for this check.",
        "observation_not_captured": "The observation has no fetch association in this run.",
        "observation_link_changed": "The candidate's attached observation changed after collection.",
        "observation_identity_mismatch": "The observation, fetch request and candidate identities do not match exactly; no aliases are inferred.",
        "observation_catalog_mismatch": "The observation does not match the configured source/catalog used for this fetch.",
        "observation_resource_mismatch": "The observation does not match the candidate's captured resource identity.",
        "observation_endpoint_mismatch": "The metadata endpoint does not match the collector's configured request.",
        "collector_not_supported": "No selection policy supports this observation format and collector version.",
        "claim_not_found": "The collector's required timestamp field was not captured.",
        "claim_identity_ambiguous": "The required timestamp field occurs more than once; no claim was selected.",
        "claim_scope_mismatch": "The selected claim's resource or semantics do not match the collector contract.",
        "observation_id_conflict": "Different captured observation payloads share an ID; no evaluation was created.",
    }
)

_EVALUATION_EXPLANATIONS = MappingProxyType(
    {
        "collection_failed": "Metadata collection failed; dataset-content freshness is unknown.",
        "collection_blocked": "Metadata collection was blocked; dataset-content freshness is unknown.",
        "timestamp_absent": "Metadata was collected but no timestamp evidence was provided.",
        "timestamp_null": "Metadata was collected but the timestamp is explicitly null.",
        "timestamp_wrong_type": "Metadata was collected but the timestamp has the wrong value type.",
        "sanitized_evidence": "The retained timestamp evidence was sanitized and cannot establish a freshness verdict.",
        "invalid_timestamp": "Metadata was collected but the complete timestamp is invalid or unusable.",
        "unsupported_leap_second": "The timestamp uses unsupported leap-second notation.",
        "unsupported_precision": "The timestamp's fractional precision exceeds the supported seven digits.",
        "insufficient_precision": "The timestamp's year-only precision cannot establish freshness.",
        "timezone_unknown_offset": "The timestamp supplies an unknown offset (-00:00); no timezone is assumed.",
        "timezone_missing": "The timestamp lacks an established timezone; no provider timezone is assumed.",
        "named_timezone_unsupported": "Named-zone interpretation is unsupported by this evaluation policy.",
        "future_timestamp": "The timestamp lies after the evaluation reference; freshness is unknown.",
        "interval_future_overlap": "The timestamp interval extends into the future and cannot establish a definite freshness verdict.",
        "interval_crosses_cutoff": "The timestamp interval crosses the freshness cutoff and cannot establish a definite verdict.",
        "freshness_pass": "Applicable content-update evidence falls within the inclusive elapsed-time freshness window. This grants no loading authority.",
        "freshness_fail": "Applicable content-update evidence is older than the elapsed-time freshness window. Inspection and loading eligibility are unchanged.",
    }
)


def _explain(evaluation):
    if evaluation.reason == "semantics_not_applicable":
        claim = evaluation.observation.claims[evaluation.claim_reference.index]
        meaning = (
            "describes catalog metadata"
            if claim.semantics == "CATALOG_METADATA_UPDATE"
            else "has unverified update semantics"
        )
        return f"Metadata was collected and its timestamp parsed, but this field {meaning}; dataset-content freshness is unknown."
    return _EVALUATION_EXPLANATIONS[evaluation.reason]


@dataclass(frozen=True)
class FreshnessRequest:
    request_number: int
    source: str
    dataset_id: str
    max_days_old: int
    evaluation_id: str | None
    grounding_reason: str | None
    explanation: str
    selection_rule_version: str | None

    def to_dict(self):
        source, dataset_id = sanitize_url_text(self.source), sanitize_url_text(self.dataset_id)
        return {
            "request_number": self.request_number,
            "source": source,
            "dataset_id": dataset_id,
            "identity_sanitized": (source, dataset_id) != (self.source, self.dataset_id),
            "max_days_old": self.max_days_old,
            "evaluation_id": self.evaluation_id,
            "grounding_reason": self.grounding_reason,
            "explanation": sanitize_url_text(self.explanation),
            "selection_rule_version": self.selection_rule_version,
        }


@dataclass(frozen=True)
class _FetchAssociation:
    candidate: object
    adapter: object
    source: str
    requested_id: str
    catalog_identity: str
    observation: FreshnessObservation
    download_url: str | None
    metadata_endpoint: str | None


@dataclass
class FreshnessRun:
    run_id: UUID = field(default_factory=uuid4)
    observations: list[FreshnessObservation] = field(default_factory=list)
    evaluations: list[FreshnessEvaluation] = field(default_factory=list)
    requests: list[FreshnessRequest] = field(default_factory=list)
    _associations: list[_FetchAssociation] = field(default_factory=list, repr=False)

    def __post_init__(self):
        if type(self.run_id) is not UUID:
            raise ValueError("Run ID must be a UUID")

    def capture(self, candidate, source, requested_id, adapter):
        observation = getattr(candidate, "freshness_observation", None)
        if not isinstance(observation, FreshnessObservation):
            return
        # Deduplicate identical records only. Conflicting ID payloads remain visible.
        if observation not in self.observations:
            self.observations.append(observation)
        endpoint = None
        if source == "cbs":
            endpoint = safe_url_identity(
                f"{_CBS_ODATA_BASE}/{requested_id}/TableInfos?$format=json"
            )
        elif source == "ckan":
            api_base, _landing, (_search, show, _path) = adapter._route_configuration()
            endpoint = safe_url_identity(f"{api_base}/{show}?{urlencode({'id': requested_id})}")
        self._associations.append(
            _FetchAssociation(
                candidate,
                adapter,
                source,
                requested_id,
                sanitize_url_text(adapter.adapter_identity),
                observation,
                candidate.download_url,
                endpoint,
            )
        )

    def select(self, candidates, source, dataset_id, tool_map):
        """Return (observation, explicit reference, rule version), or a grounding reason."""
        matches = [c for c in candidates if c.source == source and c.id == dataset_id]
        if not matches:
            return "candidate_not_inspected"
        if len(matches) != 1:
            return "candidate_identity_ambiguous"
        candidate = matches[0]
        observation = getattr(candidate, "freshness_observation", None)
        if not isinstance(observation, FreshnessObservation):
            return "observation_missing"
        associations = [a for a in self._associations if a.candidate is candidate]
        if not associations:
            return "observation_not_captured"
        association = associations[-1]
        if observation != association.observation:
            return "observation_link_changed"
        if (
            observation.source != source
            or observation.dataset_id != dataset_id
            or association.source != source
            or association.requested_id != dataset_id
        ):
            return "observation_identity_mismatch"
        adapter = tool_map.get(source)
        if (
            adapter is not association.adapter
            or adapter.source_type != source
            or observation.catalog_identity != association.catalog_identity
            or observation.catalog_identity != sanitize_url_text(adapter.adapter_identity)
        ):
            return "observation_catalog_mismatch"
        policy = SELECTION_RULES.get(source)
        if (
            policy is None
            or observation.record_version != "1"
            or observation.collector_rule_version != policy.collector_version
        ):
            return "collector_not_supported"
        if observation.metadata_endpoint != association.metadata_endpoint:
            return "observation_endpoint_mismatch"
        if candidate.download_url != association.download_url or (
            association.download_url is not None
            and observation.resource_identity != safe_url_identity(association.download_url)
        ):
            return "observation_resource_mismatch"
        if source == "cbs" and (
            observation.resource_id != dataset_id
            or observation.resource_identity
            != safe_url_identity(f"{_CBS_ODATA_BASE}/{dataset_id}/TypedDataSet?$format=json")
        ):
            return "observation_resource_mismatch"
        if any(
            o.observation_id == observation.observation_id and o != observation
            for o in self.observations
        ):
            return "observation_id_conflict"
        if not observation.claims:
            return observation, None, policy.version
        indices = [i for i, c in enumerate(observation.claims) if c.field_path == policy.field_path]
        if not indices:
            return "claim_not_found"
        if len(indices) != 1:
            return "claim_identity_ambiguous"
        index = indices[0]
        claim = observation.claims[index]
        if claim.resource != observation.metadata_endpoint or claim.semantics != policy.semantics:
            return "claim_scope_mismatch"
        return observation, ClaimReference(observation.observation_id, index), policy.version

    def record_request(
        self,
        source,
        dataset_id,
        threshold,
        *,
        evaluation=None,
        grounding_reason=None,
        selection_rule_version=None,
    ):
        explanation = (
            _explain(evaluation)
            if evaluation is not None
            else _GROUNDING_EXPLANATIONS[grounding_reason]
        )
        if evaluation is not None:
            self.evaluations.append(evaluation)
        request = FreshnessRequest(
            len(self.requests) + 1,
            source,
            dataset_id,
            threshold,
            str(evaluation.evaluation_id) if evaluation is not None else None,
            grounding_reason,
            explanation,
            selection_rule_version,
        )
        self.requests.append(request)
        verdict = evaluation.verdict if evaluation is not None else "UNKNOWN"
        reason_code = (
            grounding_reason
            if grounding_reason in ("candidate_not_inspected", "candidate_identity_ambiguous")
            else f"freshness_{verdict.lower()}"
        )
        return {
            "source": sanitize_url_text(source),
            "dataset_id": sanitize_url_text(dataset_id),
            "max_days_old": threshold,
            "passes": {"PASS": True, "FAIL": False, "UNKNOWN": None}[verdict],
            "verdict": verdict,
            "reason_code": reason_code,
            "reason": sanitize_url_text(explanation),
            "evaluation_reason": evaluation.reason if evaluation is not None else None,
            "evaluation_id": request.evaluation_id,
            "observation_id": evaluation.observation.observation_id
            if evaluation is not None
            else None,
            "grounding_reason": grounding_reason,
            "selection_rule_version": selection_rule_version,
        }

    @property
    def has_records(self):
        return bool(self.observations or self.evaluations or self.requests)

    def to_dict(self):
        return {
            "report_format_version": "1",
            "run_id": str(self.run_id),
            "observations": [o.to_dict() for o in self.observations],
            "evaluations": [e.to_dict() for e in self.evaluations],
            "requests": [r.to_dict() for r in self.requests],
        }
