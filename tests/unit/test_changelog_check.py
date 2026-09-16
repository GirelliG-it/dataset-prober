"""Offline checks for the PR changelog policy, including untrusted PR text."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/check_changelog.py"
SPEC = importlib.util.spec_from_file_location("check_changelog", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

EMPTY = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0]\n\n- Released behavior.\n"
EXISTING = EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n### Fixed\n\n- Fix parsing.")


@pytest.mark.parametrize(
    "entry",
    [
        "- Users can inspect resources without loading them.",
        "- Fix parsing of\n  semicolon-delimited files.",
        "* Preserve existing tables when a load fails.",
        "+ Report cache usage accurately.",
    ],
)
def test_added_unreleased_entry_satisfies_policy(entry):
    after = EMPTY.replace("## [Unreleased]", f"## [Unreleased]\n\n### Added\n\n{entry}")
    assert checker.satisfies_policy(EMPTY, after, "")


def test_updated_unreleased_entry_satisfies_policy():
    assert checker.satisfies_policy(EXISTING, EXISTING.replace("parsing", "CSV parsing"), "")


@pytest.mark.parametrize(
    "after",
    [
        EMPTY,
        EMPTY.replace("Released behavior.", "Changed released behavior."),
        EMPTY.replace("# Changelog", "# A better changelog"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n### Added"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n-"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n- ** **"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n- TODO"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n- TBD"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n\n- <!-- Describe change. -->"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n<!--\n- Hidden change.\n-->"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n```markdown\n- Example only.\n```"),
        EMPTY.replace("## [Unreleased]", "## [Unreleased]\n~~~\n- Example only.\n~~~"),
        "# Changelog\n## [0.1.0]\n- A release change only.\n",
        "<!--\n## [Unreleased]\n- Hidden section.\n-->\n",
        "## [Unreleased]\n- Ambiguous.\n## [Unreleased]\n- Second section.\n",
        "## [0.1.0]\n### [Unreleased]\n- Inside an old release.\n",
    ],
)
def test_non_entries_and_changes_outside_unreleased_fail(after):
    assert not checker.satisfies_policy(EMPTY, after, "")


def test_removal_reordering_duplicate_and_wrapping_do_not_count():
    before = EMPTY.replace("## [Unreleased]", "## [Unreleased]\n- Fix parsing.\n- Improve errors.")
    for after in (
        EMPTY,
        before.replace("- Fix parsing.\n- Improve errors.", "- Improve errors.\n- Fix parsing."),
        before.replace("- Fix parsing.", "- Fix parsing.\n- Fix parsing."),
        before.replace("- Fix parsing.", "- Fix\n  parsing."),
        before.replace("- Fix parsing.", "- **Fix parsing.**"),
        before.replace("- Fix parsing.", "- Fix parsing. <!-- Added comment. -->"),
    ):
        assert not checker.satisfies_policy(before, after, "")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "## Summary\nInternal cleanup.",
        "## Changelog: not needed",
        "## Changelog: not needed\n \t\n",
        "## Changelog: not needed\n<!-- Explain why. -->",
        "## Changelog: not needed\nTODO",
        "## Changelog: not needed\nTBD",
        "## Changelog: not needed\n...",
        "## Changelog: not needed\n## Tests\nTests passed.",
        "<!--\n## Changelog: not needed\nHidden explanation.\n-->",
        "```markdown\n## Changelog: not needed\nExample explanation.\n```",
        "## Changelog: not needed\n```\nOnly a code example.\n```",
        (ROOT / ".github/pull_request_template.md").read_text(),
    ],
)
def test_missing_empty_or_template_explanations_fail(body):
    assert not checker.satisfies_policy(EMPTY, EMPTY, body)


@pytest.mark.parametrize(
    "body",
    [
        "## Changelog: not needed\nThis only improves CI; runtime behavior is unchanged.",
        "## Changelog: not needed\n\n- Test refactoring only.\n\n## Tests\nOffline suite passed.",
        "### Changelog: not needed\nThis updates internal contributor guidance.",
        "## Changelog: not needed\r\nOnly internal fixtures change.\r\n",
    ],
)
def test_valid_exemption_satisfies_policy_without_a_changelog_change(body):
    assert checker.satisfies_policy(EMPTY, EMPTY, body)


def _event(body=None):
    return {"pull_request": {"body": body, "base": {"sha": "b" * 40}, "head": {"sha": "c" * 40}}}


def test_comparison_uses_merge_base_and_pr_head_not_latest_main(monkeypatch):
    calls = []
    responses = {
        ("merge-base", "b" * 40, "c" * 40): "a" * 40 + "\n",
        ("ls-tree", "--name-only", "a" * 40, "--", "CHANGELOG.md"): "CHANGELOG.md\n",
        ("ls-tree", "--name-only", "c" * 40, "--", "CHANGELOG.md"): "CHANGELOG.md\n",
        ("show", f"{'a' * 40}:CHANGELOG.md"): EXISTING,
        ("show", f"{'c' * 40}:CHANGELOG.md"): EXISTING,
    }

    def git(*args):
        calls.append(args)
        return responses[args]

    monkeypatch.setattr(checker, "_git", git)
    # A different latest-main changelog must not make an unchanged PR entry pass.
    assert not checker.check_event(_event())
    assert ("show", f"{'b' * 40}:CHANGELOG.md") not in calls
    assert len(calls) == 5


def test_new_changelog_file_is_supported(monkeypatch):
    monkeypatch.setattr(checker, "_git", lambda *args: "")
    assert checker._changelog_at("a" * 40) == ""
    assert checker.satisfies_policy("", EXISTING, "")


def test_git_is_executed_as_an_argument_list_without_a_shell(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="result")

    monkeypatch.setattr(checker.subprocess, "run", run)
    assert checker._git("merge-base", "a" * 40, "b" * 40) == "result"
    args, kwargs = calls[0]
    assert args == ["git", "merge-base", "a" * 40, "b" * 40]
    assert kwargs.get("shell", False) is False
    assert kwargs["check"] is True


@pytest.mark.parametrize("sha", ["--help", "$(touch marker)", "a" * 39, None])
def test_invalid_revisions_fail_before_git(monkeypatch, sha):
    event = _event()
    event["pull_request"]["head"]["sha"] = sha

    def unexpected_git(*args):
        pytest.fail("Invalid revision reached Git")

    monkeypatch.setattr(checker, "_git", unexpected_git)
    with pytest.raises(ValueError):
        checker.check_event(event)


def test_untrusted_pr_body_is_data_and_not_echoed(monkeypatch, tmp_path, capsys):
    marker = tmp_path / "must-not-exist"
    body = f"## Changelog: not needed\nInternal test for $(touch {marker}) `touch {marker}`.\n::error::fake"
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(body)))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr("sys.argv", [str(SCRIPT)])
    git_calls = []

    def git(*args):
        git_calls.append(args)
        return "a" * 40 if args[0] == "merge-base" else ""

    monkeypatch.setattr(checker, "_git", git)
    assert checker.main() == 0
    assert not marker.exists()
    assert body not in capsys.readouterr().out
    assert all("touch" not in arg for call in git_calls for arg in call)


def test_failure_explains_both_remedies(monkeypatch, tmp_path, capsys):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(_event()))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--event-path", str(path)])
    monkeypatch.setattr(checker, "_git", lambda *args: "a" * 40 if args[0] == "merge-base" else "")
    assert checker.main() == 1
    output = capsys.readouterr().out
    assert "## [Unreleased]" in output
    assert "## Changelog: not needed" in output
    assert "nonempty explanation" in output
    assert "edit the PR description" in output


def test_unavailable_git_history_fails_closed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(_event("## Changelog: not needed\nInternal CI only.")))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--event-path", str(path)])

    def failed_git(*args):
        raise subprocess.CalledProcessError(128, "git", stderr="untrusted details")

    monkeypatch.setattr(checker, "_git", failed_git)
    assert checker.main() == 1
    output = capsys.readouterr().out
    assert "fetch-depth: 0" in output
    assert "untrusted details" not in output


def test_workflow_reruns_for_body_and_commit_changes_with_read_only_permissions():
    workflow = yaml.load(
        (ROOT / ".github/workflows/changelog.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"pull_request"}
    assert {"opened", "reopened", "synchronize", "edited"} <= set(
        workflow["on"]["pull_request"]["types"]
    )
    assert "paths" not in workflow["on"]["pull_request"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["changelog"]
    assert job["name"] == "Changelog check"
    assert "permissions" not in job
    checkout = job["steps"][0]["with"]
    assert checkout["fetch-depth"] == "0"
    assert checkout["persist-credentials"] == "false"
    assert checkout["ref"] == "${{ github.event.pull_request.head.sha }}"
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == ["python .github/scripts/check_changelog.py"]


@pytest.mark.parametrize("exemption", [False, True])
def test_cli_with_real_read_only_git_history_and_untrusted_body(tmp_path, exemption):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    marker = tmp_path / "not-executed"
    body = f"Only CI changes; $(touch {marker}) and `touch {marker}` are untrusted text."
    if exemption:
        body = f"## Changelog: not needed\n{body}"
    event = {"pull_request": {"base": {"sha": head}, "head": {"sha": head}, "body": body}}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--event-path", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == (0 if exemption else 1)
    assert "touch" not in result.stdout
    assert not marker.exists()
