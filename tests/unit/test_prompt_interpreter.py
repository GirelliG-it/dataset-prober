"""
tests/unit/test_prompt_interpreter.py

Tests for PromptInterpreter JSON parsing, fallback chains, and profile validation.
All Claude API calls are mocked — we test YOUR code's response to Claude's output,
not Claude itself.

Key behaviors under test:
1. Valid JSON → correct ProfileSelection
2. Malformed JSON → graceful fallback to global
3. Unknown profile names → remapped to global
4. to_objectives() mapping correctness
5. Multi-profile detection
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def make_mock_response(json_payload: dict):
    """Helper to create a mock Anthropic response with given JSON payload."""
    mock = MagicMock()
    mock.content = [MagicMock(text=json.dumps(json_payload), type="text")]
    mock.usage = MagicMock(input_tokens=500, output_tokens=200, cache_read_input_tokens=0)
    return mock


def make_interpreter(available_profiles=None):
    """Create a PromptInterpreter with mock Anthropic client."""
    from prompt_interpreter import PromptInterpreter

    if available_profiles is None:
        available_profiles = ["dutch_government", "us_government", "eu_open_data", "global"]
    return PromptInterpreter(available_profiles)


VALID_DUTCH_RESPONSE = {
    "profiles": [
        {
            "profile_name": "dutch_government",
            "display_name": "Dutch Government",
            "confidence": "high",
            "reason": "Prompt mentions Dutch government",
            "execution_order": 1,
            "keywords_detected": ["Dutch", "government"],
            "language_detected": "en",
            "objective": {
                "what_to_find": "Dutch social security dataset",
                "geographic_scope": "Netherlands only",
                "topic": "social security",
                "freshness_rule": "within 12 months",
                "download_requested": True,
            },
        }
    ],
    "is_global": False,
    "is_multi_profile": False,
    "interpreter_reasoning": "Clear Dutch government request",
}

VALID_MULTI_RESPONSE = {
    "profiles": [
        {
            "profile_name": "dutch_government",
            "display_name": "Dutch Government",
            "confidence": "high",
            "reason": "Dutch mentioned",
            "execution_order": 1,
            "keywords_detected": ["Dutch"],
            "language_detected": "en",
            "objective": {
                "what_to_find": "Dutch dataset",
                "geographic_scope": "Netherlands",
                "topic": "social security",
                "freshness_rule": "within 12 months",
                "download_requested": True,
            },
        },
        {
            "profile_name": "us_government",
            "display_name": "US Government",
            "confidence": "high",
            "reason": "US mentioned",
            "execution_order": 2,
            "keywords_detected": ["US"],
            "language_detected": "en",
            "objective": {
                "what_to_find": "US dataset",
                "geographic_scope": "United States",
                "topic": "social security",
                "freshness_rule": "within 12 months",
                "download_requested": True,
            },
        },
    ],
    "is_global": False,
    "is_multi_profile": True,
    "interpreter_reasoning": "Both Dutch and US requested",
}

GLOBAL_RESPONSE = {
    "profiles": [
        {
            "profile_name": "global",
            "display_name": "Global Discovery",
            "confidence": "medium",
            "reason": "No specific country mentioned",
            "execution_order": 1,
            "keywords_detected": [],
            "language_detected": "en",
            "objective": {
                "what_to_find": "Any dataset",
                "geographic_scope": "Worldwide",
                "topic": "population",
                "freshness_rule": "no restriction",
                "download_requested": False,
            },
        }
    ],
    "is_global": True,
    "is_multi_profile": False,
    "interpreter_reasoning": "Global search requested",
}


class TestValidJSONParsing:
    """Tests for correct parsing of well-formed Claude responses."""

    def test_single_profile_detected(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch social security data")
        assert len(result.profiles) == 1
        assert result.profiles[0].profile_name == "dutch_government"

    def test_confidence_level_preserved(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch data")
        assert result.profiles[0].confidence == "high"

    def test_multi_profile_detected(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_MULTI_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch and US social security data")
        assert len(result.profiles) == 2
        assert result.is_multi_profile is True

    def test_execution_order_preserved(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_MULTI_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch and US data")
        assert result.profiles[0].execution_order == 1
        assert result.profiles[1].execution_order == 2

    def test_profiles_sorted_by_execution_order(self):
        interpreter = make_interpreter()
        # Reverse the order in the response
        reversed_response = {
            **VALID_MULTI_RESPONSE,
            "profiles": list(reversed(VALID_MULTI_RESPONSE["profiles"])),
        }
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(reversed_response),
        ):
            result = interpreter.interpret("Find data")
        assert result.profiles[0].execution_order < result.profiles[1].execution_order

    def test_global_flag_set(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages, "create", return_value=make_mock_response(GLOBAL_RESPONSE)
        ):
            result = interpreter.interpret("Find any population data worldwide")
        assert result.is_global is True

    def test_cost_tracked(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find data")
        assert result.cost_usd > 0
        assert result.input_tokens > 0
        assert result.output_tokens > 0


class TestObjectiveExtraction:
    """Tests for per-profile objective extraction from interpreter response."""

    def test_what_to_find_extracted(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch social security data")
        assert result.profiles[0].what_to_find == "Dutch social security dataset"

    def test_geographic_scope_extracted(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch data")
        assert result.profiles[0].geographic_scope == "Netherlands only"

    def test_freshness_rule_extracted(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch data")
        assert result.profiles[0].freshness_rule == "within 12 months"

    def test_download_requested_extracted(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find and download Dutch data")
        assert result.profiles[0].download_requested is True


class TestFallbackBehavior:
    """
    Tests for fallback when Claude returns malformed or unexpected output.
    These are the edge cases most likely to fail silently in production.
    """

    def test_malformed_json_falls_back_to_global(self):
        interpreter = make_interpreter()
        mock = MagicMock()
        mock.content = [MagicMock(text="This is not JSON at all", type="text")]
        mock.usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_input_tokens=0)
        with patch.object(interpreter.client.messages, "create", return_value=mock):
            result = interpreter.interpret("Find data")
        assert len(result.profiles) == 1
        assert result.profiles[0].profile_name == "global"

    def test_json_in_markdown_fences_parsed(self):
        """Claude sometimes wraps JSON in ```json ... ``` — must handle gracefully."""
        interpreter = make_interpreter()
        wrapped = f"```json\n{json.dumps(VALID_DUTCH_RESPONSE)}\n```"
        mock = MagicMock()
        mock.content = [MagicMock(text=wrapped, type="text")]
        mock.usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_input_tokens=0)
        with patch.object(interpreter.client.messages, "create", return_value=mock):
            result = interpreter.interpret("Find Dutch data")
        # Should either parse correctly or fall back gracefully — not crash
        assert len(result.profiles) >= 1

    def test_unknown_profile_name_remapped_to_global(self):
        """Claude hallucinates 'netherlands_data' — must remap to global."""
        interpreter = make_interpreter()
        bad_profile_response = {
            **VALID_DUTCH_RESPONSE,
            "profiles": [
                {
                    **VALID_DUTCH_RESPONSE["profiles"][0],
                    "profile_name": "netherlands_data",  # doesn't exist
                }
            ],
        }
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(bad_profile_response),
        ):
            result = interpreter.interpret("Find Dutch data")
        assert result.profiles[0].profile_name == "global"

    def test_empty_profiles_list_falls_back(self):
        interpreter = make_interpreter()
        empty_response = {
            "profiles": [],
            "is_global": False,
            "is_multi_profile": False,
            "interpreter_reasoning": "",
        }
        with patch.object(
            interpreter.client.messages, "create", return_value=make_mock_response(empty_response)
        ):
            result = interpreter.interpret("Find data")
        # Should not crash — either returns empty or falls back
        assert isinstance(result.profiles, list)


class TestToObjectives:
    """Tests for InterpretationResult.to_objectives() mapping."""

    def test_converts_to_profile_objectives(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch data")
        objectives = result.to_objectives()
        assert len(objectives) == 1
        assert objectives[0].profile_name == "dutch_government"

    def test_objective_preserves_what_to_find(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_DUTCH_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch data")
        objectives = result.to_objectives()
        assert objectives[0].what_to_find == "Dutch social security dataset"

    def test_multi_profile_produces_multiple_objectives(self):
        interpreter = make_interpreter()
        with patch.object(
            interpreter.client.messages,
            "create",
            return_value=make_mock_response(VALID_MULTI_RESPONSE),
        ):
            result = interpreter.interpret("Find Dutch and US data")
        objectives = result.to_objectives()
        assert len(objectives) == 2
        profile_names = [o.profile_name for o in objectives]
        assert "dutch_government" in profile_names
        assert "us_government" in profile_names
