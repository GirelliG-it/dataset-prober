"""Require a changed Unreleased entry or an explained PR-body exemption.

Uses only the standard library. PR text is read as JSON data, never interpolated
into shell commands or echoed into workflow commands.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
BULLET = re.compile(r"^ {0,3}[-*+][ \t]+(.*)$")
FAILURE = (
    "Changelog check failed. Add or update a descriptive bullet under ## [Unreleased] "
    "in CHANGELOG.md, or add a ## Changelog: not needed section to the PR description "
    "with a nonempty explanation of why no user-facing entry is needed. "
    "Headings, comments, whitespace-only edits, deletions, and placeholders do not count. "
    "See CONTRIBUTING.md. Push a commit or edit the PR description to rerun the check."
)


def _visible_lines(text: str) -> list[str]:
    """Ignore template comments and fenced examples, not general Markdown rendering."""
    text = re.sub(r"<!--.*?(?:-->|$)", "", text, flags=re.DOTALL)
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        lines.append(line)
    return lines


def _section(text: str, title: str, *, heading_level: int | None = None) -> list[str]:
    lines = _visible_lines(text)
    starts = [
        (index, len(match[1]))
        for index, line in enumerate(lines)
        if (match := HEADING.match(line))
        and match[2].casefold() == title.casefold()
        and (heading_level is None or len(match[1]) == heading_level)
    ]
    if len(starts) != 1:
        return []
    start, level = starts[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if (match := HEADING.match(lines[index])) and len(match[1]) <= level
        ),
        len(lines),
    )
    return lines[start + 1 : end]


def _content(text: str) -> str:
    text = re.sub(r"[*_`~]", "", text)
    return " ".join(text.split())


def _has_description(text: str) -> bool:
    return any(char.isalnum() for char in text) and text.casefold().strip(" .:-") not in {
        "todo",
        "tbd",
        "n/a",
        "none",
        "not needed",
        "explain why",
    }


def unreleased_entries(changelog: str) -> set[str]:
    """Return prose bullets, normalizing wrapping and ignoring empty placeholders."""
    entries = []
    current = []
    for line in [*_section(changelog, "[Unreleased]", heading_level=2), "## end"]:
        bullet = BULLET.match(line)
        if bullet or HEADING.match(line) or (line.strip() and not line[0].isspace()):
            if current:
                entries.append(_content(" ".join(current)))
                current = []
        if bullet:
            current.append(bullet[1])
        elif current and line[:1].isspace():
            current.append(line.strip())
    return {entry for entry in entries if _has_description(entry)}


def has_exemption(body: str) -> bool:
    explanation = " ".join(
        line for line in _section(body, "Changelog: not needed") if not HEADING.match(line)
    )
    return _has_description(_content(explanation))


def satisfies_policy(before: str, after: str, body: str) -> bool:
    return bool(unreleased_entries(after) - unreleased_entries(before)) or has_exemption(body)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def _changelog_at(revision: str) -> str:
    if not _git("ls-tree", "--name-only", revision, "--", "CHANGELOG.md").strip():
        return ""
    return _git("show", f"{revision}:CHANGELOG.md")


def check_event(event: dict) -> bool:
    pr = event["pull_request"]
    base = pr["base"]["sha"]
    head = pr["head"]["sha"]
    body = pr.get("body") or ""
    if not isinstance(body, str) or any(
        not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)
    ):
        raise ValueError("Invalid pull request event")
    # Compare only this branch's changes, not entries newly introduced on main.
    merge_base = _git("merge-base", base, head).strip()
    return satisfies_policy(_changelog_at(merge_base), _changelog_at(head), body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args()
    if not args.event_path:
        print("Changelog check could not read a PR event. Set GITHUB_EVENT_PATH or --event-path.")
        return 1
    try:
        event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
        passed = check_event(event)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        print(
            "Changelog check could not read the PR event or Git history. "
            "Ensure checkout includes the PR head and base history (fetch-depth: 0)."
        )
        return 1
    if not passed:
        print(FAILURE)
        return 1
    print("Changelog requirement satisfied. Reviewers must still assess accuracy and relevance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
