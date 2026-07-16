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
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config_loader import get_anthropic_api_key

load_dotenv(Path(__file__).parent.parent / ".env")

console = Console()

# Cost tracking for the interpreter call itself
INTERPRETER_MODEL = "claude-sonnet-4-6"


@dataclass
class ProfileSelection:
    """A single profile selected for a run."""
    profile_name: str          # e.g. "dutch_government"
    display_name: str          # e.g. "Dutch Government"
    confidence: str            # "high", "medium", "low"
    reason: str                # Why this profile was selected
    execution_order: int       # 1 = first, 2 = second, etc.
    keywords_detected: list    # Keywords from prompt that triggered this
    language_detected: str     # Language of user prompt
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
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

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
        from orchestrator import ProfileObjective
        return [
            ProfileObjective(
                profile_name=p.profile_name,
                display_name=p.display_name,
                what_to_find=p.what_to_find or f"Datasets from {p.display_name}",
                geographic_scope=p.geographic_scope or p.display_name,
                topic=p.topic or "general",
                freshness_rule=p.freshness_rule or "no specific rule",
                download_requested=p.download_requested,
                execution_order=p.execution_order
            )
            for p in self.profiles
        ]


# System prompt for the interpreter — no profile-specific content
INTERPRETER_SYSTEM_PROMPT = """You are a data source profile classifier for an open data discovery agent.

Your job is to analyze a user's dataset request and determine which data source profile(s) to use.

AVAILABLE PROFILES:
- dutch_government: Official Dutch government sources (CBS, overheid.nl, RIVM, RDW, Den Haag Open Data)
  → Use when: prompt mentions Netherlands, Dutch, NL, gemeente, provincie, CBS, overheid, or Dutch place names
- us_government: US federal government sources (data.gov, SSA, Census Bureau, BLS, CDC)
  → Use when: prompt mentions United States, US, USA, American, federal, SSA, Census, or US agencies
- eu_open_data: European Union sources (EU Open Data Portal, Eurostat)
  → Use when: prompt mentions EU, European Union, Eurostat, or cross-European data
- global: Unrestricted web search — no preset sources
  → Use when: prompt is truly international, mentions multiple non-EU/NL/US regions,
    or explicitly asks for global data

MULTI-PROFILE RULES:
- If a prompt clearly mentions multiple specific countries/regions, select multiple profiles
- Example: "Dutch and US social security data" → [dutch_government, us_government]
- Execute profiles in the order they appear in the prompt

CONFIDENCE LEVELS:
- high: Clear geographic or source indicators in the prompt
- medium: Implied by context but not explicitly stated
- low: Ambiguous — could match multiple profiles

RESPONSE FORMAT:
Respond ONLY with valid JSON. No preamble, no explanation, no markdown fences.

{
  "profiles": [
    {
      "profile_name": "dutch_government",
      "display_name": "Dutch Government",
      "confidence": "high",
      "reason": "Prompt explicitly mentions Dutch government and CBS",
      "execution_order": 1,
      "keywords_detected": ["Dutch", "government", "CBS"],
      "language_detected": "en",
      "objective": {
        "what_to_find": "One official Dutch government dataset about social security",
        "geographic_scope": "Netherlands only",
        "topic": "social security",
        "freshness_rule": "updated within 12 months",
        "download_requested": true
      }
    }
  ],
  "is_global": false,
  "is_multi_profile": false,
  "interpreter_reasoning": "Brief explanation of classification decision"
}"""


class PromptInterpreter:
    """
    Interprets user prompts to select appropriate data source profiles.
    Makes a single Claude API call per interpretation.
    """

    def __init__(self, available_profiles: list[str]):
        """
        Args:
            available_profiles: List of profile names available on disk
        """
        self.available_profiles = available_profiles
        self.client = anthropic.Anthropic(
            api_key=get_anthropic_api_key()
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

        # Add available profiles context to the user message
        user_message = f"""User request: {user_prompt}

Available profiles on this system: {', '.join(self.available_profiles)}

Classify this request and return JSON."""

        response = self.client.messages.create(
            model=INTERPRETER_MODEL,
            max_tokens=1024,
            system=INTERPRETER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        # Extract token usage
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

        # Calculate cost (Sonnet 4.6 pricing)
        cost_usd = (
            (input_tokens / 1_000_000) * 3.00 +
            (output_tokens / 1_000_000) * 15.00 +
            (cache_read_tokens / 1_000_000) * 0.30
        )

        # Parse response
        raw_text = response.content[0].text.strip()

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            # Fallback: try to extract JSON from response
            import re
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                # Last resort: default to global
                data = {
                    "profiles": [{
                        "profile_name": "global",
                        "display_name": "Global Discovery",
                        "confidence": "low",
                        "reason": "Could not parse interpreter response — defaulting to global",
                        "execution_order": 1,
                        "keywords_detected": [],
                        "language_detected": "en"
                    }],
                    "is_global": True,
                    "is_multi_profile": False,
                    "interpreter_reasoning": "Fallback to global due to parse error"
                }

        # Validate profiles exist
        profiles = []
        for p in data.get("profiles", []):
            name = p.get("profile_name", "global")
            if name not in self.available_profiles:
                name = "global"
            obj = p.get("objective", {})
            profiles.append(ProfileSelection(
                profile_name=name,
                display_name=p.get("display_name", name),
                confidence=p.get("confidence", "medium"),
                reason=p.get("reason", ""),
                execution_order=p.get("execution_order", 1),
                keywords_detected=p.get("keywords_detected", []),
                language_detected=p.get("language_detected", "en"),
                what_to_find=obj.get("what_to_find", ""),
                geographic_scope=obj.get("geographic_scope", ""),
                topic=obj.get("topic", ""),
                freshness_rule=obj.get("freshness_rule", ""),
                download_requested=obj.get("download_requested", False)
            ))

        return InterpretationResult(
            profiles=sorted(profiles, key=lambda p: p.execution_order),
            is_global=data.get("is_global", False),
            is_multi_profile=data.get("is_multi_profile", False),
            raw_prompt=user_prompt,
            interpreter_reasoning=data.get("interpreter_reasoning", ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd
        )

    def present_and_confirm(
        self,
        result: InterpretationResult,
        pricing_config=None
    ) -> bool:
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
            confidence_color = {
                "high": "green",
                "medium": "yellow",
                "low": "red"
            }.get(p.confidence, "white")

            table.add_row(
                str(p.execution_order),
                p.display_name,
                f"[{confidence_color}]{p.confidence}[/{confidence_color}]",
                p.reason[:60] + "..." if len(p.reason) > 60 else p.reason
            )

        console.print()
        console.print(Panel(
            table,
            title="[bold cyan]Detected Profiles[/bold cyan]",
            box=box.ROUNDED
        ))

        # Show execution order for multi-profile
        if result.is_multi_profile:
            console.print(
                f"[dim]Execution order: "
                f"{' → '.join(p.display_name for p in result.profiles)}[/dim]"
            )

        # Show interpreter cost
        cost_str = f"${result.cost_usd:.4f}" if result.cost_usd >= 0.001 else "<$0.001"
        console.print(
            f"[dim]Interpretation: {result.input_tokens + result.output_tokens} tokens | "
            f"cost: {cost_str}[/dim]"
        )

        # Global warning
        if result.is_global:
            # Find global profile's warning message
            console.print()
            console.print(
                "[yellow]⚠️  Global Discovery Mode\n\n"
                "This profile uses web search to find data sources worldwide.\n"
                "It may take significantly longer and consume more API credits\n"
                "than a targeted profile.\n\n"
                "Consider using a targeted profile if your data is from a\n"
                "specific country or organization.[/yellow]"
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
        choice = console.input(
            "[cyan]Proceed with this plan? (Y/n/list): [/cyan]"
        ).strip().lower()

        if choice == "list":
            self._show_available_profiles()
            return self.present_and_confirm(result, pricing_config)

        return choice in ("", "y", "yes")

    def _show_available_profiles(self):
        """Show all available profiles."""
        console.print("\n[bold]Available profiles:[/bold]")
        for name in self.available_profiles:
            console.print(f"  - {name}")
        console.print()

    def manual_select(self) -> list[str]:
        """
        Let user manually select profiles when auto-detection fails
        or user wants to override.

        Returns:
            List of selected profile names
        """
        self._show_available_profiles()
        raw = console.input(
            "[cyan]Enter profile name(s) separated by commas: [/cyan]"
        ).strip()

        selected = []
        for name in raw.split(","):
            name = name.strip()
            if name in self.available_profiles:
                selected.append(name)
            else:
                console.print(f"[red]Unknown profile: {name} — skipping[/red]")

        return selected if selected else ["global"]
