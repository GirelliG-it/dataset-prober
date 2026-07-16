"""
src/orchestrator.py

Manages the execution of multiple profiles sequentially.
Prevents redundancy by passing concise handoff summaries between profiles
instead of full conversation history.

Flow:
    InterpretationResult (with per-profile objectives)
          ↓
    Orchestrator.run()
          ↓
    Profile 1 → ProfileResult (what was sought, what was found)
          ↓
    Orchestrator generates handoff summary (200 tokens, not 300k)
          ↓
    Profile 2 starts fresh with handoff context only
          ↓
    ...repeat for each profile...
          ↓
    AggregatedResult (all datasets, full cost breakdown)
"""

from dataclasses import dataclass, field
from typing import Optional

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class ProfileObjective:
    """
    What a specific profile is expected to find.
    Set by the interpreter upfront — not inferred during the run.
    """
    profile_name: str
    display_name: str
    what_to_find: str        # e.g. "One Dutch social security dataset"
    geographic_scope: str    # e.g. "Netherlands only"
    topic: str               # e.g. "social security"
    freshness_rule: str      # e.g. "updated within 12 months"
    download_requested: bool
    execution_order: int


@dataclass
class ProfileResult:
    """
    What a profile actually found and did.
    Generated after a profile completes its run.
    """
    profile_name: str
    display_name: str
    objective: ProfileObjective

    # Outcomes
    datasets_found: list = field(default_factory=list)      # DatasetResult objects
    datasets_downloaded: list = field(default_factory=list)  # Successfully downloaded
    datasets_failed: list = field(default_factory=list)      # Found but couldn't download

    # Status
    objective_met: bool = False
    partial_success: bool = False
    failure_reason: Optional[str] = None

    # Cost
    tokens_used: int = 0
    cost_usd: float = 0.0
    api_calls: int = 0

    def handoff_summary(self) -> str:
        """
        Generate a concise handoff summary for the next profile.
        Replaces full conversation history — keeps context small.
        Target: under 300 tokens.
        """
        lines = ["HANDOFF FROM PREVIOUS PROFILE:"]
        lines.append(f"Profile: {self.display_name}")
        lines.append(f"Objective: {self.objective.what_to_find}")
        lines.append("")

        if self.datasets_downloaded:
            lines.append("✅ Successfully downloaded:")
            for d in self.datasets_downloaded:
                lines.append(
                    f"  - {d.title} ({d.id}) | "
                    f"{d.row_count or '?'} rows | "
                    f"modified: {d.modified or 'unknown'}"
                )
        elif self.datasets_found:
            lines.append("⚠️  Found but could not download:")
            for d in self.datasets_found[:3]:
                lines.append(f"  - {d.title} ({d.id}) — {d.error or d.status}")
        else:
            lines.append("❌ Nothing found for this objective.")
            if self.failure_reason:
                lines.append(f"   Reason: {self.failure_reason}")

        lines.append("")
        lines.append(
            "YOUR TASK: Focus only on your assigned scope. "
            "Do not re-search what was already handled above."
        )

        return "\n".join(lines)


@dataclass
class AggregatedResult:
    """
    Combined results from all profiles in a session.
    """
    profile_results: list[ProfileResult] = field(default_factory=list)
    interpreter_cost_usd: float = 0.0
    interpreter_tokens: int = 0

    @property
    def all_datasets(self) -> list:
        """All DatasetResult objects across all profiles."""
        datasets = []
        for pr in self.profile_results:
            datasets.extend(pr.datasets_found)
        return datasets

    @property
    def downloaded_datasets(self) -> list:
        """Only successfully downloaded datasets."""
        datasets = []
        for pr in self.profile_results:
            datasets.extend(pr.datasets_downloaded)
        return datasets

    @property
    def total_cost_usd(self) -> float:
        return self.interpreter_cost_usd + sum(
            pr.cost_usd for pr in self.profile_results
        )

    @property
    def total_tokens(self) -> int:
        return self.interpreter_tokens + sum(
            pr.tokens_used for pr in self.profile_results
        )

    @property
    def total_api_calls(self) -> int:
        return sum(pr.api_calls for pr in self.profile_results)

    def cost_summary(self) -> str:
        lines = ["\n📊 Session Cost Breakdown:"]
        lines.append(
            f"  Interpreter: {self.interpreter_tokens:,} tokens | "
            f"${self.interpreter_cost_usd:.4f}"
        )
        for pr in self.profile_results:
            lines.append(
                f"  {pr.display_name}: {pr.tokens_used:,} tokens | "
                f"${pr.cost_usd:.4f} | {pr.api_calls} calls"
            )
        lines.append(
            "  ─────────────────────────────────────────"
        )
        lines.append(
            f"  Total: {self.total_tokens:,} tokens | "
            f"${self.total_cost_usd:.4f} | {self.total_api_calls} API calls"
        )
        return "\n".join(lines)

    def print_summary_table(self):
        """Print a final summary table of all discovered datasets."""
        all_ds = self.all_datasets
        if not all_ds:
            console.print("[yellow]No datasets found across all profiles.[/yellow]")
            return

        table = Table(title="Session Results", box=box.ROUNDED)
        table.add_column("Profile", style="dim")
        table.add_column("Dataset", style="cyan", max_width=35)
        table.add_column("ID", max_width=15)
        table.add_column("Rows", justify="right")
        table.add_column("Modified")
        table.add_column("License")
        table.add_column("Status")

        for d in all_ds:
            status_color = {
                "downloaded": "green",
                "probed": "cyan",
                "found": "white",
                "failed": "red",
                "skipped": "yellow"
            }.get(d.status, "white")

            table.add_row(
                d.source_name[:15],
                d.title[:35],
                d.id[:15],
                str(d.row_count) if d.row_count else "-",
                (d.modified or "unknown")[:10],
                d.license_grade() if d.license else "?",
                f"[{status_color}]{d.status}[/{status_color}]"
            )

        console.print(table)


class Orchestrator:
    """
    Manages sequential profile execution with concise handoff summaries.

    Key responsibilities:
    1. Run profiles in order
    2. Evaluate what each profile found vs what it was supposed to find
    3. Generate compact handoff summaries (not full conversation history)
    4. Pass handoff context to the next profile's initial message
    5. Track costs across all profiles
    6. Stop early if all objectives are met
    """

    def __init__(self, objectives: list[ProfileObjective]):
        """
        Args:
            objectives: Per-profile objectives set by the interpreter.
                        Each profile knows exactly what it needs to find.
        """
        self.objectives = {obj.profile_name: obj for obj in objectives}

    def build_initial_message(
        self,
        user_prompt: str,
        objective: ProfileObjective,
        previous_results: list[ProfileResult]
    ) -> str:
        """
        Build the initial user message for a profile run.
        For the first profile: just the user prompt + objective.
        For subsequent profiles: prompt + objective + compact handoff summary.

        This replaces passing full conversation history between profiles.
        """
        lines = [f"User request: {user_prompt}", ""]
        lines.append("YOUR OBJECTIVE FOR THIS RUN:")
        lines.append(f"  {objective.what_to_find}")
        lines.append(f"  Geographic scope: {objective.geographic_scope}")
        lines.append(f"  Topic: {objective.topic}")
        lines.append(f"  Freshness rule: {objective.freshness_rule}")
        lines.append(
            f"  Download: {'Yes — download if found' if objective.download_requested else 'No — report only'}"
        )

        if previous_results:
            lines.append("")
            lines.append("=" * 50)
            for prev in previous_results:
                lines.append(prev.handoff_summary())
            lines.append("=" * 50)

        return "\n".join(lines)

    def evaluate_result(
        self,
        profile_result: ProfileResult,
        objective: ProfileObjective
    ) -> ProfileResult:
        """
        Evaluate whether a profile met its objective.
        Updates objective_met and partial_success flags.
        """
        downloaded = profile_result.datasets_downloaded
        found = profile_result.datasets_found

        if downloaded:
            profile_result.objective_met = True
        elif found:
            profile_result.partial_success = True
            profile_result.failure_reason = (
                f"Found {len(found)} dataset(s) but could not download. "
                f"Manual download may be required."
            )
        else:
            profile_result.failure_reason = (
                f"No datasets found matching: {objective.what_to_find}"
            )

        return profile_result

    def all_objectives_met(self, results: list[ProfileResult]) -> bool:
        """
        Check if all registered objectives have been met.
        Requires: results is non-empty AND covers all registered objectives
        AND every result has objective_met=True.
        """
        if not results:
            return False
        if len(results) < len(self.objectives):
            return False
        return all(r.objective_met for r in results)

    def print_progress(
        self,
        profile_name: str,
        current: int,
        total: int,
        result: Optional[ProfileResult] = None
    ):
        """Print progress between profile runs."""
        if result:
            if result.objective_met:
                icon = "✅"
                status = f"found {len(result.datasets_downloaded)} dataset(s)"
            elif result.partial_success:
                icon = "⚠️ "
                status = f"found but couldn't download — {result.failure_reason}"
            else:
                icon = "❌"
                status = result.failure_reason or "nothing found"

            console.print(
                f"\n{icon} Profile {current}/{total} ({profile_name}): {status}"
            )
            console.print(
                f"   Cost: ${result.cost_usd:.4f} | "
                f"Tokens: {result.tokens_used:,} | "
                f"Calls: {result.api_calls}"
            )
