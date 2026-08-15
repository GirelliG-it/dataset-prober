"""Authoritative local runtime capability resolution for validated profiles."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from dataset_prober.config_loader import CatalogConfig, Profile

if TYPE_CHECKING:
    from dataset_prober.tools.base import DataSourceTool


_RESOLVER_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ResolutionIssue:
    """One structured, secret-safe local capability-resolution issue."""

    code: str
    catalog_id: str | None
    adapter: str | None
    required: bool
    blocking: bool
    message: str
    exception_class: str | None = None


class ProfileResolutionError(ValueError):
    """Expected fail-closed outcome from local profile capability resolution."""

    def __init__(self, issues: tuple[ResolutionIssue, ...]) -> None:
        if not issues:
            raise ValueError("ProfileResolutionError requires at least one issue")
        self.issues = tuple(issues)
        rendered = "; ".join(
            f"{issue.code} ({issue.catalog_id or 'profile'})" for issue in self.issues
        )
        super().__init__(f"Profile capability resolution failed: {rendered}")


@dataclass(frozen=True, slots=True)
class _ResolvedCatalog:
    """Resolver-owned catalog aligned with its exact constructed adapter instance."""

    catalog: CatalogConfig
    tool: DataSourceTool
    source_key: str
    _construction_token: InitVar[object | None] = None

    def __post_init__(self, _construction_token: object | None) -> None:
        if _construction_token is not _RESOLVER_CONSTRUCTION_TOKEN:
            raise TypeError("Resolved catalogs must be created by resolve_profile()")
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise ValueError("Resolved catalog source_key must be a nonblank string")
        if self.source_key != self.catalog.adapter:
            raise ValueError("Resolved catalog source_key must equal its catalog adapter")


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """Immutable, internally aligned executable view of one validated profile."""

    profile: Profile
    entries: tuple[_ResolvedCatalog, ...]
    issues: tuple[ResolutionIssue, ...] = ()
    _construction_token: InitVar[object | None] = None
    _execution_map: Mapping[str, DataSourceTool] = field(init=False, repr=False, compare=False)

    def __post_init__(self, _construction_token: object | None) -> None:
        if _construction_token is not _RESOLVER_CONSTRUCTION_TOKEN:
            raise TypeError("Resolved profiles must be created by resolve_profile()")
        entries = tuple(self.entries)
        issues = tuple(self.issues)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "issues", issues)
        if not entries:
            raise ValueError("ResolvedProfile requires at least one executable catalog")
        if any(issue.blocking for issue in issues):
            raise ValueError("ResolvedProfile may contain only nonblocking issues")

        source_keys = tuple(entry.source_key for entry in entries)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("ResolvedProfile source keys must be unique")
        if any(entry.source_key != entry.catalog.adapter for entry in entries):
            raise ValueError("ResolvedProfile source keys must equal catalog adapters")

        profile_catalogs = self.profile.catalogs
        if any(
            not any(entry.catalog is profile_catalog for profile_catalog in profile_catalogs)
            for entry in entries
        ):
            raise ValueError("ResolvedProfile entries must use catalogs from its profile")
        priorities = tuple(entry.catalog.priority for entry in entries)
        if priorities != tuple(sorted(priorities)):
            raise ValueError("ResolvedProfile entries must follow catalog priority order")
        required_catalogs = tuple(
            catalog for catalog in self.profile.agent_usable_catalogs if catalog.required
        )
        if any(
            not any(entry.catalog is required_catalog for entry in entries)
            for required_catalog in required_catalogs
        ):
            raise ValueError("ResolvedProfile must include every required agent-usable catalog")

        execution_map = MappingProxyType({entry.source_key: entry.tool for entry in entries})
        object.__setattr__(self, "_execution_map", execution_map)

    @property
    def catalogs(self) -> tuple[CatalogConfig, ...]:
        return tuple(entry.catalog for entry in self.entries)

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(entry.source_key for entry in self.entries)

    @property
    def tools(self) -> tuple[DataSourceTool, ...]:
        return tuple(entry.tool for entry in self.entries)

    @property
    def execution_map(self) -> Mapping[str, DataSourceTool]:
        return self._execution_map

    @property
    def system_prompt_context(self) -> str:
        return self.profile.system_prompt_context(self.catalogs)


ToolFactory = Callable[[dict[str, Any]], "DataSourceTool"]


def _issue(
    code: str,
    catalog: CatalogConfig | None,
    *,
    blocking: bool,
    message: str,
    exception: Exception | None = None,
) -> ResolutionIssue:
    exception_class = None
    if exception is not None:
        candidate = type(exception).__name__
        exception_class = candidate if candidate.isidentifier() else "Exception"
    return ResolutionIssue(
        code=code,
        catalog_id=catalog.catalog_id if catalog else None,
        adapter=catalog.adapter if catalog else None,
        required=catalog.required if catalog else True,
        blocking=blocking,
        message=message,
        exception_class=exception_class,
    )


def _catalog_tool_config(profile: Profile, catalog: CatalogConfig) -> dict[str, Any]:
    return {
        "catalog_id": catalog.catalog_id,
        "adapter": catalog.adapter,
        "name": catalog.name,
        "base_url": catalog.base_url,
        "api_key_env": catalog.api_key_env,
        "timeout_seconds": catalog.timeout_seconds,
        "priority": catalog.priority,
        "required": catalog.required,
        "ckan_dialect": catalog.ckan_dialect,
        "landing_base_url": catalog.landing_base_url,
        "trusted_domains": profile.trusted_domains,
        "blocked_sources": profile.blocked_sources,
        "sample_rows": profile.budget.sample_rows,
        "download_timeout_seconds": profile.budget.download_timeout_seconds,
    }


def resolve_profile(
    profile: Profile,
    *,
    registry: Mapping[str, ToolFactory],
) -> ResolvedProfile:
    """Resolve one profile's truthful local agent capabilities exactly once."""

    profile.require_runnable()
    catalogs = tuple(sorted(profile.catalogs, key=lambda catalog: catalog.priority))
    usable_ids = {catalog.catalog_id for catalog in profile.agent_usable_catalogs}
    issues: list[ResolutionIssue] = []

    for catalog in catalogs:
        if catalog.catalog_id not in usable_ids:
            issues.append(
                _issue(
                    "policy_excluded",
                    catalog,
                    blocking=catalog.required,
                    message="Catalog is excluded by the agent capability policy.",
                )
            )

    usable_catalogs = tuple(catalog for catalog in catalogs if catalog.catalog_id in usable_ids)
    seen_adapters: set[str] = set()
    duplicate_found = False
    for catalog in usable_catalogs:
        if catalog.adapter in seen_adapters:
            duplicate_found = True
            issues.append(
                _issue(
                    "duplicate_adapter",
                    catalog,
                    blocking=True,
                    message="Adapter is declared by more than one agent-usable catalog.",
                )
            )
        seen_adapters.add(catalog.adapter)

    if duplicate_found:
        issues.append(
            _issue(
                "no_executable_sources",
                None,
                blocking=True,
                message="No executable source map can be built from duplicate adapters.",
            )
        )
        raise ProfileResolutionError(tuple(issues))

    entries: list[_ResolvedCatalog] = []
    for catalog in usable_catalogs:
        blocking = catalog.required
        if catalog.api_key_env and not os.environ.get(catalog.api_key_env):
            issues.append(
                _issue(
                    "missing_credential",
                    catalog,
                    blocking=blocking,
                    message="Configured credential environment variable is missing or empty.",
                )
            )
            continue

        try:
            factory = registry.get(catalog.adapter)
        except Exception as exc:
            issues.append(
                _issue(
                    "adapter_not_registered",
                    catalog,
                    blocking=blocking,
                    message="Adapter could not be read from the injected runtime registry.",
                    exception=exc,
                )
            )
            continue
        if factory is None:
            issues.append(
                _issue(
                    "adapter_not_registered",
                    catalog,
                    blocking=blocking,
                    message="Adapter is not present in the injected runtime registry.",
                )
            )
            continue

        try:
            tool = factory(_catalog_tool_config(profile, catalog))
        except Exception as exc:
            issues.append(
                _issue(
                    "adapter_initialization_failed",
                    catalog,
                    blocking=blocking,
                    message="Adapter construction failed.",
                    exception=exc,
                )
            )
            continue

        try:
            source_key = tool.source_type
        except Exception as exc:
            issues.append(
                _issue(
                    "source_type_failed",
                    catalog,
                    blocking=True,
                    message="Adapter source identity could not be read.",
                    exception=exc,
                )
            )
            continue
        if (
            not isinstance(source_key, str)
            or not source_key.strip()
            or source_key != catalog.adapter
        ):
            issues.append(
                _issue(
                    "source_mismatch",
                    catalog,
                    blocking=True,
                    message="Adapter source identity does not match its declared adapter.",
                )
            )
            continue

        try:
            available = tool.is_available()
        except Exception as exc:
            issues.append(
                _issue(
                    "availability_check_failed",
                    catalog,
                    blocking=blocking,
                    message="Adapter availability check failed.",
                    exception=exc,
                )
            )
            continue
        if available is not True:
            issues.append(
                _issue(
                    "adapter_unavailable",
                    catalog,
                    blocking=blocking,
                    message="Adapter is not locally available.",
                )
            )
            continue

        entries.append(
            _ResolvedCatalog(
                catalog=catalog,
                tool=tool,
                source_key=source_key,
                _construction_token=_RESOLVER_CONSTRUCTION_TOKEN,
            )
        )

    if not entries:
        issues.append(
            _issue(
                "no_executable_sources",
                None,
                blocking=True,
                message="No executable catalog source remains after local capability resolution.",
            )
        )
    if any(issue.blocking for issue in issues):
        raise ProfileResolutionError(tuple(issues))
    return ResolvedProfile(
        profile=profile,
        entries=tuple(entries),
        issues=tuple(issues),
        _construction_token=_RESOLVER_CONSTRUCTION_TOKEN,
    )


__all__ = (
    "ProfileResolutionError",
    "ResolutionIssue",
    "ResolvedProfile",
    "resolve_profile",
)
