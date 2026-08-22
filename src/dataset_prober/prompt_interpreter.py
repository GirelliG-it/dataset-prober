"""
src/prompt_interpreter.py

Uses Claude API to interpret user prompts and select the appropriate
data source profile(s). This is the "pre-flight" step that runs before
the agent loop — it classifies intent, selects profiles, and presents
the plan to the user for confirmation before spending any budget.

Flow:
    User prompt
        ↓
    PromptInterpreter.interpret()  ← Claude API call
        ↓
    InterpretationResult
        ↓
    User confirms / corrects
        ↓
    Agent runs with selected profiles
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dataset_prober.config_loader import Profile, get_anthropic_api_key
from dataset_prober.profile_contract import ProfileStatus

load_dotenv(Path(__file__).parent.parent.parent / ".env")

console = Console()

# Cost tracking for the interpreter call itself
INTERPRETER_MODEL = "claude-sonnet-4-6"


@dataclass
class ProfileSelection:
    """A single profile selected for a run."""

    profile_name: str  # e.g. "dutch_government"
    display_name: str  # e.g. "Dutch Government"
    confidence: str  # "high", "medium", "low"
    reason: str  # Why this profile was selected
    execution_order: int  # 1 = first, 2 = second, etc.
    keywords_detected: list  # Keywords from prompt that triggered this
    language_detected: str  # Language of user prompt
    # Per-profile objective — what this profile specifically needs to find
    what_to_find: str = ""
    geographic_scope: str = ""
    topic: str = ""
    freshness_rule: str = ""
    download_requested: bool = False


@dataclass
class InterpretationResult:
    """
    Complete result of prompt interpretation.
    Contains all selected profiles and cost tracking.
    """

    profiles: list[ProfileSelection]
    is_global: bool
    is_multi_profile: bool
    raw_prompt: str
    interpreter_reasoning: str

    # Cost tracking for this interpretation call
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Actual reported usage from the completed interpreter response."""

        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    @property
    def profile_names(self) -> list[str]:
        return [p.profile_name for p in self.profiles]

    @property
    def primary_language(self) -> str:
        if self.profiles:
            return self.profiles[0].language_detected
        return "en"

    def to_objectives(self) -> list:
        """Convert profile selections to ProfileObjective objects for the orchestrator."""
        from dataset_prober.orchestrator import ProfileObjective

        return [
            ProfileObjective(
                profile_name=p.profile_name,
                display_name=p.display_name,
                what_to_find=p.what_to_find or f"Datasets from {p.display_name}",
                geographic_scope=p.geographic_scope or p.display_name,
                topic=p.topic or "general",
                freshness_rule=p.freshness_rule or "no specific rule",
                download_requested=p.download_requested,
                execution_order=p.execution_order,
            )
            for p in self.profiles
        ]


class ProfileInterpretationError(ValueError):
    """The model response did not select valid profiles from the enabled set."""


def _required_value(values: Mapping[str, object], field: str, path: str) -> object:
    if field not in values:
        raise ProfileInterpretationError(f"{path}.{field} is required")
    return values[field]


def _required_string(values: Mapping[str, object], field: str, path: str) -> str:
    value = _required_value(values, field, path)
    if not isinstance(value, str):
        raise ProfileInterpretationError(f"{path}.{field} must be a string")
    return value


def _profile_lines(profiles: Sequence[Profile]) -> list[str]:
    lines: list[str] = []
    for profile in profiles:
        regions = ", ".join(str(region) for region in profile.scope_regions) or "unspecified"
        lines.append(
            f"- {profile.profile_id}: {profile.name}; {profile.description}; "
            f"selection guidance regions: {regions}"
        )
    return lines


def _system_prompt(profiles: Sequence[Profile]) -> str:
    rendered_profiles = "\n".join(_profile_lines(profiles))
    return f"""You select data-source profiles for an open-data discovery agent.

Select only from these enabled profiles supplied by trusted application code:
{rendered_profiles}

Profile descriptions and regions are selection guidance, not deterministic semantic verification.
Never invent, rename, or substitute a profile. Select each profile at most once and
preserve the requested execution order.

Respond only with one valid JSON object, without markdown fences. It must contain a
non-empty `profiles` array. Each item must contain `profile_name`, `confidence`, `reason`,
`execution_order`, `keywords_detected`, `language_detected`, and an `objective` object with
`what_to_find`, `geographic_scope`, `topic`, `freshness_rule`, and `download_requested`.
The response may also contain `interpreter_reasoning`."""


class PromptInterpreter:
    """
    Interprets user prompts to select appropriate data source profiles.
    Makes a single Claude API call per interpretation.
    """

    def __init__(
        self,
        available_profiles: Sequence[Profile],
        client: anthropic.Anthropic | None = None,
    ):
        """
        Args:
            available_profiles: Validated enabled profile descriptors
            client: Optional Anthropic client supplied by the caller
        """
        profiles = tuple(available_profiles)
        if not profiles:
            raise ProfileInterpretationError("At least one enabled profile is required")
        for profile in profiles:
            if profile.status is not ProfileStatus.ENABLED:
                raise ProfileInterpretationError(
                    f"Profile '{profile.profile_id}' is not enabled for automatic selection"
                )
        profile_ids = [profile.profile_id for profile in profiles]
        if len(set(profile_ids)) != len(profile_ids):
            raise ProfileInterpretationError("Enabled profile descriptors must have unique IDs")

        self.available_profiles = profiles
        self._profiles_by_id = {profile.profile_id: profile for profile in profiles}
        self.system_prompt = _system_prompt(profiles)
        self.client = (
            client
            if client is not None
            else anthropic.Anthropic(api_key=get_anthropic_api_key(), max_retries=0)
        )

    def interpret(self, user_prompt: str) -> InterpretationResult:
        """
        Interpret a user prompt and return profile selection(s).

        Args:
            user_prompt: Raw user input

        Returns:
            InterpretationResult with selected profiles and cost tracking
        """
        console.print("\n[dim]🔎 Interpreting your request...[/dim]")

        user_message = f"""User request: {user_prompt}

Enabled profiles supplied for this decision:
{chr(10).join(_profile_lines(self.available_profiles))}

Classify this request and return JSON."""

        response = self.client.messages.create(
            model=INTERPRETER_MODEL,
            max_tokens=1024,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        content = getattr(response, "content", None)
        if (
            not isinstance(content, Sequence)
            or isinstance(content, (str, bytes, bytearray))
            or not content
        ):
            raise ProfileInterpretationError(
                "Interpreter response content must be a non-empty sequence of text blocks"
            )

        text_fragments: list[str] = []
        for block in content:
            if getattr(block, "type", None) != "text":
                raise ProfileInterpretationError(
                    "Interpreter response content must contain only text blocks"
                )
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise ProfileInterpretationError(
                    "Interpreter response content text must be a string"
                )
            text_fragments.append(text)

        raw_text = "".join(text_fragments).strip()
        if not raw_text:
            raise ProfileInterpretationError("Interpreter response content must not be empty")

        # Extract token usage
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

        # Calculate cost (Sonnet 4.6 pricing)
        cost_usd = (
            (input_tokens / 1_000_000) * 3.00
            + (output_tokens / 1_000_000) * 15.00
            + (cache_read_tokens / 1_000_000) * 0.30
        )

        # Parse response
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ProfileInterpretationError(
                "Interpreter response must be one valid JSON object"
            ) from exc
        if not isinstance(data, Mapping):
            raise ProfileInterpretationError("Interpreter response must be a JSON object")
        interpreter_reasoning = data.get("interpreter_reasoning", "")
        if not isinstance(interpreter_reasoning, str):
            raise ProfileInterpretationError("interpreter_reasoning must be a string")

        raw_profiles = data.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise ProfileInterpretationError(
                "Interpreter response must select at least one enabled profile"
            )

        profiles: list[ProfileSelection] = []
        seen_names: set[str] = set()
        seen_orders: set[int] = set()
        for index, item in enumerate(raw_profiles):
            item_path = f"profiles[{index}]"
            if not isinstance(item, Mapping):
                raise ProfileInterpretationError(f"{item_path} must be an object")
            name = _required_string(item, "profile_name", item_path)
            if not isinstance(name, str) or name not in self._profiles_by_id:
                raise ProfileInterpretationError(
                    f"{item_path} selects a profile outside the enabled set"
                )
            if name in seen_names:
                raise ProfileInterpretationError("Interpreter response contains duplicate profiles")
            seen_names.add(name)

            execution_order = _required_value(item, "execution_order", item_path)
            if (
                isinstance(execution_order, bool)
                or not isinstance(execution_order, int)
                or execution_order <= 0
                or execution_order in seen_orders
            ):
                raise ProfileInterpretationError(
                    f"profiles[{index}].execution_order must be a unique positive integer"
                )
            seen_orders.add(execution_order)

            confidence = _required_string(item, "confidence", item_path)
            if confidence not in {"high", "medium", "low"}:
                raise ProfileInterpretationError(
                    f"{item_path}.confidence must be high, medium, or low"
                )
            reason = _required_string(item, "reason", item_path)
            keywords_detected = _required_value(item, "keywords_detected", item_path)
            if not isinstance(keywords_detected, list) or not all(
                isinstance(keyword, str) for keyword in keywords_detected
            ):
                raise ProfileInterpretationError(
                    f"{item_path}.keywords_detected must be a list of strings"
                )
            language_detected = _required_string(item, "language_detected", item_path)

            obj = _required_value(item, "objective", item_path)
            if not isinstance(obj, Mapping):
                raise ProfileInterpretationError(f"{item_path}.objective must be an object")
            objective_path = f"{item_path}.objective"
            what_to_find = _required_string(obj, "what_to_find", objective_path)
            geographic_scope = _required_string(obj, "geographic_scope", objective_path)
            topic = _required_string(obj, "topic", objective_path)
            freshness_rule = _required_string(obj, "freshness_rule", objective_path)
            download_requested = _required_value(obj, "download_requested", objective_path)
            if not isinstance(download_requested, bool):
                raise ProfileInterpretationError(
                    f"{objective_path}.download_requested must be a Boolean"
                )

            authoritative_profile = self._profiles_by_id[name]
            profiles.append(
                ProfileSelection(
                    profile_name=name,
                    display_name=authoritative_profile.name,
                    confidence=confidence,
                    reason=reason,
                    execution_order=execution_order,
                    keywords_detected=keywords_detected,
                    language_detected=language_detected,
                    what_to_find=what_to_find,
                    geographic_scope=geographic_scope,
                    topic=topic,
                    freshness_rule=freshness_rule,
                    download_requested=download_requested,
                )
            )

        return InterpretationResult(
            profiles=sorted(profiles, key=lambda p: p.execution_order),
            is_global=any(profile.profile_name == "global" for profile in profiles),
            is_multi_profile=len(profiles) > 1,
            raw_prompt=user_prompt,
            interpreter_reasoning=interpreter_reasoning,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
        )

    def present_and_confirm(self, result: InterpretationResult, pricing_config=None) -> bool:
        """
        Show the interpretation result to the user and ask for confirmation.

        Args:
            result: The interpretation result to present
            pricing_config: Optional PricingConfig for cost display

        Returns:
            True if user confirms, False if user cancels
        """

        # Profile table
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("#", width=3)
        table.add_column("Profile")
        table.add_column("Confidence")
        table.add_column("Reason")

        for p in result.profiles:
            confidence_color = {"high": "green", "medium": "yellow", "low": "red"}.get(
                p.confidence, "white"
            )

            table.add_row(
                str(p.execution_order),
                p.display_name,
                f"[{confidence_color}]{p.confidence}[/{confidence_color}]",
                p.reason[:60] + "..." if len(p.reason) > 60 else p.reason,
            )

        console.print()
        console.print(
            Panel(table, title="[bold cyan]Detected Profiles[/bold cyan]", box=box.ROUNDED)
        )

        # Show execution order for multi-profile
        if result.is_multi_profile:
            console.print(
                f"[dim]Execution order: {' → '.join(p.display_name for p in result.profiles)}[/dim]"
            )

        # Show interpreter cost
        cost_str = f"${result.cost_usd:.4f}" if result.cost_usd >= 0.001 else "<$0.001"
        console.print(
            f"[dim]Interpretation actual reported usage: {result.total_tokens} tokens | "
            f"estimated cost: {cost_str}[/dim]"
        )
        if result.cache_creation_tokens > 0:
            console.print(
                "[dim]Cache-creation cost is not represented because the pricing contract "
                "has no cache-write rate.[/dim]"
            )

        # Low confidence warning
        low_confidence = [p for p in result.profiles if p.confidence == "low"]
        if low_confidence:
            console.print(
                f"\n[yellow]⚠️  Low confidence on: "
                f"{', '.join(p.display_name for p in low_confidence)}[/yellow]"
            )

        # Confirm
        console.print()
        choice = console.input("[cyan]Proceed with this plan? (Y/n/list): [/cyan]").strip().lower()

        if choice == "list":
            self._show_available_profiles()
            return self.present_and_confirm(result, pricing_config)

        return choice in ("", "y", "yes")

    def _show_available_profiles(self):
        """Show the enabled profiles supplied to this interpreter."""
        console.print("\n[bold]Enabled profiles:[/bold]")
        for profile in self.available_profiles:
            console.print(f"  - {profile.profile_id}: {profile.name}")
        console.print()

    def manual_select(self) -> list[str]:
        """
        Let user manually select profiles when auto-detection fails
        or user wants to override.

        Returns:
            List of selected profile names
        """
        self._show_available_profiles()
        raw = console.input("[cyan]Enter profile name(s) separated by commas: [/cyan]").strip()

        selected: list[str] = []
        for name in raw.split(","):
            name = name.strip()
            if name in self._profiles_by_id:
                selected.append(name)
            else:
                console.print(f"[red]Unknown profile: {name} — skipping[/red]")

        return selected
