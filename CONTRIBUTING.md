# Contributing

Run the development checks described in [README.md](README.md#development-checks).

## Changelog requirement

Each pull request must do one of the following:

- Add or update a descriptive bullet under `## [Unreleased]` in `CHANGELOG.md`,
  usually under `### Added`, `### Changed`, or `### Fixed`. Describe the effect
  for someone using the tool; commit mechanics and implementation details belong
  in the PR description. Do not bump the package version as part of routine entries.
- For purely internal work with no user-relevant change, include a Markdown
  section in the PR description with this heading and a concrete explanation:

  ```markdown
  ## Changelog: not needed
  This only improves CI checks and contributor instructions; runtime behavior is unchanged.
  ```

The `Changelog check` compares the PR head's Unreleased bullets with the merge
base. Changes to released sections, deletions alone, reordered entries, line
wrapping, headings, HTML comments, fenced examples, and empty or `TODO`/`TBD`
placeholders do not count. The exemption explanation must be in the section
itself, not a comment or an unrelated section.

Automation establishes that an entry or explanation exists. Reviewers still
assess its accuracy, relevance, and whether an exemption is justified. The check
runs on PR creation, reopening, description edits, new commits, and readiness for
review. On failure, push an entry or edit the description with an explanation.

The checker uses Python's standard library, reads PR text as event JSON data,
and runs with read-only repository permissions. It does not execute PR text or
post comments. Changes to the checker or workflow itself also need careful review.

To reproduce a check locally against existing Git history:

```bash
python .github/scripts/check_changelog.py --event-path /path/to/pr-event.json
```

The event must contain `pull_request.base.sha`, `pull_request.head.sha`, and
optionally `pull_request.body`. Both commit histories must exist locally.
Focused tests: `python -m pytest tests/unit/test_changelog_check.py`.

## Make the check required on main

After the workflow has run on a PR, a repository administrator can edit the
branch protection rule for `main` under **Settings → Branches**, enable
**Require status checks to pass before merging**, and select **Changelog check**
(workflow: **Changelog**). Require the GitHub Actions source when offered, retain
the existing CI requirement, and save the rule. If the repository uses rulesets,
add the same required status check to the active branch ruleset targeting `main`
under **Settings → Rules → Rulesets**. Choose one existing protection mechanism;
adding this workflow alone does not make it a merge requirement.

See GitHub's [required status check documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging).
No repository settings are changed by these files.
