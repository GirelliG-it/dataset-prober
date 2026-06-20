# dataset-prober

A Python tool that probes, discovers, and downloads open datasets using DuckDB httpfs and agentic AI. Supports both cloud (Claude) and local (Ollama) models.

---

## How it works

The project has two main entry points:

### 1. `run.py` — Manual dataset prober
Probe specific dataset URLs interactively or from a JSON file. DuckDB queries each URL directly via httpfs — no file is downloaded until you choose to. Optionally runs a Claude or local model analysis on the results.

### 2. `dataset_agent.py` — Agentic dataset discovery
Give a natural language prompt and let the agent autonomously find, evaluate, and download datasets. Uses a profile-driven architecture to search the right sources for your geography and topic. Tracks cost per API call and enforces freshness, license, and budget rules.

---

## Agentic Architecture

```
User prompt
     ↓
Prompt Interpreter (Claude API) — classifies intent, selects profiles
     ↓
User confirms plan
     ↓
Orchestrator — manages sequential profile execution
     ↓
For each profile:
  ├── Config Loader — loads profile YAML (sources, budget, license rules)
  ├── Tool Factory — instantiates CBS, CKAN, Tavily tools
  ├── Agent Loop (Claude) — searches, fetches, checks freshness, downloads
  └── Handoff Summary — compact context passed to next profile
     ↓
Aggregated results + cost breakdown
```

### Profiles
Profiles live in `config/profiles/` and define everything — no hardcoded values in code:

| Profile | Sources | Use for |
|---|---|---|
| `dutch_government` | CBS, data.overheid.nl, Den Haag Open Data | Dutch official statistics |
| `us_government` | data.gov (CKAN), SSA, Census | US federal open data |
| `eu_open_data` | EU Open Data Portal, Eurostat | EU-wide statistics |
| `global` | Tavily web search | Any country (with cost warning) |

### Tools
Each tool implements a standard interface (`search`, `fetch`, `download`):
- **CBSTool** — CBS OData catalog, direct table fetch, cbsodata download
- **CKANTool** — Generic CKAN API (data.gov, overheid.nl, EU portal)
- **TavilyTool** — Web search + JS-rendered page extraction (fallback)

---

## Components

```
dataset-prober/
├── src/
│   ├── dataset_agent.py      — Agentic entry point
│   ├── prompt_interpreter.py — Claude-based intent classifier
│   ├── orchestrator.py       — Multi-profile execution with handoffs
│   ├── config_loader.py      — Profile YAML loader
│   ├── tools/
│   │   ├── base.py           — DataSourceTool interface + DatasetResult
│   │   ├── cbs_tool.py       — CBS Statistics Netherlands
│   │   ├── ckan_tool.py      — Generic CKAN (data.gov, overheid.nl)
│   │   └── tavily_tool.py    — Web search fallback
│   ├── run.py                — Manual prober entry point
│   ├── prober.py             — DuckDB httpfs prober + downloader
│   ├── agent.py              — Claude/Ollama dataset analysis
│   └── crawler.py            — Web crawler for dataset links
├── config/
│   └── profiles/
│       ├── dutch_government.yaml
│       ├── us_government.yaml
│       ├── eu_open_data.yaml
│       └── global.yaml
└── output/
    ├── datasets.duckdb       — Downloaded datasets
    ├── agent_results.json    — Last agent run results
    └── probe_results.json    — Last prober run results
```

---

## Stages (manual prober)

- **Stage 1** — DuckDB prober: checks availability, row counts, schema
- **Stage 2** — Claude/Ollama analysis: plain-language readiness assessment
- **Stage 3** — Web crawler: finds embedded dataset links on webpages
- **Stage 4** — Download to DuckDB: loads selected datasets for local SQL querying

---

## Known Limitations

- **SSA.gov** blocks programmatic HTTP access — US social security files require manual download
- **CBS OData** returns JSON, not CSV — httpfs cannot probe OData endpoints directly (use CBSTool instead)
- **JavaScript-rendered portals** (e.g. CBS Statline, Socrata) are handled via Tavily extraction; some remain inaccessible
- **denhaag.incijfers.nl** blocks automated access (robots.txt) — excluded from all profiles

---

## Usage

### Agentic discovery

```bash
python src/dataset_agent.py
```

With options:
```bash
python src/dataset_agent.py --timeout 15 --max-searches 8 --download
python src/dataset_agent.py --profile dutch_government
python src/dataset_agent.py --list-profiles
```

All CLI flags override profile defaults — you control every limit:

| Flag | Description |
|---|---|
| `--timeout N` | Run timeout in minutes |
| `--max-searches N` | Maximum catalog searches |
| `--max-crawls N` | Maximum page extractions |
| `--max-probes N` | Maximum dataset probes |
| `--max-tokens N` | Maximum tokens per Claude call |
| `--download` | Allow agent to download to DuckDB |
| `--profile NAME` | Force a specific profile (skip auto-detection) |
| `--list-profiles` | Show available profiles and exit |

### Manual prober

**Interactive mode:**
```bash
python src/run.py
```

**Batch mode (JSON file):**
```bash
python src/run.py --file sources.json
```

**With Claude analysis:**
```bash
python src/run.py --analyze
```

**With local Ollama model (free, offline):**
```bash
python src/run.py --analyze --local
python src/run.py --analyze --local --model gemma3:12b
```

---

## Setup

```bash
pip install -r requirements.txt
```

Add your API keys to a `.env` file:
```
ANTHROPIC_API_KEY=your-key-here
TAVILY_API_KEY=your-key-here
DATAGOV_API_KEY=DEMO_KEY
```

### Local model support (optional)
Install [Ollama](https://ollama.com) and pull a model:
```bash
ollama pull qwen2.5-coder:3b   # fast, interactive
ollama pull gemma3:12b          # slower, more thorough
```

---

## License Evaluation

The agent evaluates dataset licenses using CCREL/ODRL standards:

| License | Grade | Meaning |
|---|---|---|
| CC0 / Public Domain | A | Unrestricted use |
| CC-BY | B | Attribution required |
| CC-BY-SA | B- | Share-alike required |
| CC-BY-NC | C | Non-commercial only |
| Unknown | ? | Verify before use |

---

## Example output

See [`output/analysis_summary.txt`](output/analysis_summary.txt) for a sample Claude analysis.

---

## Tools & Resources

- [DuckDB Web Shell](https://shell.duckdb.org) — browser-based DuckDB for quickly testing CSV URLs
- [DuckDB httpfs docs](https://duckdb.org/docs/extensions/httpfs.html) — reference for remote file querying
- [CBS OData API](https://www.cbs.nl/nl-nl/onze-diensten/open-data/statline-als-open-data) — CBS Statistics Netherlands API
- [Data.gov CKAN API](https://open.gsa.gov/api/datadotgov/) — US government open data catalog API
- [Open Definition](https://opendefinition.org) — what "open" means for data
- [Open Data Handbook](https://opendatahandbook.org) — practical guide to open data
- [W3C Data License Best Practices](https://www.w3.org/TR/dwbp/#DataLicense) — licensing standards

---

## Acknowledgements

Inspired by [Jurjen van Genugten](https://www.linkedin.com/in/jurjenvangenugten/)'s LinkedIn post demonstrating DuckDB httpfs for probing Dutch open datasets.
