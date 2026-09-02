"""Pure, deterministic primitives for static profile-contract validation.

This module deliberately knows nothing about YAML, the environment, adapters, or
network availability.  It validates only the static shape and syntax of a profile.
Runtime resolution and preflight belong to later stages.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import InitVar, dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class ProfileStatus(StrEnum):
    """Static profile-selection policy; not a runtime availability signal."""

    ENABLED = "enabled"
    MANUAL_ONLY = "manual_only"
    DISABLED = "disabled"


class CKANDialect(StrEnum):
    """Closed CKAN protocol and landing-route shapes."""

    CKAN_ACTION = "ckan_action"
    EU_HUB = "eu_hub"


class CKANSearchMode(StrEnum):
    """Closed CKAN package-search filtering strategies."""

    SERVER_LITERAL_CSV = "server_literal_csv"
    LOCAL_RESOURCE_METADATA = "local_resource_metadata"


@dataclass(frozen=True, slots=True)
class ContractIssue:
    """One deterministic static-contract validation issue."""

    code: str
    path: str
    message: str


class ProfileContractError(ValueError):
    """Aggregate of all static-contract issues found during one validation."""

    def __init__(self, issues: Collection[ContractIssue]) -> None:
        self.issues = tuple(issues)
        if not self.issues:
            raise ValueError("ProfileContractError requires at least one issue")
        details = "; ".join(f"{issue.path}: {issue.code}: {issue.message}" for issue in self.issues)
        super().__init__(details)


_ID_PATTERN = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_ENV_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_BUDGET_FIELDS = (
    "max_searches",
    "max_results",
    "max_probes",
    "max_model_calls",
    "max_tokens",
    "max_total_tokens",
    "timeout_minutes",
    "sample_rows",
    "download_timeout_seconds",
)
_OBSOLETE_BUDGET_FIELDS = ("max_crawls",)
_CATALOG_FIELDS = (
    "catalog_id",
    "adapter",
    "name",
    "base_url",
    "api_key_env",
    "timeout_seconds",
    "priority",
    "required",
)
_CKAN_CATALOG_FIELDS = (
    "ckan_dialect",
    "ckan_search_mode",
    "landing_base_url",
)
_MALFORMED_PERCENT_PATTERN = r"%(?![0-9A-Fa-f]{2})"
_MISSING = object()


def _issue(code: str, path: str, message: str) -> ContractIssue:
    return ContractIssue(code=code, path=path, message=message)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(_ID_PATTERN, value) is not None


def _positive_integer_issue(value: object, path: str) -> ContractIssue | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return _issue("invalid_integer", path, "must be an integer, not a Boolean")
    if value <= 0:
        return _issue("non_positive_integer", path, "must be greater than zero")
    return None


def _canonical_hostname(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    if any(character in value for character in ("/", "\\", "@", "?", "#", "%")):
        return None

    hostname = value[:-1] if value.endswith(".") else value
    if not hostname or hostname.endswith("."):
        return None

    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        pass

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(ascii_hostname) > 253:
        return None

    labels = ascii_hostname.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return None
    label_pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if any(re.fullmatch(label_pattern, label) is None for label in labels):
        return None
    return ascii_hostname


def _url_issues(value: object, path: str) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if not isinstance(value, str):
        return [_issue("invalid_type", path, "must be a URL string")]

    if value != value.strip() or any(character.isspace() for character in value):
        issues.append(_issue("url_whitespace", path, "must not contain whitespace"))

    malformed_percent_encoding = re.search(_MALFORMED_PERCENT_PATTERN, value) is not None
    decoded_value = value if malformed_percent_encoding else unquote(value)
    if malformed_percent_encoding:
        issues.append(
            _issue(
                "malformed_percent_encoding",
                path,
                "must contain only complete hexadecimal percent escapes",
            )
        )

    if any(ord(character) < 32 or ord(character) == 127 for character in decoded_value):
        issues.append(_issue("url_control_character", path, "must not contain control characters"))
    if "\\" in decoded_value:
        issues.append(_issue("url_backslash", path, "must not contain backslashes"))

    try:
        parsed = urlsplit(value)
    except ValueError:
        issues.append(_issue("invalid_url", path, "must be an unambiguous absolute URL"))
        return issues

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        issues.append(_issue("invalid_url_scheme", path, "must use HTTP or HTTPS"))

    try:
        hostname = parsed.hostname
    except ValueError:
        hostname = None
    if hostname is not None and "%" in hostname:
        issues.append(
            _issue(
                "encoded_or_scoped_host",
                path,
                "must not contain encoded or scoped hostname data",
            )
        )
    elif hostname is None or _canonical_hostname(hostname) is None:
        issues.append(_issue("invalid_url_host", path, "must contain a valid hostname"))

    try:
        username = parsed.username
        password = parsed.password
    except ValueError:
        username = password = None
        issues.append(_issue("invalid_url", path, "must have an unambiguous authority"))
    if username is not None or password is not None:
        issues.append(_issue("embedded_credentials", path, "must not contain embedded credentials"))

    if "?" in value:
        issues.append(_issue("url_query_not_allowed", path, "must not contain a query"))
    if "#" in value:
        issues.append(_issue("url_fragment_not_allowed", path, "must not contain a fragment"))

    authority = parsed.netloc.rsplit("@", 1)[-1]
    empty_port = bool(authority) and authority.endswith(":")
    if empty_port:
        issues.append(_issue("invalid_url_port", path, "must not contain an empty port"))

    try:
        port = parsed.port
    except ValueError:
        if not empty_port:
            issues.append(_issue("invalid_url_port", path, "must contain a valid numeric port"))
    else:
        expected_port = 80 if scheme == "http" else 443 if scheme == "https" else None
        if not empty_port and port is not None and port != expected_port:
            issues.append(
                _issue(
                    "disallowed_port",
                    path,
                    "must use the standard port for its network scheme",
                )
            )

    return issues


def _landing_origin_issues(value: object, path: str) -> list[ContractIssue]:
    issues = _url_issues(value, path)
    if not isinstance(value, str):
        return issues
    try:
        parsed = urlsplit(value)
    except ValueError:
        return issues
    if parsed.path not in {"", "/"}:
        issues.append(
            _issue(
                "url_path_not_allowed",
                path,
                "must contain only a public portal origin",
            )
        )
    return issues


def _catalog_issues(
    values: Mapping[str, object],
    path: str,
    *,
    supported_adapters: frozenset[str] | None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    for field in _CATALOG_FIELDS:
        if field not in values:
            issues.append(_issue("missing_field", f"{path}.{field}", "is required"))

    adapter = values.get("adapter", _MISSING)
    is_ckan = adapter == "ckan"
    if is_ckan:
        for field in _CKAN_CATALOG_FIELDS:
            if field not in values:
                issues.append(_issue("missing_field", f"{path}.{field}", "is required"))
    elif isinstance(adapter, str) and adapter.strip():
        for field in _CKAN_CATALOG_FIELDS:
            value = values.get(field, _MISSING)
            if value is not _MISSING and value is not None:
                issues.append(
                    _issue(
                        "field_not_applicable",
                        f"{path}.{field}",
                        "is available only for CKAN catalogs",
                    )
                )

    catalog_id = values.get("catalog_id", _MISSING)
    if catalog_id is not _MISSING and not _valid_id(catalog_id):
        issues.append(_issue("invalid_id", f"{path}.catalog_id", "must use lowercase snake_case"))

    if adapter is not _MISSING:
        if not isinstance(adapter, str) or not adapter.strip():
            issues.append(_issue("blank_value", f"{path}.adapter", "must be a nonblank string"))
        elif supported_adapters is not None and adapter not in supported_adapters:
            issues.append(
                _issue(
                    "unsupported_adapter",
                    f"{path}.adapter",
                    "is not present in the supplied supported-adapter collection",
                )
            )

    name = values.get("name", _MISSING)
    if name is not _MISSING and (not isinstance(name, str) or not name.strip()):
        issues.append(_issue("blank_value", f"{path}.name", "must be a nonblank string"))

    base_url = values.get("base_url", _MISSING)
    if base_url is not _MISSING:
        issues.extend(_url_issues(base_url, f"{path}.base_url"))

    ckan_dialect = values.get("ckan_dialect", _MISSING)
    if is_ckan and ckan_dialect is not _MISSING:
        try:
            if not isinstance(ckan_dialect, str):
                raise TypeError
            CKANDialect(ckan_dialect)
        except (TypeError, ValueError):
            issues.append(
                _issue(
                    "invalid_ckan_dialect",
                    f"{path}.ckan_dialect",
                    "must be ckan_action or eu_hub",
                )
            )

    ckan_search_mode = values.get("ckan_search_mode", _MISSING)
    if is_ckan and ckan_search_mode is not _MISSING:
        try:
            if not isinstance(ckan_search_mode, str):
                raise TypeError
            CKANSearchMode(ckan_search_mode)
        except (TypeError, ValueError):
            issues.append(
                _issue(
                    "invalid_ckan_search_mode",
                    f"{path}.ckan_search_mode",
                    "must be server_literal_csv or local_resource_metadata",
                )
            )

    landing_base_url = values.get("landing_base_url", _MISSING)
    if is_ckan and landing_base_url is not _MISSING:
        issues.extend(_landing_origin_issues(landing_base_url, f"{path}.landing_base_url"))

    api_key_env = values.get("api_key_env", _MISSING)
    if (
        api_key_env is not _MISSING
        and api_key_env is not None
        and (not isinstance(api_key_env, str) or re.fullmatch(_ENV_PATTERN, api_key_env) is None)
    ):
        issues.append(
            _issue(
                "invalid_environment_name",
                f"{path}.api_key_env",
                "must be a syntactically valid environment-variable name or null",
            )
        )

    for field in ("timeout_seconds", "priority"):
        value = values.get(field, _MISSING)
        if value is not _MISSING:
            issue = _positive_integer_issue(value, f"{path}.{field}")
            if issue:
                issues.append(issue)

    required = values.get("required", _MISSING)
    if required is not _MISSING and not isinstance(required, bool):
        issues.append(_issue("invalid_boolean", f"{path}.required", "must be an actual Boolean"))

    return issues


def _budget_issues(values: Mapping[str, object], path: str) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for field in _BUDGET_FIELDS:
        field_path = f"{path}.{field}"
        if field not in values:
            issues.append(_issue("missing_field", field_path, "is required"))
            continue
        issue = _positive_integer_issue(values[field], field_path)
        if issue:
            issues.append(issue)
    for field in _OBSOLETE_BUDGET_FIELDS:
        if field in values:
            issues.append(
                _issue(
                    "obsolete_field",
                    f"{path}.{field}",
                    "is obsolete and must be removed",
                )
            )
    return issues


def _host_rule_issues(values: Mapping[str, object], path: str) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if "hostname" not in values:
        issues.append(_issue("missing_field", f"{path}.hostname", "is required"))
    elif _canonical_hostname(values["hostname"]) is None:
        issues.append(
            _issue("invalid_hostname", f"{path}.hostname", "must be a canonicalizable hostname")
        )

    if "include_subdomains" not in values:
        issues.append(_issue("missing_field", f"{path}.include_subdomains", "is required"))
    elif not isinstance(values["include_subdomains"], bool):
        issues.append(
            _issue(
                "invalid_boolean",
                f"{path}.include_subdomains",
                "must be an actual Boolean",
            )
        )
    return issues


@dataclass(frozen=True, slots=True)
class CatalogContract:
    """Static identity and context-free syntax for one catalog declaration.

    Adapter registration is a complete-profile invariant because the supported
    adapter collection is supplied by the profile's caller.
    """

    catalog_id: str
    adapter: str
    name: str
    base_url: str
    api_key_env: str | None
    timeout_seconds: int
    priority: int
    required: bool
    ckan_dialect: CKANDialect | None = None
    ckan_search_mode: CKANSearchMode | None = None
    landing_base_url: str | None = None

    def __post_init__(self) -> None:
        issues = _catalog_issues(
            {
                "catalog_id": self.catalog_id,
                "adapter": self.adapter,
                "name": self.name,
                "base_url": self.base_url,
                "api_key_env": self.api_key_env,
                "timeout_seconds": self.timeout_seconds,
                "priority": self.priority,
                "required": self.required,
                "ckan_dialect": self.ckan_dialect,
                "ckan_search_mode": self.ckan_search_mode,
                "landing_base_url": self.landing_base_url,
            },
            "catalog",
            supported_adapters=None,
        )
        if issues:
            raise ProfileContractError(issues)
        if self.adapter == "ckan":
            object.__setattr__(self, "ckan_dialect", CKANDialect(self.ckan_dialect))
            object.__setattr__(
                self,
                "ckan_search_mode",
                CKANSearchMode(self.ckan_search_mode),
            )


@dataclass(frozen=True, slots=True)
class BudgetContract:
    """Canonical profile budget fields with static positive-integer validation."""

    max_searches: int
    max_results: int
    max_probes: int
    max_model_calls: int
    max_tokens: int
    max_total_tokens: int
    timeout_minutes: int
    sample_rows: int
    download_timeout_seconds: int

    def __post_init__(self) -> None:
        issues = _budget_issues(
            {field: getattr(self, field) for field in _BUDGET_FIELDS},
            "budget",
        )
        if issues:
            raise ProfileContractError(issues)


@dataclass(frozen=True, slots=True)
class HostRule:
    """Canonical exact-host or explicit subdomain matching rule."""

    hostname: str
    include_subdomains: bool

    def __post_init__(self) -> None:
        issues = _host_rule_issues(
            {"hostname": self.hostname, "include_subdomains": self.include_subdomains},
            "host_rule",
        )
        if issues:
            raise ProfileContractError(issues)
        canonical = _canonical_hostname(self.hostname)
        if canonical is None:  # Kept explicit for static type narrowing.
            raise AssertionError("validated hostname did not canonicalize")
        object.__setattr__(self, "hostname", canonical)

    def matches(self, hostname: str) -> bool:
        """Return whether *hostname* matches this rule without resolving DNS."""

        candidate = _canonical_hostname(hostname)
        if candidate is None:
            return False
        if candidate == self.hostname:
            return True
        if not self.include_subdomains:
            return False
        try:
            ipaddress.ip_address(self.hostname)
        except ValueError:
            return candidate.endswith(f".{self.hostname}")
        return False


def _parse_supported_adapters(
    value: object,
    issues: list[ContractIssue],
) -> frozenset[str] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Collection):
        issues.append(
            _issue(
                "invalid_collection",
                "supported_adapters",
                "must be a collection of adapter names",
            )
        )
        return None
    if not all(isinstance(adapter, str) for adapter in value):
        issues.append(
            _issue(
                "invalid_collection",
                "supported_adapters",
                "must contain only adapter-name strings",
            )
        )
        return None
    return frozenset(value)


@dataclass(frozen=True, slots=True)
class ProfileContract:
    """Immutable, statically validated complete profile contract.

    ``supported_adapters`` supplies construction context only.  It is copied for
    validation and is not retained by the resulting contract.
    """

    profile_id: str
    status: ProfileStatus
    reason: str | None
    catalogs: tuple[CatalogContract, ...]
    budget: BudgetContract
    supported_adapters: InitVar[object]
    trusted_hosts: tuple[HostRule, ...] = ()
    blocked_hosts: tuple[HostRule, ...] = ()

    def __post_init__(self, supported_adapters: object) -> None:
        issues: list[ContractIssue] = []

        if not _valid_id(self.profile_id):
            issues.append(_issue("invalid_id", "profile_id", "must use lowercase snake_case"))
        valid_status = isinstance(self.status, ProfileStatus)
        if not valid_status:
            issues.append(_issue("invalid_status", "status", "must be a declared ProfileStatus"))
        if self.reason is not None and not isinstance(self.reason, str):
            issues.append(_issue("invalid_type", "reason", "must be a string or null"))
        elif (
            valid_status
            and self.status
            in {
                ProfileStatus.MANUAL_ONLY,
                ProfileStatus.DISABLED,
            }
            and (self.reason is None or not self.reason.strip())
        ):
            issues.append(
                _issue("reason_required", "reason", "is required for this profile status")
            )

        parsed_supported_adapters = _parse_supported_adapters(supported_adapters, issues)

        catalogs_are_valid = _is_sequence(self.catalogs)
        if catalogs_are_valid:
            catalogs = tuple(self.catalogs)
        else:
            catalogs = ()
            issues.append(_issue("invalid_collection", "catalogs", "must be a sequence"))
        if (
            catalogs_are_valid
            and valid_status
            and self.status in {ProfileStatus.ENABLED, ProfileStatus.MANUAL_ONLY}
            and not catalogs
        ):
            issues.append(
                _issue("catalogs_required", "catalogs", "must contain at least one catalog")
            )

        seen_ids: set[str] = set()
        seen_priorities: set[int] = set()
        for index, catalog in enumerate(catalogs):
            if not isinstance(catalog, CatalogContract):
                issues.append(
                    _issue(
                        "invalid_type",
                        f"catalogs[{index}]",
                        "must be a CatalogContract",
                    )
                )
                continue
            if (
                parsed_supported_adapters is not None
                and catalog.adapter not in parsed_supported_adapters
            ):
                issues.append(
                    _issue(
                        "unsupported_adapter",
                        f"catalogs[{index}].adapter",
                        "is not present in the supplied supported-adapter collection",
                    )
                )
            if catalog.catalog_id in seen_ids:
                issues.append(
                    _issue(
                        "duplicate_catalog_id",
                        f"catalogs[{index}].catalog_id",
                        "must be unique within the profile",
                    )
                )
            seen_ids.add(catalog.catalog_id)
            if catalog.priority in seen_priorities:
                issues.append(
                    _issue(
                        "duplicate_priority",
                        f"catalogs[{index}].priority",
                        "must be unique within the profile",
                    )
                )
            seen_priorities.add(catalog.priority)

        if not isinstance(self.budget, BudgetContract):
            issues.append(_issue("invalid_type", "budget", "must be a BudgetContract"))

        normalized_host_rules: dict[str, tuple[object, ...]] = {}
        for field, raw_rules in (
            ("trusted_hosts", self.trusted_hosts),
            ("blocked_hosts", self.blocked_hosts),
        ):
            if not _is_sequence(raw_rules):
                issues.append(_issue("invalid_collection", field, "must be a sequence"))
                normalized_host_rules[field] = ()
                continue
            rules = tuple(raw_rules)
            normalized_host_rules[field] = rules
            for index, rule in enumerate(rules):
                if not isinstance(rule, HostRule):
                    issues.append(
                        _issue(
                            "invalid_type",
                            f"{field}[{index}]",
                            "must be a HostRule",
                        )
                    )

        if issues:
            raise ProfileContractError(issues)

        object.__setattr__(self, "catalogs", catalogs)
        object.__setattr__(self, "trusted_hosts", normalized_host_rules["trusted_hosts"])
        object.__setattr__(self, "blocked_hosts", normalized_host_rules["blocked_hosts"])

    def is_trusted_host(self, hostname: str) -> bool:
        """Return whether any trusted-host rule matches; an empty tuple matches nothing."""

        return any(rule.matches(hostname) for rule in self.trusted_hosts)

    def is_blocked_host(self, hostname: str) -> bool:
        """Return whether any blocked-host rule matches; an empty tuple matches nothing."""

        return any(rule.matches(hostname) for rule in self.blocked_hosts)


def _parse_status(value: object, issues: list[ContractIssue]) -> ProfileStatus | None:
    if not isinstance(value, str):
        issues.append(
            _issue("invalid_status", "status", "must be enabled, manual_only, or disabled")
        )
        return None
    try:
        return ProfileStatus(value)
    except ValueError:
        issues.append(
            _issue("invalid_status", "status", "must be enabled, manual_only, or disabled")
        )
        return None


def _parse_catalogs(
    value: object,
    supported_adapters: frozenset[str] | None,
    issues: list[ContractIssue],
) -> tuple[CatalogContract, ...]:
    if not _is_sequence(value):
        issues.append(_issue("invalid_collection", "catalogs", "must be a sequence"))
        return ()

    catalogs: list[CatalogContract] = []
    seen_ids: set[str] = set()
    seen_priorities: set[int] = set()
    for index, raw_catalog in enumerate(value):
        path = f"catalogs[{index}]"
        if not isinstance(raw_catalog, Mapping):
            issues.append(_issue("invalid_type", path, "must be a mapping"))
            continue

        catalog_values = dict(raw_catalog)
        catalog_issues = _catalog_issues(
            catalog_values,
            path,
            supported_adapters=supported_adapters,
        )

        catalog_id = catalog_values.get("catalog_id")
        if _valid_id(catalog_id):
            if catalog_id in seen_ids:
                catalog_issues.append(
                    _issue(
                        "duplicate_catalog_id",
                        f"{path}.catalog_id",
                        "must be unique within the profile",
                    )
                )
            seen_ids.add(catalog_id)

        priority = catalog_values.get("priority")
        if isinstance(priority, int) and not isinstance(priority, bool) and priority > 0:
            if priority in seen_priorities:
                catalog_issues.append(
                    _issue(
                        "duplicate_priority",
                        f"{path}.priority",
                        "must be unique within the profile",
                    )
                )
            seen_priorities.add(priority)

        issues.extend(catalog_issues)
        if not catalog_issues:
            catalogs.append(
                CatalogContract(
                    catalog_id=catalog_values["catalog_id"],
                    adapter=catalog_values["adapter"],
                    name=catalog_values["name"],
                    base_url=catalog_values["base_url"],
                    api_key_env=catalog_values["api_key_env"],
                    timeout_seconds=catalog_values["timeout_seconds"],
                    priority=catalog_values["priority"],
                    required=catalog_values["required"],
                    ckan_dialect=catalog_values.get("ckan_dialect"),
                    ckan_search_mode=catalog_values.get("ckan_search_mode"),
                    landing_base_url=catalog_values.get("landing_base_url"),
                )
            )
    return tuple(catalogs)


def _parse_budget(value: object, issues: list[ContractIssue]) -> BudgetContract | None:
    if not isinstance(value, Mapping):
        issues.append(_issue("invalid_type", "budget", "must be a mapping"))
        return None
    budget_values = dict(value)
    budget_issues = _budget_issues(budget_values, "budget")
    issues.extend(budget_issues)
    if budget_issues:
        return None
    return BudgetContract(**{field: budget_values[field] for field in _BUDGET_FIELDS})


def _parse_host_rules(
    value: object,
    path: str,
    issues: list[ContractIssue],
) -> tuple[HostRule, ...]:
    if not _is_sequence(value):
        issues.append(_issue("invalid_collection", path, "must be a sequence"))
        return ()

    rules: list[HostRule] = []
    for index, raw_rule in enumerate(value):
        rule_path = f"{path}[{index}]"
        if not isinstance(raw_rule, Mapping):
            issues.append(_issue("invalid_type", rule_path, "must be a mapping"))
            continue
        values = dict(raw_rule)
        rule_issues = _host_rule_issues(values, rule_path)
        issues.extend(rule_issues)
        if not rule_issues:
            rules.append(
                HostRule(
                    hostname=values["hostname"],
                    include_subdomains=values["include_subdomains"],
                )
            )
    return tuple(rules)


def build_profile_contract(
    *,
    profile_id: object,
    status: object,
    catalogs: object,
    budget: object,
    supported_adapters: Collection[str],
    reason: object = None,
    trusted_hosts: object = (),
    blocked_hosts: object = (),
) -> ProfileContract:
    """Validate static inputs completely and return one immutable profile contract.

    ``supported_adapters`` is supplied by the caller so this pure module never
    imports or assumes the runtime tool registry.  Runtime availability is not
    checked here.
    """

    issues: list[ContractIssue] = []

    if not _valid_id(profile_id):
        issues.append(_issue("invalid_id", "profile_id", "must use lowercase snake_case"))
    parsed_status = _parse_status(status, issues)

    parsed_reason: str | None
    if reason is None:
        parsed_reason = None
    elif not isinstance(reason, str):
        issues.append(_issue("invalid_type", "reason", "must be a string or null"))
        parsed_reason = None
    else:
        parsed_reason = reason
    if parsed_status in {ProfileStatus.MANUAL_ONLY, ProfileStatus.DISABLED} and (
        parsed_reason is None or not parsed_reason.strip()
    ):
        issues.append(_issue("reason_required", "reason", "is required for this profile status"))

    parsed_supported_adapters = _parse_supported_adapters(supported_adapters, issues)

    parsed_catalogs = _parse_catalogs(catalogs, parsed_supported_adapters, issues)
    if (
        parsed_status in {ProfileStatus.ENABLED, ProfileStatus.MANUAL_ONLY}
        and _is_sequence(catalogs)
        and not catalogs
    ):
        issues.append(_issue("catalogs_required", "catalogs", "must contain at least one catalog"))

    parsed_budget = _parse_budget(budget, issues)
    parsed_trusted_hosts = _parse_host_rules(trusted_hosts, "trusted_hosts", issues)
    parsed_blocked_hosts = _parse_host_rules(blocked_hosts, "blocked_hosts", issues)

    if issues:
        raise ProfileContractError(issues)

    if parsed_status is None or parsed_budget is None or not isinstance(profile_id, str):
        raise AssertionError("successful validation did not produce a complete contract")

    return ProfileContract(
        profile_id=profile_id,
        status=parsed_status,
        reason=parsed_reason,
        catalogs=parsed_catalogs,
        budget=parsed_budget,
        supported_adapters=parsed_supported_adapters,
        trusted_hosts=parsed_trusted_hosts,
        blocked_hosts=parsed_blocked_hosts,
    )


__all__ = (
    "BudgetContract",
    "CKANDialect",
    "CatalogContract",
    "ContractIssue",
    "HostRule",
    "ProfileContract",
    "ProfileContractError",
    "ProfileStatus",
    "build_profile_contract",
)
