# dataset-prober

`dataset-prober` is a functional pre-release Python prototype for finding open-data
resources, deterministically assessing inspected content and optionally loading an
explicitly approved resource into a local DuckDB database.

It is not yet a stable release. The current pre-release implements bounded classification,
consent, URL-safety and non-destructive loading contracts for its supported routes, but
provenance, packaging, CI and other v0.1 release work remain incomplete. Read
[Current limitations](#current-limitations) before using it with untrusted URLs or an
existing database.

## Why this project exists

Finding a web page that mentions data is not the same as finding a queryable dataset.
Search results frequently lead to reports, PDFs, documentation, API specifications,
catalog records or landing pages. A useful discovery workflow has to continue from the
topic-level search result to a concrete resource, inspect that resource, and retain the
evidence needed to explain what was found.

This project explores that workflow with Python, agentic AI, catalog adapters and DuckDB:

1. discover candidate resources from a question or a supplied URL;
2. inspect candidates for rows, columns, types and sample values;
3. report the inspection result for human review; and
4. optionally use SQL to load an approved, supported resource into DuckDB.

Reports, PDFs, documentation and landing pages can be useful evidence or pointers, but
they are not verified datasets. A resource should only be called a verified dataset after
deterministic inspection has established that it contains a supported, queryable
structure with observations. The current prototype enforces that rule for its supported
CSV and CBS/OData loading paths; other candidates remain visible as report-only resources.

## Discovery and verification are different

When an enabled profile is available, the agentic workflow can use Claude to interpret a
natural-language request and choose among the enabled descriptors. In the current degraded
mode there are no enabled profiles; the Dutch manual-only profile can instead be named
explicitly to plan CBS searches and return candidates. Model output, search snippets,
filenames and catalog metadata are discovery evidence; none of them prove that a resource
is a dataset.

The deterministic part retrieves enabled resources through a guarded transport and
classifies the inspected content before using DuckDB SQL to report row counts, column
names, types and sample records. CBS/OData records are assessed through the same central
assessment model. Eligibility is derived from a coherent, classifier-issued assessment;
an `ok`, `found` or `probed` lifecycle status does not make a resource load-eligible.

Classifier-issued assessment evidence is bound to the canonical identity of the inspected
candidate. One resource's assessment cannot authorize another resource, and model output
cannot replace deterministic verification.

## What works today

- The manual prober accepts named URLs interactively or from JSON, classifies inspected
  content, displays both verified and report-only results, and offers only verified,
  load-eligible resources for exact selection when `--download` is present.
- Apache/nginx-style directory listings can be explored interactively before probing a
  concrete file.
- The crawler follows relevant same-domain pages and collects links whose filenames look
  like data resources.
- The agentic command can run the Dutch profile only when it is selected explicitly. That
  manual-only profile exposes the supported CBS StatLine OData v3 catalog. Automatic
  profile selection is temporarily unavailable because no bundled profile is enabled.
- Direct resources remain available through the non-agentic manual prober. Tavily
  provider-side search and extraction remain disabled because their source-fetch transport
  is opaque to the application.
- Supported CSV resources are retrieved through the application-owned guarded HTTP transport
  and probed or loaded from temporary local copies with DuckDB. CBS tables use the same
  guarded transport for OData catalogue, sample and paginated dataset retrieval.
- Guarded catalog responses are limited to 32 MiB per response and dataset retrieval is
  limited to 512 MiB per load attempt. Sensitive URL credentials, query values and fragments
  are replaced by sanitized identities in console output, model-facing results and saved JSON.
- Every load requires `--download`, exact registered-resource selection and per-resource
  `y`/`yes` consent for the displayed destination and table. The actual payload is retrieved
  and assessed again before persistent DuckDB access. Table creation is transactional, and
  an existing target table is never overwritten.
- Manual probe results and agent results are written as JSON. Optional post-probe analysis
  can use Claude or a local Ollama model.
- Offline unit, fixture-based and mocked tests cover selected parsing, configuration,
  orchestration, consent-gate, URL-guard, CSV-dialect and probing behavior. CI runs Ruff
  checks and the tests not marked `integration` on Python 3.12.

## Current commands

The project currently supports Python 3.12. For development from a checkout:

```bash
python -m pip install -e ".[dev]"
```

The editable install exposes three console commands:

```bash
# Agent-assisted candidate discovery
dataset-prober --list-profiles
dataset-prober --profile dutch_government

# Manual URL probing and optional analysis
dataset-prober-probe
dataset-prober-probe --file sources.json
dataset-prober-probe --file sources.json --download
dataset-prober-probe --analyze
dataset-prober-probe --analyze --local --model qwen2.5-coder:3b

# Same-domain link crawling followed by probing
dataset-prober-crawl
```

The JSON input for `dataset-prober-probe --file` is a list of named URLs:

```json
[
  {
    "name": "Example observations",
    "url": "https://data.example.org/observations.csv"
  }
]
```

`--download` only enables the loading workflow. The user must still select an exact
load-eligible resource and answer `y` or `yes` to the consent prompt for that resource's
displayed destination and table.

### Bundled profile availability

Profile lifecycle is validated when configuration is loaded. `enabled` profiles may be
selected automatically or explicitly, `manual_only` profiles require an explicit name,
and `disabled` profiles cannot be selected or run. The current pre-release deliberately
has no enabled profile:

| Profile | Status | Active catalog |
| --- | --- | --- |
| `dutch_government` | `manual_only` | CBS StatLine OData v3 (`cbs`) |
| `us_government` | `disabled` | None; Data.gov v4 needs a source-specific adapter |
| `eu_open_data` | `disabled` | None; current EU routes lack compatible registered adapters |
| `global` | `disabled` | None; no supported, certified safe discovery transport |

Running `dataset-prober` without `--profile` therefore exits locally without constructing
the prompt interpreter or agent tools. `dataset-prober --profile dutch_government` is the
only bundled agentic route currently admitted and is clearly identified as manual-only.
This temporary profile restriction does not affect `dataset-prober-probe`: a user can still
supply a concrete URL directly for guarded deterministic inspection.

Explicit Dutch agent-assisted discovery and Claude analysis require `ANTHROPIC_API_KEY`.
Tavily provider search and extraction remain disabled even when `TAVILY_API_KEY` is set.
The optional local analysis path expects an Ollama server
at `http://localhost:11434` and the requested model to be available there. Environment
variables can be placed in a repository-root `.env` file for the current checkout-based
workflow.

By default, a checkout-based manual workflow writes `output/probe_results.json` and an
approved load uses `output/datasets.duckdb`. The agentic workflow writes
`output/agent_results.json` and can use the same DuckDB file. `DATASET_PROBER_OUTPUT` can
select another output directory, but artifact names are fixed and the console commands do
not yet expose the existing output-directory setting as a complete installed-runtime
interface.

## Current limitations

Use the prototype on disposable outputs and review every candidate yourself.

- Deterministic classification is intentionally narrow and conservative. Supported CSV and
  CBS/OData content must be non-empty and structurally queryable. Documentary, HTML, empty,
  ambiguous, unsupported, contradictory and erroneous content remains report-only. There
  is no PDF extraction, semantic document analysis or general-purpose format classifier.
- The generic loader is CSV-oriented. Discovery may identify JSON, Excel, Parquet, GeoJSON
  or other machine-readable resources, but those formats remain report-only and have no
  v0.1 loader.
- Loading requires an explicit `--download` flag, exact inspected-resource selection and
  per-resource `y`/`yes` consent bound to the planned destination and table. Prompt wording
  does not grant download authority, and `--download` alone is not authorization.
- DuckDB loads refuse an existing target table. A new table is created and validated in one
  transaction; parsing or validation failures roll back without replacing, dropping or
  altering an existing table. Explicit overwrite, append and upsert modes are not supported.
- Enabled application-controlled source fetches on the v0.1 stabilization branch use a
  centralized transport that validates URL syntax, DNS answers and every redirect, ignores
  environment proxies, connects to a validation-time address, permits only HTTP port 80 and
  HTTPS port 443, and rejects HTTPS-to-HTTP redirects. One monotonic deadline covers DNS,
  address attempts, TLS, redirects, headers and body reads. Opaque source-fetch paths are
  disabled rather than described as protected. This is a bounded application control, not a
  claim that third-party services or malicious code in the same process are SSRF-safe. A DNS
  worker that exceeds the caller's deadline can finish in the background, with concurrency
  bounded by the transport.
- Planned table names use a collision-resistant digest of the complete source-specific
  identity without placing raw sensitive URL values in the identifier.
- Results capture some source metadata, rows, columns, samples, status, errors, licenses and
  estimated model usage, depending on the path. Retrieval history, checksums, consent,
  table mapping, terminal failures, model usage and costs are not yet recorded completely
  by one authoritative mechanism.
- Fresh load-time assessment verifies that the retrieved payload remains eligible; it does
  not prove byte-for-byte identity with the initially inspected payload. Checksums and
  durable retrieval identity remain deferred.
- Profile-agent run budgets deterministically enforce search-count, per-search result-count,
  probe-count and model-call ceilings, and every profile-agent model request uses the
  remaining time on a monotonic deadline. `max_tokens` is the requested output cap for one
  call; a response stopped at that limit returns the partial results without an automatic
  retry or execution of truncated tool requests. `max_total_tokens` is a between-call
  reported-token stop threshold: once cumulative reported usage reaches it, no next call is
  made. A completed call may cross that threshold because its input usage is learned only
  afterward, and a timed-out attempt may have unknown server-side usage. Before each CBS or
  CKAN discovery request, the configured source timeout is capped by freshly remaining run
  time, and sequential inspection steps recheck that same monotonic deadline. No subsequent
  discovery/inspection source operation or model operation starts after deadline exhaustion
  is observed. Synchronous HTTP may not cancel at the mathematically exact deadline if data
  continues arriving. A load authorized by explicit consent retains its separate
  loading-timeout contract and is not governed by the profile-agent run deadline. Profile
  freshness, licensing, and
  geographic-scope instructions guide model selection, but do not prove semantic relevance
  or complete policy compliance.
- Output paths are centrally resolved, but artifact filenames and some runtime assumptions
  remain checkout-oriented. Repeated runs can replace result files or leave stale artifacts
  that look current.
- The current test suite protects selected behaviors but does not globally prohibit
  network access, exercise every console workflow, or verify a fresh non-editable wheel.
  Its patch-heavy test design also remains scheduled for separate pre-release work.

## Planned v0.1 stabilization

The following remain v0.1 release contracts. The current pre-release implements the
classification, loading-authorization, guarded-transport and non-destructive persistence
boundaries described above for enabled routes. That does not make v0.1 complete: each
contract remains subject to release verification, and provenance, cost, runtime,
packaging and CI work below is still incomplete.

1. Reports, PDFs, documentation, search snippets and landing pages are never reported as
   verified datasets.
2. Verification requires deterministic evidence of queryable records, rows, columns,
   dimensions, features or another supported structured form.
3. Queryable but unsupported resources are reported as unsupported and are not downloaded
   or loaded.
4. Prompt text never grants download authority; consent and selection are explicit, exact,
   per resource and fail closed.
5. Existing DuckDB tables are never overwritten; validation or loading failures roll back
   without leaving a partial table.
6. Application-controlled source URLs pass through a centralized runtime safety boundary,
   including redirect validation and minimum SSRF protection; unsafe opaque transport
   paths are disabled when that protection cannot be demonstrated.
7. Source identity, queryability evidence, selection, consent, retrieval outcome, table
   mapping and failure status are recorded accurately.
8. Every relevant model call is included in usage and cost totals calculated by one
   authoritative mechanism.
9. Runtime paths and filenames are centrally owned and independent of the current working
   directory.
10. Environment-, source- and user-specific hardcoded values are removed or centralized;
    stable safety invariants remain fixed only with a documented reason.
11. A fresh non-editable wheel contains the complete package and exposes working console
    entry points.
12. Critical release contracts are protected by offline tests and enforced in CI.

The bounded plan targets a release candidate by August 28, 2026, with August 29–31 reserved
for contract defects and release verification. Stabilization is focused on making the
existing discovery, verification and approved-loading workflow reliable; it does not add
new data formats, source adapters or product features.

## Technical scope

The repository is relevant to several complementary areas:

- **Python:** packaged command-line workflows, source adapters, configuration profiles and
  orchestration.
- **DuckDB and SQL:** guarded local CSV inspection, schema and row-count queries, local
  analytical tables and sample-based analysis.
- **Testing:** pytest unit tests, fixture-based parser tests, mocked boundary tests and Ruff
  checks in GitHub Actions.
- **Agentic AI:** Claude-driven prompt interpretation, source selection and tool use, with
  optional Claude or Ollama summaries. AI assists discovery and explanation; deterministic
  code owns verification and loading policy.

## Development checks

```bash
ruff check .
ruff format --check .
pytest -m "not integration"
```

These are the checks currently run by CI. Passing them does not imply that the planned v0.1
contracts are complete.

## Acknowledgements

Inspired by [Jurjen van Genugten](https://www.linkedin.com/in/jurjenvangenugten/)'s
demonstration of using DuckDB `httpfs` to probe Dutch open datasets. (None of his code was used or copied.)

## License

See [LICENSE](LICENSE).
