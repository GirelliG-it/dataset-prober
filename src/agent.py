import os
import json
import anthropic
from dotenv import load_dotenv
from pathlib import Path

# Load API key from .env
load_dotenv(Path(__file__).parent.parent / ".env")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def summarize_probe_results(results: list[dict]) -> str:
    """Send probe results to Claude and get a dataset analysis summary."""

    # Build a concise representation of the results
    results_text = ""
    for r in results:
        results_text += f"\n--- {r['name']} ---\n"
        results_text += f"Status: {r['status']}\n"
        if r['status'] == 'ok':
            results_text += f"Rows: {r['row_count']}\n"
            cols = [f"{c['name']} ({c['type']})" for c in r['columns']]
            results_text += f"Columns: {', '.join(cols)}\n"
            if r['sample']:
                results_text += f"Sample row: {r['sample'][0]}\n"
        else:
            results_text += f"Error: {r['error']}\n"

    prompt = f"""You are a data analyst reviewing Dutch open datasets probed via DuckDB httpfs.

Here are the probe results:
{results_text}

For each dataset with status 'ok', provide:
1. A one-sentence description of what the dataset contains
2. Its likely use case for public sector analysis
3. A readiness assessment: is it analysis-ready?

For failed datasets, briefly explain what likely went wrong.

Be concise and practical. Write as if briefing a data team."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text


if __name__ == "__main__":
    # Load saved probe results
    output_path = Path(__file__).parent.parent / "output" / "probe_results.json"

    if not output_path.exists():
        print("No probe results found. Run run.py first.")
        exit(1)

    with open(output_path) as f:
        results = json.load(f)

    print("\nSending results to Claude for analysis...\n")
    summary = summarize_probe_results(results)
    print(summary)

    # Save summary
    summary_path = Path(__file__).parent.parent / "output" / "analysis_summary.txt"
    with open(summary_path, "w") as f:
        f.write(summary)
    print(f"\nSummary saved to {summary_path}")
