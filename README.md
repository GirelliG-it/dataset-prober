# dataset-prober
A Python tool that probes open datasets via DuckDB httpfs and uses Claude to generate data quality assessments.


## How it works

By running `run.py` you get an interactive prompt that asks you for the name and URL of the dataset you want to probe. After entering this information, you can choose to keep adding more datasets or run immediately. The tool then runs in stages:

- First, `prober.py` queries each dataset URL directly using DuckDB's httpfs extension — no file is downloaded. It returns row counts, column names, data types, and sample rows.
- Second, `agent.py` feeds those results to Claude, which generates a plain-language analysis of each dataset.
- Optionally, datasets can be crawled from a webpage and downloaded into a local DuckDB database.


## Components
- `src/prober.py` — queries each URL via DuckDB httpfs, returns row counts, column names, data types, sample rows, and includes `download_to_duckdb()` for loading datasets into a local DuckDB database
- `src/agent.py` — sends probe results to Claude for plain-language analysis and readiness assessment
- `src/run.py` — entry point, handles interactive and batch input modes
- `src/crawler.py` — crawls a webpage looking for embedded dataset links (.csv, .xlsx, .json)


## Stages
- **Stage 1** — DuckDB prober: checks availability, row counts, schema
- **Stage 2** — Claude API analysis: plain-language readiness assessment
- **Stage 3** — Web crawler: finds embedded dataset links on webpages
- **Stage 4** — Download to DuckDB: loads selected datasets for local SQL querying


## Known limitations
- JavaScript-rendered websites (e.g. CBS Aruba, Socrata-based portals) are not supported. A Playwright-based solution is planned for a future stage.


## Usage

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

## Setup

```bash
pip install -r requirements.txt
```

Add your API key to a `.env` file:## Example output

See [`output/analysis_summary.txt`](output/analysis_summary.txt) for a sample Claude analysis.


## Acknowledgements
Inspired by [Jurjen van Genugten](https://www.linkedin.com/in/jurjenvangenugten/)'s LinkedIn post demonstrating DuckDB httpfs for probing Dutch open datasets.
