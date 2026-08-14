"""
src/config_loader.py

Loads and validates profile YAML files.
Provides the Profile object used throughout the agent pipeline.
All agent configuration flows through here — no hardcoded values anywhere else.
"""

import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

import yaml

from dataset_prober.loading_policy import sanitize_url_text
from dataset_prober.profile_contract import (
    ContractIssue,
    HostRule,
    ProfileContract,
    ProfileContractError,
    ProfileStatus,
    build_profile_contract,
)

# Profiles are package data - resolved relative to this module, not the
# project root, so the path holds inside an installed wheel.
DEFAULT_PROFILES_DIR = Path(__file__).parent / "profiles"


class ProfileUnavailableError(ValueError):
    """A configured profile is not selectable or runnable under its lifecycle policy."""


def _url_hostname(url: object) -> str | None:
    if not isinstance(url, str) or not url or url != url.strip():
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", url):
        return None
    decoded_url = unquote(url)
    if "\\" in url or "\\" in decoded_url:
        return None
    if any(unicodedata.category(character) == "Cc" for character in url):
        return None
    if any(unicodedata.category(character) == "Cc" for character in decoded_url):
        return None
    if any(character.isspace() for character in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":") or hostname is None or "%" in hostname:
        return None
    if port == 0:
        return None
    return hostname


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    """Configuration for a single catalog source within a profile."""

    catalog_id: str
    adapter: str
    name: str
    base_url: str
    api_key_env: Optional[str]  # Environment variable name for API key
    timeout_seconds: int
    priority: int
    required: bool

    @property
    def type(self) -> str:
        """Compatibility alias for runtime code that historically used ``type``."""

        return self.adapter

    @property
    def api_key(self) -> Optional[str]:
        """Resolve API key from environment variable."""
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env)
        if not key:
            raise EnvironmentError(
                f"API key environment variable '{self.api_key_env}' "
                f"is not set. Required for catalog: {self.name}"
            )
        return key


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """User-controlled budget limits for an agent run."""

    max_searches: int
    max_crawls: int
    max_probes: int
    max_tokens: int
    timeout_minutes: int
    sample_rows: int
    download_timeout_seconds: int

    def override(self, **kwargs) -> "BudgetConfig":
        """
        Return a new BudgetConfig with specified values overridden.
        Used to apply CLI flag overrides on top of profile defaults.
        """
        current = {
            "max_searches": self.max_searches,
            "max_crawls": self.max_crawls,
            "max_probes": self.max_probes,
            "max_tokens": self.max_tokens,
            "timeout_minutes": self.timeout_minutes,
            "sample_rows": self.sample_rows,
            "download_timeout_seconds": self.download_timeout_seconds,
        }
        current.update({k: v for k, v in kwargs.items() if v is not None})
        return BudgetConfig(**current)


@dataclass
class PricingConfig:
    """Claude API pricing for cost tracking."""

    input_per_million: float
    output_per_million: float
    cache_read_per_million: float

    def calculate_cost(
        self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
    ) -> float:
        """Calculate cost in USD for a set of token counts."""
        return (
            (input_tokens / 1_000_000) * self.input_per_million
            + (output_tokens / 1_000_000) * self.output_per_million
            + (cache_read_tokens / 1_000_000) * self.cache_read_per_million
        )

    def format_cost(self, cost_usd: float) -> str:
        """Format cost as a human-readable string."""
        if cost_usd < 0.001:
            return f"${cost_usd * 100:.4f}¢"
        return f"${cost_usd:.4f}"


@dataclass
class LicenseConfig:
    """License preferences and rules."""

    preference: list  # Preferred licenses — flag if not found
    warn: list  # Warn if dataset uses these
    reject: list  # Skip datasets with these licenses


@dataclass
class Profile:
    """
    Complete profile configuration for an agent run.
    Loaded from a YAML file and optionally overridden by CLI flags.
    """

    contract: ProfileContract

    # Ancillary display/runtime fields. Contract values are exposed by properties below.
    name: str
    description: str
    language: str
    cost_warning: bool
    warning_message: Optional[str]

    # Configuration sections outside the static contract
    pricing: PricingConfig
    license: LicenseConfig

    # Geographic scope guidance for model-assisted selection and reporting
    scope_regions: list = field(default_factory=list)
    scope_instruction: str = ""

    # Optional sections
    opendatasoft_portals: list = field(default_factory=list)
    credibility_signals: dict = field(default_factory=dict)
    domain_keywords: dict = field(default_factory=dict)

    # Raw profile data (for passing to tools)
    raw: dict = field(default_factory=dict)

    _catalogs: tuple[CatalogConfig, ...] = field(init=False, repr=False)
    _budget: BudgetConfig = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._catalogs = tuple(
            CatalogConfig(
                catalog_id=catalog.catalog_id,
                adapter=catalog.adapter,
                name=catalog.name,
                base_url=catalog.base_url,
                api_key_env=catalog.api_key_env,
                timeout_seconds=catalog.timeout_seconds,
                priority=catalog.priority,
                required=catalog.required,
            )
            for catalog in self.contract.catalogs
        )
        budget = self.contract.budget
        self._budget = BudgetConfig(
            max_searches=budget.max_searches,
            max_crawls=budget.max_crawls,
            max_probes=budget.max_probes,
            max_tokens=budget.max_tokens,
            timeout_minutes=budget.timeout_minutes,
            sample_rows=budget.sample_rows,
            download_timeout_seconds=budget.download_timeout_seconds,
        )

    @property
    def profile_id(self) -> str:
        return self.contract.profile_id

    @property
    def status(self) -> ProfileStatus:
        return self.contract.status

    @property
    def reason(self) -> str | None:
        return self.contract.reason

    @property
    def catalogs(self) -> tuple[CatalogConfig, ...]:
        return self._catalogs

    @property
    def agent_usable_catalogs(self) -> tuple[CatalogConfig, ...]:
        """Catalogs that may be described to the agent, in declared order."""

        return tuple(catalog for catalog in self.catalogs if catalog.adapter != "tavily")

    @property
    def budget(self) -> BudgetConfig:
        return self._budget

    @property
    def trusted_hosts(self) -> tuple[HostRule, ...]:
        return self.contract.trusted_hosts

    @property
    def blocked_hosts(self) -> tuple[HostRule, ...]:
        return self.contract.blocked_hosts

    @property
    def trusted_domains(self) -> tuple[str, ...]:
        """Validated hostname strings for legacy adapter configuration."""

        return tuple(rule.hostname for rule in self.trusted_hosts)

    @property
    def blocked_sources(self) -> tuple[str, ...]:
        """Validated hostname strings for legacy adapter configuration."""

        return tuple(rule.hostname for rule in self.blocked_hosts)

    def require_runnable(self) -> None:
        """Reject a disabled profile at every execution-facing boundary."""

        if self.status is ProfileStatus.DISABLED:
            raise ProfileUnavailableError(f"Profile '{self.profile_id}' is disabled: {self.reason}")

    def get_catalog_by_type(self, catalog_type: str) -> Optional[CatalogConfig]:
        """Return the first catalog matching a given type, or None."""
        for catalog in self.catalogs:
            if catalog.type == catalog_type:
                return catalog
        return None

    def has_catalog_type(self, catalog_type: str) -> bool:
        """Check if this profile includes a catalog of the given type."""
        return self.get_catalog_by_type(catalog_type) is not None

    def is_domain_trusted(self, url: str) -> bool:
        """Check a parsed URL hostname against validated trusted-host rules."""

        hostname = _url_hostname(url)
        return hostname is not None and self.contract.is_trusted_host(hostname)

    def is_source_blocked(self, url: str) -> bool:
        """Check a parsed URL hostname against validated blocked-host rules."""

        hostname = _url_hostname(url)
        return hostname is not None and self.contract.is_blocked_host(hostname)

    def system_prompt_context(self) -> str:
        """
        Generate a profile-specific context string for the agent system prompt.
        Injected into the system prompt at runtime — no hardcoded values in the agent.
        """
        self.require_runnable()

        lines = [
            f"ACTIVE PROFILE: {self.name}",
            f"Language: {self.language}",
            "",
        ]

        # Geographic prose guides selection; it is not deterministic verification.
        if self.scope_instruction:
            lines.append("SCOPE GUIDANCE (NOT DETERMINISTIC VERIFICATION):")
            lines.append(f"  {self.scope_instruction.strip()}")
            if self.scope_regions:
                lines.append(f"  Geographic scope: {', '.join(self.scope_regions)}")
            lines.append("")

        lines += [
            "AVAILABLE CATALOG SOURCES (in priority order):",
        ]

        for cat in self.agent_usable_catalogs:
            lines.append(
                f"  - {sanitize_url_text(cat.name)} (adapter: {cat.adapter}, "
                f"url: {sanitize_url_text(cat.base_url)})"
            )

        if self.trusted_hosts:
            lines.append("")
            lines.append("TRUSTED DOMAINS (prefer results from these):")
            for rule in self.trusted_hosts:
                suffix = " (including subdomains)" if rule.include_subdomains else ""
                lines.append(f"  - {sanitize_url_text(rule.hostname)}{suffix}")

        if self.blocked_hosts:
            lines.append("")
            lines.append("BLOCKED SOURCES (do not access these):")
            for rule in self.blocked_hosts:
                suffix = " (including subdomains)" if rule.include_subdomains else ""
                lines.append(f"  - {sanitize_url_text(rule.hostname)}{suffix}")

        lines.append("")
        lines.append("LICENSE RULES:")
        lines.append(f"  Preferred: {', '.join(self.license.preference)}")
        lines.append(f"  Warn: {', '.join(self.license.warn)}")
        lines.append(f"  Reject: {', '.join(self.license.reject)}")

        if self.opendatasoft_portals:
            lines.append("")
            lines.append("OPENDATASOFT PORTALS (CSV URLs follow /exports/csv pattern):")
            for portal in self.opendatasoft_portals:
                lines.append(f"  - {portal}")

        return "\n".join(lines)


_CONTRACT_TOP_LEVEL_FIELDS = (
    "status",
    "reason",
    "catalogs",
    "budget",
    "trusted_hosts",
    "blocked_hosts",
)
_ANCILLARY_TOP_LEVEL_FIELDS = {
    "name",
    "description",
    "language",
    "cost_warning",
    "warning_message",
    "scope",
    "pricing",
    "license_preference",
    "license_warn",
    "license_reject",
    "opendatasoft_portals",
    "credibility_signals",
    "domain_keywords",
}
_CATALOG_FIELDS = {
    "catalog_id",
    "adapter",
    "name",
    "base_url",
    "api_key_env",
    "timeout_seconds",
    "priority",
    "required",
}
_BUDGET_FIELDS = {
    "max_searches",
    "max_crawls",
    "max_probes",
    "max_tokens",
    "timeout_minutes",
    "sample_rows",
    "download_timeout_seconds",
}
_HOST_RULE_FIELDS = {"hostname", "include_subdomains"}


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _shape_issues(raw: Mapping[str, object]) -> list[ContractIssue]:
    """Report only YAML-shape rules not owned by the P1 contract builder."""

    issues: list[ContractIssue] = []
    obsolete_top_level = {"trusted_domains", "blocked_sources"}
    for key in raw:
        if key in obsolete_top_level:
            issues.append(
                ContractIssue(
                    code="obsolete_field",
                    path=key,
                    message="must use structured trusted_hosts or blocked_hosts rules",
                )
            )
        elif key not in set(_CONTRACT_TOP_LEVEL_FIELDS) | _ANCILLARY_TOP_LEVEL_FIELDS:
            issues.append(
                ContractIssue(
                    code="unknown_field",
                    path=key,
                    message="is not a declared profile field",
                )
            )

    for field_name in _CONTRACT_TOP_LEVEL_FIELDS:
        if field_name not in raw:
            issues.append(
                ContractIssue(
                    code="missing_field",
                    path=field_name,
                    message="is required",
                )
            )

    catalogs = raw.get("catalogs")
    if _is_sequence(catalogs):
        for index, catalog in enumerate(catalogs):
            if not isinstance(catalog, Mapping):
                continue
            for field_name in catalog:
                path = f"catalogs[{index}].{field_name}"
                if field_name == "type":
                    issues.append(
                        ContractIssue(
                            code="obsolete_field",
                            path=path,
                            message="must use adapter",
                        )
                    )
                elif field_name not in _CATALOG_FIELDS:
                    issues.append(
                        ContractIssue(
                            code="unknown_field",
                            path=path,
                            message="is not a declared catalog field",
                        )
                    )

    budget = raw.get("budget")
    if isinstance(budget, Mapping):
        for field_name in budget:
            if field_name not in _BUDGET_FIELDS:
                issues.append(
                    ContractIssue(
                        code="unknown_field",
                        path=f"budget.{field_name}",
                        message="is not a declared budget field",
                    )
                )

    for collection_name in ("trusted_hosts", "blocked_hosts"):
        rules = raw.get(collection_name)
        if not _is_sequence(rules):
            continue
        for index, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                continue
            for field_name in rule:
                if field_name not in _HOST_RULE_FIELDS:
                    issues.append(
                        ContractIssue(
                            code="unknown_field",
                            path=f"{collection_name}[{index}].{field_name}",
                            message="is not a declared host-rule field",
                        )
                    )

    return issues


class ConfigLoader:
    """Load bundled profile descriptors through the immutable P1 contract."""

    def __init__(self, profiles_dir: Optional[Path] = None):
        self.profiles_dir = profiles_dir or DEFAULT_PROFILES_DIR

    def configured_profile_ids(self) -> list[str]:
        """Return every configured profile ID, including disabled descriptors."""

        if not self.profiles_dir.exists():
            return []
        return sorted(
            file.stem for file in self.profiles_dir.glob("*.yaml") if not file.name.startswith("_")
        )

    def profile_descriptors(self) -> list[Profile]:
        """Return every validated descriptor for listing and diagnostics."""

        return [self.load(profile_id) for profile_id in self.configured_profile_ids()]

    def automatically_selectable_profile_ids(self) -> list[str]:
        """Return only profiles eligible for automatic selection."""

        return [
            profile.profile_id
            for profile in self.profile_descriptors()
            if profile.status is ProfileStatus.ENABLED
        ]

    def explicitly_selectable_profile_ids(self) -> list[str]:
        """Return profiles eligible for an explicit user selection."""

        return [
            profile.profile_id
            for profile in self.profile_descriptors()
            if profile.status in {ProfileStatus.ENABLED, ProfileStatus.MANUAL_ONLY}
        ]

    def load(self, profile_name: str) -> Profile:
        """Load and statically validate a profile descriptor by filename-derived ID."""

        profile_path = self.profiles_dir / f"{profile_name}.yaml"
        if not profile_path.exists():
            available = self.configured_profile_ids()
            raise FileNotFoundError(
                f"Profile '{profile_name}' not found at {profile_path}.\n"
                f"Configured profiles: {', '.join(available)}"
            )

        try:
            with profile_path.open(encoding="utf-8") as profile_file:
                raw = yaml.safe_load(profile_file)
        except yaml.YAMLError as exc:
            raise ProfileContractError(
                (
                    ContractIssue(
                        code="invalid_yaml",
                        path="profile",
                        message="must contain valid YAML",
                    ),
                )
            ) from exc

        return self._parse(raw, profile_name)

    def _parse(self, raw: object, profile_name: str) -> Profile:
        """Validate one raw YAML value, then construct the runtime wrapper."""

        if not isinstance(raw, Mapping):
            raise ProfileContractError(
                (
                    ContractIssue(
                        code="invalid_type",
                        path="profile",
                        message="must be a mapping",
                    ),
                )
            )

        raw_profile = dict(raw)
        shape_issues = _shape_issues(raw_profile)
        missing_contract_fields = {
            issue.path
            for issue in shape_issues
            if issue.code == "missing_field" and "." not in issue.path
        }
        contract: ProfileContract | None = None
        contract_issues: tuple[ContractIssue, ...] = ()
        if not missing_contract_fields:
            from dataset_prober.tools import TOOL_REGISTRY

            try:
                contract = build_profile_contract(
                    profile_id=profile_name,
                    status=raw_profile["status"],
                    reason=raw_profile["reason"],
                    catalogs=raw_profile["catalogs"],
                    budget=raw_profile["budget"],
                    supported_adapters=TOOL_REGISTRY.keys(),
                    trusted_hosts=raw_profile["trusted_hosts"],
                    blocked_hosts=raw_profile["blocked_hosts"],
                )
            except ProfileContractError as exc:
                contract_issues = exc.issues

        if shape_issues or contract_issues:
            raise ProfileContractError((*shape_issues, *contract_issues))
        if contract is None:
            raise AssertionError("successful validation did not produce a profile contract")

        pricing_raw = raw_profile["pricing"]
        pricing = PricingConfig(
            input_per_million=pricing_raw["input_per_million"],
            output_per_million=pricing_raw["output_per_million"],
            cache_read_per_million=pricing_raw["cache_read_per_million"],
        )
        license_config = LicenseConfig(
            preference=raw_profile.get("license_preference", []),
            warn=raw_profile.get("license_warn", []),
            reject=raw_profile.get("license_reject", []),
        )
        scope = raw_profile.get("scope", {})

        return Profile(
            contract=contract,
            name=raw_profile["name"],
            description=raw_profile.get("description", ""),
            language=raw_profile.get("language", "en"),
            cost_warning=raw_profile.get("cost_warning", False),
            warning_message=raw_profile.get("warning_message"),
            pricing=pricing,
            license=license_config,
            scope_regions=scope.get("regions", []),
            scope_instruction=scope.get("instruction", ""),
            opendatasoft_portals=raw_profile.get("opendatasoft_portals", []),
            credibility_signals=raw_profile.get("credibility_signals", {}),
            domain_keywords=raw_profile.get("domain_keywords", {}),
            raw=raw_profile,
        )


def load_profile(profile_name: str, profiles_dir: Optional[Path] = None) -> Profile:
    """Convenience function to load a profile in one call."""
    return ConfigLoader(profiles_dir).load(profile_name)


def get_anthropic_api_key() -> str:
    """Resolve and validate the Anthropic API key from the environment."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Add it to your .env file (see README Setup)."
        )
    return key
