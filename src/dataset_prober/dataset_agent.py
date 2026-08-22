"""
src/dataset_agent.py — Profile-driven agentic dataset discovery

Architecture:
    1. Local capability resolution determines executable profiles
    2. PromptInterpreter classifies user intent among resolved profiles
    3. User confirms the plan
    4. For each profile (sequential):
       a. Reuse the locally resolved profile capabilities
       b. Agent loop runs with Claude — tools and system prompt
          are injected from profile, no hardcoded values
       c. Token and cost tracked per call
    5. Results aggregated and saved

No hardcoded URLs, sources, or limits anywhere in this file.
Everything comes from profiles in dataset_prober/profiles/*.yaml.

Usage:
    python src/dataset_agent.py
    python src/dataset_agent.py --timeout 15 --max-searches 8
    python src/dataset_agent.py --profile dutch_government
    python src/dataset_agent.py --download
"""

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv(Path(__file__).parent.parent.parent / ".env")

import anthropic  # noqa: E402

# Local imports
from dataset_prober.config_loader import (  # noqa: E402
    BudgetConfig,
    ConfigLoader,
    Profile,
    get_anthropic_api_key,
)
from dataset_prober.loading_policy import (  # noqa: E402
    InspectedResourceError,
    LoaderKind,
    LoadingPolicySession,
    loader_for_resource,
    parse_exact_selection,
    sanitize_url_text,
)
from dataset_prober.orchestrator import AggregatedResult, Orchestrator, ProfileResult  # noqa: E402
from dataset_prober.paths import AppPaths  # noqa: E402
from dataset_prober.profile_contract import ProfileStatus  # noqa: E402
from dataset_prober.profile_resolution import (  # noqa: E402
    ProfileResolutionError,
    ResolvedProfile,
    resolve_profile,
)
from dataset_prober.prompt_interpreter import (  # noqa: E402
    InterpretationResult,
    ProfileInterpretationError,
    PromptInterpreter,
)
from dataset_prober.resource_classification import unknown_assessment  # noqa: E402
from dataset_prober.tools import TOOL_REGISTRY, DatasetResult  # noqa: E402
from dataset_prober.tools.base import RunDeadlineExceeded  # noqa: E402

console = Console()


def _downgrade_registration_failure(result: DatasetResult, error: InspectedResourceError) -> None:
    """Keep discovery identity while discarding inspection facts that failed registration."""
    diagnostic = sanitize_url_text(f"Inspection registration failed: {error}")
    result.status = "failed"
    result.row_count = None
    result.columns = None
    result.sample = None
    result.error = diagnostic
    result.assessment = unknown_assessment(explanation=diagnostic)


AGENT_MODEL = "claude-sonnet-4-6"


# ─── Session cost tracker ────────────────────────────────────────────────────


@dataclass
class SessionCost:
    """Tracks actual reported profile-agent usage and call outcomes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    model_calls_attempted: int = 0
    model_calls_completed: int = 0
    model_calls_timed_out: int = 0
    interpreter_cost_usd: float = 0.0

    def record_attempt(self) -> None:
        self.model_calls_attempted += 1

    def record_usage(self, usage) -> int:
        """Record one completed response and return its reported token total."""

        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_tokens += cache_creation_tokens
        self.cache_read_tokens += cache_read_tokens
        self.model_calls_completed += 1
        return input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens

    def record_timeout(self) -> None:
        """Record an attempted call that returned no usage report."""

        self.model_calls_timed_out += 1

    @property
    def total_calls(self) -> int:
        """Compatibility view of attempted profile-agent calls."""

        return self.model_calls_attempted

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def total_cost(self, pricing) -> float:
        return (
            (self.input_tokens / 1_000_000) * pricing.input_per_million
            + (self.output_tokens / 1_000_000) * pricing.output_per_million
            + (self.cache_read_tokens / 1_000_000) * pricing.cache_read_per_million
            + self.interpreter_cost_usd
        )

    def summary(self, pricing) -> str:
        cost = self.total_cost(pricing)
        return (
            f"📊 Session: {self.model_calls_attempted} attempted calls | "
            f"{self.total_tokens:,} actual reported tokens | "
            f"estimated cost: ${cost:.4f}"
        )


# ─── Budget tracker ──────────────────────────────────────────────────────────


@dataclass
class Budget:
    """Runtime budget — enforces limits from profile, overrideable by CLI."""

    max_searches: int
    max_results: int
    max_probes: int
    max_model_calls: int
    max_tokens: int
    max_total_tokens: int
    timeout_seconds: float
    sample_rows: int
    download_timeout_seconds: int

    searches_used: int = 0
    probes_used: int = 0
    model_calls_used: int = 0
    tokens_used: int = 0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _interval_started_at: float = field(init=False, repr=False)
    _deadline: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.reset_timer()

    @classmethod
    def from_profile(
        cls,
        budget_config: BudgetConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "Budget":
        return cls(
            max_searches=budget_config.max_searches,
            max_results=budget_config.max_results,
            max_probes=budget_config.max_probes,
            max_model_calls=budget_config.max_model_calls,
            max_tokens=budget_config.max_tokens,
            max_total_tokens=budget_config.max_total_tokens,
            timeout_seconds=budget_config.timeout_minutes * 60,
            sample_rows=budget_config.sample_rows,
            download_timeout_seconds=budget_config.download_timeout_seconds,
            clock=clock,
        )

    def time_remaining(self) -> float:
        return max(0.0, self._deadline - self.clock())

    def elapsed_minutes(self) -> float:
        return max(0.0, self.clock() - self._interval_started_at) / 60

    def timed_out(self) -> bool:
        return self.time_remaining() <= 0

    def can_search(self) -> bool:
        return self.searches_used < self.max_searches and not self.timed_out()

    def can_probe(self) -> bool:
        return self.probes_used < self.max_probes and not self.timed_out()

    def model_calls_exhausted(self) -> bool:
        return self.model_calls_used >= self.max_model_calls

    def total_tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.max_total_tokens

    def remaining_token_allowance(self) -> int:
        return max(0, self.max_total_tokens - self.tokens_used)

    def status_line(self) -> str:
        remaining = self.time_remaining()
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        return (
            f"⏱  {mins}m{secs}s remaining | "
            f"🔍 {self.searches_used}/{self.max_searches} searches | "
            f"📊 {self.probes_used}/{self.max_probes} probes | "
            f"🤖 {self.model_calls_used}/{self.max_model_calls} calls | "
            f"🔤 {self.tokens_used:,}/{self.max_total_tokens:,} reported-token stop threshold"
        )

    def reset_timer(self) -> None:
        self._interval_started_at = self.clock()
        self._deadline = self._interval_started_at + self.timeout_seconds


# ─── Dynamic tool definitions for Claude ────────────────────────────────────


def build_tool_definitions(resolved_profile: ResolvedProfile, budget: Budget) -> list[dict]:
    """
    Build Claude tool definitions from one authoritative resolved capability view.
    """
    catalog_types = list(resolved_profile.source_keys)
    has_cbs = "cbs" in catalog_types
    has_ckan = "ckan" in catalog_types

    tools = []

    # search_catalog — adapts description based on available catalogs
    catalog_desc = []
    if has_cbs:
        catalog_desc.append("CBS OData catalog (for Dutch statistics)")
    if has_ckan:
        catalog_desc.append("CKAN catalog (for government open data portals)")
    if not catalog_desc:
        catalog_desc.append("no source catalog enabled under the v0.1 URL-safety policy")
    available_catalogs = ", ".join(catalog_types) or "none under the current safety policy"
    identifier_guidance = "Dataset identifier from the selected active catalog"
    if has_cbs and not has_ckan:
        identifier_guidance = "CBS table identifier (for example, '37230ned')"
    elif has_ckan and not has_cbs:
        identifier_guidance = "CKAN package name or identifier"
    elif has_cbs and has_ckan:
        identifier_guidance = "CBS table identifier or CKAN package identifier"

    tools.append(
        {
            "name": "search_catalog",
            "description": (
                f"Search for datasets using available catalogs: {', '.join(catalog_desc)}. "
                f"The source parameter determines which catalog to use. "
                f"Use multiple searches with different keywords to maximize coverage."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Search keyword — try both native language and English",
                    },
                    "source": {
                        "type": "string",
                        "enum": catalog_types,
                        "description": f"Which catalog to search. Available: {available_catalogs}",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": budget.max_results,
                        "description": "Number of results to return within the configured cap",
                    },
                },
                "required": ["keyword", "source", "max_results"],
            },
        }
    )

    # fetch_dataset
    tools.append(
        {
            "name": "fetch_dataset",
            "description": (
                "Fetch full metadata and sample data for a specific identifier through "
                "the selected active catalog. "
                "Always fetch before downloading to verify quality and freshness."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": identifier_guidance,
                    },
                    "source": {
                        "type": "string",
                        "enum": catalog_types,
                        "description": "Which tool to use for fetching",
                    },
                },
                "required": ["dataset_id", "source"],
            },
        }
    )

    # check_freshness
    tools.append(
        {
            "name": "check_freshness",
            "description": (
                "Check if a dataset meets the freshness requirement from the user's prompt. "
                "Parse the last_updated date and compare against the max_days_old rule. "
                "Always check freshness before recommending or downloading a dataset."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": "Dataset identifier for reference",
                    },
                    "last_updated": {
                        "type": "string",
                        "description": "Last update date from metadata (ISO format preferred)",
                    },
                    "max_days_old": {
                        "type": "integer",
                        "description": "Maximum allowed age in days (from user's freshness rule)",
                    },
                },
                "required": ["dataset_id", "last_updated", "max_days_old"],
            },
        }
    )

    # download_dataset
    download_properties = {
        "dataset_id": {"type": "string", "description": "Dataset identifier"},
        "title": {
            "type": "string",
            "description": "Human-readable dataset title (used as DuckDB table name)",
        },
        "source": {
            "type": "string",
            "enum": catalog_types,
            "description": "Which active catalog tool should request loading",
        },
    }
    download_guidance: list[str] = []
    if has_cbs:
        download_properties["table_id"] = {
            "type": "string",
            "description": "CBS table identifier",
        }
        download_guidance.append("For CBS, provide the table_id.")
    if has_ckan:
        download_properties["download_url"] = {
            "type": "string",
            "description": "Inspected direct resource URL",
        }
        download_guidance.append("For CKAN resources, provide the inspected download_url.")

    tools.append(
        {
            "name": "download_dataset",
            "description": (
                "Request interactive approval to load one inspected dataset into DuckDB. "
                "This tool call does not grant permission: deterministic code asks the user "
                "to confirm this exact resource, and denial stops before the loader. "
                "Only request approval after freshness and quality checks. "
                + " ".join(download_guidance)
            ),
            "input_schema": {
                "type": "object",
                "properties": download_properties,
                "required": ["dataset_id", "title", "source"],
            },
        }
    )

    return tools


# ─── Tool executor ───────────────────────────────────────────────────────────


def execute_tool(
    tool_name: str,
    tool_input: dict,
    tool_map: dict,
    budget: Budget,
    profile: Profile,
    loading_session: LoadingPolicySession,
    found_datasets: list,
    session_cost: SessionCost,
    paths: AppPaths,
) -> dict:
    """
    Route a Claude tool call to the correct tool implementation.
    Updates budget and found_datasets in place.
    Returns JSON-serializable result dict.
    """
    if tool_name == "search_catalog":
        requested_results = tool_input.get("max_results")
        if (
            isinstance(requested_results, bool)
            or not isinstance(requested_results, int)
            or requested_results <= 0
        ):
            return {"error": "max_results must be a positive integer"}
        effective_results = min(requested_results, budget.max_results)
        source = tool_input["source"]
        keyword = tool_input["keyword"]

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available in current profile"}

        if not budget.can_search():
            return {"error": "Search budget exhausted or timed out"}

        console.print(
            f"  🔍 [cyan]Searching {sanitize_url_text(source)}:[/cyan] {sanitize_url_text(keyword)}"
        )
        budget.searches_used += 1

        try:
            results = list(
                tool.search(
                    keyword,
                    effective_results,
                    remaining_time=budget.time_remaining,
                )
            )[:effective_results]
        except RunDeadlineExceeded:
            deadline_error = sanitize_url_text(
                "Profile-agent run deadline exhausted before source operation completed"
            )
            console.print(f"    → ⏰ [yellow]{deadline_error}[/yellow]")
            return {"error": deadline_error}
        console.print(f"    → Found {len(results)} results")
        return {"results": [r.to_dict() for r in results]}

    elif tool_name == "fetch_dataset":
        source = tool_input["source"]
        dataset_id = tool_input["dataset_id"]

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available in current profile"}

        if not budget.can_probe():
            return {"error": "Probe budget exhausted or timed out"}

        console.print(
            f"  📊 [cyan]Fetching ({sanitize_url_text(source)}):[/cyan] "
            f"{sanitize_url_text(dataset_id)}"
        )
        budget.probes_used += 1

        try:
            result = tool.fetch(
                dataset_id,
                budget.sample_rows,
                remaining_time=budget.time_remaining,
            )
        except RunDeadlineExceeded:
            deadline_error = sanitize_url_text(
                "Profile-agent run deadline exhausted before source operation completed"
            )
            console.print(f"    → ⏰ [yellow]{deadline_error}[/yellow]")
            return {"error": deadline_error}

        found_datasets.append(result)
        if result.assessment.load_eligible:
            try:
                loading_session.register_dataset_result(result, tool.adapter_identity)
            except InspectedResourceError as exc:
                _downgrade_registration_failure(result, exc)

        if result.assessment.load_eligible:
            console.print(
                f"    → ✅ Verified: {sanitize_url_text(result.title)} | "
                f"{len(result.columns or [])} columns | modified: {result.modified}"
            )
        else:
            console.print(
                f"    → ⚠️  Report-only ({result.assessment.reason.value}): "
                f"{sanitize_url_text(result.error) if result.error else result.assessment.explanation}"
            )

        return result.to_dict()

    elif tool_name == "check_freshness":
        dataset_id = tool_input["dataset_id"]
        last_updated = tool_input["last_updated"]
        max_days_old = tool_input["max_days_old"]

        console.print(
            f"  📅 [cyan]Checking freshness:[/cyan] {sanitize_url_text(dataset_id)} "
            f"(last: {last_updated})"
        )

        # Create a temporary DatasetResult to use its freshness logic
        temp = DatasetResult(
            id=dataset_id,
            title=dataset_id,
            description="",
            source="",
            source_name="",
            url="",
            modified=last_updated,
            download_url=None,
            format=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
        )

        days = temp.freshness_days()
        passes = temp.passes_freshness(max_days_old)

        if days is None:
            console.print(f"    → ⚠️  Cannot parse date: {last_updated}")
            return {
                "dataset_id": dataset_id,
                "last_updated": last_updated,
                "days_old": None,
                "max_days_old": max_days_old,
                "passes": None,
                "reason": f"Cannot parse date format: {last_updated}",
            }

        icon = "✅" if passes else "⏭️ "
        status = "passes" if passes else "fails"
        console.print(f"    → {icon} {days} days old — {status} the {max_days_old}-day rule")

        return {
            "dataset_id": dataset_id,
            "last_updated": last_updated,
            "days_old": days,
            "max_days_old": max_days_old,
            "passes": passes,
            "reason": f"Updated {days} days ago — {status} the rule (max {max_days_old} days)",
        }

    elif tool_name == "download_dataset":
        if not loading_session.download_enabled:
            console.print("  ⛔ [yellow]Load offer blocked — --download was not set[/yellow]")
            return {"error": "Download not permitted — --download was not set"}

        source = tool_input["source"]
        dataset_id = tool_input["dataset_id"]
        table_id = tool_input.get("table_id")

        matches = [d for d in found_datasets if d.source == source and d.id == dataset_id]
        if not matches:
            return {"error": "Download denied — resource is not an inspected candidate"}
        if len(matches) > 1:
            return {"error": "Download denied — inspected resource identity is ambiguous"}
        dataset = matches[0]
        if dataset.status != "probed":
            return {"error": "Download denied — resource has not passed inspection"}
        if not dataset.assessment.load_eligible:
            return {
                "error": (
                    f"Download denied — resource is report-only: {dataset.assessment.reason.value}"
                )
            }
        if table_id and table_id != dataset.id:
            return {"error": "Download denied — table ID does not match inspected resource"}
        if (
            loader_for_resource(dataset.source, dataset.format, dataset.download_url or "")
            is LoaderKind.UNSUPPORTED
        ):
            return {
                "error": (
                    "Download denied — unsupported or unknown format: "
                    f"{dataset.format or 'unknown'}"
                )
            }

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available"}

        try:
            authorization = loading_session.request_authorization(
                source_key=source,
                adapter_identity=tool.adapter_identity,
                resource_id=dataset_id,
                destination=paths.duckdb_path,
                input_func=console.input,
            )
        except InspectedResourceError as exc:
            return {"error": f"Download denied — {sanitize_url_text(str(exc))}"}
        if authorization is None:
            console.print(f"  ⛔ [yellow]Not approved: {sanitize_url_text(dataset.title)}[/yellow]")
            return {"error": "Download denied — exact affirmative consent was not given"}

        console.print(f"  💾 [cyan]Downloading:[/cyan] {sanitize_url_text(dataset.title)}")
        result = tool.download(dataset, str(paths.duckdb_path), authorization)

        if result.status == "downloaded":
            rows = f"{result.row_count:,}" if result.row_count is not None else "unknown"
            console.print(f"    → ✅ {rows} rows saved to DuckDB")
        else:
            console.print(f"    → ❌ {sanitize_url_text(result.error or 'load failed')}")

        return result.to_dict()

    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ─── Agent loop ──────────────────────────────────────────────────────────────


def run_profile(
    user_prompt: str,
    resolved_profile: ResolvedProfile,
    budget: Budget,
    loading_session: LoadingPolicySession,
    session_cost: SessionCost,
    paths: AppPaths,
    initial_message: Optional[str] = None,
) -> ProfileResult:
    """
    Run the agent loop for a single already-resolved profile.
    Accepts optional initial_message from orchestrator (replaces full history).
    Returns ProfileResult with found/downloaded datasets and cost tracking.
    """
    profile = resolved_profile.profile
    profile.require_runnable()

    # Build every model- and executor-facing capability surface from the same
    # immutable resolved object before touching either profile-agent API boundary.
    tool_map = resolved_profile.execution_map
    tool_definitions = build_tool_definitions(resolved_profile, budget)
    system_context = resolved_profile.system_prompt_context
    expected_sources = resolved_profile.source_keys
    if tuple(tool_map.keys()) != expected_sources:
        raise RuntimeError("Resolved execution-map keys do not match source keys")
    source_enums = []
    for definition in tool_definitions:
        source = definition["input_schema"]["properties"].get("source")
        if source is not None:
            source_enums.append(tuple(source["enum"]))
    if not source_enums or any(enum != expected_sources for enum in source_enums):
        raise RuntimeError("Model source enums do not match resolved source keys")

    from dataset_prober.orchestrator import ProfileResult as PR

    profile_result = PR(
        profile_name=profile.name,
        display_name=profile.name,
        objective=None,  # set by orchestrator
    )
    client = anthropic.Anthropic(api_key=get_anthropic_api_key(), max_retries=0)

    # Build dynamic system prompt — no hardcoded values
    system_prompt = f"""You are an autonomous dataset discovery agent.

{system_context}

BEHAVIOUR RULES:
1. Parse the user's prompt carefully — extract topic, geography, freshness rules, and download intent.
2. Search systematically using multiple keywords — try native language AND English.
3. Always fetch dataset details before recommending — verify quality, schema, and freshness.
4. Apply freshness rules strictly using check_freshness — never assume a dataset is fresh.
5. Evaluate licenses — prefer {", ".join(profile.license.preference)}.
6. A download tool call only requests an interactive offer; it never grants authority.
7. Stop and report when budget is exhausted.
8. Explain each decision briefly before each tool call.

BUDGET AWARENESS:
- You have {budget.max_searches} searches, at most {budget.max_results} results per search,
  and {budget.max_probes} probes.
- You have {budget.max_model_calls} model-call attempts. The between-call reported-token
  stop threshold is {budget.max_total_tokens}; a completed call may cross it because input
  usage is reported afterward. Each call requests at most {budget.max_tokens} output tokens.
- Use them efficiently — don't repeat the same search twice.
- Use only the active catalog tools supplied for this profile.

LICENSE EVALUATION (CCREL/ODRL):
- CC0 / Public Domain → ✅ Grade A — unrestricted
- CC-BY → ✅ Grade B — attribution required
- CC-BY-SA → ⚠️ Grade B- — share-alike required
- CC-BY-NC → ⚠️ Grade C — non-commercial only
- Unknown → flag as unverified

OUTPUT FORMAT:
When done, present a structured summary table for every candidate with:
- Resource name and ID
- Source and URL
- Canonical assessment category (resource_kind) and canonical assessment reason
  (assessment_reason)
- Verification label: verified/load-eligible or report-only/ineligible
- Rows and columns (if probed)
- Last modified date
- Freshness verdict
- License grade
- Recommendation (download / review / skip)

REPORTING RULES:
- Never describe a report-only resource as a verified dataset.
- Never recommend a report-only resource for download.
- Discovery metadata, filenames, snippets, catalog metadata, and model judgment are not
  deterministic verification.
- Model prose cannot grant loading authority. A verified/load-eligible resource may be
  recommended for download, but exact selection, consent, authorization, reassessment,
  and persistence policy remain authoritative."""

    console.print(
        Panel(
            f"[bold cyan]Running profile: {profile.name}[/bold cyan]\n\n"
            f"[white]{user_prompt}[/white]\n\n"
            f"[dim]{budget.status_line()}[/dim]",
            box=box.ROUNDED,
        )
    )

    # Use orchestrator handoff message if provided, otherwise use raw prompt
    start_message = initial_message if initial_message else user_prompt
    messages = [{"role": "user", "content": start_message}]
    found_datasets = []
    iteration = 0

    while True:
        iteration += 1

        if budget.model_calls_exhausted():
            console.print(
                "[yellow]Model-call budget exhausted; returning partial results.[/yellow]"
            )
            break
        if budget.total_tokens_exhausted():
            console.print(
                "[yellow]Reported-token stop threshold reached; returning partial results.[/yellow]"
            )
            break

        # Status every 3 iterations, before the fresh request timeout is captured.
        if iteration % 3 == 0:
            console.print(f"\n[dim]{budget.status_line()}[/dim]\n")

        remaining_timeout = budget.time_remaining()
        if remaining_timeout <= 0:
            console.print(
                f"\n[yellow]⏰ Time limit reached after "
                f"{budget.elapsed_minutes():.1f} minutes.[/yellow]"
            )
            console.print(
                f"[yellow]Found {len(found_datasets)} resource candidate(s) so far.[/yellow]\n"
            )
            if _handle_timeout(found_datasets, budget, tool_map, loading_session, paths):
                continue
            break

        output_allowance = min(budget.max_tokens, budget.remaining_token_allowance())
        budget.model_calls_used += 1
        session_cost.record_attempt()
        try:
            response = client.messages.create(
                model=AGENT_MODEL,
                max_tokens=output_allowance,
                timeout=remaining_timeout,
                system=system_prompt,
                tools=tool_definitions,
                messages=messages,
            )
        except anthropic.APITimeoutError:
            session_cost.record_timeout()
            console.print("[yellow]Model request timed out; returning partial results.[/yellow]")
            break

        # Track actual usage reported by this completed response exactly once.
        previous_input = session_cost.input_tokens
        previous_output = session_cost.output_tokens
        previous_cache_read = session_cost.cache_read_tokens
        call_tokens = session_cost.record_usage(response.usage)
        budget.tokens_used += call_tokens

        call_cost = profile.pricing.calculate_cost(
            session_cost.input_tokens - previous_input,
            session_cost.output_tokens - previous_output,
            session_cost.cache_read_tokens - previous_cache_read,
        )
        console.print(
            f"[dim]  actual reported tokens: {call_tokens:,} | "
            f"estimated call cost: {profile.pricing.format_cost(call_cost)} | "
            "estimated session cost: "
            f"{profile.pricing.format_cost(session_cost.total_cost(profile.pricing))}[/dim]"
        )

        # Add response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason before executing any returned tool blocks.
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    console.print(
                        f"\n[bold green]Agent Report:[/bold green]\n{sanitize_url_text(block.text)}"
                    )
            break

        if response.stop_reason == "max_tokens":
            console.print("[yellow]Model output limit reached; returning partial results.[/yellow]")
            break

        if response.stop_reason != "tool_use":
            console.print(f"[red]Unexpected stop reason: {response.stop_reason}[/red]")
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                if hasattr(block, "text") and block.text:
                    console.print(f"\n[dim italic]{sanitize_url_text(block.text)}[/dim italic]")
                continue

            result = execute_tool(
                tool_name=block.name,
                tool_input=block.input,
                tool_map=tool_map,
                budget=budget,
                paths=paths,
                profile=profile,
                loading_session=loading_session,
                found_datasets=found_datasets,
                session_cost=session_cost,
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    profile_result.datasets_found = found_datasets
    profile_result.datasets_downloaded = [d for d in found_datasets if d.status == "downloaded"]
    profile_result.datasets_failed = [d for d in found_datasets if d.status == "failed"]
    profile_result.input_tokens = session_cost.input_tokens
    profile_result.output_tokens = session_cost.output_tokens
    profile_result.cache_creation_input_tokens = session_cost.cache_creation_tokens
    profile_result.cache_read_input_tokens = session_cost.cache_read_tokens
    profile_result.model_calls_attempted = session_cost.model_calls_attempted
    profile_result.model_calls_completed = session_cost.model_calls_completed
    profile_result.model_calls_timed_out = session_cost.model_calls_timed_out
    profile_result.token_stop_threshold = budget.max_total_tokens
    profile_result.cost_usd = session_cost.total_cost(profile.pricing)
    if session_cost.model_calls_attempted != (
        session_cost.model_calls_completed + session_cost.model_calls_timed_out
    ):
        raise AssertionError("Returned profile result has incomplete model-call accounting")
    return profile_result


def _handle_timeout(
    found_datasets: list,
    budget: Budget,
    tool_map: dict,
    loading_session: LoadingPolicySession,
    paths: AppPaths,
) -> bool:
    """Handle timeout and return whether the agent loop should continue."""
    console.print("\n[bold yellow]What would you like to do?[/bold yellow]")
    console.print("  1. Continue searching (resets timer)")
    console.print("  2. Download what was found so far")
    console.print("  3. Show results and exit")

    try:
        choice = console.input("\n[cyan]Your choice (1/2/3):[/cyan] ").strip()
    except (EOFError, KeyboardInterrupt):
        _print_results_table(found_datasets)
        return False

    if choice == "1":
        console.print("[green]Continuing...[/green]")
        budget.reset_timer()
        return True
    elif choice == "2" and found_datasets and loading_session.download_enabled:
        candidates = [
            dataset
            for dataset in found_datasets
            if dataset.status == "probed"
            and dataset.assessment.load_eligible
            and dataset.source in tool_map
            and loader_for_resource(dataset.source, dataset.format, dataset.download_url or "")
            is not LoaderKind.UNSUPPORTED
        ]
        for index, dataset in enumerate(candidates, 1):
            console.print(
                f"  {index}. {sanitize_url_text(dataset.title)} "
                f"[{dataset.source}:{sanitize_url_text(dataset.id)}]"
            )
        try:
            selection = console.input(
                "[cyan]Select exact resources (e.g. 1,3 or 'all' or 'none'):[/cyan] "
            )
            indices = parse_exact_selection(selection, len(candidates))
        except (EOFError, KeyboardInterrupt, ValueError):
            indices = []

        for index in indices:
            dataset = candidates[index]
            tool = tool_map[dataset.source]
            try:
                authorization = loading_session.request_authorization(
                    source_key=dataset.source,
                    adapter_identity=tool.adapter_identity,
                    resource_id=dataset.id,
                    destination=paths.duckdb_path,
                    input_func=console.input,
                )
            except InspectedResourceError:
                continue
            if authorization is not None:
                tool.download(dataset, str(paths.duckdb_path), authorization)
    else:
        _print_results_table(found_datasets)
    return False


def _print_results_table(datasets: list):
    """Print a summary table of discovered resource candidates."""
    if not datasets:
        console.print("[yellow]No resource candidates found.[/yellow]")
        return

    table = Table(title="Discovered Resources", box=box.ROUNDED)
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Source", max_width=10)
    table.add_column("Rows", justify="right")
    table.add_column("Modified")
    table.add_column("License")
    table.add_column("Status")
    table.add_column("Assessment")

    for d in datasets:
        status_color = {
            "downloaded": "green",
            "probed": "cyan",
            "found": "white",
            "failed": "red",
            "skipped": "yellow",
        }.get(d.status, "white")

        table.add_row(
            sanitize_url_text(d.title)[:40],
            d.source,
            str(d.row_count) if d.row_count else "-",
            (d.modified or "unknown")[:10],
            d.license_grade() if d.license else "?",
            f"[{status_color}]{d.status}[/{status_color}]",
            (
                "verified"
                if d.assessment.load_eligible
                else f"report-only: {d.assessment.reason.value}"
            ),
        )

    console.print(table)


# ─── Entry point ─────────────────────────────────────────────────────────────


def _positive_budget_cli_value(raw_value: str) -> int:
    """Parse one positive integer CLI budget override locally."""

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _print_token_planning_estimate(
    profile_names: list[str],
    resolved_profiles: dict[str, ResolvedProfile],
    effective_budget_configs: dict[str, BudgetConfig],
    interpretation: InterpretationResult | None,
) -> None:
    """Display budget-based planning thresholds without starting runtime timers."""

    interpreter_tokens = interpretation.total_tokens if interpretation is not None else 0
    threshold_subtotal = sum(
        effective_budget_configs[profile_name].max_total_tokens for profile_name in profile_names
    )
    console.print("\n[bold cyan]Token planning estimate[/bold cyan]")
    console.print(
        "[dim]This is a budget-based planning estimate, not predicted consumption or a "
        "guaranteed maximum.[/dim]"
    )
    if interpretation is None:
        console.print("  Interpreter: not used (explicit profile selection).")
    else:
        console.print(f"  Interpreter actual reported tokens already spent: {interpreter_tokens:,}")

    for profile_name in profile_names:
        profile = resolved_profiles[profile_name].profile
        budget = effective_budget_configs[profile_name]
        console.print(f"  {profile.name}:")
        console.print(
            f"    Between-call reported-token stop threshold: {budget.max_total_tokens:,}"
        )
        console.print(f"    Model-call attempts: {budget.max_model_calls:,}")
        console.print(f"    Per-call requested output cap: {budget.max_tokens:,} tokens")

    console.print(f"  Selected-profile threshold subtotal: {threshold_subtotal:,}")
    console.print(
        "  Nominal session planning figure: "
        f"{interpreter_tokens + threshold_subtotal:,} "
        f"({interpreter_tokens:,} interpreter actual + {threshold_subtotal:,} thresholds)"
    )
    console.print(
        "[yellow]A final completed call can cross a profile's stop threshold because "
        "input usage is reported only after the response.[/yellow]\n"
    )


def main():
    parser = argparse.ArgumentParser(description="Profile-driven agentic dataset discovery")
    parser.add_argument("--profile", help="Force a specific profile (skips auto-detection)")
    parser.add_argument(
        "--timeout",
        type=_positive_budget_cli_value,
        default=None,
        help="Timeout in minutes (overrides profile default)",
    )
    parser.add_argument(
        "--max-searches",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_searches",
        help="Maximum catalog searches (overrides profile default)",
    )
    parser.add_argument(
        "--max-results",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_results",
        help="Maximum results returned by one catalog search",
    )
    parser.add_argument(
        "--max-probes",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_probes",
        help="Maximum dataset probes (overrides profile default)",
    )
    parser.add_argument(
        "--max-model-calls",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_model_calls",
        help="Maximum profile-agent model-call attempts",
    )
    parser.add_argument(
        "--max-tokens",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_tokens",
        help="Per-call requested profile-agent output-token cap",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=_positive_budget_cli_value,
        default=None,
        dest="max_total_tokens",
        help="Between-call reported-token stop threshold for one profile run",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Offer exact per-resource loading choices after inspection",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        dest="list_profiles",
        help="List available profiles and exit",
    )
    args = parser.parse_args()

    # CLI overrides dict (None values are ignored by budget.override())
    cli_overrides = {
        "timeout_minutes": args.timeout,
        "max_searches": args.max_searches,
        "max_results": args.max_results,
        "max_probes": args.max_probes,
        "max_model_calls": args.max_model_calls,
        "max_tokens": args.max_tokens,
        "max_total_tokens": args.max_total_tokens,
    }

    # Load validated profile descriptors before constructing any model or tool boundary.
    loader = ConfigLoader()
    configured_profile_ids = loader.configured_profile_ids()

    if args.list_profiles:
        console.print("\n[bold]Configured profiles:[/bold]")
        for profile in loader.profile_descriptors():
            console.print(
                f"  [cyan]{profile.profile_id}[/cyan] "
                f"({profile.status.value}) — {profile.description}",
                soft_wrap=True,
            )
            if profile.status is not ProfileStatus.ENABLED:
                console.print(f"    {profile.reason}", soft_wrap=True)
        console.print()
        return

    selected_profile: Profile | None = None
    enabled_profiles: list[Profile] = []
    if args.profile:
        if args.profile not in configured_profile_ids:
            console.print(f"[red]Profile '{args.profile}' is not configured.[/red]")
            console.print(f"Configured: {', '.join(configured_profile_ids)}")
            return
        selected_profile = loader.load(args.profile)
        if selected_profile.status is ProfileStatus.DISABLED:
            console.print(
                f"[red]Profile '{args.profile}' is disabled:[/red] {selected_profile.reason}",
                soft_wrap=True,
            )
            return
        if selected_profile.status is ProfileStatus.MANUAL_ONLY:
            console.print(
                f"[yellow]Profile '{args.profile}' is manual_only:[/yellow] "
                f"{selected_profile.reason}",
                soft_wrap=True,
            )
    else:
        enabled_profiles = [
            profile
            for profile in loader.profile_descriptors()
            if profile.status is ProfileStatus.ENABLED
        ]
        if not enabled_profiles:
            console.print(
                "[yellow]Automatic profile selection is unavailable because no profile "
                "is enabled. Use --list-profiles to inspect configured statuses or "
                "explicitly select a manual-only profile.[/yellow]"
            )
            return

    # Get user prompt
    console.print("\n[bold cyan]Dataset Discovery Agent[/bold cyan]")
    console.print("[dim]Describe the datasets you are looking for.[/dim]\n")
    user_prompt = console.input("[cyan]What datasets are you looking for?[/cyan]\n> ").strip()

    if not user_prompt:
        console.print("[red]No prompt provided. Exiting.[/red]")
        return

    resolved_profiles: dict[str, ResolvedProfile] = {}
    candidate_profiles = [selected_profile] if selected_profile is not None else enabled_profiles
    for candidate in candidate_profiles:
        try:
            resolved_profiles[candidate.profile_id] = resolve_profile(
                candidate,
                registry=TOOL_REGISTRY,
            )
        except ProfileResolutionError as exc:
            diagnostic = sanitize_url_text(str(exc))
            if selected_profile is not None:
                console.print(
                    f"[red]Profile capability resolution failed:[/red] {diagnostic}",
                    soft_wrap=True,
                )
                return
            console.print(
                f"[yellow]Excluding profile '{candidate.profile_id}':[/yellow] {diagnostic}",
                soft_wrap=True,
            )

    if not resolved_profiles:
        console.print("[yellow]No executable profile capabilities are available.[/yellow]")
        return

    paths = AppPaths.resolve()

    loading_session = LoadingPolicySession(download_enabled=args.download)

    if loading_session.download_enabled:
        console.print(
            "[yellow]⚠️  Load offers enabled — each resource still requires exact "
            "affirmative consent[/yellow]\n"
        )

    interpretation: InterpretationResult | None = None

    # Determine profiles to run
    if args.profile:
        # The explicit descriptor was validated and admitted before prompting.
        profile_names = [selected_profile.profile_id]
        console.print(f"[dim]Using profile: {args.profile}[/dim]\n")
    else:
        # Auto-detect via prompt interpreter
        try:
            interpreter = PromptInterpreter(
                [resolved.profile for resolved in resolved_profiles.values()]
            )
            interpretation = interpreter.interpret(user_prompt)
        except ProfileInterpretationError as exc:
            console.print(
                f"[red]Profile interpretation failed:[/red] {sanitize_url_text(str(exc))}",
                soft_wrap=True,
            )
            return
        unresolved_selections = [
            profile_name
            for profile_name in interpretation.profile_names
            if profile_name not in resolved_profiles
        ]
        if unresolved_selections:
            console.print(
                "[red]Profile interpretation selected an unresolved profile.[/red]",
                soft_wrap=True,
            )
            return
        profile_names = interpretation.profile_names

    effective_budget_configs = {
        profile_name: resolved_profiles[profile_name].profile.budget.override(
            **{key: value for key, value in cli_overrides.items() if value is not None}
        )
        for profile_name in profile_names
    }
    _print_token_planning_estimate(
        profile_names,
        resolved_profiles,
        effective_budget_configs,
        interpretation,
    )

    if interpretation is not None:
        confirmed = interpreter.present_and_confirm(interpretation, None)
        if not confirmed:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    for profile_name in profile_names:
        for issue in resolved_profiles[profile_name].issues:
            console.print(
                f"[yellow]Profile '{profile_name}' capability warning:[/yellow] "
                f"{issue.code} ({issue.catalog_id or 'profile'})",
                soft_wrap=True,
            )

    # Build objectives from interpretation
    objectives = interpretation.to_objectives() if not args.profile else []

    # Initialize orchestrator
    orchestrator = Orchestrator(objectives)
    aggregated = AggregatedResult(
        interpreter_cost_usd=interpretation.cost_usd if interpretation is not None else 0.0,
        interpreter_input_tokens=(interpretation.input_tokens if interpretation is not None else 0),
        interpreter_output_tokens=(
            interpretation.output_tokens if interpretation is not None else 0
        ),
        interpreter_cache_creation_input_tokens=(
            interpretation.cache_creation_tokens if interpretation is not None else 0
        ),
        interpreter_cache_read_input_tokens=(
            interpretation.cache_read_tokens if interpretation is not None else 0
        ),
        interpreter_model_calls_attempted=1 if interpretation is not None else 0,
        interpreter_model_calls_completed=1 if interpretation is not None else 0,
    )

    # Run profiles sequentially with orchestrator handoffs
    previous_results = []

    for i, profile_name in enumerate(profile_names, 1):
        if len(profile_names) > 1:
            console.print(
                f"\n[bold]── Profile {i}/{len(profile_names)}: {profile_name} ──[/bold]\n"
            )

        resolved_profile = resolved_profiles[profile_name]
        profile = resolved_profile.profile

        # Show global warning
        if profile.cost_warning:
            console.print(profile.warning_message or "⚠️  This may be slow and costly.")
            confirm = console.input("[cyan]Continue? (Y/n): [/cyan]").strip().lower()
            if confirm not in ("", "y", "yes"):
                console.print(f"[yellow]Skipping {profile_name}.[/yellow]")
                continue

        # Get objective for this profile
        objective = orchestrator.objectives.get(profile_name)

        # Build initial message with handoff context from previous profiles
        initial_message = (
            orchestrator.build_initial_message(
                user_prompt=user_prompt, objective=objective, previous_results=previous_results
            )
            if objective
            else user_prompt
        )

        # Fresh cost tracker per profile
        profile_session_cost = SessionCost()
        profile_session_cost.interpreter_cost_usd = 0.0

        budget = Budget.from_profile(effective_budget_configs[profile_name])
        profile_result = run_profile(
            user_prompt=user_prompt,
            resolved_profile=resolved_profile,
            budget=budget,
            loading_session=loading_session,
            session_cost=profile_session_cost,
            paths=paths,
            initial_message=initial_message,
        )

        if objective:
            profile_result.objective = objective
            profile_result = orchestrator.evaluate_result(profile_result, objective)

        orchestrator.print_progress(profile_name, i, len(profile_names), profile_result)
        previous_results.append(profile_result)
        aggregated.profile_results.append(profile_result)

        # Stop early if all objectives met
        if orchestrator.all_objectives_met(previous_results):
            console.print("\n[green]✅ All objectives met — stopping early.[/green]")
            break

    # Final summary
    console.print(aggregated.cost_summary())
    aggregated.print_summary_table()

    # Save results
    all_datasets = aggregated.all_datasets
    if all_datasets:
        output_path = paths.agent_results_path
        paths.ensure_output_dir()
        with open(output_path, "w") as f:
            json.dump([r.to_dict() for r in all_datasets], f, indent=2, default=str)
        console.print(f"\n[green]Results saved to {output_path}[/green]")
    else:
        console.print("\n[yellow]No resource candidates found across all profiles.[/yellow]")


if __name__ == "__main__":
    main()
