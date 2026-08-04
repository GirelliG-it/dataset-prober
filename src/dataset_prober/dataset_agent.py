"""
src/dataset_agent.py — Profile-driven agentic dataset discovery

Architecture:
    1. PromptInterpreter classifies user intent → selects profiles
    2. User confirms the plan
    3. For each profile (sequential):
       a. ConfigLoader loads profile YAML
       b. tools_for_profile() instantiates the right tools
       c. Agent loop runs with Claude — tools and system prompt
          are injected from profile, no hardcoded values
       d. Token and cost tracked per call
    4. Results aggregated and saved

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
from dataset_prober.orchestrator import AggregatedResult, Orchestrator, ProfileResult  # noqa: E402
from dataset_prober.prompt_interpreter import PromptInterpreter  # noqa: E402
from dataset_prober.tools import DatasetResult, tools_for_profile  # noqa: E402

console = Console()

AGENT_MODEL = "claude-sonnet-4-6"


# ─── Session cost tracker ────────────────────────────────────────────────────


@dataclass
class SessionCost:
    """Tracks token usage and cost across the entire session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_calls: int = 0
    interpreter_cost_usd: float = 0.0

    def add(self, usage, pricing):
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.total_calls += 1

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
            f"📊 Session: {self.total_calls} API calls | "
            f"{self.input_tokens + self.output_tokens:,} tokens | "
            f"cost: ${cost:.4f}"
        )


# ─── Budget tracker ──────────────────────────────────────────────────────────


@dataclass
class Budget:
    """Runtime budget — enforces limits from profile, overrideable by CLI."""

    max_searches: int
    max_crawls: int
    max_probes: int
    max_tokens: int
    timeout_seconds: float

    searches_used: int = 0
    crawls_used: int = 0
    probes_used: int = 0
    tokens_used: int = 0
    start_time: float = field(default_factory=time.time)

    @classmethod
    def from_profile(cls, budget_config: BudgetConfig) -> "Budget":
        return cls(
            max_searches=budget_config.max_searches,
            max_crawls=budget_config.max_crawls,
            max_probes=budget_config.max_probes,
            max_tokens=budget_config.max_tokens,
            timeout_seconds=budget_config.timeout_minutes * 60,
        )

    def time_remaining(self) -> float:
        return self.timeout_seconds - (time.time() - self.start_time)

    def elapsed_minutes(self) -> float:
        return (time.time() - self.start_time) / 60

    def timed_out(self) -> bool:
        return self.time_remaining() <= 0

    def can_search(self) -> bool:
        return self.searches_used < self.max_searches and not self.timed_out()

    def can_crawl(self) -> bool:
        return self.crawls_used < self.max_crawls and not self.timed_out()

    def can_probe(self) -> bool:
        return self.probes_used < self.max_probes and not self.timed_out()

    def status_line(self) -> str:
        remaining = self.time_remaining()
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        return (
            f"⏱  {mins}m{secs}s remaining | "
            f"🔍 {self.searches_used}/{self.max_searches} searches | "
            f"🌐 {self.crawls_used}/{self.max_crawls} crawls | "
            f"📊 {self.probes_used}/{self.max_probes} probes | "
            f"🔤 {self.tokens_used:,}/{self.max_tokens:,} tokens"
        )

    def reset_timer(self):
        self.start_time = time.time()


# ─── Dynamic tool definitions for Claude ────────────────────────────────────


def build_tool_definitions(profile: Profile) -> list[dict]:
    """
    Build Claude tool definitions dynamically from profile.
    No hardcoded tool descriptions — profile context is injected at runtime.
    """
    catalog_types = [c.type for c in profile.catalogs]
    has_cbs = "cbs" in catalog_types
    has_ckan = "ckan" in catalog_types

    tools = []

    # search_catalog — adapts description based on available catalogs
    catalog_desc = []
    if has_cbs:
        catalog_desc.append("CBS OData catalog (for Dutch statistics)")
    if has_ckan:
        catalog_desc.append("CKAN catalog (for government open data portals)")
    catalog_desc.append("Tavily web search (fallback for any source)")

    tools.append(
        {
            "name": "search_catalog",
            "description": (
                f"Search for datasets using available catalogs: {', '.join(catalog_desc)}. "
                f"The source parameter determines which catalog to use. "
                f"Use multiple searches with different keywords to maximize coverage. "
                f"Always prefer catalog APIs over web search when available."
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
                        "enum": catalog_types + ["tavily"],
                        "description": f"Which catalog to search. Available: {', '.join(catalog_types + ['tavily'])}",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return",
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
                "Fetch full metadata and sample data for a specific dataset by its ID. "
                "For CBS: use the table ID (e.g. '37230ned'). "
                "For CKAN: use the package name or ID. "
                "For web URLs: use the full URL. "
                "Always fetch before downloading to verify quality and freshness."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": "Dataset identifier (CBS table ID, CKAN package name, or URL)",
                    },
                    "source": {
                        "type": "string",
                        "enum": catalog_types + ["tavily"],
                        "description": "Which tool to use for fetching",
                    },
                    "sample_rows": {
                        "type": "integer",
                        "description": "Number of sample rows to retrieve",
                    },
                },
                "required": ["dataset_id", "source", "sample_rows"],
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
    tools.append(
        {
            "name": "download_dataset",
            "description": (
                "Download a dataset into the local DuckDB database. "
                "ONLY call this if: (1) the user explicitly requested downloading, "
                "AND (2) the dataset has passed freshness and quality checks. "
                "For CBS datasets, provide the table_id. "
                "For CSV datasets, provide the download_url."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string", "description": "Dataset identifier"},
                    "title": {
                        "type": "string",
                        "description": "Human-readable dataset title (used as DuckDB table name)",
                    },
                    "source": {
                        "type": "string",
                        "enum": catalog_types + ["tavily"],
                        "description": "Which tool to use for downloading",
                    },
                    "download_url": {
                        "type": "string",
                        "description": "Direct file URL for CSV downloads (omit for CBS)",
                    },
                    "table_id": {
                        "type": "string",
                        "description": "CBS table ID for CBS downloads (omit for CSV)",
                    },
                },
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
    allow_download: bool,
    found_datasets: list,
    session_cost: SessionCost,
) -> dict:
    """
    Route a Claude tool call to the correct tool implementation.
    Updates budget and found_datasets in place.
    Returns JSON-serializable result dict.
    """
    db_path = str(Path(__file__).parent.parent.parent / "output" / "datasets.duckdb")

    if tool_name == "search_catalog":
        source = tool_input["source"]
        keyword = tool_input["keyword"]
        max_results = tool_input["max_results"]

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available in current profile"}

        if not budget.can_search():
            return {"error": "Search budget exhausted or timed out"}

        console.print(f"  🔍 [cyan]Searching {source}:[/cyan] {keyword}")
        budget.searches_used += 1

        results = tool.search(keyword, max_results)
        console.print(f"    → Found {len(results)} results")
        return {"results": [r.to_dict() for r in results]}

    elif tool_name == "fetch_dataset":
        source = tool_input["source"]
        dataset_id = tool_input["dataset_id"]
        sample_rows = tool_input["sample_rows"]

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available in current profile"}

        if not budget.can_probe():
            return {"error": "Probe budget exhausted or timed out"}

        console.print(f"  📊 [cyan]Fetching ({source}):[/cyan] {dataset_id}")
        budget.probes_used += 1

        result = tool.fetch(dataset_id, sample_rows)

        if result.status == "probed":
            console.print(
                f"    → ✅ {result.title} | {len(result.columns or [])} columns | modified: {result.modified}"
            )
            found_datasets.append(result)
        else:
            console.print(f"    → ❌ {result.error or 'fetch failed'}")

        return result.to_dict()

    elif tool_name == "check_freshness":
        dataset_id = tool_input["dataset_id"]
        last_updated = tool_input["last_updated"]
        max_days_old = tool_input["max_days_old"]

        console.print(f"  📅 [cyan]Checking freshness:[/cyan] {dataset_id} (last: {last_updated})")

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
        if not allow_download:
            console.print("  ⛔ [yellow]Download blocked — not requested by user[/yellow]")
            return {"error": "Download not permitted — user did not request download"}

        source = tool_input["source"]
        dataset_id = tool_input["dataset_id"]
        title = tool_input["title"]
        download_url = tool_input.get("download_url")
        table_id = tool_input.get("table_id")

        tool = tool_map.get(source)
        if not tool:
            return {"error": f"Tool '{source}' not available"}

        console.print(f"  💾 [cyan]Downloading:[/cyan] {title}")

        # Find the DatasetResult in found_datasets or create one
        dataset = next((d for d in found_datasets if d.id == dataset_id), None)
        if not dataset:
            dataset = DatasetResult(
                id=table_id or dataset_id,
                title=title,
                description="",
                source=source,
                source_name=tool.source_name,
                url=download_url or "",
                download_url=download_url,
                format="CSV",
                modified=None,
                frequency=None,
                license=None,
                license_url=None,
                row_count=None,
                columns=[],
                sample=None,
                language=None,
                tags=[],
            )

        # Override ID with CBS table_id if provided
        if table_id:
            dataset.id = table_id

        result = tool.download(dataset, db_path)

        if result.status == "downloaded":
            console.print(f"    → ✅ {result.row_count:,} rows saved to DuckDB")
        else:
            console.print(f"    → ❌ {result.error}")

        return result.to_dict()

    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ─── Agent loop ──────────────────────────────────────────────────────────────


def run_profile(
    user_prompt: str,
    profile: Profile,
    budget: Budget,
    allow_download: bool,
    session_cost: SessionCost,
    cli_overrides: dict,
    initial_message: Optional[str] = None,
) -> ProfileResult:
    """
    Run the agent loop for a single profile.
    Accepts optional initial_message from orchestrator (replaces full history).
    Returns ProfileResult with found/downloaded datasets and cost tracking.
    """
    from dataset_prober.orchestrator import ProfileResult as PR

    profile_result = PR(
        profile_name=profile.name,
        display_name=profile.name,
        objective=None,  # set by orchestrator
    )
    client = anthropic.Anthropic(api_key=get_anthropic_api_key())

    # Apply CLI overrides to budget
    budget_config = profile.budget.override(**cli_overrides)
    budget = Budget.from_profile(budget_config)

    # Instantiate tools
    tool_instances = tools_for_profile(profile)
    tool_map = {t.source_type: t for t in tool_instances}

    # Build dynamic tool definitions
    tool_definitions = build_tool_definitions(profile)

    # Build dynamic system prompt — no hardcoded values
    system_prompt = f"""You are an autonomous dataset discovery agent.

{profile.system_prompt_context()}

BEHAVIOUR RULES:
1. Parse the user's prompt carefully — extract topic, geography, freshness rules, and download intent.
2. Search systematically using multiple keywords — try native language AND English.
3. Always fetch dataset details before recommending — verify quality, schema, and freshness.
4. Apply freshness rules strictly using check_freshness — never assume a dataset is fresh.
5. Evaluate licenses — prefer {", ".join(profile.license.preference)}.
6. Only download if the user explicitly requested it.
7. Stop and report when budget is exhausted.
8. Explain each decision briefly before each tool call.

BUDGET AWARENESS:
- You have {budget.max_searches} searches, {budget.max_crawls} crawls, {budget.max_probes} probes.
- Use them efficiently — don't repeat the same search twice.
- Prioritize catalog APIs over web search when available.

LICENSE EVALUATION (CCREL/ODRL):
- CC0 / Public Domain → ✅ Grade A — unrestricted
- CC-BY → ✅ Grade B — attribution required
- CC-BY-SA → ⚠️ Grade B- — share-alike required
- CC-BY-NC → ⚠️ Grade C — non-commercial only
- Unknown → flag as unverified

OUTPUT FORMAT:
When done, present a structured summary table with:
- Dataset name and ID
- Source and URL
- Rows and columns (if probed)
- Last modified date
- Freshness verdict
- License grade
- Recommendation (download / review / skip)"""

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

        # Check timeout
        if budget.timed_out():
            console.print(
                f"\n[yellow]⏰ Time limit reached after "
                f"{budget.elapsed_minutes():.1f} minutes.[/yellow]"
            )
            console.print(f"[yellow]Found {len(found_datasets)} dataset(s) so far.[/yellow]\n")
            _handle_timeout(found_datasets, budget, tool_map, allow_download)
            break

        # Status every 3 iterations
        if iteration % 3 == 0:
            console.print(f"\n[dim]{budget.status_line()}[/dim]\n")

        # Call Claude
        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=profile.budget.max_tokens,
            system=system_prompt,
            tools=tool_definitions,
            messages=messages,
        )

        # Track tokens and cost
        session_cost.add(response.usage, profile.pricing)
        budget.tokens_used += response.usage.input_tokens + response.usage.output_tokens

        call_cost = profile.pricing.calculate_cost(
            response.usage.input_tokens,
            response.usage.output_tokens,
            getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )
        console.print(
            f"[dim]  tokens: {response.usage.input_tokens + response.usage.output_tokens:,} | "
            f"call cost: {profile.pricing.format_cost(call_cost)} | "
            f"session total: {profile.pricing.format_cost(session_cost.total_cost(profile.pricing))}[/dim]"
        )

        # Add response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    console.print(f"\n[bold green]Agent Report:[/bold green]\n{block.text}")
            break

        if response.stop_reason != "tool_use":
            console.print(f"[red]Unexpected stop reason: {response.stop_reason}[/red]")
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                if hasattr(block, "text") and block.text:
                    console.print(f"\n[dim italic]{block.text}[/dim italic]")
                continue

            result = execute_tool(
                tool_name=block.name,
                tool_input=block.input,
                tool_map=tool_map,
                budget=budget,
                profile=profile,
                allow_download=allow_download,
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
    profile_result.tokens_used = session_cost.input_tokens + session_cost.output_tokens
    profile_result.cost_usd = session_cost.total_cost(profile.pricing)
    profile_result.api_calls = session_cost.total_calls
    return profile_result


def _handle_timeout(found_datasets: list, budget: Budget, tool_map: dict, allow_download: bool):
    """Handle timeout — ask user what to do with partial results."""
    console.print("\n[bold yellow]What would you like to do?[/bold yellow]")
    console.print("  1. Continue searching (resets timer)")
    console.print("  2. Download what was found so far")
    console.print("  3. Show results and exit")

    choice = console.input("\n[cyan]Your choice (1/2/3):[/cyan] ").strip()

    if choice == "1":
        console.print("[green]Continuing...[/green]")
        budget.reset_timer()
    elif choice == "2" and found_datasets and allow_download:
        db_path = str(Path(__file__).parent.parent.parent / "output" / "datasets.duckdb")
        for dataset in found_datasets:
            tool = tool_map.get(dataset.source)
            if tool and dataset.status == "probed":
                tool.download(dataset, db_path)
    else:
        _print_results_table(found_datasets)


def _print_results_table(datasets: list):
    """Print a summary table of discovered datasets."""
    if not datasets:
        console.print("[yellow]No datasets found.[/yellow]")
        return

    table = Table(title="Discovered Datasets", box=box.ROUNDED)
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Source", max_width=10)
    table.add_column("Rows", justify="right")
    table.add_column("Modified")
    table.add_column("License")
    table.add_column("Status")

    for d in datasets:
        status_color = {
            "downloaded": "green",
            "probed": "cyan",
            "found": "white",
            "failed": "red",
            "skipped": "yellow",
        }.get(d.status, "white")

        table.add_row(
            d.title[:40],
            d.source,
            str(d.row_count) if d.row_count else "-",
            (d.modified or "unknown")[:10],
            d.license_grade() if d.license else "?",
            f"[{status_color}]{d.status}[/{status_color}]",
        )

    console.print(table)


# ─── Entry point ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Profile-driven agentic dataset discovery")
    parser.add_argument("--profile", help="Force a specific profile (skips auto-detection)")
    parser.add_argument(
        "--timeout", type=int, default=None, help="Timeout in minutes (overrides profile default)"
    )
    parser.add_argument(
        "--max-searches",
        type=int,
        default=None,
        dest="max_searches",
        help="Maximum catalog searches (overrides profile default)",
    )
    parser.add_argument(
        "--max-crawls",
        type=int,
        default=None,
        dest="max_crawls",
        help="Maximum page extractions (overrides profile default)",
    )
    parser.add_argument(
        "--max-probes",
        type=int,
        default=None,
        dest="max_probes",
        help="Maximum dataset probes (overrides profile default)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        dest="max_tokens",
        help="Maximum tokens per Claude call (overrides profile default)",
    )
    parser.add_argument(
        "--download", action="store_true", help="Allow agent to download datasets to DuckDB"
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
        "max_crawls": args.max_crawls,
        "max_probes": args.max_probes,
        "max_tokens": args.max_tokens,
    }

    # Load available profiles
    loader = ConfigLoader()
    available_profiles = loader.list_profiles()

    if args.list_profiles:
        console.print("\n[bold]Available profiles:[/bold]")
        for name in available_profiles:
            profile = loader.load(name)
            console.print(f"  [cyan]{name}[/cyan] — {profile.description}")
        console.print()
        return

    # Get user prompt
    console.print("\n[bold cyan]Dataset Discovery Agent[/bold cyan]")
    console.print("[dim]Describe the datasets you are looking for.[/dim]\n")
    user_prompt = console.input("[cyan]What datasets are you looking for?[/cyan]\n> ").strip()

    if not user_prompt:
        console.print("[red]No prompt provided. Exiting.[/red]")
        return

    # Detect download intent from prompt
    download_keywords = [
        "download",
        "save",
        "store",
        "load into duckdb",
        "downloaden",
        "opslaan",
        "bewaar",
    ]
    allow_download = args.download or any(kw in user_prompt.lower() for kw in download_keywords)

    if allow_download:
        console.print(
            "[yellow]⚠️  Download mode enabled — datasets will be saved to DuckDB[/yellow]\n"
        )

    # Session cost tracker
    session_cost = SessionCost()

    # Determine profiles to run
    if args.profile:
        # Manual profile override
        if args.profile not in available_profiles:
            console.print(f"[red]Profile '{args.profile}' not found.[/red]")
            console.print(f"Available: {', '.join(available_profiles)}")
            return
        profile_names = [args.profile]
        console.print(f"[dim]Using profile: {args.profile}[/dim]\n")
    else:
        # Auto-detect via prompt interpreter
        interpreter = PromptInterpreter(available_profiles)
        interpretation = interpreter.interpret(user_prompt)
        session_cost.interpreter_cost_usd = interpretation.cost_usd

        confirmed = interpreter.present_and_confirm(interpretation, None)
        if not confirmed:
            console.print("[yellow]Cancelled.[/yellow]")
            return

        profile_names = interpretation.profile_names

    # Build objectives from interpretation
    objectives = interpretation.to_objectives() if not args.profile else []

    # Initialize orchestrator
    orchestrator = Orchestrator(objectives)
    aggregated = AggregatedResult(
        interpreter_cost_usd=session_cost.interpreter_cost_usd,
        interpreter_tokens=session_cost.input_tokens + session_cost.output_tokens,
    )

    # Run profiles sequentially with orchestrator handoffs
    previous_results = []

    for i, profile_name in enumerate(profile_names, 1):
        if len(profile_names) > 1:
            console.print(
                f"\n[bold]── Profile {i}/{len(profile_names)}: {profile_name} ──[/bold]\n"
            )

        profile = loader.load(profile_name)

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

        budget = Budget.from_profile(profile.budget)
        profile_result = run_profile(
            user_prompt=user_prompt,
            profile=profile,
            budget=budget,
            allow_download=allow_download,
            session_cost=profile_session_cost,
            cli_overrides={k: v for k, v in cli_overrides.items() if v is not None},
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
        output_path = Path(__file__).parent.parent.parent / "output" / "agent_results.json"
        with open(output_path, "w") as f:
            json.dump([r.to_dict() for r in all_datasets], f, indent=2, default=str)
        console.print(f"\n[green]Results saved to {output_path}[/green]")
    else:
        console.print("\n[yellow]No datasets found across all profiles.[/yellow]")


if __name__ == "__main__":
    main()
