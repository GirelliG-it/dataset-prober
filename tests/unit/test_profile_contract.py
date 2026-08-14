"""Contract tests for pure static profile validation primitives."""

from __future__ import annotations

from collections import UserString
from dataclasses import FrozenInstanceError

import pytest

from dataset_prober.profile_contract import (
    BudgetContract,
    CatalogContract,
    CKANDialect,
    HostRule,
    ProfileContract,
    ProfileContractError,
    ProfileStatus,
    build_profile_contract,
)


def _catalog(**overrides: object) -> dict[str, object]:
    adapter = overrides.get("adapter", "ckan")
    values: dict[str, object] = {
        "catalog_id": "primary_catalog",
        "adapter": adapter,
        "name": "Primary catalog",
        "base_url": "https://catalog.example/api/3",
        "api_key_env": None,
        "timeout_seconds": 30,
        "priority": 1,
        "required": True,
        "ckan_dialect": "ckan_action" if adapter == "ckan" else None,
        "landing_base_url": "https://catalog.example" if adapter == "ckan" else None,
    }
    values.update(overrides)
    return values


def _budget(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "max_searches": 5,
        "max_crawls": 8,
        "max_probes": 15,
        "max_tokens": 4096,
        "timeout_minutes": 10,
        "sample_rows": 10,
        "download_timeout_seconds": 300,
    }
    values.update(overrides)
    return values


def _build(**overrides: object):
    values: dict[str, object] = {
        "profile_id": "test_profile",
        "status": "enabled",
        "reason": None,
        "catalogs": [_catalog()],
        "budget": _budget(),
        "supported_adapters": {"cbs", "ckan", "tavily"},
        "trusted_hosts": [{"hostname": "catalog.example", "include_subdomains": True}],
        "blocked_hosts": [{"hostname": "blocked.example", "include_subdomains": False}],
    }
    values.update(overrides)
    return build_profile_contract(**values)


def _issues(**overrides: object):
    with pytest.raises(ProfileContractError) as error:
        _build(**overrides)
    return error.value.issues


@pytest.mark.parametrize(
    ("status", "reason", "catalogs"),
    [
        ("enabled", None, [_catalog()]),
        ("manual_only", "Requires an explicit runtime preflight", [_catalog()]),
        ("disabled", "No supported implementation", []),
    ],
)
def test_all_profile_lifecycle_states(status, reason, catalogs):
    contract = _build(status=status, reason=reason, catalogs=catalogs)

    assert contract.status is ProfileStatus(status)
    assert contract.reason == reason
    assert len(contract.catalogs) == len(catalogs)


@pytest.mark.parametrize("status", ["manual_only", "disabled"])
@pytest.mark.parametrize("reason", [None, "", "  \t"])
def test_non_enabled_profiles_require_a_nonblank_reason(status, reason):
    issues = _issues(status=status, reason=reason)

    assert [(issue.code, issue.path) for issue in issues] == [("reason_required", "reason")]


@pytest.mark.parametrize("status", ["enabled", "manual_only"])
def test_selectable_policy_states_require_a_catalog(status):
    reason = "Manual preflight is required" if status == "manual_only" else None

    issues = _issues(status=status, reason=reason, catalogs=[])

    assert [(issue.code, issue.path) for issue in issues] == [("catalogs_required", "catalogs")]


def test_disabled_profile_may_have_no_catalogs():
    contract = _build(status="disabled", reason="No runnable source", catalogs=[])

    assert contract.catalogs == ()


@pytest.mark.parametrize("status", ["", "automatic", "ENABLED", 1, True])
def test_profile_status_must_be_one_of_the_three_declared_values(status):
    issues = _issues(status=status)

    assert (issues[0].code, issues[0].path) == ("invalid_status", "status")


@pytest.mark.parametrize("profile_id", ["profile", "profile_2", "eu_open_data"])
def test_valid_profile_ids(profile_id):
    assert _build(profile_id=profile_id).profile_id == profile_id


@pytest.mark.parametrize(
    "profile_id",
    ["", "Profile", "profile-name", "profile name", "_profile", "profile_", "a__b"],
)
def test_invalid_profile_ids(profile_id):
    issues = _issues(profile_id=profile_id)

    assert (issues[0].code, issues[0].path) == ("invalid_id", "profile_id")


@pytest.mark.parametrize("catalog_id", ["catalog", "catalog_2", "data_gov"])
def test_valid_catalog_ids(catalog_id):
    contract = _build(catalogs=[_catalog(catalog_id=catalog_id)])

    assert contract.catalogs[0].catalog_id == catalog_id


@pytest.mark.parametrize(
    "catalog_id",
    ["", "Catalog", "catalog-name", "catalog name", "_catalog", "catalog_", "a__b"],
)
def test_invalid_catalog_ids(catalog_id):
    issues = _issues(catalogs=[_catalog(catalog_id=catalog_id)])

    assert (issues[0].code, issues[0].path) == ("invalid_id", "catalogs[0].catalog_id")


def test_catalog_ids_must_be_unique_within_profile():
    issues = _issues(
        catalogs=[
            _catalog(),
            _catalog(name="Second", base_url="https://second.example", priority=2),
        ]
    )

    assert [(issue.code, issue.path) for issue in issues] == [
        ("duplicate_catalog_id", "catalogs[1].catalog_id")
    ]


def test_catalog_priorities_must_be_unique_within_profile():
    issues = _issues(
        catalogs=[
            _catalog(),
            _catalog(
                catalog_id="second_catalog",
                name="Second",
                base_url="https://second.example",
            ),
        ]
    )

    assert [(issue.code, issue.path) for issue in issues] == [
        ("duplicate_priority", "catalogs[1].priority")
    ]


def test_two_catalogs_may_share_one_adapter():
    contract = _build(
        catalogs=[
            _catalog(),
            _catalog(
                catalog_id="second_catalog",
                name="Second",
                base_url="https://second.example",
                priority=2,
            ),
        ]
    )

    assert [catalog.adapter for catalog in contract.catalogs] == ["ckan", "ckan"]


def test_adapter_support_is_supplied_explicitly():
    issues = _issues(catalogs=[_catalog(adapter="eurostat")])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("unsupported_adapter", "catalogs[0].adapter")
    ]


def test_direct_complete_contract_cannot_bypass_supported_adapter_validation():
    catalog = CatalogContract(**_catalog(adapter="eurostat"))

    with pytest.raises(ProfileContractError) as error:
        ProfileContract(
            profile_id="test_profile",
            status=ProfileStatus.ENABLED,
            reason=None,
            catalogs=(catalog,),
            budget=BudgetContract(**_budget()),
            supported_adapters={"ckan"},
        )

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("unsupported_adapter", "catalogs[0].adapter")
    ]


def test_direct_complete_contract_does_not_retain_supported_adapter_collection():
    supported_adapters = ["ckan"]
    contract = ProfileContract(
        profile_id="test_profile",
        status=ProfileStatus.ENABLED,
        reason=None,
        catalogs=(CatalogContract(**_catalog()),),
        budget=BudgetContract(**_budget()),
        supported_adapters=supported_adapters,
    )

    supported_adapters.clear()

    assert contract.catalogs[0].adapter == "ckan"
    assert not hasattr(contract, "supported_adapters")


def test_supported_adapter_collection_must_not_be_a_string():
    issues = _issues(supported_adapters="ckan")

    assert [(issue.code, issue.path) for issue in issues] == [
        ("invalid_collection", "supported_adapters")
    ]


@pytest.mark.parametrize("name", [None, "", " \t"])
def test_catalog_name_must_be_present_and_nonblank(name):
    catalog = _catalog()
    if name is None:
        del catalog["name"]
    else:
        catalog["name"] = name

    issues = _issues(catalogs=[catalog])

    expected_code = "missing_field" if name is None else "blank_value"
    assert (issues[0].code, issues[0].path) == (expected_code, "catalogs[0].name")


@pytest.mark.parametrize(
    "field",
    [
        "catalog_id",
        "adapter",
        "name",
        "base_url",
        "api_key_env",
        "timeout_seconds",
        "priority",
        "required",
    ],
)
def test_all_catalog_fields_are_required(field):
    catalog = _catalog()
    del catalog[field]

    issues = _issues(catalogs=[catalog])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("missing_field", f"catalogs[0].{field}")
    ]


@pytest.mark.parametrize(
    "dialect",
    ["ckan_action", "eu_hub", CKANDialect.CKAN_ACTION, CKANDialect.EU_HUB],
)
def test_ckan_dialects_are_accepted_and_normalized(dialect):
    contract = _build(catalogs=[_catalog(ckan_dialect=dialect)])

    assert contract.catalogs[0].ckan_dialect is CKANDialect(dialect)


@pytest.mark.parametrize("field", ["ckan_dialect", "landing_base_url"])
def test_ckan_catalogs_require_route_fields_in_raw_declarations(field):
    catalog = _catalog()
    del catalog[field]

    issues = _issues(catalogs=[catalog])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("missing_field", f"catalogs[0].{field}")
    ]


@pytest.mark.parametrize(
    "dialect",
    [None, "", " ", 1, True, UserString("ckan_action"), "CKAN_ACTION", "EU_HUB", "unknown"],
)
def test_ckan_dialect_must_be_one_exact_declared_value(dialect):
    issues = _issues(catalogs=[_catalog(ckan_dialect=dialect)])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("invalid_ckan_dialect", "catalogs[0].ckan_dialect")
    ]


def test_direct_catalog_construction_validates_and_normalizes_ckan_dialect():
    catalog = CatalogContract(**_catalog(ckan_dialect="eu_hub"))

    assert catalog.ckan_dialect is CKANDialect.EU_HUB

    with pytest.raises(ProfileContractError) as error:
        CatalogContract(**_catalog(ckan_dialect="EU_HUB"))

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("invalid_ckan_dialect", "catalog.ckan_dialect")
    ]

    with pytest.raises(ProfileContractError) as error:
        CatalogContract(**_catalog(ckan_dialect=UserString("ckan_action")))

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("invalid_ckan_dialect", "catalog.ckan_dialect")
    ]


def test_ckan_landing_origin_may_differ_from_api_host_and_accept_root_slash():
    contract = _build(
        catalogs=[
            _catalog(
                base_url="https://api.public.example/api/3",
                landing_base_url="https://catalog.public.example/",
            )
        ]
    )

    assert contract.catalogs[0].base_url == "https://api.public.example/api/3"
    assert contract.catalogs[0].landing_base_url == "https://catalog.public.example/"


@pytest.mark.parametrize(
    ("landing_base_url", "code"),
    [
        (None, "invalid_type"),
        (7, "invalid_type"),
        ("", "invalid_url_scheme"),
        ("ftp://catalog.example", "invalid_url_scheme"),
        ("https:///missing-host", "invalid_url_host"),
        ("https://user:secret@catalog.example", "embedded_credentials"),
        ("https://catalog.example:444", "disallowed_port"),
        ("https://catalog.example/dataset", "url_path_not_allowed"),
        ("https://catalog.example?view=data", "url_query_not_allowed"),
        ("https://catalog.example#data", "url_fragment_not_allowed"),
    ],
)
def test_ckan_landing_url_must_be_a_safe_origin(landing_base_url, code):
    issues = _issues(catalogs=[_catalog(landing_base_url=landing_base_url)])

    assert any(
        issue.code == code and issue.path == "catalogs[0].landing_base_url" for issue in issues
    )


@pytest.mark.parametrize("include_null_fields", [False, True])
def test_non_ckan_catalogs_do_not_require_ckan_route_fields(include_null_fields):
    catalog = _catalog(adapter="cbs")
    if not include_null_fields:
        catalog.pop("ckan_dialect")
        catalog.pop("landing_base_url")

    contract = _build(catalogs=[catalog])

    assert contract.catalogs[0].ckan_dialect is None
    assert contract.catalogs[0].landing_base_url is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ckan_dialect", "ckan_action"),
        ("landing_base_url", "https://catalog.example"),
    ],
)
def test_non_ckan_catalogs_reject_non_null_ckan_route_fields(field, value):
    issues = _issues(catalogs=[_catalog(adapter="cbs", **{field: value})])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("field_not_applicable", f"catalogs[0].{field}")
    ]


@pytest.mark.parametrize("required", [0, 1, None, "true"])
def test_catalog_required_must_be_an_actual_boolean(required):
    issues = _issues(catalogs=[_catalog(required=required)])

    assert (issues[0].code, issues[0].path) == (
        "invalid_boolean",
        "catalogs[0].required",
    )


@pytest.mark.parametrize("required", [True, False])
def test_catalog_required_accepts_boolean_values(required):
    contract = _build(catalogs=[_catalog(required=required)])

    assert contract.catalogs[0].required is required


@pytest.mark.parametrize(
    "api_key_env",
    [None, "API_KEY", "_PRIVATE_KEY", "KEY2", "lower_case_key"],
)
def test_valid_environment_variable_names(api_key_env):
    contract = _build(catalogs=[_catalog(api_key_env=api_key_env)])

    assert contract.catalogs[0].api_key_env == api_key_env


@pytest.mark.parametrize(
    "api_key_env",
    ["", "2API_KEY", "API-KEY", "API KEY", "API.KEY", True, 123],
)
def test_invalid_environment_variable_names(api_key_env):
    issues = _issues(catalogs=[_catalog(api_key_env=api_key_env)])

    assert (issues[0].code, issues[0].path) == (
        "invalid_environment_name",
        "catalogs[0].api_key_env",
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://catalog.example",
        "http://catalog.example:80/path",
        "https://catalog.example",
        "https://catalog.example:443/path",
        "https://[2001:4860:4860::8888]/catalog",
    ],
)
def test_valid_static_catalog_urls(base_url):
    assert _build(catalogs=[_catalog(base_url=base_url)]).catalogs[0].base_url == base_url


@pytest.mark.parametrize(
    ("base_url", "code"),
    [
        ("catalog.example/api", "invalid_url_scheme"),
        ("ftp://catalog.example/data", "invalid_url_scheme"),
        ("https:///missing-host", "invalid_url_host"),
        ("https://user@catalog.example", "embedded_credentials"),
        ("https://user:password@catalog.example", "embedded_credentials"),
        ("https://catalog.example/path?limit=1", "url_query_not_allowed"),
        ("https://catalog.example/path#section", "url_fragment_not_allowed"),
        (" https://catalog.example", "url_whitespace"),
        ("https://catalog.example ", "url_whitespace"),
        ("https://catalog.example/with space", "url_whitespace"),
        ("https://catalog.example/line\nbreak", "url_control_character"),
        (r"https:\\catalog.example\path", "url_backslash"),
        ("https://catalog.example:444", "disallowed_port"),
        ("http://catalog.example:443", "disallowed_port"),
        ("https://catalog.example:notaport", "invalid_url_port"),
        ("https://catalog.example:", "invalid_url_port"),
        ("https://catalog.example:/path", "invalid_url_port"),
        ("https://[fe80::1%25eth0]/catalog", "encoded_or_scoped_host"),
        ("https://%31%32%37.0.0.1/catalog", "encoded_or_scoped_host"),
    ],
)
def test_invalid_static_catalog_urls(base_url, code):
    issues = _issues(catalogs=[_catalog(base_url=base_url)])

    assert any(issue.code == code and issue.path == "catalogs[0].base_url" for issue in issues)


@pytest.mark.parametrize(
    ("base_url", "code"),
    [
        ("https://catalog.example/%5cadmin", "url_backslash"),
        ("https://catalog.example/%5Cadmin", "url_backslash"),
        ("https://catalog.example/%0aheader", "url_control_character"),
        ("https://catalog.example/%0D%0Aheader", "url_control_character"),
        ("https://catalog.example/%00data", "url_control_character"),
        ("https://catalog.example/%7fdata", "url_control_character"),
        ("https://catalog.example/%zz", "malformed_percent_encoding"),
        ("https://catalog.example/%", "malformed_percent_encoding"),
        ("https://catalog.example/%0", "malformed_percent_encoding"),
        ("https://catalog.example/%GG", "malformed_percent_encoding"),
        ("https://catalog.example/%ZZ", "malformed_percent_encoding"),
    ],
)
def test_percent_decoded_unsafe_content_and_malformed_escapes_fail_closed(base_url, code):
    issues = _issues(catalogs=[_catalog(base_url=base_url)])

    assert [(issue.code, issue.path) for issue in issues] == [(code, "catalogs[0].base_url")]


def test_url_errors_do_not_repeat_credentials_or_query_values():
    sensitive_url = "https://alice:secret@catalog.example/path?token=private#sensitive"

    with pytest.raises(ProfileContractError) as error:
        _build(catalogs=[_catalog(base_url=sensitive_url)])

    rendered = str(error.value)
    for secret in ("alice", "secret", "private", "sensitive"):
        assert secret not in rendered


def test_encoded_url_errors_do_not_repeat_url_or_decoded_content():
    sensitive_url = "https://catalog.example/%0asecret-marker"

    with pytest.raises(ProfileContractError) as error:
        _build(catalogs=[_catalog(base_url=sensitive_url)])

    rendered = str(error.value)
    assert sensitive_url not in rendered
    assert "secret-marker" not in rendered


def test_host_rule_canonicalizes_case_and_trailing_root_dot():
    rule = HostRule("Trusted.Example.", include_subdomains=False)

    assert rule.hostname == "trusted.example"
    assert rule.matches("TRUSTED.EXAMPLE.")


def test_host_rule_exact_matching_rejects_prefix_suffix_and_lookalike_attacks():
    rule = HostRule("trusted.example", include_subdomains=False)

    assert rule.matches("trusted.example")
    assert not rule.matches("sub.trusted.example")
    assert not rule.matches("nottrusted.example")
    assert not rule.matches("trusted.example.evil")


def test_host_rule_subdomains_require_an_explicit_dot_boundary():
    rule = HostRule("trusted.example", include_subdomains=True)

    assert rule.matches("trusted.example")
    assert rule.matches("api.trusted.example")
    assert rule.matches("deep.api.trusted.example.")
    assert not rule.matches("nottrusted.example")
    assert not rule.matches("trusted.example.evil")


def test_empty_host_rule_collections_match_nothing():
    contract = _build(trusted_hosts=[], blocked_hosts=[])

    assert not contract.is_trusted_host("anything.example")
    assert not contract.is_blocked_host("anything.example")


def test_profile_host_rule_collections_apply_exact_and_subdomain_policy():
    contract = _build(
        trusted_hosts=[{"hostname": "trusted.example", "include_subdomains": True}],
        blocked_hosts=[{"hostname": "blocked.example", "include_subdomains": False}],
    )

    assert contract.is_trusted_host("data.trusted.example")
    assert not contract.is_trusted_host("trusted.example.evil")
    assert contract.is_blocked_host("blocked.example")
    assert not contract.is_blocked_host("sub.blocked.example")


@pytest.mark.parametrize(
    "hostname",
    ["", " trusted.example", "*.trusted.example", ".trusted.example", "a..example"],
)
def test_invalid_host_rules_are_rejected(hostname):
    with pytest.raises(ProfileContractError) as error:
        HostRule(hostname, include_subdomains=True)

    assert error.value.issues[0].code == "invalid_hostname"


@pytest.mark.parametrize(
    "field",
    [
        "max_searches",
        "max_crawls",
        "max_probes",
        "max_tokens",
        "timeout_minutes",
        "sample_rows",
        "download_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_budget_values_must_be_positive(field, value):
    issues = _issues(budget=_budget(**{field: value}))

    assert (issues[0].code, issues[0].path) == (
        "non_positive_integer",
        f"budget.{field}",
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_searches",
        "max_crawls",
        "max_probes",
        "max_tokens",
        "timeout_minutes",
        "sample_rows",
        "download_timeout_seconds",
    ],
)
def test_boolean_budget_values_do_not_pass_integer_validation(field):
    issues = _issues(budget=_budget(**{field: True}))

    assert (issues[0].code, issues[0].path) == (
        "invalid_integer",
        f"budget.{field}",
    )


@pytest.mark.parametrize("field", ["timeout_seconds", "priority"])
def test_boolean_catalog_integers_are_rejected(field):
    issues = _issues(catalogs=[_catalog(**{field: True})])

    assert (issues[0].code, issues[0].path) == (
        "invalid_integer",
        f"catalogs[0].{field}",
    )


@pytest.mark.parametrize("field", ["timeout_seconds", "priority"])
@pytest.mark.parametrize("value", [1.5, "30"])
def test_non_integer_catalog_numeric_values_are_rejected(field, value):
    issues = _issues(catalogs=[_catalog(**{field: value})])

    assert (issues[0].code, issues[0].path) == (
        "invalid_integer",
        f"catalogs[0].{field}",
    )


@pytest.mark.parametrize("value", [1.5, "5"])
def test_non_integer_budget_numeric_values_are_rejected(value):
    issues = _issues(budget=_budget(max_searches=value))

    assert (issues[0].code, issues[0].path) == (
        "invalid_integer",
        "budget.max_searches",
    )


@pytest.mark.parametrize("field", ["timeout_seconds", "priority"])
@pytest.mark.parametrize("value", [0, -1])
def test_catalog_integer_fields_must_be_positive(field, value):
    issues = _issues(catalogs=[_catalog(**{field: value})])

    assert (issues[0].code, issues[0].path) == (
        "non_positive_integer",
        f"catalogs[0].{field}",
    )


@pytest.mark.parametrize("include_subdomains", [0, 1, None, "true"])
def test_host_rule_subdomain_flag_must_be_an_actual_boolean(include_subdomains):
    issues = _issues(
        trusted_hosts=[
            {
                "hostname": "trusted.example",
                "include_subdomains": include_subdomains,
            }
        ]
    )

    assert (issues[0].code, issues[0].path) == (
        "invalid_boolean",
        "trusted_hosts[0].include_subdomains",
    )


def test_non_mapping_catalog_and_host_rule_members_are_structured_failures():
    issues = _issues(catalogs=[42], trusted_hosts=[42], blocked_hosts=[42])

    assert [(issue.code, issue.path) for issue in issues] == [
        ("invalid_type", "catalogs[0]"),
        ("invalid_type", "trusted_hosts[0]"),
        ("invalid_type", "blocked_hosts[0]"),
    ]


def test_direct_complete_contract_collection_shape_failures_are_structured():
    with pytest.raises(ProfileContractError) as error:
        ProfileContract(
            profile_id="test_profile",
            status=ProfileStatus.ENABLED,
            reason=None,
            catalogs=None,
            budget=BudgetContract(**_budget()),
            supported_adapters={"ckan"},
            trusted_hosts=None,
            blocked_hosts=None,
        )

    assert [(issue.code, issue.path) for issue in error.value.issues] == [
        ("invalid_collection", "catalogs"),
        ("invalid_collection", "trusted_hosts"),
        ("invalid_collection", "blocked_hosts"),
    ]


def test_returned_contract_and_nested_values_are_immutable():
    contract = _build()

    with pytest.raises(FrozenInstanceError):
        contract.profile_id = "changed"
    with pytest.raises(FrozenInstanceError):
        contract.catalogs[0].name = "changed"
    with pytest.raises(FrozenInstanceError):
        contract.budget.max_searches = 99
    with pytest.raises(FrozenInstanceError):
        contract.trusted_hosts[0].hostname = "changed.example"
    assert isinstance(contract.catalogs, tuple)
    assert isinstance(contract.trusted_hosts, tuple)


def test_builder_copies_caller_mappings_and_sequences():
    catalog = _catalog()
    catalogs = [catalog]
    budget = _budget()
    trusted_rule = {"hostname": "trusted.example", "include_subdomains": True}
    trusted_hosts = [trusted_rule]

    contract = _build(catalogs=catalogs, budget=budget, trusted_hosts=trusted_hosts)
    catalog["name"] = "Mutated"
    catalogs.clear()
    budget["max_searches"] = 999
    trusted_rule["hostname"] = "mutated.example"
    trusted_hosts.clear()

    assert contract.catalogs[0].name == "Primary catalog"
    assert contract.budget.max_searches == 5
    assert contract.trusted_hosts[0].hostname == "trusted.example"


def test_multiple_violations_are_aggregated_in_stable_order_with_precise_paths():
    invalid_catalog = _catalog(
        catalog_id="Bad-ID",
        adapter="unsupported",
        name=" ",
        base_url="ftp://user:secret@example.test:22/path?token=value",
        api_key_env="2INVALID",
        timeout_seconds=0,
        priority=True,
        required="yes",
    )
    invalid_budget = _budget(max_searches=0, max_crawls=True)

    first = _issues(
        profile_id="Bad Profile",
        status="manual_only",
        reason="",
        catalogs=[invalid_catalog],
        budget=invalid_budget,
    )
    second = _issues(
        profile_id="Bad Profile",
        status="manual_only",
        reason="",
        catalogs=[invalid_catalog],
        budget=invalid_budget,
    )

    expected = [
        ("invalid_id", "profile_id"),
        ("reason_required", "reason"),
        ("invalid_id", "catalogs[0].catalog_id"),
        ("unsupported_adapter", "catalogs[0].adapter"),
        ("blank_value", "catalogs[0].name"),
        ("invalid_url_scheme", "catalogs[0].base_url"),
        ("embedded_credentials", "catalogs[0].base_url"),
        ("url_query_not_allowed", "catalogs[0].base_url"),
        ("disallowed_port", "catalogs[0].base_url"),
        ("invalid_environment_name", "catalogs[0].api_key_env"),
        ("non_positive_integer", "catalogs[0].timeout_seconds"),
        ("invalid_integer", "catalogs[0].priority"),
        ("invalid_boolean", "catalogs[0].required"),
        ("non_positive_integer", "budget.max_searches"),
        ("invalid_integer", "budget.max_crawls"),
    ]
    assert [(issue.code, issue.path) for issue in first] == expected
    assert first == second


def test_new_url_and_missing_field_issues_have_stable_aggregate_order():
    catalog = _catalog(base_url="https://catalog.example/%0aheader")
    del catalog["api_key_env"]

    first = _issues(catalogs=[catalog])
    second = _issues(catalogs=[catalog])

    assert [(issue.code, issue.path) for issue in first] == [
        ("missing_field", "catalogs[0].api_key_env"),
        ("url_control_character", "catalogs[0].base_url"),
    ]
    assert first == second


def test_ckan_route_issues_have_stable_aggregate_order():
    catalog = _catalog(base_url="https://catalog.example/%0aheader")
    del catalog["ckan_dialect"]
    del catalog["landing_base_url"]

    first = _issues(catalogs=[catalog])
    second = _issues(catalogs=[catalog])

    assert [(issue.code, issue.path) for issue in first] == [
        ("missing_field", "catalogs[0].ckan_dialect"),
        ("missing_field", "catalogs[0].landing_base_url"),
        ("url_control_character", "catalogs[0].base_url"),
    ]
    assert first == second


def test_public_nested_contract_constructors_enforce_their_local_invariants():
    with pytest.raises(ProfileContractError):
        CatalogContract(
            catalog_id="bad-id",
            adapter="ckan",
            name="Catalog",
            base_url="https://catalog.example",
            api_key_env=None,
            timeout_seconds=30,
            priority=1,
            required=True,
        )
    with pytest.raises(ProfileContractError):
        BudgetContract(0, 1, 1, 1, 1, 1, 1)
