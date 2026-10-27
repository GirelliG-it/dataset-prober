# Changelog

All notable changes to `dataset-prober` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

Entries describe what changed **for someone using the tool**, not what was done
to the source. For the latter, read `git log`.

## [Unreleased]

### Added

- Agent runs write a separate versioned `freshness-<run-UUID>.json` report with
  original observations, immutable evaluations and request/grounding diagnostics,
  including unevaluated evidence, handled timeouts and runs without candidates.
  Existing report paths are never overwritten. Candidate-summary and report
  writes are attempted independently; handled encoding/I/O failures return CLI
  status 2 without changing completed decisions. This is a run report, not a
  history service or a crash-recovery guarantee. See the
  [run-report contract](docs/v0.2-freshness-run-report.md).
- A standalone, pure freshness evaluator produces immutable evaluation records
  linked to existing observations. It uses caller-supplied IDs and UTC references,
  exact 100-nanosecond comparisons, explicit claim selection, scoped timezone
  rules and separate semantic applicability. Invalid, future, sanitized or
  insufficient evidence remains UNKNOWN; catalog metadata cannot establish
  dataset-content freshness. Fixed-offset date intervals are supported;
  named-zone interpretation is deferred. Agent checks now use this evaluator
  without timezone assumptions; no history storage or loading authorization is added.
  See the [evaluator policy](docs/v0.2-freshness-evaluation.md).
- Fetched CBS and CKAN candidates now include an immutable freshness observation
  with complete provider timestamp claims, field origins, resource identity, UTC
  collection time and collection outcome. Missing, null and wrong-type values
  remain distinguishable; sanitized evidence is marked explicitly. Object keys
  that collide after sanitization, including nested keys, cause capture to fail
  without retaining a partial claim or changing the provider payload. Collected
  metadata survives later sample failures, including fetch deadlines handled by
  the agent: the original observation is saved with a failed, non-loadable
  candidate while existing timeout handling is preserved. Existing summary dates
  are unchanged; operational freshness checks use the captured observation through
  the evaluator. Durable freshness history remains unimplemented. See the
  [freshness evidence contract](docs/v0.2-freshness-evidence.md).
- The bounded CBS metadata experiment accepts `--database PATH` to compare against
  persisted observations across runs. UUID-based observation IDs remain unchanged
  on save retries. Failed and blocked checks remain in history, while save errors
  are reported separately and return a nonzero exit code. Store-close failures
  retain completed observations, comparisons, and save outcomes for report writing
  and reconciliation, with a separate close diagnostic and exit code 2. The
  database parent must already exist; no default path or automatic retry is introduced.
- A standalone DuckDB monitoring store retains successful, failed, and blocked
  observations across normal close/reopen without changing comparison semantics.
  It accepts an explicit database path, preserves UTC microseconds and all
  evidence for supported representations, rejects confirmed duplicate IDs, and
  reports other constraint failures, invalid history, or unsupported formats as
  storage errors. Observations whose JSON round trip changes their values,
  including surrogate-pair strings, are rejected before insertion without
  altering history. Opening a store rejects
  incompatible column types, missing required NOT NULL constraints, or a missing
  observation ID primary key without changing existing schema or data. Appends
  are atomic and support compatible tables with reordered columns. Crash or
  power-loss recovery has not been verified.
- An opt-in supervised CBS metadata experiment saves two bounded metadata
  observations and their monitoring comparison locally, without retrieving
  dataset records or authorizing ingestion.

### Changed

- The source checkout enables automatic selection of the Dutch government profile,
  using the existing CBS StatLine and data.overheid.nl catalogs. The proposed plan
  requires confirmation; explicit `--profile dutch_government` selection remains
  available. US, EU, Global and Tavily discovery remain disabled. Existing budgets,
  deterministic verification and per-resource loading authorization are unchanged.
- Agent freshness checks now evaluate immutable evidence from a uniquely matched
  fetched candidate, validating its fetch association, source/catalog, resource,
  collector version and selected field. They never fall back to a legacy summary
  date. Current CBS unverified and CKAN catalog-metadata claims yield UNKNOWN
  dataset-content freshness, with detailed evaluation reasons. Each grounded
  check retains its own evaluation ID, reference time and threshold. The check accepts exactly
  `source`, `dataset_id` and `max_days_old`; any missing or extra field,
  including a supplied `last_updated`, is rejected with
  `unexpected_tool_input`. Missing or ambiguous candidates and unusable dates
  return an unknown verdict. Freshness results include stable reason codes
  (`candidate_not_inspected`, `candidate_identity_ambiguous`,
  `freshness_unknown`, `freshness_pass`, `freshness_fail`) and do not change
  loading eligibility.

### Fixed

- Malformed agent tool arguments now return structured errors before searching,
  probing, or requesting load consent, allowing earlier candidates and completed
  model-call usage to remain available when the run ends. All four tools validate
  object shape, required and permitted fields, and field values. Identifiers are
  nonempty strings and are matched without trimming or coercion; freshness age
  limits must be nonnegative integers (zero is valid; booleans are rejected).
  Freshness shape errors retain `unexpected_tool_input`; invalid field values use
  `invalid_tool_input`. Search result caps and the disabled-loading guard remain
  in force, and valid load requests still require exact resource consent.
- Profile interpretation now explicitly requests the field types accepted by its
  validator, including `high`, `medium`, or `low` confidence strings, and supplies
  a matching JSON example. Malformed responses still stop before profile execution;
  numeric confidence values are not converted or retried automatically.
- Resource assessment cleanup no longer deadlocks when garbage collection
  runs during classifier evidence registration. This prevents intermittent
  hangs during classification and test fixture setup.

## [0.1.0] - 2026-09-04

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

- The Dutch profile remains manual-only and now exposes the retained CBS StatLine OData v3
  route plus the official data.overheid.nl CKAN v3 route at `/data/api/3`; neither catalog
  requires an API key. The data.overheid route locally filters a bounded package response using
  explicit CSV resource metadata instead of the incompatible literal CKAN CSV query filter.
  Unsupported `opendatasoft_portals` configuration now fails closed. Offline contract checks,
  a bounded catalog compatibility audit, and controlled read-only catalog/API health checks pass.
  A loader-compatible RDW CSV was retrieved within a 32 MiB ceiling and deterministically inspected
  as 6,650 non-empty tabular rows. Query-driven resources without `.csv` path evidence remain
  report-only; persistent loading was not performed, and the Dutch profile remains manual-only.
- Profile-agent runs now enforce explicit per-search result and model-call ceilings, a
  between-call reported-token stop threshold, a per-call requested output cap and monotonic
  request deadlines. CBS and CKAN discovery and inspection operations now cap configured
  source timeouts by freshly remaining run time between sequential operations. Output-limited
  model responses return partial results without retrying or executing truncated tool calls.
  The unused crawl budget was removed, and continuing after a timeout no longer resets any non-time budget.
  Pre-run output labels thresholds as a budget-based planning estimate; final output reports
  token usage returned by completed model responses and locally observed attempted, completed,
  and timed-out call outcomes.
- Bundled profiles now load through the immutable static profile contract and fail closed
  according to explicit lifecycle status: Dutch discovery is manual-only; US and EU discovery
  remain disabled until their promised transports are repaired and certified; Global discovery
  remains disabled pending a new safe discovery design and certification.
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

- The installed `dataset-prober-crawl --help` command now exits normally before
  runtime output-path resolution, so help works outside a source checkout without
  `DATASET_PROBER_OUTPUT`.
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

## [0.1.0-pre-restructure] - 2026-07-24

First working version, predating this changelog. Manual URL probing, agentic
discovery through source profiles, DuckDB loading behind explicit download
consent, source adapters for CBS, CKAN, and Tavily, and freshness, licence, and
budget checks.
