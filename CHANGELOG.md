# Changelog

All notable changes to `dataset-prober` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

Entries describe what changed **for someone using the tool**, not what was done
to the source. For the latter, read `git log`.

## [Unreleased]

### Added

- Deterministic resource assessments now distinguish verified non-empty tabular
  data from documentary, erroneous, empty, unsupported, contradictory, and
  ambiguous resources using stable machine-readable reason codes.
- Interactive directory descent for autoindex sources. A source URL that serves
  a folder listing (Apache/nginx style, as used by RIVM and INSPIRE portals) is
  now walked before probing: pick a subfolder, descend, pick files. Chosen files
  flow into the existing probe and download pipeline.
- HTML landing-page guard. A URL that serves HTML is rejected before load, and a
  table whose contents look like markup causes the complete load transaction to
  roll back. Redirect traps and portal landing pages now fail honestly instead
  of being stored as data.

### Changed

- Profile-agent runs now enforce explicit per-search result and model-call ceilings, a
  between-call reported-token stop threshold, a per-call requested output cap and monotonic
  request deadlines. CBS and CKAN discovery and inspection operations now cap configured
  source timeouts by freshly remaining run time between sequential operations. Output-limited
  model responses
  return partial results without retrying or executing truncated tool calls. The unused crawl
  budget was removed, and continuing after a timeout no longer resets any non-time budget.
  Pre-run output labels thresholds as a budget-based planning estimate; final output reports
  token usage returned by completed model responses and locally observed attempted, completed,
  and timed-out call outcomes.
- Bundled profiles now load through the immutable static profile contract and fail closed
  according to explicit lifecycle status: Dutch CBS is manual-only, while US, EU and Global
  discovery remain disabled until their promised transports are repaired and certified.
- Only deterministically verified, supported, non-empty tabular resources may
  enter selection and consent. The actual CSV or CBS payload is assessed again
  before persistent DuckDB access; report-only candidates remain visible in
  summaries with their assessment reason.
- Classifier-issued assessment evidence is bound to the canonical candidate
  identity, so one resource's assessment cannot authorize another resource.
- The CSV dialect decision is now made once and shared by probing and
  downloading. European-dialect files (`;` delimited, `#` comment preamble,
  ragged rows) previously failed at probe time and never reached download.
  Clean comma-separated files keep DuckDB's inferred column types; only files
  that fail or mis-sniff fall back to all-VARCHAR, and are cast in SQL when
  analysed.
- Table names produced by the manual probe path no longer repeat the filename.
  `_url_identity` returns the URL hash only; readability comes from the title
  suffix. A file at `.../2026_05_NO2.csv` previously produced
  `t_2026_05_no2_csv_00175841_2026_05_no2_csv` and now produces
  `t_00175841_2026_05_no2_csv`.

  **Tables created before this change keep their old names and will not be
  found by the new scheme.** Drop and re-download them, or rename them with
  `ALTER TABLE ... RENAME TO ...`. Tables created by the CBS, CKAN, and Tavily
  adapters are unaffected — they key on catalogue IDs, not URLs.
- Generated table names now always carry a 12-character hash of the full
  dataset ID, not only when the ID happens to start with a digit. Two IDs
  that share a long common prefix (same host, near-identical paths) used to
  truncate to the same table name, and the second `CREATE OR REPLACE TABLE`
  silently overwrote the first.

  **This renames tables across every source** — manual URLs, CBS, CKAN, and
  Tavily alike — not only the URL-keyed path affected by the entry above.
  Existing tables won't be found under their old name; re-download or rename
  them manually.

### Fixed

- Persistent DuckDB loads now refuse to overwrite an existing target table.
- Table creation and validation now run atomically, so a failed load rolls back
  without leaving a partial table or persistent staging artifact.
- Mis-sniffed CSVs are detected reliably. The previous check looked only for
  generic `column0`, `column1` names; the common real-world failure is a file
  collapsing into a single column named after its first physical line, which
  went undetected.
- Directory listings are fetched once per level instead of twice.
- CKAN and Tavily dataset probing now shares the same CSV-dialect detection
  used by downloading. Both previously called `read_csv_auto` directly, so a
  semicolon-delimited or comment-preamble file found via a CKAN catalogue or
  a Tavily web search could probe with garbled columns even though the
  identical file downloaded correctly afterwards.
- Analysing probe results with `--local` (Ollama only) no longer requires
  `ANTHROPIC_API_KEY` to be set. The Claude client used to be constructed at
  import time regardless of which model you asked for.

## [0.1.0]

First working version, predating this changelog. Manual URL probing, agentic
discovery through source profiles, DuckDB loading behind explicit download
consent, source adapters for CBS, CKAN, and Tavily, and freshness, licence, and
budget checks.
