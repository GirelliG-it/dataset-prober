"""Fail-closed selection, format admission, and one-shot load authorization."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from dataset_prober.resource_classification import (
    AssessmentReason,
    ResourceAssessment,
    _classifier_evidence_token_for_candidate,
)


class SourceKey(StrEnum):
    """Closed set of v0.1 loading sources."""

    MANUAL = "manual"
    CKAN = "ckan"
    TAVILY = "tavily"
    CBS = "cbs"


class ResourceFormat(StrEnum):
    """Formats recognized by narrow v0.1 admission."""

    CSV = "CSV"
    GEOJSON = "GEOJSON"
    JSON = "JSON"
    PARQUET = "PARQUET"
    XLSX = "XLSX"
    XLS = "XLS"
    PDF = "PDF"
    HTML = "HTML"
    ODATA = "ODATA"
    ODATA_CSV = "ODATA/CSV"
    UNKNOWN = "UNKNOWN"


class LoaderKind(StrEnum):
    """Implemented persistent loaders plus a non-authorizable comparison value."""

    DUCKDB_CSV = "DUCKDB_CSV"
    CBS_ODATA = "CBS_ODATA"
    UNSUPPORTED = "UNSUPPORTED"


class AuthorizationState(StrEnum):
    ISSUED = "ISSUED"
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"


class LoadingPolicyError(RuntimeError):
    """Base class for deterministic loading-policy denials."""


class DownloadDisabledError(LoadingPolicyError):
    """The CLI invocation did not explicitly enable download offers."""


class InspectedResourceError(LoadingPolicyError):
    """No unique, successfully inspected resource supports the request."""


class AuthorizationMismatchError(LoadingPolicyError):
    """Actual writer claims differ from the exact consented operation."""


class AuthorizationStateError(LoadingPolicyError):
    """An authorization is active or permanently terminal."""


_FORMAT_BY_SUFFIX = {
    ".csv": ResourceFormat.CSV,
    ".geojson": ResourceFormat.GEOJSON,
    ".json": ResourceFormat.JSON,
    ".parquet": ResourceFormat.PARQUET,
    ".xlsx": ResourceFormat.XLSX,
    ".xls": ResourceFormat.XLS,
    ".pdf": ResourceFormat.PDF,
    ".html": ResourceFormat.HTML,
    ".htm": ResourceFormat.HTML,
}

_CSV_SOURCES = frozenset({SourceKey.MANUAL, SourceKey.CKAN, SourceKey.TAVILY})
_DEFAULT_ADAPTER_IDENTITIES = {
    SourceKey.MANUAL: "Manual URL",
    SourceKey.CKAN: "CKAN Catalog",
    SourceKey.TAVILY: "Web Search (Tavily)",
    SourceKey.CBS: "CBS Statistics Netherlands",
}


def _source_key(value: str | SourceKey) -> SourceKey:
    try:
        return SourceKey(str(value).strip().lower())
    except (TypeError, ValueError) as exc:
        raise LoadingPolicyError(f"Unknown loading source: {value!r}") from exc


def _resource_format(value: str | ResourceFormat | None) -> ResourceFormat:
    if not value:
        return ResourceFormat.UNKNOWN
    normalized = str(value).strip().upper()
    aliases = {
        "ODATA": ResourceFormat.ODATA,
        "ODATA/CSV": ResourceFormat.ODATA_CSV,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ResourceFormat(normalized)
    except ValueError:
        return ResourceFormat.UNKNOWN


def detect_resource_format(url: str) -> ResourceFormat | None:
    """Return narrow decoded-path suffix evidence, never query/fragment evidence."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        path = unquote(urlsplit(url).path).lower()
    except (TypeError, ValueError):
        return None
    for suffix, resource_format in _FORMAT_BY_SUFFIX.items():
        if path.endswith(suffix):
            return resource_format
    return None


def loader_for_resource(
    source: str | SourceKey,
    resource_format: str | ResourceFormat | None,
    retrieval_url: str,
) -> LoaderKind:
    """Select the one implemented loader using authoritative admission rules."""
    source_key = _source_key(source)
    normalized_format = _resource_format(resource_format)
    if not isinstance(retrieval_url, str) or not retrieval_url.strip():
        return LoaderKind.UNSUPPORTED
    if source_key in _CSV_SOURCES:
        detected = detect_resource_format(retrieval_url)
        if normalized_format is ResourceFormat.CSV and detected is ResourceFormat.CSV:
            return LoaderKind.DUCKDB_CSV
        return LoaderKind.UNSUPPORTED
    if source_key is SourceKey.CBS and normalized_format in {
        ResourceFormat.ODATA,
        ResourceFormat.ODATA_CSV,
    }:
        return LoaderKind.CBS_ODATA
    return LoaderKind.UNSUPPORTED


def is_supported_format(source: str, resource_format: str | None) -> bool:
    """Whether a source/format pair has a v0.1 loader, apart from URL evidence."""
    source_key = _source_key(source)
    normalized = _resource_format(resource_format)
    if source_key in _CSV_SOURCES:
        return normalized is ResourceFormat.CSV
    return source_key is SourceKey.CBS and normalized in {
        ResourceFormat.ODATA,
        ResourceFormat.ODATA_CSV,
    }


def configured_adapter_identity(source: str | SourceKey, config: dict[str, Any]) -> str:
    """Return the exact configured identity used by registration and writers."""
    source_key = _source_key(source)
    configured_name = config.get("name")
    if isinstance(configured_name, str) and configured_name.strip():
        name = configured_name.strip()
    else:
        name = _DEFAULT_ADAPTER_IDENTITIES[source_key]
    configured_base = config.get("base_url")
    if isinstance(configured_base, str) and configured_base.strip():
        return f"{name} ({configured_base.strip().rstrip('/')})"
    return name


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Canonical immutable identity of one inspected loading candidate."""

    source_key: SourceKey
    adapter_identity: str
    resource_id: str
    retrieval_url: str


def canonical_candidate_identity(
    source: str | SourceKey,
    adapter_identity: str,
    resource_id: str,
    retrieval_url: str,
) -> CandidateIdentity:
    """Construct candidate identity using the loading policy's authoritative rules."""
    return CandidateIdentity(
        source_key=_source_key(source),
        adapter_identity=str(adapter_identity),
        resource_id=str(resource_id),
        retrieval_url=str(retrieval_url),
    )


def canonicalize_destination(destination: str | Path) -> str:
    """Resolve a destination without creating it or opening DuckDB."""
    return str(Path(destination).expanduser().resolve(strict=False))


def derive_table_name(
    source: str | SourceKey,
    adapter_identity: str,
    resource_id: str,
    retrieval_url: str,
) -> str:
    """Derive a safe table name from the complete canonical source identity."""
    source_key = _source_key(source)
    digest = hashlib.sha256()
    for value in (source_key.value, adapter_identity, resource_id, retrieval_url):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    encoded_digest = base64.b32encode(digest.digest()).decode("ascii").rstrip("=").lower()
    return f"{source_key.value}_{encoded_digest}"


@dataclass(frozen=True, slots=True)
class LoadClaims:
    """Exact actual or authorized operation compared at activation."""

    source_key: SourceKey
    adapter_identity: str
    resource_id: str
    retrieval_url: str
    verified_format: ResourceFormat
    loader_kind: LoaderKind
    database_path: str
    planned_table_name: str
    assessment: ResourceAssessment

    @property
    def candidate_identity(self) -> CandidateIdentity:
        return canonical_candidate_identity(
            self.source_key,
            self.adapter_identity,
            self.resource_id,
            self.retrieval_url,
        )


@dataclass(frozen=True, slots=True)
class _InspectedSnapshot:
    source_key: SourceKey
    adapter_identity: str
    resource_id: str
    retrieval_url: str
    verified_format: ResourceFormat
    display_title: str
    assessment: ResourceAssessment


@dataclass(slots=True)
class _AuthorizationRecord:
    claims: LoadClaims
    assessment_evidence: object
    state: AuthorizationState = AuthorizationState.ISSUED
    active_token: object | None = None


def _claims(
    *,
    source_key: str | SourceKey,
    adapter_identity: str,
    resource_id: str,
    retrieval_url: str,
    verified_format: str | ResourceFormat | None,
    assessment: ResourceAssessment,
    destination: str | Path,
) -> LoadClaims:
    identity = canonical_candidate_identity(
        source_key,
        adapter_identity,
        resource_id,
        retrieval_url,
    )
    normalized_format = _resource_format(verified_format)
    if not isinstance(assessment, ResourceAssessment):
        raise InspectedResourceError("Deterministic resource assessment is missing")
    loader = loader_for_resource(identity.source_key, normalized_format, identity.retrieval_url)
    return LoadClaims(
        source_key=identity.source_key,
        adapter_identity=identity.adapter_identity,
        resource_id=identity.resource_id,
        retrieval_url=identity.retrieval_url,
        verified_format=normalized_format,
        loader_kind=loader,
        database_path=canonicalize_destination(destination),
        planned_table_name=derive_table_name(
            identity.source_key,
            identity.adapter_identity,
            identity.resource_id,
            identity.retrieval_url,
        ),
        assessment=assessment,
    )


def claims_for_probe(result: Any, destination: str | Path) -> LoadClaims:
    """Reconstruct manual writer claims from the current mutable probe result."""
    return _claims(
        source_key=SourceKey.MANUAL,
        adapter_identity=configured_adapter_identity(SourceKey.MANUAL, {}),
        resource_id=result.url,
        retrieval_url=result.url,
        verified_format=getattr(result, "format", None),
        assessment=getattr(result, "assessment", None),
        destination=destination,
    )


def claims_for_dataset(dataset: Any, adapter_identity: str, destination: str | Path) -> LoadClaims:
    """Reconstruct adapter writer claims from the current mutable result."""
    return _claims(
        source_key=dataset.source,
        adapter_identity=adapter_identity,
        resource_id=dataset.id,
        retrieval_url=dataset.download_url or "",
        verified_format=dataset.format,
        assessment=getattr(dataset, "assessment", None),
        destination=destination,
    )


def parse_exact_selection(selection: str, candidate_count: int) -> list[int]:
    """Return selected zero-based indices or reject the whole input."""
    value = selection.strip().lower()
    if value == "none":
        return []
    if value == "all":
        return list(range(candidate_count))
    if not value:
        raise ValueError("Selection is empty")

    tokens = value.split(",")
    if any(not token.strip().isdigit() for token in tokens):
        raise ValueError("Selection must contain only displayed numbers")

    indices = [int(token.strip()) - 1 for token in tokens]
    if any(index < 0 or index >= candidate_count for index in indices):
        raise ValueError("Selection contains an out-of-range number")
    if len(indices) != len(set(indices)):
        raise ValueError("Selection contains a duplicate number")
    return indices


def safe_url_identity(url: str) -> str:
    """Render sensitive URLs without user information, query values, or fragments."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    try:
        parsed = urlsplit(url)
        sensitive = bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
        if not sensitive:
            return url
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        if parsed.netloc:
            prefix = f"{parsed.scheme}://" if parsed.scheme else "//"
            sanitized = f"{prefix}{hostname}{port}{parsed.path}"
        elif parsed.scheme:
            sanitized = f"{parsed.scheme}:{parsed.path}"
        else:
            sanitized = parsed.path
    except (TypeError, ValueError):
        sanitized = "unparseable-url"
    return f"{sanitized} (SHA-256: {digest})"


_URL_SHAPED_TEXT = re.compile(
    r"(?:(?:[a-z][a-z0-9+.-]*:)?//|[a-z][a-z0-9+.-]*:)[^\s]+",
    flags=re.IGNORECASE,
)


def sanitize_url_text(text: str) -> str:
    """Redact sensitive components from every URL-shaped value in presentation text."""
    return _URL_SHAPED_TEXT.sub(
        lambda match: safe_url_identity(match.group(0)),
        text,
    )


def sanitize_for_presentation(value):
    """Return a redacted copy suitable for logs, model messages, and saved reports."""
    if isinstance(value, str):
        return sanitize_url_text(value)
    if isinstance(value, dict):
        return {key: sanitize_for_presentation(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_presentation(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_presentation(item) for item in value)
    return value


_AUTHORIZED_LOAD_ISSUER = object()


class AuthorizedLoad:
    """Opaque, one-shot reference to an authorization record owned by a session."""

    __slots__ = ("__session", "__nonce")

    def __init__(self, session: LoadingPolicySession, nonce: str, issuer: object) -> None:
        if issuer is not _AUTHORIZED_LOAD_ISSUER:
            raise TypeError("AuthorizedLoad values are issued only by LoadingPolicySession")
        self.__session = session
        self.__nonce = nonce

    @property
    def state(self) -> AuthorizationState:
        return self.__session._state(self.__nonce)

    @contextmanager
    def activate(self, actual_claims: LoadClaims) -> Iterator[_ActivePermit]:
        permit = self.__session._activate(self.__nonce, actual_claims)
        try:
            yield permit
        finally:
            self.__session._finish(self.__nonce, permit)


class _ActivePermit:
    """Private permit valid only while its authorization record is ACTIVE."""

    __slots__ = ("__session", "__nonce", "__token")

    def __init__(
        self, session: LoadingPolicySession, nonce: str, token: object, issuer: object
    ) -> None:
        if issuer is not _AUTHORIZED_LOAD_ISSUER:
            raise TypeError("Active permits are created only during authorization activation")
        self.__session = session
        self.__nonce = nonce
        self.__token = token

    def _assert_claims(self, actual_claims: LoadClaims) -> None:
        self.__session._validate_active(self.__nonce, self.__token, actual_claims=actual_claims)

    def _assert_destination(self, destination: str | Path) -> None:
        self.__session._validate_active(
            self.__nonce,
            self.__token,
            destination=canonicalize_destination(destination),
        )

    def _current_claims(self) -> LoadClaims:
        return self.__session._active_claims(self.__nonce, self.__token)


class LoadingPolicySession:
    """Single CLI-session owner of inspection snapshots and load authorization."""

    def __init__(self, *, download_enabled: bool) -> None:
        self.__download_enabled = bool(download_enabled)
        self.__snapshots: list[_InspectedSnapshot] = []
        self.__records: dict[str, _AuthorizationRecord] = {}
        self.__lock = threading.Lock()

    @property
    def download_enabled(self) -> bool:
        """The immutable load-offer decision made when this session was created."""
        return self.__download_enabled

    def register_probe_result(self, result: Any) -> None:
        """Copy one successful manual probe into the immutable registry."""
        if result.status != "ok":
            raise InspectedResourceError("Manual resource was not successfully inspected")
        snapshot = _InspectedSnapshot(
            source_key=SourceKey.MANUAL,
            adapter_identity=configured_adapter_identity(SourceKey.MANUAL, {}),
            resource_id=str(result.url),
            retrieval_url=str(result.url),
            verified_format=_resource_format(getattr(result, "format", None)),
            display_title=str(result.name),
            assessment=getattr(result, "assessment", None),
        )
        self._register(snapshot)

    def register_dataset_result(self, dataset: Any, adapter_identity: str) -> None:
        """Copy one successfully probed adapter result into the immutable registry."""
        if dataset.status != "probed":
            raise InspectedResourceError("Adapter resource was not successfully inspected")
        snapshot = _InspectedSnapshot(
            source_key=_source_key(dataset.source),
            adapter_identity=str(adapter_identity),
            resource_id=str(dataset.id),
            retrieval_url=str(dataset.download_url or ""),
            verified_format=_resource_format(dataset.format),
            display_title=str(dataset.title),
            assessment=getattr(dataset, "assessment", None),
        )
        self._register(snapshot)

    def _register(self, snapshot: _InspectedSnapshot) -> None:
        if not isinstance(snapshot.assessment, ResourceAssessment):
            raise InspectedResourceError("Deterministic resource assessment is missing")
        if not snapshot.assessment.load_eligible:
            if snapshot.assessment.reason is AssessmentReason.VERIFIED_TABULAR_DATA:
                raise InspectedResourceError(
                    "Eligible-looking assessment is not classifier-issued inspection evidence"
                )
            raise InspectedResourceError(
                f"Resource is report-only: {snapshot.assessment.reason.value}"
            )
        identity = canonical_candidate_identity(
            snapshot.source_key,
            snapshot.adapter_identity,
            snapshot.resource_id,
            snapshot.retrieval_url,
        )
        if _classifier_evidence_token_for_candidate(snapshot.assessment, identity) is None:
            raise InspectedResourceError(
                "Classifier-issued assessment does not match the inspected candidate"
            )
        loader = loader_for_resource(
            snapshot.source_key, snapshot.verified_format, snapshot.retrieval_url
        )
        if loader is LoaderKind.UNSUPPORTED:
            raise InspectedResourceError("Inspected resource has no supported loader")
        with self.__lock:
            self.__snapshots.append(snapshot)

    def request_authorization(
        self,
        *,
        source_key: str | SourceKey,
        adapter_identity: str,
        resource_id: str,
        destination: str | Path,
        input_func: Callable[[str], str],
    ) -> AuthorizedLoad | None:
        """Display, collect strict consent, and issue as one application operation."""
        if not self.__download_enabled:
            raise DownloadDisabledError("Download authorization requires explicit --download")

        source = _source_key(source_key)
        adapter = str(adapter_identity)
        identifier = str(resource_id)
        with self.__lock:
            matches = [
                snapshot
                for snapshot in self.__snapshots
                if snapshot.source_key is source
                and snapshot.adapter_identity == adapter
                and snapshot.resource_id == identifier
            ]
        if not matches:
            raise InspectedResourceError("Resource is not a registered inspected candidate")
        if len(matches) != 1:
            raise InspectedResourceError("Inspected resource identity is ambiguous")

        snapshot = matches[0]
        claims = _claims(
            source_key=snapshot.source_key,
            adapter_identity=snapshot.adapter_identity,
            resource_id=snapshot.resource_id,
            retrieval_url=snapshot.retrieval_url,
            verified_format=snapshot.verified_format,
            assessment=snapshot.assessment,
            destination=destination,
        )
        if claims.loader_kind is LoaderKind.UNSUPPORTED:
            raise InspectedResourceError("Inspected resource has no supported loader")

        prompt = self._consent_prompt(snapshot, claims)
        try:
            response = input_func(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        if response.strip().lower() not in {"y", "yes"}:
            return None

        nonce = secrets.token_urlsafe(32)
        assessment_evidence = _classifier_evidence_token_for_candidate(
            claims.assessment,
            claims.candidate_identity,
        )
        if assessment_evidence is None:
            raise InspectedResourceError(
                "Classifier-issued assessment no longer matches the inspected candidate"
            )
        with self.__lock:
            self.__records[nonce] = _AuthorizationRecord(
                claims=claims,
                assessment_evidence=assessment_evidence,
            )
        return AuthorizedLoad(self, nonce, _AUTHORIZED_LOAD_ISSUER)

    @staticmethod
    def _consent_prompt(snapshot: _InspectedSnapshot, claims: LoadClaims) -> str:
        return (
            "Approve this exact one-shot DuckDB load?\n"
            f"  Source: {claims.source_key.value}\n"
            f"  Adapter: {sanitize_url_text(claims.adapter_identity)}\n"
            f"  Resource ID: {sanitize_url_text(claims.resource_id)}\n"
            f"  Title: {sanitize_url_text(snapshot.display_title)}\n"
            f"  Retrieval URL: {safe_url_identity(claims.retrieval_url)}\n"
            f"  Verified format: {claims.verified_format.value}\n"
            f"  Classification: {claims.assessment.resource_kind.value}\n"
            f"  Queryability: {claims.assessment.queryability_outcome.value}\n"
            f"  Assessment reason: {claims.assessment.reason.value}\n"
            f"  Loader: {claims.loader_kind.value}\n"
            f"  DuckDB destination: {claims.database_path}\n"
            f"  Planned table: {claims.planned_table_name}\n"
            "Type 'yes' to approve this resource and destination: "
        )

    def _state(self, nonce: str) -> AuthorizationState:
        with self.__lock:
            record = self.__records.get(nonce)
            if record is None:
                raise AuthorizationStateError("Unknown authorization")
            return record.state

    def _activate(self, nonce: str, actual_claims: LoadClaims) -> _ActivePermit:
        with self.__lock:
            record = self.__records.get(nonce)
            if record is None:
                raise AuthorizationStateError("Unknown authorization")
            if record.state is not AuthorizationState.ISSUED:
                raise AuthorizationStateError(
                    f"Authorization cannot activate from {record.state.value}"
                )
            if not self._claims_match(record, actual_claims):
                record.state = AuthorizationState.REJECTED
                raise AuthorizationMismatchError("Actual load claims do not match consent")
            token = object()
            record.active_token = token
            record.state = AuthorizationState.ACTIVE
        return _ActivePermit(self, nonce, token, _AUTHORIZED_LOAD_ISSUER)

    @staticmethod
    def _claims_match(record: _AuthorizationRecord, actual_claims: LoadClaims) -> bool:
        if not isinstance(actual_claims, LoadClaims):
            return False
        if record.claims != actual_claims:
            return False
        if record.claims.assessment is not actual_claims.assessment:
            return False
        actual_evidence = _classifier_evidence_token_for_candidate(
            actual_claims.assessment,
            actual_claims.candidate_identity,
        )
        return actual_evidence is record.assessment_evidence

    def _validate_active(
        self,
        nonce: str,
        token: object,
        *,
        actual_claims: LoadClaims | None = None,
        destination: str | None = None,
    ) -> None:
        with self.__lock:
            record = self.__records.get(nonce)
            valid = (
                record is not None
                and record.state is AuthorizationState.ACTIVE
                and record.active_token is token
            )
            if not valid:
                raise AuthorizationStateError("Active permit is no longer valid")
            mismatch = actual_claims is not None and not self._claims_match(
                record,
                actual_claims,
            )
            mismatch = mismatch or (
                destination is not None and destination != record.claims.database_path
            )
            if mismatch:
                record.state = AuthorizationState.REJECTED
                record.active_token = None
                raise AuthorizationMismatchError("Active permit does not match writer operation")

    def _active_claims(self, nonce: str, token: object) -> LoadClaims:
        self._validate_active(nonce, token)
        with self.__lock:
            return self.__records[nonce].claims

    def _finish(self, nonce: str, permit: _ActivePermit) -> None:
        del permit
        with self.__lock:
            record = self.__records.get(nonce)
            if record is not None and record.state is AuthorizationState.ACTIVE:
                record.state = AuthorizationState.CONSUMED
                record.active_token = None
