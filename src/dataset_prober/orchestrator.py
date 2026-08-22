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
    AggregatedResult (all datasets, actual reported usage and estimated cost)
"""

from dataclasses import dataclass, field
from typing import Optional

from rich import box
from rich.console import Console
from rich.table import Table

from dataset_prober.loading_policy import sanitize_url_text

console = Console()


@dataclass
class ProfileObjective:
    """
    What a specific profile is expected to find.
    Set by the interpreter upfront — not inferred during the run.
    """

    profile_name: str
    display_name: str
    what_to_find: str  # e.g. "One Dutch social security dataset"
    geographic_scope: str  # e.g. "Netherlands only"
    topic: str  # e.g. "social security"
    freshness_rule: str  # e.g. "updated within 12 months"
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
    datasets_found: list = field(default_factory=list)  # DatasetResult objects
    datasets_downloaded: list = field(default_factory=list)  # Successfully downloaded
    datasets_failed: list = field(default_factory=list)  # Found but couldn't download

    # Status
    objective_met: bool = False
    partial_success: bool = False
    failure_reason: Optional[str] = None

    # Actual usage reported by completed profile-agent responses
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model_calls_attempted: int = 0
    model_calls_completed: int = 0
    model_calls_timed_out: int = 0
    token_stop_threshold: int = 0
    cost_usd: float = 0.0

    @property
    def tokens_used(self) -> int:
        """Actual reported tokens from completed profile-agent responses."""

        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def api_calls(self) -> int:
        """Compatibility view of attempted profile-agent model calls."""

        return self.model_calls_attempted

    def handoff_summary(self) -> str:
        """
        Generate a concise handoff summary for the next profile.
        Replaces full conversation history — keeps context small.
        Target: under 300 tokens.
        """
        lines = ["HANDOFF FROM PREVIOUS PROFILE:"]
        lines.append(f"Profile: {sanitize_url_text(self.display_name)}")
        lines.append(f"Objective: {sanitize_url_text(self.objective.what_to_find)}")
        lines.append("")

        if self.datasets_downloaded:
            lines.append("✅ Successfully downloaded:")
            for d in self.datasets_downloaded:
                lines.append(
                    f"  - {sanitize_url_text(d.title)} ({sanitize_url_text(d.id)}) | "
                    f"{d.row_count or '?'} rows | "
                    f"modified: {d.modified or 'unknown'}"
                )
        elif self.datasets_found:
            lines.append("⚠️  Inspected resources not loaded:")
            for d in self.datasets_found[:3]:
                assessment = (
                    "verified but not loaded"
                    if d.assessment.load_eligible
                    else f"report-only: {d.assessment.reason.value}"
                )
                lines.append(
                    f"  - {sanitize_url_text(d.title)} ({sanitize_url_text(d.id)}) — {assessment}"
                )
        else:
            lines.append("❌ Nothing found for this objective.")
            if self.failure_reason:
                lines.append(f"   Reason: {sanitize_url_text(self.failure_reason)}")

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
    interpreter_input_tokens: int = 0
    interpreter_output_tokens: int = 0
    interpreter_cache_creation_input_tokens: int = 0
    interpreter_cache_read_input_tokens: int = 0
    interpreter_model_calls_attempted: int = 0
    interpreter_model_calls_completed: int = 0
    interpreter_model_calls_timed_out: int = 0

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
        return self.interpreter_cost_usd + sum(pr.cost_usd for pr in self.profile_results)

    @property
    def interpreter_tokens(self) -> int:
        return (
            self.interpreter_input_tokens
            + self.interpreter_output_tokens
            + self.interpreter_cache_creation_input_tokens
            + self.interpreter_cache_read_input_tokens
        )

    @property
    def total_input_tokens(self) -> int:
        return self.interpreter_input_tokens + sum(pr.input_tokens for pr in self.profile_results)

    @property
    def total_output_tokens(self) -> int:
        return self.interpreter_output_tokens + sum(pr.output_tokens for pr in self.profile_results)

    @property
    def total_cache_creation_input_tokens(self) -> int:
        return self.interpreter_cache_creation_input_tokens + sum(
            pr.cache_creation_input_tokens for pr in self.profile_results
        )

    @property
    def total_cache_read_input_tokens(self) -> int:
        return self.interpreter_cache_read_input_tokens + sum(
            pr.cache_read_input_tokens for pr in self.profile_results
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.total_input_tokens
            + self.total_output_tokens
            + self.total_cache_creation_input_tokens
            + self.total_cache_read_input_tokens
        )

    @property
    def total_model_calls_attempted(self) -> int:
        return self.interpreter_model_calls_attempted + sum(
            pr.model_calls_attempted for pr in self.profile_results
        )

    @property
    def total_model_calls_completed(self) -> int:
        return self.interpreter_model_calls_completed + sum(
            pr.model_calls_completed for pr in self.profile_results
        )

    @property
    def total_model_calls_timed_out(self) -> int:
        return self.interpreter_model_calls_timed_out + sum(
            pr.model_calls_timed_out for pr in self.profile_results
        )

    @property
    def total_api_calls(self) -> int:
        return self.total_model_calls_attempted

    def cost_summary(self) -> str:
        lines = [
            "\nActual reported model usage",
            "  Token usage below is reported by completed responses; "
            "call outcomes are observed locally.",
        ]
        if self.interpreter_model_calls_attempted:
            lines.extend(
                [
                    "  Interpreter:",
                    f"    Input tokens: {self.interpreter_input_tokens:,} | "
                    f"Output tokens: {self.interpreter_output_tokens:,}",
                    "    Cache-creation input tokens: "
                    f"{self.interpreter_cache_creation_input_tokens:,} | "
                    "Cache-read input tokens: "
                    f"{self.interpreter_cache_read_input_tokens:,}",
                    f"    Total reported tokens: {self.interpreter_tokens:,}",
                    "    Calls — attempted: "
                    f"{self.interpreter_model_calls_attempted} | completed: "
                    f"{self.interpreter_model_calls_completed} | timed out: "
                    f"{self.interpreter_model_calls_timed_out}",
                    f"    Estimated cost: ${self.interpreter_cost_usd:.4f}",
                ]
            )
        else:
            lines.append("  Interpreter: not used (explicit profile selection).")

        for pr in self.profile_results:
            percentage = (
                (pr.tokens_used / pr.token_stop_threshold) * 100 if pr.token_stop_threshold else 0.0
            )
            lines.extend(
                [
                    f"  {pr.display_name}:",
                    f"    Input tokens: {pr.input_tokens:,} | Output tokens: {pr.output_tokens:,}",
                    f"    Cache-creation input tokens: {pr.cache_creation_input_tokens:,} | "
                    f"Cache-read input tokens: {pr.cache_read_input_tokens:,}",
                    f"    Total reported tokens: {pr.tokens_used:,}",
                    f"    Calls — attempted: {pr.model_calls_attempted} | "
                    f"completed: {pr.model_calls_completed} | "
                    f"timed out: {pr.model_calls_timed_out}",
                    "    Between-call reported-token stop threshold: "
                    f"{pr.token_stop_threshold:,} | used: {percentage:.1f}%",
                    f"    Estimated cost: ${pr.cost_usd:.4f}",
                ]
            )
        lines.extend(
            [
                "  ─────────────────────────────────────────",
                "  Session totals:",
                f"    Input tokens: {self.total_input_tokens:,} | "
                f"Output tokens: {self.total_output_tokens:,}",
                "    Cache-creation input tokens: "
                f"{self.total_cache_creation_input_tokens:,} | "
                f"Cache-read input tokens: {self.total_cache_read_input_tokens:,}",
                f"    Total reported tokens: {self.total_tokens:,}",
                f"    Calls — attempted: {self.total_model_calls_attempted} | "
                f"completed: {self.total_model_calls_completed} | "
                f"timed out: {self.total_model_calls_timed_out}",
                f"    Total estimated cost: ${self.total_cost_usd:.4f}",
            ]
        )
        if self.total_model_calls_timed_out:
            lines.append(
                "  Timed-out attempts returned no usage; their server-side token usage, "
                "if any, is unknown."
            )
        if self.total_cache_creation_input_tokens:
            lines.append(
                "  Cache-creation cost is not represented because the pricing contract "
                "has no cache-write rate."
            )
        return "\n".join(lines)

    def print_summary_table(self):
        """Print a final summary table of all discovered resources."""
        all_ds = self.all_datasets
        if not all_ds:
            console.print("[yellow]No resources found across all profiles.[/yellow]")
            return

        table = Table(title="Session Results", box=box.ROUNDED)
        table.add_column("Profile", style="dim")
        table.add_column("Resource", style="cyan", max_width=35)
        table.add_column("ID", max_width=15)
        table.add_column("Rows", justify="right")
        table.add_column("Modified")
        table.add_column("License")
        table.add_column("Status")
        table.add_column("Assessment")

        for d in all_ds:
            status_color = {
                "downloaded": "green",
                "probed": "cyan",
                "found": "white",
                "failed": "red",
                "skipped": "yellow",
            }.get(d.status, "white")

            table.add_row(
                sanitize_url_text(d.source_name)[:15],
                sanitize_url_text(d.title)[:35],
                sanitize_url_text(d.id)[:15],
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
        self, user_prompt: str, objective: ProfileObjective, previous_results: list[ProfileResult]
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
        self, profile_result: ProfileResult, objective: ProfileObjective
    ) -> ProfileResult:
        """
        Evaluate whether a profile met its objective.
        Updates objective_met and partial_success flags.
        """
        downloaded = profile_result.datasets_downloaded
        found = profile_result.datasets_found
        eligible = [dataset for dataset in found if dataset.assessment.load_eligible]

        if downloaded:
            profile_result.objective_met = True
        elif eligible:
            profile_result.partial_success = True
            profile_result.failure_reason = (
                f"Verified {len(eligible)} load-eligible resource(s), but none were loaded."
            )
        elif found:
            profile_result.partial_success = True
            reasons = sorted({dataset.assessment.reason.value for dataset in found})
            profile_result.failure_reason = (
                f"Inspected {len(found)} resource candidate(s), but none were verified "
                f"load-eligible ({', '.join(reasons)})."
            )
        else:
            profile_result.failure_reason = (
                f"No resource candidates found matching: {objective.what_to_find}"
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
        self, profile_name: str, current: int, total: int, result: Optional[ProfileResult] = None
    ):
        """Print progress between profile runs."""
        if result:
            if result.objective_met:
                icon = "✅"
                status = f"found {len(result.datasets_downloaded)} dataset(s)"
            elif result.partial_success:
                icon = "⚠️ "
                status = result.failure_reason or "resource candidates remain report-only"
            else:
                icon = "❌"
                status = result.failure_reason or "nothing found"

            console.print(
                f"\n{icon} Profile {current}/{total} ({sanitize_url_text(profile_name)}): "
                f"{sanitize_url_text(status)}"
            )
            console.print(
                f"   Estimated cost: ${result.cost_usd:.4f} | "
                f"Actual reported tokens: {result.tokens_used:,} | "
                f"Attempted calls: {result.api_calls}"
            )
