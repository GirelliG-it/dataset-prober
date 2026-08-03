import json
import sys
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

from dataset_prober.config_loader import get_anthropic_api_key
from dataset_prober.paths import AppPaths

# Load API key from .env
load_dotenv(Path(__file__).parent.parent.parent / ".env")

OLLAMA_URL = "http://localhost:11434/api/chat"

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """
    Lazily build the Anthropic client. Module-level instantiation used to
    require ANTHROPIC_API_KEY even for --local (Ollama-only) runs, since it
    raised on import before argparse ever got a chance to read --local.
    """
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_anthropic_api_key(), max_retries=3)
    return _client


def _build_prompt(results: list[dict]) -> str:
    """Build the analysis prompt from probe results."""
    results_text = ""
    for r in results:
        results_text += f"\n--- {r['name']} ---\n"
        results_text += f"Status: {r['status']}\n"
        if r["status"] == "ok":
            results_text += f"Rows: {r['row_count']}\n"
            cols = [f"{c['name']} ({c['type']})" for c in r["columns"]]
            results_text += f"Columns: {', '.join(cols)}\n"
            if r["sample"]:
                results_text += f"Sample row: {r['sample'][0]}\n"
        else:
            results_text += f"Error: {r['error']}\n"

    return f"""You are a data analyst reviewing Dutch open datasets probed via DuckDB httpfs.

Here are the probe results:
{results_text}

For each dataset with status 'ok', provide:
1. A one-sentence description of what the dataset contains
2. Its likely use case for public sector analysis
3. A readiness assessment: is it analysis-ready?

For failed datasets, briefly explain what likely went wrong.

Be concise and practical. Write as if briefing a data team."""


def summarize_probe_results(results: list[dict]) -> str:
    """Send probe results to Claude and get a dataset analysis summary."""
    message = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": _build_prompt(results)}],
    )
    return message.content[0].text


def summarize_probe_results_local(results: list[dict], model: str = "qwen2.5-coder:3b") -> str:
    """Send probe results to a local Ollama model and get a dataset analysis summary."""
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": model,
            "messages": [{"role": "user", "content": _build_prompt(results)}],
            "stream": False,
        },
        timeout=600,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyse probe results with Claude or a local model"
    )
    parser.add_argument(
        "--local", action="store_true", help="Use local Ollama model instead of Claude"
    )
    parser.add_argument(
        "--model",
        default="qwen2.5-coder:3b",
        help="Ollama model to use (default: qwen2.5-coder:3b)",
    )
    args = parser.parse_args()
    paths = AppPaths.resolve()

    output_path = paths.probe_results_path

    if not output_path.exists():
        print("No probe results found. Run run.py first.")
        sys.exit(1)

    with open(output_path) as f:
        results = json.load(f)

    if args.local:
        print(f"\nSending results to local model ({args.model})...\n")
        summary = summarize_probe_results_local(results, model=args.model)
    else:
        print("\nSending results to Claude for analysis...\n")
        summary = summarize_probe_results(results)

    print(summary)

    summary_path = paths.analysis_summary_path
    paths.ensure_output_dir()
    with open(summary_path, "w") as f:
        f.write(summary)
    print(f"\nSummary saved to {summary_path}")


if __name__ == "__main__":
    main()
