# dataset-prober

`dataset-prober` is a functional pre-release Python prototype for finding open-data
resources, inspecting their structure with DuckDB and optionally loading selected data
into a local DuckDB database.

It is not yet a stable release. In particular, the current `main` branch does not enforce
all of the classification, consent, URL-safety, overwrite-protection, provenance and
installation contracts planned for v0.1. Read [Current limitations](#current-limitations)
before using it with untrusted URLs or an existing database.

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
structure. The current prototype does not yet enforce that rule reliably end to end.

## Discovery and verification are different

The agentic workflow uses Claude to interpret a natural-language request, choose source
profiles, plan searches and call source tools. CBS, CKAN and Tavily integrations can then
return candidate resources. Model output, search snippets, filenames and catalog metadata
are discovery evidence; none of them prove that a resource is a dataset.

The deterministic part uses DuckDB SQL to try to read CSV-like resources and report row
counts, column names, DuckDB types and sample records. The CBS adapter uses the CBS OData
catalog and `cbsodata` for table retrieval. These observations are stronger evidence than
an AI recommendation, but the current acceptance checks can still produce false positives.
Treat the current `ok` and `probed` statuses as prototype inspection results, not as a
v0.1 verification guarantee.

## What works today

- The manual prober accepts named URLs interactively or from JSON, attempts a DuckDB
  structural probe, displays the result and asks which successful probes to load.
- Apache/nginx-style directory listings can be explored interactively before probing a
  concrete file.
- The crawler follows relevant same-domain pages and collects links whose filenames look
  like data resources.
- The agentic command can interpret a request, choose bundled geographic source profiles,
  search CBS and CKAN catalogs, use Tavily as a web-search fallback, fetch candidates and
  present an aggregated result.
- CSV-like resources can be probed and loaded with DuckDB `httpfs`. CBS tables have a
  separate loader through `cbsodata`. These are the only implemented loading paths.
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
dataset-prober

# Manual URL probing and optional analysis
dataset-prober-probe
dataset-prober-probe --file sources.json
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

Agent-assisted discovery and Claude analysis require `ANTHROPIC_API_KEY`. Tavily fallback
search requires `TAVILY_API_KEY`. The optional local analysis path expects an Ollama server
at `http://localhost:11434` and the requested model to be available there. Environment
variables can be placed in a repository-root `.env` file for the current checkout-based
workflow.

The manual workflow writes `output/probe_results.json` and, after an explicit interactive
selection, `output/datasets.duckdb`. The agentic workflow writes
`output/agent_results.json` and can write to the same DuckDB file. These fixed paths are a
current limitation, not a stable storage interface.

## Current limitations

Use the prototype on disposable outputs and review every candidate yourself.

- Resource classification is incomplete. Extension checks, catalog metadata or a
  permissive CSV parse can mistake HTML, prose, an error response, an API specification or
  another non-dataset resource for structured data. PDFs, reports, documentation, search
  snippets and landing pages must not be reported or used as verified datasets.
- The implemented generic loader is CSV-oriented, while discovery can identify extensions
  such as JSON, Excel, Parquet and GeoJSON. Unsupported formats are not yet consistently
  stopped before they reach CSV probing or loading code.
- Agentic download authority is not fail closed. The current command accepts `--download`
  but also infers permission from words in the prompt; negated or ambiguous text can
  therefore enable download mode. The agent also lacks exact, explicit, per-resource
  selection. Do not rely on the agentic loading path where writes are unacceptable.
- DuckDB loading uses replacement semantics. Colliding table names or a failed validation
  after replacement can overwrite or remove existing data. There is no complete explicit
  overwrite policy or transactional preservation guarantee.
- A URL-safety guard exists and has unit tests, but application-controlled runtime fetches
  do not consistently pass through it. Redirect handling and SSRF protection are not an
  end-to-end guarantee, especially for transports whose DNS and redirect behavior is
  opaque to the application.
- Table names include shortened hashes, but table identity is not yet based on a complete,
  collision-resistant canonical source identity.
- Results capture some source metadata, rows, columns, samples, status, errors, licenses and
  estimated model usage, depending on the path. Retrieval history, checksums, consent,
  table mapping, terminal failures, model usage and costs are not yet recorded completely
  by one authoritative mechanism.
- Profile freshness, license, scope and budget instructions are partly configuration or
  model-prompt guidance rather than complete deterministic enforcement.
- Output filenames and several runtime paths are fixed relative to the checkout. Repeated
  runs can replace result files or leave stale artifacts that look current.
- The current test suite protects selected behaviors but does not globally prohibit
  network access, exercise every console workflow, or verify a fresh non-editable wheel.

## Planned v0.1 stabilization

The following are release contracts for the planned v0.1 stabilization work; they are not
claims about the current `main` branch:

1. Reports, PDFs, documentation, search snippets and landing pages are never reported as
   verified datasets.
2. Verification requires deterministic evidence of queryable records, rows, columns,
   dimensions, features or another supported structured form.
3. Queryable but unsupported resources are reported as unsupported and are not downloaded
   or loaded.
4. Prompt text never grants download authority; consent and selection are explicit, exact,
   per resource and fail closed.
5. Validation or loading failures cannot silently overwrite or destroy existing DuckDB
   data, and overwriting follows an explicit policy.
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
- **DuckDB and SQL:** remote CSV inspection, schema and row-count queries, local analytical
  tables and sample-based analysis.
- **Testing:** pytest unit tests, fixture-based parser tests, mocked boundary tests and Ruff
  checks in GitHub Actions.
- **Agentic AI:** Claude-driven prompt interpretation, source selection and tool use, with
  optional Claude or Ollama summaries. AI assists discovery and explanation; deterministic
  code must own verification and loading policy for v0.1.

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
