"""
tests/unit/test_orchestrator.py

Tests for orchestrator state transitions, handoff summaries, and early stop logic.
No network calls — all fixtures use pre-built ProfileResult objects.

Key behaviors under test:
1. all_objectives_met() — correct early stop conditions
2. handoff_summary() — compact, informative, never full conversation history
3. evaluate_result() — correct mapping of outcomes to objective_met/partial_success
4. build_initial_message() — handoff injected correctly for subsequent profiles
"""

import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


class TestAllObjectivesMet:
    """
    Tests for Orchestrator.all_objectives_met().

    The critical invariant: early stop fires ONLY when every profile
    in the run has met its objective. Not when one has, not when
    partial success exists.
    """

    def test_single_met_returns_true(self, dutch_objective):
        from orchestrator import Orchestrator, ProfileResult
        o = Orchestrator([dutch_objective])
        pr = ProfileResult("dutch_government", "Dutch Government", dutch_objective)
        pr.objective_met = True
        assert o.all_objectives_met([pr]) is True

    def test_single_not_met_returns_false(self, dutch_objective):
        from orchestrator import Orchestrator, ProfileResult
        o = Orchestrator([dutch_objective])
        pr = ProfileResult("dutch_government", "Dutch Government", dutch_objective)
        pr.objective_met = False
        assert o.all_objectives_met([pr]) is False

    def test_both_met_returns_true(self, dutch_objective, us_objective, profile_result_met):
        from orchestrator import Orchestrator, ProfileResult
        o = Orchestrator([dutch_objective, us_objective])
        us_met = ProfileResult("us_government", "US Government", us_objective)
        us_met.objective_met = True
        assert o.all_objectives_met([profile_result_met, us_met]) is True

    def test_first_met_second_not_run_returns_false(self, dutch_objective, us_objective, profile_result_met):
        """
        Critical: Profile 1 met objective but Profile 2 hasn't run.
        This is the early stop bug we identified — must return False.
        """
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective, us_objective])
        assert o.all_objectives_met([profile_result_met]) is False

    def test_partial_success_is_not_met(self, dutch_objective, profile_result_partial):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        assert o.all_objectives_met([profile_result_partial]) is False

    def test_empty_results_returns_false(self, dutch_objective):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        assert o.all_objectives_met([]) is False

    def test_failed_result_is_not_met(self, dutch_objective, profile_result_empty):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        assert o.all_objectives_met([profile_result_empty]) is False

    def test_mixed_met_and_partial_returns_false(
        self, dutch_objective, us_objective, profile_result_met, profile_result_partial
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective, us_objective])
        assert o.all_objectives_met([profile_result_met, profile_result_partial]) is False


class TestHandoffSummary:
    """
    Tests for ProfileResult.handoff_summary().

    The handoff must be:
    - Compact (under 500 chars)
    - Contain dataset identity (title, id)
    - NOT contain full conversation history
    - Correctly describe the outcome (downloaded / found but blocked / nothing)
    """

    def test_downloaded_summary_contains_title(self, profile_result_met):
        summary = profile_result_met.handoff_summary()
        assert "Sociale zekerheid" in summary or "37789ksz" in summary

    def test_downloaded_summary_contains_status(self, profile_result_met):
        summary = profile_result_met.handoff_summary()
        assert "downloaded" in summary.lower() or "✅" in summary

    def test_partial_summary_contains_failure_reason(self, profile_result_partial):
        summary = profile_result_partial.handoff_summary()
        assert "SSA" in summary or "blocked" in summary.lower() or "failed" in summary.lower()

    def test_partial_summary_does_not_claim_success(self, profile_result_partial):
        summary = profile_result_partial.handoff_summary()
        assert "objective_met" not in summary
        # Should not say downloaded when it wasn't
        assert "✅" not in summary or "blocked" in summary.lower()

    def test_empty_summary_says_nothing_found(self, profile_result_empty):
        summary = profile_result_empty.handoff_summary()
        assert "nothing" in summary.lower() or "not found" in summary.lower() or "❌" in summary

    def test_summary_is_compact(self, profile_result_met):
        """Handoff must stay under 500 chars to avoid context bloat."""
        summary = profile_result_met.handoff_summary()
        assert len(summary) < 500, f"Handoff too long: {len(summary)} chars"

    def test_summary_instructs_next_profile(self, profile_result_met):
        """Next profile must be told not to re-search what was already found."""
        summary = profile_result_met.handoff_summary()
        assert "do not" in summary.lower() or "your task" in summary.lower() or "scope" in summary.lower()

    def test_summary_does_not_contain_full_history(self, profile_result_met):
        """Handoff must never contain conversation history markers."""
        summary = profile_result_met.handoff_summary()
        assert "role" not in summary
        assert "content" not in summary
        assert "messages" not in summary


class TestEvaluateResult:
    """
    Tests for Orchestrator.evaluate_result().

    Maps ProfileResult outcomes to objective_met / partial_success / failed.
    """

    def test_downloaded_sets_objective_met(
        self, dutch_objective, profile_result_met
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        profile_result_met.objective_met = False  # reset
        result = o.evaluate_result(profile_result_met, dutch_objective)
        assert result.objective_met is True

    def test_found_not_downloaded_sets_partial(
        self, us_objective, profile_result_partial
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([us_objective])
        profile_result_partial.objective_met = False  # reset
        profile_result_partial.partial_success = False  # reset
        result = o.evaluate_result(profile_result_partial, us_objective)
        assert result.partial_success is True
        assert result.objective_met is False

    def test_nothing_found_sets_failed(
        self, us_objective, profile_result_empty
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([us_objective])
        result = o.evaluate_result(profile_result_empty, us_objective)
        assert result.objective_met is False
        assert result.partial_success is False
        assert result.failure_reason is not None

    def test_partial_has_failure_reason(
        self, us_objective, profile_result_partial
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([us_objective])
        profile_result_partial.partial_success = False  # reset
        result = o.evaluate_result(profile_result_partial, us_objective)
        assert result.failure_reason is not None
        assert len(result.failure_reason) > 0


class TestBuildInitialMessage:
    """
    Tests for Orchestrator.build_initial_message().

    Verifies that handoff context is injected correctly and that
    the first profile gets just the prompt, not a spurious handoff.
    """

    def test_first_profile_no_handoff(self, dutch_objective):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        msg = o.build_initial_message(
            user_prompt="Find Dutch social security data",
            objective=dutch_objective,
            previous_results=[]
        )
        assert "HANDOFF FROM PREVIOUS PROFILE" not in msg
        assert "Find Dutch social security data" in msg

    def test_second_profile_gets_handoff(
        self, dutch_objective, us_objective, profile_result_met
    ):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective, us_objective])
        msg = o.build_initial_message(
            user_prompt="Find Dutch and US social security data",
            objective=us_objective,
            previous_results=[profile_result_met]
        )
        assert "HANDOFF FROM PREVIOUS PROFILE" in msg

    def test_message_contains_objective(self, dutch_objective):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        msg = o.build_initial_message(
            user_prompt="Find Dutch social security data",
            objective=dutch_objective,
            previous_results=[]
        )
        assert dutch_objective.what_to_find in msg
        assert dutch_objective.geographic_scope in msg

    def test_message_contains_freshness_rule(self, dutch_objective):
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective])
        msg = o.build_initial_message(
            user_prompt="Find Dutch social security data",
            objective=dutch_objective,
            previous_results=[]
        )
        assert dutch_objective.freshness_rule in msg

    def test_handoff_replaces_not_appends_history(
        self, dutch_objective, us_objective, profile_result_met
    ):
        """
        The handoff summary must be compact.
        The full conversation history of Profile 1 must NOT appear.
        """
        from orchestrator import Orchestrator
        o = Orchestrator([dutch_objective, us_objective])
        msg = o.build_initial_message(
            user_prompt="Find data",
            objective=us_objective,
            previous_results=[profile_result_met]
        )
        # Should not contain raw API response markers
        assert '"role"' not in msg
        assert '"content"' not in msg
        # Should be reasonably compact
        assert len(msg) < 2000
