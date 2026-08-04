  -A Python tool that probes, discovers, and downloads open datasets using DuckDB httpfs and agentic AI. Supports both cloud (Claude) and local (Ollama) models.
  -
  ----
  -
  -## How it works
  -
  -The project has two main entry points:
  -
  -### 1. `run.py` — Manual dataset prober
  -Probe specific dataset URLs interactively or from a JSON file. DuckDB queries each URL directly via httpfs — no file is downloaded until you choose to. Optionally runs a Claude or local
  model analysis on the results.
  -
  -### 2. `dataset_agent.py` — Agentic dataset discovery
  -Give a natural language prompt and let the agent autonomously find, evaluate, and download datasets. Uses a profile-drive architecture to search the right sources for your geography and
  topic. Tracks cost per API call and enforces freshness, license, and budget rules.
  -
  ----
  -
  -## Agentic Architecture
  -
  -```
  -User prompt
  -     ↓
  -Prompt Interpreter (Claude API) — classifies intent, selects profiles, sets per-profile objectives
  -     ↓
  -User confirms plan (pre-flight, before any budget is spent)
  -     ↓
  -Orchestrator — manages sequential profile execution with compact handoff summaries
  -     ↓
  -For each profile:
  -  ├── Config Loader — loads profile YAML (sources, budget, scope, license rules)
  -  ├── Tool Factory — instantiates CBS, CKAN, Tavily tools
  -  ├── Agent Loop (Claude) — searches, fetches, checks freshness, downloads
  -  ├── Handoff Summary — compact context passed to next profile (~300 tokens, not full history)
  -  └── Early stop — halts when ALL profile objectives are met
  -     ↓
  -Aggregated results + per-profile cost breakdown
  -```
  -
  -### Profiles
  -Profiles live in `config/profiles/` — no hardcoded values in code:
  -
  -| Profile | Sources | Use for |
  -|---|---|---|
  -| `dutch_government` | CBS, data.overheid.nl, Den Haag Open Data | Dutch official statistics |
  -| `us_government` | data.gov (CKAN), Census, CMS, BLS, HHS | US federal open data |
  -| `eu_open_data` | EU Open Data Portal, Eurostat | EU-wide statistics |
  -| `global` | Tavily web search | Any country (with cost warning) |
  -
  -All budget limits, trusted domains, blocked sources, and license rules are configured per profile. CLI flags override profile defaults — the end-user controls everything.
  -
  -### Tools
  -Each tool implements a standard interface (`search`, `fetch`, `download`):
  -- **CBSTool** — CBS OData catalog, direct table fetch, cbsodata download
  -- **CKANTool** — Generic CKAN API (data.gov, overheid.nl, EU portal); blocked sources checked before probing
  -- **TavilyTool** — Web search + JS-rendered page extraction (fallback)
  -
  -CKAN and Tavily probe and download CSVs through the same `tools/base.py` helpers (dialect detection, HTML-landing-page guard, table naming), so a file behaves identically whichever tool
  found it.
  -
  ----
  -
  -## Components
  -
  -```
  -dataset-prober/
  -├── src/
  -│   ├── dataset_agent.py      — Agentic entry point
  -│   ├── prompt_interpreter.py — Claude-based intent classifier with per-profile objectives
  -│   ├── orchestrator.py       — Multi-profile execution, handoff summaries, early stop
  -│   ├── config_loader.py      — Profile YAML loader with scope enforcement
  -│   ├── tools/
  -│   │   ├── base.py           — DataSourceTool interface, DatasetResult, and the shared
  -│   │   │                        CSV probe/download helpers every tool routes through
  -│   │   ├── cbs_tool.py       — CBS Statistics Netherlands
  -│   │   ├── ckan_tool.py      — Generic CKAN (data.gov, overheid.nl)
  -│   │   └── tavily_tool.py    — Web search fallback
  -│   ├── run.py                — Manual prober entry point
  -│   ├── prober.py             — DuckDB httpfs prober + downloader
  -│   ├── agent.py              — Claude/Ollama dataset analysis
  -│   └── crawler.py            — Web crawler + directory-listing (autoindex) descent
  -├── config/
  -│   └── profiles/
  -│       ├── dutch_government.yaml
  -│       ├── us_government.yaml
  -│       ├── eu_open_data.yaml
  -│       └── global.yaml
  -├── tests/
  -│   ├── conftest.py                           — shared fixtures
  -│   ├── unit/
  -│   │   ├── test_pure_functions.py            — SQL-injection binding, table naming, freshness, license grade, pricing
  -│   │   ├── test_orchestrator.py              — early stop, handoff, evaluate_result
  -│   │   ├── test_prompt_interpreter.py        — JSON parsing, fallback, objectives
  -│   │   ├── test_expand_directories.py        — autoindex pre-pass: pass-through, one fetch per level
  -│   │   └── test_resolve_directory.py         — directory descent against real RIVM autoindex markup
  -│   ├── integration_light/
  -│   │   └── test_config_loader.py              — profile loading, scope, budget
  -│   └── mocked/
  -│       └── test_prober.py                    — ProbeResult structure, save_results
  -├── output/
  -│   ├── datasets.duckdb       — Downloaded datasets
  -│   ├── agent_results.json    — Last agent run results
  -│   └── probe_results.json    — Last prober run results
  -├── .pre-commit-config.yaml   — lint/format hooks (ruff, whitespace, private-key check)
  -└── pyproject.toml            — pinned dependencies, pytest config, ruff config
  -```
  -
  ----
  -
  -## Stages (manual prober)
  -
  -- **Stage 1** — DuckDB prober: checks availability, row counts, schema
  -- **Stage 2** — Claude/Ollama analysis: plain-language readiness assessment
  -- **Stage 3** — Web crawler: finds embedded dataset links on webpages
  -- **Stage 4** — Download to DuckDB: loads selected datasets for local SQL querying
  -
  ----
  -
  -## Known Limitations
  -
  -- **SSA.gov** blocks all server-side HTTP access — excluded from `us_government` profile; use Census, CMS, BLS instead
  -- **CBS OData** returns JSON, not CSV — httpfs cannot probe OData endpoints directly (CBSTool handles this natively)
  -- **JavaScript-rendered portals** (e.g. CBS Statline, Socrata) handled via Tavily extraction; some remain inaccessible
  -- **denhaag.incijfers.nl** blocks automated access (robots.txt) — excluded from all profiles
  -
  ----
  -
  -## Usage
  -
  -### Agentic discovery
  +`dataset-prober` is a functional pre-release Python prototype for finding open-data
  +resources, inspecting their structure with DuckDB and optionally loading selected data
  +into a local DuckDB database.
  +
  +It is not yet a stable release. In particular, the current `main` branch does not enforce
  +all of the classification, consent, URL-safety, overwrite-protection, provenance and
  +installation contracts planned for v0.1. Read [Current limitations](#current-limitations)
  +before using it with untrusted URLs or an existing database.
  +
  +## Why this project exists
  +
  +Finding a web page that mentions data is not the same as finding a queryable dataset.
  +Search results frequently lead to reports, PDFs, documentation, API specifications,
  +catalog records or landing pages. A useful discovery workflow has to continue from the
  +topic-level search result to a concrete resource, inspect that resource, and retain the
  +evidence needed to explain what was found.
  +
  +This project explores that workflow with Python, agentic AI, catalog adapters and DuckDB:
  +
  +1. discover candidate resources from a question or a supplied URL;
  +2. inspect candidates for rows, columns, types and sample values;
  +3. report the inspection result for human review; and
  +4. optionally use SQL to load an approved, supported resource into DuckDB.
  +
  +Reports, PDFs, documentation and landing pages can be useful evidence or pointers, but
  +they are not verified datasets. A resource should only be called a verified dataset after
  +deterministic inspection has established that it contains a supported, queryable
  +structure. The current prototype does not yet enforce that rule reliably end to end.
  +
  +## Discovery and verification are different
  +
  +The agentic workflow uses Claude to interpret a natural-language request, choose source
  +profiles, plan searches and call source tools. CBS, CKAN and Tavily integrations can then
  +return candidate resources. Model output, search snippets, filenames and catalog metadata
  +are discovery evidence; none of them prove that a resource is a dataset.
  +
  +The deterministic part uses DuckDB SQL to try to read CSV-like resources and report row
  +counts, column names, DuckDB types and sample records. The CBS adapter uses the CBS OData
  +catalog and `cbsodata` for table retrieval. These observations are stronger evidence than
  +an AI recommendation, but the current acceptance checks can still produce false positives.
  +Treat the current `ok` and `probed` statuses as prototype inspection results, not as a
  +v0.1 verification guarantee.
  +
  +## What works today
  +
  +- The manual prober accepts named URLs interactively or from JSON, attempts a DuckDB
  +  structural probe, displays the result and asks which successful probes to load.
  +- Apache/nginx-style directory listings can be explored interactively before probing a
  +  concrete file.
  +- The crawler follows relevant same-domain pages and collects links whose filenames look
  +  like data resources.
  +- The agentic command can interpret a request, choose bundled geographic source profiles,
  +  search CBS and CKAN catalogs, use Tavily as a web-search fallback, fetch candidates and
  +  present an aggregated result.
  +- CSV-like resources can be probed and loaded with DuckDB `httpfs`. CBS tables have a
  +  separate loader through `cbsodata`. These are the only implemented loading paths.
  +- Manual probe results and agent results are written as JSON. Optional post-probe analysis
  +  can use Claude or a local Ollama model.
  +- Offline unit, fixture-based and mocked tests cover selected parsing, configuration,
  +  orchestration, consent-gate, URL-guard, CSV-dialect and probing behavior. CI runs Ruff
  +  checks and the tests not marked `integration` on Python 3.12.
  +
  +## Current commands
  +
  +The project currently supports Python 3.12. For development from a checkout:

   ```bash
  -python src/dataset_agent.py
  +python -m pip install -e ".[dev]"

  ## -With options:
  -bash -python src/dataset_agent.py --timeout 15 --max-searches 8 --download -python src/dataset_agent.py --profile dutch_government -python src/dataset_agent.py --list-profiles -

  ## -All CLI flags override profile defaults:

   -      Flag                Description
  ━━━━━  ━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   -      --timeout N         Run timeout in minutes
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --max-searches N    Maximum catalog searches
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --max-crawls N      Maximum page extractions
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --max-probes N      Maximum dataset probes
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --max-tokens N      Maximum tokens per Claude call
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --download          Allow agent to download to DuckDB
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --profile NAME      Force a specific profile (skip auto-detection)
  ─────  ──────────────────  ────────────────────────────────────────────────
   -      --list-profiles     Show available profiles and exit


  ## -### Manual prober

  ## -bash -python src/run.py -python src/run.py --file sources.json -python src/run.py --analyze -python src/run.py --analyze --local -python src/run.py --analyze --local --model
  gemma3:12b -

  ———


  -## Setup
  +The editable install exposes three console commands:

  -pip install -e ".[dev]"
  +# Agent-assisted candidate discovery
  +dataset-prober --list-profiles
  +dataset-prober
  +
  +# Manual URL probing and optional analysis
  +dataset-prober-probe
  +dataset-prober-probe --file sources.json
  +dataset-prober-probe --analyze
  +dataset-prober-probe --analyze --local --model qwen2.5-coder:3b
  +
  +# Same-domain link crawling followed by probing
  +dataset-prober-crawl

  -Add your API keys to a .env file:
  - -ANTHROPIC_API_KEY=your-key-here -TAVILY_API_KEY=your-key-here -DATAGOV_API_KEY=DEMO_KEY -
  +The JSON input for dataset-prober-probe --file is a list of named URLs:

  -### Local model support (optional)
  -Install Ollama (https://ollama.com) and pull a model:
  -bash -ollama pull qwen2.5-coder:3b -ollama pull gemma3:12b +json
  +[

  - {
  - "name": "Example observations",
  - "url": "https://data.example.org/observations.csv"
  - }
    +]


  ----
  -
  -## Running tests
  +Agent-assisted discovery and Claude analysis require `ANTHROPIC_API_KEY`. Tavily fallback
  +search requires `TAVILY_API_KEY`. The optional local analysis path expects an Ollama server
  +at `http://localhost:11434` and the requested model to be available there. Environment
  +variables can be placed in a repository-root `.env` file for the current checkout-based
  +workflow.
  +
  +The manual workflow writes `output/probe_results.json` and, after an explicit interactive
  +selection, `output/datasets.duckdb`. The agentic workflow writes
  +`output/agent_results.json` and can write to the same DuckDB file. These fixed paths are a
  +current limitation, not a stable storage interface.
  +
  +## Current limitations
  +
  +Use the prototype on disposable outputs and review every candidate yourself.
  +
  +- Resource classification is incomplete. Extension checks, catalog metadata or a
  +  permissive CSV parse can mistake HTML, prose, an error response, an API specification or
  +  another non-dataset resource for structured data. PDFs, reports, documentation, search
  +  snippets and landing pages must not be reported or used as verified datasets.
  +- The implemented generic loader is CSV-oriented, while discovery can identify extensions
  +  such as JSON, Excel, Parquet and GeoJSON. Unsupported formats are not yet consistently
  +  stopped before they reach CSV probing or loading code.
  +- Agentic download authority is not fail closed. The current command accepts `--download`
  +  but also infers permission from words in the prompt; negated or ambiguous text can
  +  therefore enable download mode. The agent also lacks exact, explicit, per-resource
  +  selection. Do not rely on the agentic loading path where writes are unacceptable.
  +- DuckDB loading uses replacement semantics. Colliding table names or a failed validation
  +  after replacement can overwrite or remove existing data. There is no complete explicit
  +  overwrite policy or transactional preservation guarantee.
  +- A URL-safety guard exists and has unit tests, but application-controlled runtime fetches
  +  do not consistently pass through it. Redirect handling and SSRF protection are not an
  +  end-to-end guarantee, especially for transports whose DNS and redirect behavior is
  +  opaque to the application.
  +- Table names include shortened hashes, but table identity is not yet based on a complete,
  +  collision-resistant canonical source identity.
  +- Results capture some source metadata, rows, columns, samples, status, errors, licenses and
  +  estimated model usage, depending on the path. Retrieval history, checksums, consent,
  +  table mapping, terminal failures, model usage and costs are not yet recorded completely
  +  by one authoritative mechanism.
  +- Profile freshness, license, scope and budget instructions are partly configuration or
  +  model-prompt guidance rather than complete deterministic enforcement.
  +- Output filenames and several runtime paths are fixed relative to the checkout. Repeated
  +  runs can replace result files or leave stale artifacts that look current.
  +- The current test suite protects selected behaviors but does not globally prohibit
  +  network access, exercise every console workflow, or verify a fresh non-editable wheel.
  +
  +## Planned v0.1 stabilization
  +
  +The following are release contracts for the planned v0.1 stabilization work; they are not
  +claims about the current `main` branch:
  +
  +1. Reports, PDFs, documentation, search snippets and landing pages are never reported as
  +   verified datasets.
  +2. Verification requires deterministic evidence of queryable records, rows, columns,
  +   dimensions, features or another supported structured form.
  +3. Queryable but unsupported resources are reported as unsupported and are not downloaded
  +   or loaded.
  +4. Prompt text never grants download authority; consent and selection are explicit, exact,
  +   per resource and fail closed.
  +5. Validation or loading failures cannot silently overwrite or destroy existing DuckDB
  +   data, and overwriting follows an explicit policy.
  +6. Application-controlled source URLs pass through a centralized runtime safety boundary,
  +   including redirect validation and minimum SSRF protection; unsafe opaque transport
  +   paths are disabled when that protection cannot be demonstrated.
  +7. Source identity, queryability evidence, selection, consent, retrieval outcome, table
  +   mapping and failure status are recorded accurately.
  +8. Every relevant model call is included in usage and cost totals calculated by one
  +   authoritative mechanism.
  +9. Runtime paths and filenames are centrally owned and independent of the current working
  +   directory.
  +10. Environment-, source- and user-specific hardcoded values are removed or centralized;
  +    stable safety invariants remain fixed only with a documented reason.
  +11. A fresh non-editable wheel contains the complete package and exposes working console
  +    entry points.
  +12. Critical release contracts are protected by offline tests and enforced in CI.
  +
  +The bounded plan targets a release candidate by August 28, 2026, with August 29–31 reserved
  +for contract defects and release verification. Stabilization is focused on making the
  +existing discovery, verification and approved-loading workflow reliable; it does not add
  +new data formats, source adapters or product features.
  +
  +## Technical scope
  +
  +The repository is relevant to several complementary areas:
  +
  +- **Python:** packaged command-line workflows, source adapters, configuration profiles and
  +  orchestration.
  +- **DuckDB and SQL:** remote CSV inspection, schema and row-count queries, local analytical
  +  tables and sample-based analysis.
  +- **Testing:** pytest unit tests, fixture-based parser tests, mocked boundary tests and Ruff
  +  checks in GitHub Actions.
  +- **Agentic AI:** Claude-driven prompt interpretation, source selection and tool use, with
  +  optional Claude or Ollama summaries. AI assists discovery and explanation; deterministic
  +  code must own verification and loading policy for v0.1.
  +
  +## Development checks

  ```bash
  -pytest tests/ -v
  +ruff check .
  +ruff format --check .
  +pytest -m "not integration"

  -The full suite tests across unit, integration-light, and mocked layers. No real API calls in the test suite — all external services are mocked.
  +These are the checks currently run by CI. Passing them does not imply that the planned v0.1
  +contracts are complete.

  ———


  ## -## License Evaluation

  ## -The agent evaluates dataset licenses using CCREL/ODRL standards:

   -      License                Grade    Meaning
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━
   -      CC0 / Public Domain    A        Unrestricted use
  ─────  ─────────────────────  ───────  ──────────────────────
   -      CC-BY                  B        Attribution required
  ─────  ─────────────────────  ───────  ──────────────────────
   -      CC-BY-SA               B-       Share-alike required
  ─────  ─────────────────────  ───────  ──────────────────────
   -      CC-BY-NC               C        Non-commercial only
  ─────  ─────────────────────  ───────  ──────────────────────
   -      Unknown                ?        Verify before use


  ———


  ## -## Cost tracking

  ## -Every Claude API call reports tokens used and USD cost. Per-profile cost breakdown shown at session end:

  -```
  -📊 Session Cost Breakdown:

  - Interpreter: 1,102 tokens | $0.0086
  - Dutch Government: 19,996 tokens | $0.0786 | 4 calls
  - US Government: 45,000 tokens | $0.15 | 8 calls
  - ─────────────────────────────────────────
  - Total: 66,098 tokens | $0.2372 | 13 API calls
    -```


  ———


  ## -## Tools & Resources

  -- DuckDB Web Shell (https://shell.duckdb.org) — browser-based DuckDB for quickly testing CSV URLs
  -- DuckDB httpfs docs (https://duckdb.org/docs/extensions/httpfs.html) — reference for remote file querying
  -- CBS OData API (https://www.cbs.nl/nl-nl/onze-diensten/open-data/statline-als-open-data) — CBS Statistics Netherlands API
  -- Data.gov CKAN API (https://open.gsa.gov/api/datadotgov/) — US government open data catalog API
  -- Open Definition (https://opendefinition.org) — what "open" means for data
  -- Open Data Handbook (https://opendatahandbook.org) — practical guide to open data
  -- W3C Data License Best Practices (https://www.w3.org/TR/dwbp/#DataLicense) — licensing standards


  ———


  -## Acknowledgements
  +## License

  -Inspired by Jurjen van Genugten's demonstration of DuckDB httpfs for probing Dutch open datasets (None if his code has been used or duplicated).
  +See [LICENSE](LICENSE).
