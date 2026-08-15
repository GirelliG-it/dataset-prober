"""Offline contracts for authoritative runtime profile capability resolution."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import dataset_prober.profile_resolution as profile_resolution
from dataset_prober.profile_contract import CKANDialect, build_profile_contract
from dataset_prober.profile_resolution import (
    ProfileResolutionError,
    ResolvedProfile,
    resolve_profile,
)


def _catalog(
    adapter: str,
    *,
    catalog_id: str | None = None,
    name: str | None = None,
    priority: int = 1,
    required: bool = True,
    api_key_env: str | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "catalog_id": catalog_id or f"{adapter}_catalog",
        "adapter": adapter,
        "name": name or f"{adapter.upper()} Catalog",
        "base_url": f"https://{adapter}.example/api",
        "api_key_env": api_key_env,
        "timeout_seconds": 10,
        "priority": priority,
        "required": required,
    }
    if adapter == "ckan":
        values.update(
            ckan_dialect="eu_hub",
            landing_base_url="https://portal.example",
        )
    return values


def _profile(test_profile, catalogs):
    source = test_profile.contract
    contract = build_profile_contract(
        profile_id="resolved_profile",
        status="enabled",
        reason=None,
        catalogs=catalogs,
        budget={
            field: getattr(source.budget, field)
            for field in (
                "max_searches",
                "max_crawls",
                "max_probes",
                "max_tokens",
                "timeout_minutes",
                "sample_rows",
                "download_timeout_seconds",
            )
        },
        supported_adapters={catalog["adapter"] for catalog in catalogs},
        trusted_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in source.trusted_hosts
        ],
        blocked_hosts=[
            {
                "hostname": rule.hostname,
                "include_subdomains": rule.include_subdomains,
            }
            for rule in source.blocked_hosts
        ],
    )
    return replace(test_profile, contract=contract)


class FakeTool:
    def __init__(
        self,
        config,
        *,
        source_type,
        available=True,
        source_error=None,
        availability_error=None,
        events=None,
    ):
        self.config = config
        self._source_type = source_type
        self._available = available
        self._source_error = source_error
        self._availability_error = availability_error
        self.availability_calls = 0
        self.events = events

    @property
    def source_type(self):
        if self._source_error is not None:
            raise self._source_error
        return self._source_type

    def is_available(self):
        self.availability_calls += 1
        if self.events is not None:
            self.events.append(("available", self._source_type))
        if self._availability_error is not None:
            raise self._availability_error
        return self._available


def _factory(
    adapter,
    created,
    *,
    events=None,
    available=True,
    source_type=None,
    source_error=None,
    availability_error=None,
    initialization_error=None,
):
    def construct(config):
        if events is not None:
            events.append(("construct", adapter))
        if initialization_error is not None:
            raise initialization_error
        tool = FakeTool(
            config,
            source_type=adapter if source_type is None else source_type,
            available=available,
            source_error=source_error,
            availability_error=availability_error,
            events=events,
        )
        created.append(tool)
        return tool

    return construct


def _codes(error):
    return [issue.code for issue in error.value.issues]


def test_priority_order_call_counts_and_exact_instances_are_aligned(test_profile):
    profile = _profile(
        test_profile,
        [
            _catalog("beta", priority=2),
            _catalog("alpha", priority=1),
        ],
    )
    events = []
    alpha_tools = []
    beta_tools = []
    resolved = resolve_profile(
        profile,
        registry={
            "alpha": _factory("alpha", alpha_tools, events=events),
            "beta": _factory("beta", beta_tools, events=events),
        },
    )

    assert resolved.source_keys == ("alpha", "beta")
    assert tuple(resolved.execution_map) == resolved.source_keys
    assert resolved.tools == (alpha_tools[0], beta_tools[0])
    assert tuple(resolved.execution_map.values()) == resolved.tools
    assert events == [
        ("construct", "alpha"),
        ("available", "alpha"),
        ("construct", "beta"),
        ("available", "beta"),
    ]
    assert [tool.availability_calls for tool in resolved.tools] == [1, 1]
    context = resolved.system_prompt_context
    assert context.index("ALPHA Catalog") < context.index("BETA Catalog")


def test_available_required_catalog_resolves(test_profile):
    created = []
    resolved = resolve_profile(
        _profile(test_profile, [_catalog("alpha")]),
        registry={"alpha": _factory("alpha", created)},
    )

    assert resolved.source_keys == ("alpha",)
    assert resolved.issues == ()
    assert created[0].availability_calls == 1


def test_optional_unavailable_catalog_is_excluded_with_surviving_source(test_profile):
    alpha = []
    beta = []
    resolved = resolve_profile(
        _profile(
            test_profile,
            [_catalog("alpha"), _catalog("beta", priority=2, required=False)],
        ),
        registry={
            "alpha": _factory("alpha", alpha),
            "beta": _factory("beta", beta, available=False),
        },
    )

    assert resolved.source_keys == ("alpha",)
    assert [issue.code for issue in resolved.issues] == ["adapter_unavailable"]
    assert resolved.issues[0].blocking is False
    assert beta[0].availability_calls == 1
    assert "BETA Catalog" not in resolved.system_prompt_context


def test_optional_unavailable_source_is_absent_from_every_model_surface(test_profile):
    from dataset_prober.dataset_agent import build_tool_definitions

    profile = _profile(
        test_profile,
        [
            _catalog("cbs", name="Resolved CBS"),
            _catalog("ckan", name="Unavailable CKAN", priority=2, required=False),
        ],
    )
    resolved = resolve_profile(
        profile,
        registry={
            "cbs": _factory("cbs", []),
            "ckan": _factory("ckan", [], available=False),
        },
    )
    definitions = build_tool_definitions(resolved)
    rendered = str(definitions).lower()

    assert tuple(resolved.execution_map) == resolved.source_keys == ("cbs",)
    assert "Resolved CBS" in resolved.system_prompt_context
    assert "Unavailable CKAN" not in resolved.system_prompt_context
    assert profile.catalogs[1].base_url not in resolved.system_prompt_context
    assert "ckan" not in rendered
    for definition in definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source is not None:
            assert source["enum"] == ["cbs"]


def test_required_unavailable_catalog_is_blocking_even_with_survivor(test_profile):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha"), _catalog("beta", priority=2)]),
            registry={
                "alpha": _factory("alpha", []),
                "beta": _factory("beta", [], available=False),
            },
        )

    assert _codes(error) == ["adapter_unavailable"]
    assert error.value.issues[0].blocking is True


@pytest.mark.parametrize("required", [False, True])
def test_missing_credential_follows_catalog_disposition(monkeypatch, test_profile, required):
    monkeypatch.delenv("SYNTHETIC_API_KEY", raising=False)
    profile = _profile(
        test_profile,
        [
            _catalog("alpha"),
            _catalog(
                "beta",
                priority=2,
                required=required,
                api_key_env="SYNTHETIC_API_KEY",
            ),
        ],
    )
    beta_factory_calls = []
    registry = {
        "alpha": _factory("alpha", []),
        "beta": _factory("beta", beta_factory_calls),
    }

    if required:
        with pytest.raises(ProfileResolutionError) as error:
            resolve_profile(profile, registry=registry)
        assert _codes(error) == ["missing_credential"]
    else:
        resolved = resolve_profile(profile, registry=registry)
        assert [issue.code for issue in resolved.issues] == ["missing_credential"]
        assert resolved.source_keys == ("alpha",)
    assert beta_factory_calls == []


def test_present_credential_is_checked_but_never_passed_to_resolved_tool_config(
    monkeypatch,
    test_profile,
):
    secret = "credential-value-that-must-not-propagate"
    monkeypatch.setenv("SYNTHETIC_API_KEY", secret)
    created = []
    resolved = resolve_profile(
        _profile(
            test_profile,
            [_catalog("alpha", api_key_env="SYNTHETIC_API_KEY")],
        ),
        registry={"alpha": _factory("alpha", created)},
    )

    assert created[0].config["api_key_env"] == "SYNTHETIC_API_KEY"
    assert secret not in created[0].config.values()
    assert secret not in repr(resolved)


@pytest.mark.parametrize("required", [False, True])
def test_constructor_exception_follows_catalog_disposition(test_profile, required):
    profile = _profile(
        test_profile,
        [_catalog("alpha"), _catalog("beta", priority=2, required=required)],
    )
    registry = {
        "alpha": _factory("alpha", []),
        "beta": _factory(
            "beta",
            [],
            initialization_error=RuntimeError("secret constructor payload"),
        ),
    }

    if required:
        with pytest.raises(ProfileResolutionError) as error:
            resolve_profile(profile, registry=registry)
        issues = error.value.issues
    else:
        issues = resolve_profile(profile, registry=registry).issues
    assert [issue.code for issue in issues] == ["adapter_initialization_failed"]
    assert issues[0].exception_class == "RuntimeError"


@pytest.mark.parametrize("required", [False, True])
def test_availability_exception_follows_catalog_disposition(test_profile, required):
    profile = _profile(
        test_profile,
        [_catalog("alpha"), _catalog("beta", priority=2, required=required)],
    )
    registry = {
        "alpha": _factory("alpha", []),
        "beta": _factory(
            "beta",
            [],
            availability_error=LookupError("secret availability payload"),
        ),
    }

    if required:
        with pytest.raises(ProfileResolutionError) as error:
            resolve_profile(profile, registry=registry)
        issues = error.value.issues
    else:
        issues = resolve_profile(profile, registry=registry).issues
    assert [issue.code for issue in issues] == ["availability_check_failed"]
    assert issues[0].exception_class == "LookupError"


def test_all_optional_catalogs_unavailable_adds_zero_source_failure(test_profile):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(
                test_profile,
                [
                    _catalog("alpha", required=False),
                    _catalog("beta", priority=2, required=False),
                ],
            ),
            registry={
                "alpha": _factory("alpha", [], available=False),
                "beta": _factory("beta", [], available=False),
            },
        )

    assert _codes(error) == [
        "adapter_unavailable",
        "adapter_unavailable",
        "no_executable_sources",
    ]


def test_truthy_non_boolean_availability_is_not_admitted(test_profile):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha", required=False)]),
            registry={"alpha": _factory("alpha", [], available=1)},
        )

    assert _codes(error) == ["adapter_unavailable", "no_executable_sources"]


def test_duplicate_adapters_are_rejected_before_construction(test_profile):
    constructed = []
    profile = _profile(
        test_profile,
        [
            _catalog("alpha", catalog_id="first_alpha", priority=1),
            _catalog("alpha", catalog_id="second_alpha", priority=2, required=False),
        ],
    )

    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(profile, registry={"alpha": _factory("alpha", constructed)})

    assert _codes(error) == ["duplicate_adapter", "no_executable_sources"]
    assert error.value.issues[0].blocking is True
    assert constructed == []


def test_source_type_access_failure_is_always_blocking(test_profile):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha", required=False)]),
            registry={
                "alpha": _factory(
                    "alpha",
                    [],
                    source_error=RuntimeError("https://user:secret@example.test/?token=hidden"),
                )
            },
        )

    assert _codes(error) == ["source_type_failed", "no_executable_sources"]
    assert error.value.issues[0].blocking is True
    assert error.value.issues[0].exception_class == "RuntimeError"


@pytest.mark.parametrize("source_type", ["gamma", "", 7])
def test_optional_source_type_mismatch_is_blocking_even_with_surviving_source(
    test_profile,
    source_type,
):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(
                test_profile,
                [
                    _catalog("alpha"),
                    _catalog("beta", priority=2, required=False),
                ],
            ),
            registry={
                "alpha": _factory("alpha", []),
                "beta": _factory("beta", [], source_type=source_type),
            },
        )

    assert _codes(error) == ["source_mismatch"]
    assert error.value.issues[0].blocking is True


def test_unregistered_optional_adapter_produces_zero_source_failure(test_profile):
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha", required=False)]),
            registry={},
        )

    assert _codes(error) == ["adapter_not_registered", "no_executable_sources"]


@pytest.mark.parametrize("required", [False, True])
def test_unregistered_adapter_follows_catalog_disposition_with_survivor(
    test_profile,
    required,
):
    profile = _profile(
        test_profile,
        [_catalog("alpha"), _catalog("beta", priority=2, required=required)],
    )
    if required:
        with pytest.raises(ProfileResolutionError) as error:
            resolve_profile(profile, registry={"alpha": _factory("alpha", [])})
        issues = error.value.issues
    else:
        issues = resolve_profile(
            profile,
            registry={"alpha": _factory("alpha", [])},
        ).issues

    assert [issue.code for issue in issues] == ["adapter_not_registered"]
    assert issues[0].blocking is required


def test_optional_tavily_is_policy_excluded_without_construction(test_profile):
    tavily_constructed = []
    resolved = resolve_profile(
        _profile(
            test_profile,
            [_catalog("alpha"), _catalog("tavily", priority=2, required=False)],
        ),
        registry={
            "alpha": _factory("alpha", []),
            "tavily": _factory("tavily", tavily_constructed),
        },
    )

    assert resolved.source_keys == ("alpha",)
    assert [issue.code for issue in resolved.issues] == ["policy_excluded"]
    assert tavily_constructed == []
    assert "TAVILY Catalog" not in resolved.system_prompt_context


def test_required_tavily_is_blocking_without_construction(test_profile):
    tavily_constructed = []
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha"), _catalog("tavily", priority=2)]),
            registry={
                "alpha": _factory("alpha", []),
                "tavily": _factory("tavily", tavily_constructed),
            },
        )

    assert _codes(error) == ["policy_excluded"]
    assert tavily_constructed == []


def test_only_optional_tavily_fails_with_zero_executable_sources(test_profile):
    tavily_constructed = []
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("tavily", required=False)]),
            registry={"tavily": _factory("tavily", tavily_constructed)},
        )

    assert _codes(error) == ["policy_excluded", "no_executable_sources"]
    assert tavily_constructed == []


def test_typed_ckan_route_values_reach_exact_constructed_tool(test_profile):
    created = []
    resolved = resolve_profile(
        _profile(test_profile, [_catalog("ckan")]),
        registry={"ckan": _factory("ckan", created)},
    )

    assert created[0] is resolved.tools[0]
    assert created[0].config["ckan_dialect"] is CKANDialect.EU_HUB
    assert created[0].config["landing_base_url"] == "https://portal.example"


def test_rendered_diagnostics_never_include_raw_exception_text(test_profile):
    sensitive = "https://user:secret@example.test/path?token=hidden#fragment"
    with pytest.raises(ProfileResolutionError) as error:
        resolve_profile(
            _profile(test_profile, [_catalog("alpha")]),
            registry={
                "alpha": _factory(
                    "alpha",
                    [],
                    initialization_error=RuntimeError(sensitive),
                )
            },
        )

    rendered = str(error.value)
    assert "RuntimeError" == error.value.issues[0].exception_class
    for secret in ("secret", "hidden", "fragment", sensitive):
        assert secret not in rendered
        assert secret not in error.value.issues[0].message


def test_resolved_profile_public_construction_is_disabled(test_profile):
    with pytest.raises(TypeError, match=r"created by resolve_profile\(\)"):
        ResolvedProfile(profile=test_profile, entries=())


def test_resolved_catalog_is_private_and_direct_construction_cannot_pair_foreign_tool(
    test_profile,
):
    profile = _profile(test_profile, [_catalog("cbs")])
    ckan_tool = FakeTool({}, source_type="ckan")

    assert "ResolvedCatalog" not in profile_resolution.__all__
    assert not hasattr(profile_resolution, "ResolvedCatalog")
    with pytest.raises(TypeError, match=r"created by resolve_profile\(\)"):
        profile_resolution._ResolvedCatalog(
            catalog=profile.catalogs[0],
            tool=ckan_tool,
            source_key="cbs",
        )


@pytest.mark.parametrize(
    ("entry_variant", "expected_message"),
    [
        ("foreign_catalog", "entries must use catalogs from its profile"),
        ("duplicate_source", "source keys must be unique"),
        ("incorrect_order", "entries must follow catalog priority order"),
        ("omitted_required", "must include every required agent-usable catalog"),
    ],
)
def test_resolved_profile_internal_alignment_checks_reject_invalid_entries(
    test_profile,
    entry_variant,
    expected_message,
):
    profile = _profile(
        test_profile,
        [_catalog("alpha"), _catalog("beta", priority=2)],
    )
    resolved = resolve_profile(
        profile,
        registry={
            "alpha": _factory("alpha", []),
            "beta": _factory("beta", []),
        },
    )
    if entry_variant == "foreign_catalog":
        foreign_profile = _profile(test_profile, [_catalog("gamma")])
        foreign = resolve_profile(
            foreign_profile,
            registry={"gamma": _factory("gamma", [])},
        )
        entries = (resolved.entries[0], foreign.entries[0])
    elif entry_variant == "duplicate_source":
        entries = (resolved.entries[0], resolved.entries[0])
    elif entry_variant == "incorrect_order":
        entries = tuple(reversed(resolved.entries))
    else:
        entries = (resolved.entries[0],)

    with pytest.raises(ValueError, match=expected_message):
        ResolvedProfile(
            profile=profile,
            entries=entries,
            _construction_token=profile_resolution._RESOLVER_CONSTRUCTION_TOKEN,
        )


def test_resolved_profile_replace_without_resolver_authorization_is_disabled(test_profile):
    resolved = resolve_profile(
        _profile(test_profile, [_catalog("alpha")]),
        registry={"alpha": _factory("alpha", [])},
    )

    with pytest.raises(TypeError, match=r"created by resolve_profile\(\)"):
        replace(resolved, entries=resolved.entries)


def test_resolved_views_are_immutable(test_profile):
    resolved = resolve_profile(
        _profile(test_profile, [_catalog("alpha")]),
        registry={"alpha": _factory("alpha", [])},
    )

    with pytest.raises(FrozenInstanceError):
        resolved.entries = ()
    with pytest.raises(FrozenInstanceError):
        resolved.entries[0].source_key = "other"
    with pytest.raises(TypeError):
        resolved.execution_map["other"] = resolved.tools[0]
