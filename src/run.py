import json
import argparse
from pathlib import Path
from prober import probe_all, save_results
from rich.console import Console
from rich.table import Table

console = Console()


def get_sources_interactive() -> list[dict]:
    """Prompt the user to enter dataset names and URLs interactively."""
    sources = []
    console.print("\n[bold cyan]Dataset Prober[/bold cyan] — Enter datasets to probe\n")

    while True:
        name = console.input("[cyan]Dataset name:[/cyan] ").strip()
        if not name:
            console.print("[red]Name cannot be empty.[/red]")
            continue

        url = console.input("[cyan]URL:[/cyan] ").strip()
        if not url:
            console.print("[red]URL cannot be empty.[/red]")
            continue

        sources.append({"name": name, "url": url})

        another = console.input("[cyan]Add another? (y/n):[/cyan] ").strip().lower()
        if another != "y":
            break

    return sources


def get_sources_from_file(filepath: str) -> list[dict]:
    """Load dataset sources from a JSON file."""
    path = Path(filepath)
    if not path.exists():
        console.print(f"[red]File not found: {filepath}[/red]")
        exit(1)

    with open(path) as f:
        sources = json.load(f)

    console.print(f"\n[bold cyan]Dataset Prober[/bold cyan] — Loaded {len(sources)} sources from {filepath}\n")
    return sources


def display_results(results):
    """Display probe results as a rich table."""
    table = Table(title="Probe Results")
    table.add_column("Name", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Rows", justify="right")
    table.add_column("Columns", justify="right")
    table.add_column("Error")

    for r in results:
        status_color = "green" if r.status == "ok" else "red"
        table.add_row(
            r.name,
            f"[{status_color}]{r.status}[/{status_color}]",
            str(r.row_count) if r.row_count else "-",
            str(len(r.columns)) if r.columns else "-",
            r.error[:60] if r.error else "",
        )

    console.print(table)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe open datasets via DuckDB httpfs")
    parser.add_argument("--file", help="Path to a JSON file with dataset sources")
    args = parser.parse_args()

    # Get sources
    if args.file:
        sources = get_sources_from_file(args.file)
    else:
        sources = get_sources_interactive()

    if not sources:
        console.print("[red]No sources provided. Exiting.[/red]")
        exit(1)

    # Run prober
    console.print(f"\n[bold]Probing {len(sources)} dataset(s)...[/bold]\n")
    results = probe_all(sources)

    # Display results
    display_results(results)

    # Save results
    output_path = Path(__file__).parent.parent / "output" / "probe_results.json"
    save_results(results, str(output_path))
