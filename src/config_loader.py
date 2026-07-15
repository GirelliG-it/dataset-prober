"""
src/config_loader.py

Loads and validates profile YAML files.
Provides the Profile object used throughout the agent pipeline.
All agent configuration flows through here — no hardcoded values anywhere else.
"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# Default profiles directory — relative to project root
DEFAULT_PROFILES_DIR = Path(__file__).parent.parent / "config" / "profiles"


@dataclass
class CatalogConfig:
    """Configuration for a single catalog source within a profile."""
    name: str
    type: str                        # "cbs", "ckan", "opendatasoft", "eurostat", "tavily"
    base_url: str
    api_key_env: Optional[str]       # Environment variable name for API key
    timeout_seconds: int
    priority: int

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


@dataclass
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
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0
    ) -> float:
        """Calculate cost in USD for a set of token counts."""
        return (
            (input_tokens / 1_000_000) * self.input_per_million +
            (output_tokens / 1_000_000) * self.output_per_million +
            (cache_read_tokens / 1_000_000) * self.cache_read_per_million
        )

    def format_cost(self, cost_usd: float) -> str:
        """Format cost as a human-readable string."""
        if cost_usd < 0.001:
            return f"${cost_usd * 100:.4f}¢"
        return f"${cost_usd:.4f}"


@dataclass
class LicenseConfig:
    """License preferences and rules."""
    preference: list     # Preferred licenses — flag if not found
    warn: list           # Warn if dataset uses these
    reject: list         # Skip datasets with these licenses


@dataclass
class Profile:
    """
    Complete profile configuration for an agent run.
    Loaded from a YAML file and optionally overridden by CLI flags.
    """
    # Identity
    name: str
    description: str
    language: str
    cost_warning: bool
    warning_message: Optional[str]

    # Configuration sections
    catalogs: list[CatalogConfig]
    budget: BudgetConfig
    pricing: PricingConfig
    license: LicenseConfig

    # Source filtering
    trusted_domains: list
    blocked_sources: list

    # Geographic scope — enforces profile boundaries
    scope_regions: list = field(default_factory=list)
    scope_instruction: str = ""

    # Optional sections
    opendatasoft_portals: list = field(default_factory=list)
    credibility_signals: dict = field(default_factory=dict)
    domain_keywords: dict = field(default_factory=dict)

    # Raw profile data (for passing to tools)
    raw: dict = field(default_factory=dict)

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
        """Check if a URL belongs to a trusted domain."""
        if not self.trusted_domains:
            return True  # Global profile — trust all
        url_lower = url.lower()
        return any(domain in url_lower for domain in self.trusted_domains)

    def is_source_blocked(self, url: str) -> bool:
        """Check if a URL is in the blocked sources list."""
        url_lower = url.lower()
        return any(blocked in url_lower for blocked in self.blocked_sources)

    def system_prompt_context(self) -> str:
        """
        Generate a profile-specific context string for the agent system prompt.
        Injected into the system prompt at runtime — no hardcoded values in the agent.
        """
        lines = [
            f"ACTIVE PROFILE: {self.name}",
            f"Language: {self.language}",
            "",
        ]

        # Scope restriction — injected first so Claude sees it before anything else
        if self.scope_instruction:
            lines.append("⚠️  SCOPE RESTRICTION (STRICTLY ENFORCED):")
            lines.append(f"  {self.scope_instruction.strip()}")
            if self.scope_regions:
                lines.append(f"  Geographic scope: {', '.join(self.scope_regions)}")
            lines.append("")

        lines += [
            "AVAILABLE CATALOG SOURCES (in priority order):",
        ]

        for cat in sorted(self.catalogs, key=lambda c: c.priority):
            lines.append(f"  - {cat.name} (type: {cat.type}, url: {cat.base_url})")

        if self.trusted_domains:
            lines.append("")
            lines.append("TRUSTED DOMAINS (prefer results from these):")
            for domain in self.trusted_domains:
                lines.append(f"  - {domain}")

        if self.blocked_sources:
            lines.append("")
            lines.append("BLOCKED SOURCES (do not access these):")
            for source in self.blocked_sources:
                lines.append(f"  - {source}")

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


class ConfigLoader:
    """
    Loads and validates profile YAML files.
    Manages the available profiles directory.
    """

    def __init__(self, profiles_dir: Optional[Path] = None):
        self.profiles_dir = profiles_dir or DEFAULT_PROFILES_DIR

    def list_profiles(self) -> list[str]:
        """Return list of available profile names (without .yaml extension)."""
        if not self.profiles_dir.exists():
            return []
        return [
            f.stem for f in self.profiles_dir.glob("*.yaml")
            if not f.name.startswith("_")
        ]

    def load(self, profile_name: str) -> Profile:
        """
        Load a profile by name.

        Args:
            profile_name: Profile filename without .yaml extension
                         (e.g. "dutch_government", "us_government")

        Returns:
            Fully validated Profile object

        Raises:
            FileNotFoundError: If profile file doesn't exist
            KeyError: If required fields are missing from profile
            EnvironmentError: If required API key env vars are not set
        """
        profile_path = self.profiles_dir / f"{profile_name}.yaml"

        if not profile_path.exists():
            available = self.list_profiles()
            raise FileNotFoundError(
                f"Profile '{profile_name}' not found at {profile_path}.\n"
                f"Available profiles: {', '.join(available)}"
            )

        with open(profile_path) as f:
            raw = yaml.safe_load(f)

        return self._parse(raw, profile_name)

    def _parse(self, raw: dict, profile_name: str) -> Profile:
        """Parse raw YAML dict into a Profile object."""

        # Catalogs
        catalogs = [
            CatalogConfig(
                name=c["name"],
                type=c["type"],
                base_url=c["base_url"],
                api_key_env=c.get("api_key_env"),
                timeout_seconds=c["timeout_seconds"],
                priority=c["priority"]
            )
            for c in raw.get("catalogs", [])
        ]

        # Budget
        b = raw["budget"]
        budget = BudgetConfig(
            max_searches=b["max_searches"],
            max_crawls=b["max_crawls"],
            max_probes=b["max_probes"],
            max_tokens=b["max_tokens"],
            timeout_minutes=b["timeout_minutes"],
            sample_rows=b["sample_rows"],
            download_timeout_seconds=b["download_timeout_seconds"]
        )

        # Pricing
        p = raw["pricing"]
        pricing = PricingConfig(
            input_per_million=p["input_per_million"],
            output_per_million=p["output_per_million"],
            cache_read_per_million=p["cache_read_per_million"]
        )

        # License
        lic = raw.get("license_preference", [])
        license_config = LicenseConfig(
            preference=raw.get("license_preference", []),
            warn=raw.get("license_warn", []),
            reject=raw.get("license_reject", [])
        )

        # Scope
        scope = raw.get("scope", {})
        scope_regions = scope.get("regions", [])
        scope_instruction = scope.get("instruction", "")

        return Profile(
            name=raw["name"],
            description=raw.get("description", ""),
            language=raw.get("language", "en"),
            cost_warning=raw.get("cost_warning", False),
            warning_message=raw.get("warning_message"),
            catalogs=catalogs,
            budget=budget,
            pricing=pricing,
            license=license_config,
            trusted_domains=raw.get("trusted_domains", []),
            blocked_sources=raw.get("blocked_sources", []),
            scope_regions=scope_regions,
            scope_instruction=scope_instruction,
            opendatasoft_portals=raw.get("opendatasoft_portals", []),
            credibility_signals=raw.get("credibility_signals", {}),
            domain_keywords=raw.get("domain_keywords", {}),
            raw=raw
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
