"""
dataset_agent.py — Agentic dataset discovery for dataset-prober

Uses Claude as the reasoning brain, Tavily for web search/extraction,
and cbsodata for direct CBS (Statistics Netherlands) access.

Usage:
    python src/dataset_agent.py
    python src/dataset_agent.py --timeout 10
    python src/dataset_agent.py --max-searches 5 --max-probes 15
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

# Load environment
load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
from tavily import TavilyClient

# Local tools
sys.path.insert(0, str(Path(__file__).parent))
from prober import probe_url, download_to_duckdb, ProbeResult

console = Console()

# ─── Clients ────────────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))

# ─── Budget tracking ────────────────────────────────────────────────────────

class Budget:
    def __init__(self, max_searches: int, max_crawls: int, max_probes: int, timeout_minutes: int):
        self.max_searches = max_searches
        self.max_crawls = max_crawls
        self.max_probes = max_probes
        self.searches_used = 0
        self.crawls_used = 0
        self.probes_used = 0
        self.start_time = time.time()
        self.timeout_seconds = timeout_minutes * 60

    def time_remaining(self) -> float:
        return self.timeout_seconds - (time.time() - self.start_time)

    def elapsed_minutes(self) -> float:
        return (time.time() - self.start_time) / 60

    def timed_out(self) -> bool:
        return self.time_remaining() <= 0

    def can_search(self) -> bool:
        return self.searches_used < self.max_searches and not self.timed_out()

    def can_crawl(self) -> bool:
        return self.crawls_used < self.max_crawls and not self.timed_out()

    def can_probe(self) -> bool:
        return self.probes_used < self.max_probes and not self.timed_out()

    def status(self) -> str:
        remaining = self.time_remaining()
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        return (
            f"⏱  {mins}m{secs}s remaining | "
            f"🔍 {self.searches_used}/{self.max_searches} searches | "
            f"🌐 {self.crawls_used}/{self.max_crawls} crawls | "
            f"📊 {self.probes_used}/{self.max_probes} probes"
        )

# ─── Tool implementations ───────────────────────────────────────────────────

def tool_search(query: str, budget: Budget) -> dict:
    """Search the web for dataset sources using Tavily."""
    if not budget.can_search():
        return {"error": "Search budget exhausted or timed out"}

    console.print(f"  🔍 [cyan]Searching:[/cyan] {query}")
    budget.searches_used += 1

    try:
        response = tavily_client.search(
            query=query,
            search_depth="advanced",
            max_results=5,
            include_domains=[".nl", "data.overheid.nl", "opendata.cbs.nl",
                             "rivm.nl", "rdw.nl", "government.nl"]
        )
        results = [
            {"url": r["url"], "title": r["title"], "snippet": r.get("content", "")[:200]}
            for r in response.get("results", [])
        ]
        console.print(f"    → Found {len(results)} results")
        return {"results": results}
    except Exception as e:
        return {"error": str(e)}


def tool_extract(url: str, budget: Budget) -> dict:
    """Extract content from a URL using Tavily (handles JS-rendered pages)."""
    if not budget.can_crawl():
        return {"error": "Crawl budget exhausted or timed out"}

    console.print(f"  🌐 [cyan]Extracting:[/cyan] {url}")
    budget.crawls_used += 1

    try:
        response = tavily_client.extract(urls=[url])
        results = response.get("results", [])
        if results:
            content = results[0].get("raw_content", "")[:3000]
            import re
            dataset_urls = re.findall(
                r'https?://[^\s<>"]+?\.(?:csv|xlsx|xls|json|parquet)',
                content,
                re.IGNORECASE
            )
            console.print(f"    → Extracted content, found {len(dataset_urls)} dataset URLs")
            return {"content": content, "dataset_urls": dataset_urls}
        return {"error": "No content extracted"}
    except Exception as e:
        return {"error": str(e)}


def tool_probe(name: str, url: str, budget: Budget) -> dict:
    """Probe a dataset URL using DuckDB httpfs."""
    if not budget.can_probe():
        return {"error": "Probe budget exhausted or timed out"}

    console.print(f"  📊 [cyan]Probing:[/cyan] {name}")
    budget.probes_used += 1

    try:
        result = probe_url(name, url)
        output = {
            "name": result.name,
            "url": result.url,
            "status": result.status,
            "row_count": result.row_count,
            "columns": result.columns,
            "error": result.error
        }
        if result.status == "ok":
            console.print(f"    → ✅ {result.row_count} rows, {len(result.columns)} columns")
        else:
            console.print(f"    → ❌ {result.status}: {str(result.error)[:80]}")
        return output
    except Exception as e:
        return {"error": str(e)}


def tool_check_freshness(url: str, last_updated: str, rule: str) -> dict:
    """Evaluate dataset freshness against a rule."""
    console.print(f"  📅 [cyan]Checking freshness:[/cyan] last updated {last_updated}")

    result = {
        "url": url,
        "last_updated": last_updated,
        "rule": rule,
        "passes": None,
        "reason": ""
    }

    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y"):
            try:
                update_date = datetime.strptime(last_updated.strip()[:19], fmt)
                break
            except ValueError:
                continue
        else:
            result["passes"] = None
            result["reason"] = f"Could not parse date: {last_updated}"
            console.print(f"    → ⚠️  Could not parse date")
            return result

        now = datetime.now()

        if "6 month" in rule.lower():
            cutoff = now - timedelta(days=180)
        elif "1 year" in rule.lower() or "12 month" in rule.lower():
            cutoff = now - timedelta(days=365)
        elif "2 year" in rule.lower():
            cutoff = now - timedelta(days=730)
        else:
            cutoff = now - timedelta(days=365)

        passes = update_date >= cutoff
        result["passes"] = passes
        result["reason"] = (
            f"Updated {(now - update_date).days} days ago — "
            f"{'passes' if passes else 'fails'} the '{rule}' rule"
        )
        icon = "✅" if passes else "⏭️ "
        console.print(f"    → {icon} {result['reason']}")

    except Exception as e:
        result["passes"] = None
        result["reason"] = str(e)

    return result


def tool_search_cbs_catalog(keyword: str, max_results: int = 10) -> dict:
    """
    Search the CBS catalog for tables matching a keyword.
    Scores results by relevance — title matches rank higher than description matches.
    Also filters to active/recent tables only.
    """
    console.print(f"  📚 [cyan]Searching CBS catalog:[/cyan] {keyword}")
    try:
        import cbsodata
        import requests
        from datetime import datetime, timedelta

        # Use CBS OData catalog API directly with timeout
        catalog_url = "https://opendata.cbs.nl/ODataCatalog/Tables?$format=json"
        resp = requests.get(catalog_url, timeout=30)
        resp.raise_for_status()
        all_tables = resp.json().get("value", [])

        keyword_lower = keyword.lower()
        scored = []

        for table in all_tables:
            title = table.get("Title", "").lower()
            desc = table.get("ShortDescription", "").lower()
            status = table.get("OutputStatus", "").lower()

            # Skip archived/discontinued tables
            if "archief" in status or "gestopt" in status or "discontinued" in status:
                continue

            score = 0
            if keyword_lower in title:
                score += 2  # Title match scores higher
            if keyword_lower in desc:
                score += 1

            if score == 0:
                continue

            # Parse modified date
            modified = table.get("Modified", "")
            modified_display = modified[:10] if modified else "unknown"

            scored.append({
                "score": score,
                "identifier": table.get("Identifier"),
                "title": table.get("Title"),
                "description": table.get("ShortDescription", "")[:200],
                "modified": modified_display,
                "frequency": table.get("Frequency", "unknown"),
                "period": table.get("Period", "unknown"),
                "language": table.get("Language", "nl"),
                "status": table.get("OutputStatus", "unknown")
            })

        # Sort by score then by modified date (most recent first)
        scored.sort(key=lambda x: (x["score"], x["modified"]), reverse=True)
        matches = [{k: v for k, v in m.items() if k != "score"} for m in scored[:max_results]]

        console.print(f"    → Found {len(matches)} matching CBS tables")
        return {"matches": matches, "total_found": len(scored)}
    except Exception as e:
        return {"error": str(e)}


def tool_fetch_cbs_table(table_id: str, budget: Budget, sample_only: bool = True) -> dict:
    """
    Fetch metadata and sample from a CBS table via direct OData API.
    Uses $top=10 with a 30-second timeout — never blocks on large tables.
    Full download is handled separately by tool_download.
    """
    if not budget.can_probe():
        return {"error": "Probe budget exhausted or timed out"}

    console.print(f"  🏛️  [cyan]Fetching CBS table:[/cyan] {table_id}")
    budget.probes_used += 1

    import requests

    try:
        # Step 1: Get table metadata with timeout
        meta_url = f"https://opendata.cbs.nl/ODataApi/odata/{table_id}/TableInfos?$format=json"
        meta_resp = requests.get(meta_url, timeout=30)
        meta_resp.raise_for_status()
        meta_values = meta_resp.json().get("value", [{}])
        meta = meta_values[0] if meta_values else {}
        title = meta.get("Title", table_id)
        modified = meta.get("Modified", "unknown")
        summary = meta.get("Summary", "")

        # Step 2: Get sample rows with timeout
        sample_url = (
            f"https://opendata.cbs.nl/ODataApi/odata/{table_id}"
            f"/TypedDataSet?$top=10&$format=json"
        )
        sample_resp = requests.get(sample_url, timeout=30)
        sample_resp.raise_for_status()
        sample_data = sample_resp.json().get("value", [])

        if not sample_data:
            return {"error": f"No sample data returned for {table_id}"}

        columns = [{"name": k, "type": "string"} for k in sample_data[0].keys()]

        console.print(f"    → ✅ {title}")
        console.print(f"    → {len(columns)} columns | Last modified: {modified}")

        return {
            "table_id": table_id,
            "title": title,
            "modified": modified,
            "summary": summary[:300],
            "columns": columns,
            "row_count": None,  # Unknown until full download
            "sample": sample_data[:3],
            "status": "ok",
            "source": "cbs",
            "url": f"https://opendata.cbs.nl/ODataApi/odata/{table_id}"
        }

    except requests.Timeout:
        console.print(f"    → ❌ Timed out after 30s")
        return {"error": f"CBS table {table_id} timed out — may be too large or unavailable"}
    except Exception as e:
        console.print(f"    → ❌ {str(e)[:80]}")
        return {"error": str(e)}


def tool_download(name: str, url: str, row_count: int, columns: list,
                  table_id: str = None) -> dict:
    """
    Download a dataset to local DuckDB.
    For CBS tables (table_id provided), uses cbsodata.
    For direct CSV URLs, uses DuckDB httpfs.
    """
    console.print(f"  💾 [cyan]Downloading:[/cyan] {name}")
    db_path = str(Path(__file__).parent.parent / "output" / "datasets.duckdb")

    try:
        # CBS tables — use cbsodata in a thread with timeout
        if table_id:
            import cbsodata
            import duckdb
            import pandas as pd
            import threading
            import queue as queue_module

            console.print(f"  📥 Fetching CBS table {table_id} via cbsodata (may take a few minutes)...")

            result_queue = queue_module.Queue()

            def _fetch():
                try:
                    data = cbsodata.get_data(table_id)
                    result_queue.put(("ok", data))
                except Exception as e:
                    result_queue.put(("error", str(e)))

            thread = threading.Thread(target=_fetch, daemon=True)
            thread.start()
            thread.join(timeout=300)  # 5 minute max per table

            if thread.is_alive():
                return {"success": False, "error": f"CBS download timed out after 5 minutes for {table_id}"}

            status, payload = result_queue.get()
            if status == "error":
                return {"success": False, "error": payload}

            data = payload
            if not data:
                return {"success": False, "error": "No data returned from CBS"}

            # Create safe table name
            table_name = name.lower()
            table_name = "".join(c if c.isalnum() else "_" for c in table_name)
            table_name = table_name.strip("_")[:50]

            # Save to DuckDB via pandas
            df = pd.DataFrame(data)
            con = duckdb.connect(db_path)
            con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM df")
            actual_rows = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            con.close()

            console.print(f"    → ✅ {actual_rows} rows saved to table '{table_name}'")
            return {"success": True, "db_path": db_path, "table": table_name, "rows": actual_rows}

        # Direct CSV — use existing httpfs method
        else:
            result = ProbeResult(
                url=url,
                name=name,
                status="ok",
                row_count=row_count,
                columns=columns
            )
            download_to_duckdb([result], db_path)
            return {"success": True, "db_path": db_path}

    except Exception as e:
        console.print(f"    → ❌ {str(e)[:80]}")
        return {"success": False, "error": str(e)}


# ─── Tool definitions for Claude ────────────────────────────────────────────

TOOLS = [
    {
        "name": "search",
        "description": (
            "Search the web for Dutch open dataset sources using Tavily. "
            "Prefer government domains (.nl, overheid.nl, cbs.nl, rivm.nl). "
            "Use specific queries including topic, geography, and data type. "
            "Use this for non-CBS sources. For CBS data, prefer search_cbs_catalog."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in Dutch or English"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "extract",
        "description": (
            "Extract content and dataset URLs from a webpage using Tavily. "
            "Handles JavaScript-rendered pages. Use when a search result looks "
            "promising but needs deeper inspection for direct file URLs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the page to extract"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "probe",
        "description": (
            "Probe a CSV/dataset URL directly using DuckDB httpfs. "
            "Returns row count, column names, and data types. "
            "Use only on direct file URLs ending in .csv, .parquet etc. "
            "Do NOT use on CBS OData or ZIP URLs — use fetch_cbs_table instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable dataset name"},
                "url": {"type": "string", "description": "Direct URL to the dataset file"}
            },
            "required": ["name", "url"]
        }
    },
    {
        "name": "check_freshness",
        "description": (
            "Check if a dataset meets the freshness requirement from the user's prompt. "
            "Use when you know the last update date from metadata or catalog."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Dataset URL or identifier"},
                "last_updated": {"type": "string", "description": "Last update date (e.g. '2024-03-15')"},
                "rule": {"type": "string", "description": "Freshness rule (e.g. 'within 6 months')"}
            },
            "required": ["url", "last_updated", "rule"]
        }
    },
    {
        "name": "search_cbs_catalog",
        "description": (
            "Search the CBS (Statistics Netherlands) catalog for datasets by keyword. "
            "ALWAYS use this first when looking for CBS data — it bypasses all "
            "JavaScript walls and ZIP issues. Returns table IDs, titles, descriptions, "
            "and last modification dates. Try both Dutch and English keywords."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search keyword in Dutch or English (e.g. 'bevolking', 'luchtkwaliteit')"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10)"
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "fetch_cbs_table",
        "description": (
            "Fetch data from a specific CBS table by its ID (e.g. '37230NED'). "
            "Bypasses JavaScript walls and ZIP archives completely. "
            "Returns columns, sample rows, title, and last modification date. "
            "Use sample_only=true for evaluation, false only when downloading."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {
                    "type": "string",
                    "description": "CBS table identifier (e.g. '37230NED')"
                },
                "sample_only": {
                    "type": "boolean",
                    "description": "Fetch only 10 rows for evaluation (default: true)"
                }
            },
            "required": ["table_id"]
        }
    },
    {
        "name": "download",
        "description": (
            "Download a dataset into the local DuckDB database. "
            "ONLY call this if the user explicitly requested downloading. "
            "Always confirm freshness and quality checks passed first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Dataset name"},
                "url": {"type": "string", "description": "Dataset URL"},
                "row_count": {"type": "integer", "description": "Row count from probe"},
                "columns": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Column definitions from probe"
                },
                "table_id": {
                    "type": "string",
                    "description": "CBS table ID if this is a CBS dataset (e.g. '37230ned'). Required for CBS tables."
                }
            },
            "required": ["name", "url", "row_count", "columns"]
        }
    }
]

# ─── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an autonomous dataset discovery agent for a Dutch public sector data analyst.

Your job is to find, evaluate, and report on open datasets based on the user's prompt.

AVAILABLE DATA SOURCES (in order of preference):
1. CBS (Statistics Netherlands) — use search_cbs_catalog + fetch_cbs_table. Best for demographics, economy, population, regional stats.
2. RIVM — use search + extract. Best for health, environment, air quality.
3. RDW — use search + probe. Best for vehicles, transport.
4. data.overheid.nl — use search + extract. Central Dutch open data portal.
5. Den Haag open data — three sources:
   - den-haag-opendata.opendatasoft.com — CSV API, directly probeable. Use probe() on export URLs ending in /exports/csv
   - denhaag.dataplatform.nl — gemeente data platform, use search + extract
   - geoportaal-ddh.opendata.arcgis.com — geospatial data, use search + extract
   NOTE: denhaag.incijfers.nl blocks automated access — do not attempt to crawl it.
6. Other gemeente portals — many Dutch municipalities use OpenDataSoft (*.opendatasoft.com).
   CSV export URLs follow the pattern: https://<gemeente>-opendata.opendatasoft.com/api/explore/v2.1/catalog/datasets/<id>/exports/csv
   These are directly probeable with probe().

BEHAVIOUR RULES:
1. Parse the user's prompt carefully — extract topic, geography, source preferences, freshness rules, and whether to download.
2. For CBS data: ALWAYS start with search_cbs_catalog, then fetch_cbs_table for promising results.
3. For non-CBS data: use search + extract to find direct file URLs, then probe.
4. Apply freshness rules strictly from the user's prompt.
5. Only download if the user explicitly asked to download.
6. Think step by step and explain each decision briefly in Dutch if the user wrote in Dutch.
7. Try multiple keywords (Dutch AND English) before giving up.
8. Stop and report clearly when budget is exhausted.

AUDIT TRAIL:
Before each tool call, briefly explain WHY you are calling it.
After each result, explain what you learned and what you will do next.

FRESHNESS EVALUATION:
- Extract the last modification date from catalog metadata or file metadata.
- Apply the user's time rule strictly.
- If date is unknown, flag it as "freshness unverifiable" — do not assume it passes.

LICENSE EVALUATION (CCREL / ODRL):
When dataset metadata includes license information, evaluate and report it:
- CC0 / Public Domain (creativecommons.org/publicdomain) → ✅ Unrestricted use
- CC-BY (creativecommons.org/licenses/by) → ✅ Free with attribution required
- CC-BY-SA (creativecommons.org/licenses/by-sa) → ⚠️ Share-alike required
- CC-BY-NC (creativecommons.org/licenses/by-nc) → ⚠️ Non-commercial only
- ODRL compliant → check permissions and obligations in metadata
- No license found → flag as "license unverified — verify before use"
Always include license status in the final report. For Dutch government data,
most datasets use CC0 or CC-BY, but always verify.

OUTPUT FORMAT:
Present a clear summary with:
- Dataset name and table ID (for CBS)
- Source and URL
- Rows and columns
- Last modified date
- Freshness verdict (passes/fails user's rule)
- License (CC0, CC-BY, etc. or "unverified")
- Recommendation (download / review manually / skip)"""

# ─── Agent loop ─────────────────────────────────────────────────────────────

def run_agent(user_prompt: str, budget: Budget, allow_download: bool = False) -> None:
    """Main agentic loop."""

    console.print(Panel(
        f"[bold cyan]Dataset Discovery Agent[/bold cyan]\n\n"
        f"[white]{user_prompt}[/white]\n\n"
        f"[dim]{budget.status()}[/dim]",
        box=box.ROUNDED
    ))

    messages = [{"role": "user", "content": user_prompt}]
    found_datasets = []
    iteration = 0

    while True:
        iteration += 1

        # Check timeout
        if budget.timed_out():
            console.print(f"\n[yellow]⏰ Time limit reached after {budget.elapsed_minutes():.1f} minutes.[/yellow]")
            console.print(f"[yellow]Found {len(found_datasets)} dataset(s) so far.[/yellow]\n")
            _ask_for_instructions(found_datasets, budget)
            break

        # Print budget status every 3 iterations
        if iteration % 3 == 0:
            console.print(f"\n[dim]{budget.status()}[/dim]\n")

        # Call Claude
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    console.print(f"\n[bold green]Agent Report:[/bold green]\n{block.text}")
            break

        if response.stop_reason != "tool_use":
            console.print(f"[red]Unexpected stop reason: {response.stop_reason}[/red]")
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                if hasattr(block, "text") and block.text:
                    console.print(f"\n[dim italic]{block.text}[/dim italic]")
                continue

            tool_name = block.name
            tool_input = block.input

            # Execute tool
            if tool_name == "search":
                result = tool_search(tool_input["query"], budget)
            elif tool_name == "extract":
                result = tool_extract(tool_input["url"], budget)
            elif tool_name == "probe":
                result = tool_probe(tool_input["name"], tool_input["url"], budget)
                if result.get("status") == "ok":
                    found_datasets.append(result)
            elif tool_name == "check_freshness":
                result = tool_check_freshness(
                    tool_input["url"],
                    tool_input["last_updated"],
                    tool_input["rule"]
                )
            elif tool_name == "search_cbs_catalog":
                result = tool_search_cbs_catalog(
                    tool_input["keyword"],
                    tool_input.get("max_results", 10)
                )
            elif tool_name == "fetch_cbs_table":
                result = tool_fetch_cbs_table(
                    tool_input["table_id"],
                    budget,
                    tool_input.get("sample_only", True)
                )
                if result.get("status") == "ok":
                    found_datasets.append(result)
            elif tool_name == "download":
                if allow_download:
                    result = tool_download(
                        tool_input["name"],
                        tool_input["url"],
                        tool_input["row_count"],
                        tool_input["columns"],
                        table_id=tool_input.get("table_id")
                    )
                else:
                    result = {"error": "Download not permitted — user did not request download"}
                    console.print("  ⛔ [yellow]Download blocked — not requested by user[/yellow]")
            else:
                result = {"error": f"Unknown tool: {tool_name}"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str)
            })

        # Add tool results to history
        messages.append({"role": "user", "content": tool_results})

    # Save results
    if found_datasets:
        output_path = Path(__file__).parent.parent / "output" / "agent_results.json"
        with open(output_path, "w") as f:
            json.dump(found_datasets, f, indent=2, default=str)
        console.print(f"\n[green]Results saved to {output_path}[/green]")


def _ask_for_instructions(found_datasets: list, budget: Budget) -> None:
    """Ask user for instructions when time runs out mid-run."""
    console.print("\n[bold yellow]What would you like to do?[/bold yellow]")
    console.print("  1. Continue searching (resets timer)")
    console.print("  2. Download what was found so far")
    console.print("  3. Show results and exit")

    choice = console.input("\n[cyan]Your choice (1/2/3):[/cyan] ").strip()

    if choice == "1":
        console.print("[green]Continuing...[/green]")
        budget.start_time = time.time()
    elif choice == "2":
        if found_datasets:
            console.print(f"\n[bold]Downloading {len(found_datasets)} dataset(s)...[/bold]")
            db_path = str(Path(__file__).parent.parent / "output" / "datasets.duckdb")
            results = [
                ProbeResult(
                    url=d["url"], name=d["name"], status="ok",
                    row_count=d.get("row_count", 0), columns=d.get("columns", [])
                )
                for d in found_datasets
            ]
            download_to_duckdb(results, db_path)
    else:
        _print_results_table(found_datasets)


def _print_results_table(datasets: list) -> None:
    """Print a summary table of found datasets."""
    if not datasets:
        console.print("[yellow]No datasets found.[/yellow]")
        return

    table = Table(title="Discovered Datasets", box=box.ROUNDED)
    table.add_column("Name", style="cyan")
    table.add_column("Rows", justify="right")
    table.add_column("Columns", justify="right")
    table.add_column("Modified")
    table.add_column("URL/ID")

    for d in datasets:
        identifier = d.get("table_id", d.get("url", "-"))
        if len(identifier) > 40:
            identifier = identifier[:37] + "..."
        table.add_row(
            d.get("name", d.get("title", "-")),
            str(d.get("row_count", "-")),
            str(len(d.get("columns", []))),
            str(d.get("modified", "-"))[:10],
            identifier
        )

    console.print(table)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentic dataset discovery")
    parser.add_argument("--timeout", type=int, default=10,
                        help="Timeout in minutes (default: 10)")
    parser.add_argument("--max-searches", type=int, default=5,
                        help="Maximum Tavily searches (default: 5)")
    parser.add_argument("--max-crawls", type=int, default=8,
                        help="Maximum page extractions (default: 8)")
    parser.add_argument("--max-probes", type=int, default=15,
                        help="Maximum dataset probes (default: 15)")
    parser.add_argument("--download", action="store_true",
                        help="Allow agent to download datasets")
    args = parser.parse_args()

    budget = Budget(
        max_searches=args.max_searches,
        max_crawls=args.max_crawls,
        max_probes=args.max_probes,
        timeout_minutes=args.timeout
    )

    console.print("\n[bold cyan]Dataset Discovery Agent[/bold cyan]")
    console.print("[dim]Type your dataset discovery request. Be as specific as possible.[/dim]\n")

    user_prompt = console.input("[cyan]What datasets are you looking for?[/cyan]\n> ").strip()

    if not user_prompt:
        console.print("[red]No prompt provided. Exiting.[/red]")
        exit(1)

    # Detect download intent from prompt
    download_keywords = ["download", "save", "store", "load into duckdb", "downloaden", "opslaan"]
    allow_download = args.download or any(kw in user_prompt.lower() for kw in download_keywords)

    if allow_download:
        console.print("[yellow]⚠️  Download mode enabled — datasets will be saved to DuckDB[/yellow]\n")

    run_agent(user_prompt, budget, allow_download=allow_download)
