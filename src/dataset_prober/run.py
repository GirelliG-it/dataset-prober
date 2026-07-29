import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from dataset_prober.crawler import resolve_directory
from dataset_prober.prober import download_to_duckdb, probe_all, save_results
from dataset_prober.tools.base import response_is_html

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

    console.print(
        f"\n[bold cyan]Dataset Prober[/bold cyan] — Loaded {len(sources)} sources from {filepath}\n"
    )
    return sources


def expand_directories(sources: list[dict]) -> list[dict]:
    """
    Interactive pre-pass: any source URL that is a directory listing
    (RIVM/INSPIRE autoindex) is walked so the user can descend into
    subfolders and pick concrete files. Non-directory sources pass through
    unchanged. Chosen files become normal probe sources.
    """
    expanded = []
    for src in sources:
        url = src["url"]
        # Only directory URLs need expansion. A quick HTML check avoids
        # fetching things that are plainly files.
        if not response_is_html(url):
            expanded.append(src)
            continue

        try:
            listing = resolve_directory(url)
        except Exception as e:
            console.print(f"[yellow]Could not read directory {url}: {e}[/yellow]")
            expanded.append(src)  # let it fall through and fail honestly downstream
            continue

        if not listing["subdirs"] and not listing["files"]:
            expanded.append(src)  # not actually a listing — pass through
            continue

        picked = _walk_directory(url, listing)
        expanded.extend(picked)

    return expanded


def _walk_directory(url: str, listing: dict | None = None) -> list[dict]:
    """
    Walk one directory interactively: show subdirs and files, let the user
    descend or select files. Returns the chosen files as source dicts.
    Loops until the user picks files or backs out.
    """
    while True:
        if listing is None:
            listing = resolve_directory(url)
        subdirs, files = listing["subdirs"], listing["files"]

        console.print(f"\n[bold cyan]Directory:[/bold cyan] {url}")
        if subdirs:
            console.print("[bold]Subfolders:[/bold]")
            for i, d in enumerate(subdirs, 1):
                console.print(f"  d{i}. {d['name']}")
        if files:
            console.print("[bold]Files:[/bold]")
            for i, f in enumerate(files, 1):
                date = f" [dim]({f['modified']})[/dim]" if f.get("modified") else ""
                console.print(f"  f{i}. {f['name']}{date}")
        if not subdirs and not files:
            console.print("[yellow]Empty directory.[/yellow]")
            return []

        choice = (
            console.input(
                "\n[cyan]Enter d<N> to open a subfolder, f<N>/f1,f3/'fall' for files, "
                "or 'skip':[/cyan] "
            )
            .strip()
            .lower()
        )

        if choice in ("skip", ""):
            return []
        if choice == "fall":
            return [{"name": f["name"], "url": f["url"]} for f in files]
        if choice.startswith("d") and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < len(subdirs):
                url = subdirs[idx]["url"]  # descend, loop
                listing = None  # new url -> force a fetch
                continue
        if choice.startswith("f"):
            picks = [p.strip() for p in choice.split(",")]
            chosen = []
            for pk in picks:
                if pk.startswith("f") and pk[1:].isdigit():
                    i = int(pk[1:]) - 1
                    if 0 <= i < len(files):
                        chosen.append({"name": files[i]["name"], "url": files[i]["url"]})
            if chosen:
                return chosen
        console.print("[red]Invalid choice — try again.[/red]")


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
    parser.add_argument("--analyze", action="store_true", help="Run Claude analysis after probing")
    parser.add_argument(
        "--local", action="store_true", help="Use local Ollama model instead of Claude"
    )
    parser.add_argument(
        "--model",
        default="qwen2.5-coder:3b",
        help="Ollama model to use (default: qwen2.5-coder:3b)",
    )
    args = parser.parse_args()

    # Get sources
    if args.file:
        sources = get_sources_from_file(args.file)
    else:
        sources = get_sources_interactive()

    if not sources:
        console.print("[red]No sources provided. Exiting.[/red]")
        exit(1)

    sources = expand_directories(sources)
    if not sources:
        console.print("[yellow]No files selected. Exiting.[/yellow]")
        exit(0)

    # Run prober
    console.print(f"\n[bold]Probing {len(sources)} dataset(s)...[/bold]\n")
    results = probe_all(sources)

    # Display results
    display_results(results)

    # Ask user which datasets to download
    ok_results = [r for r in results if r.status == "ok"]
    if ok_results:
        console.print("\n[bold]Available for download:[/bold]")
        for i, r in enumerate(ok_results, 1):
            console.print(f"  {i}. {r.name} ({r.row_count} rows)")

        selection = (
            console.input("\n[cyan]Download which datasets? (e.g. 1,3 or 'all' or 'none'):[/cyan] ")
            .strip()
            .lower()
        )

        if selection != "none" and selection != "":
            if selection == "all":
                to_download = ok_results
            else:
                indices = [int(x.strip()) - 1 for x in selection.split(",") if x.strip().isdigit()]
                to_download = [ok_results[i] for i in indices if i < len(ok_results)]

            if to_download:
                db_path = str(Path(__file__).parent.parent.parent / "output" / "datasets.duckdb")
                console.print(f"\n[bold]Downloading {len(to_download)} dataset(s)...[/bold]\n")
                download_to_duckdb(to_download, db_path)

    # Save results
    output_path = Path(__file__).parent.parent.parent / "output" / "probe_results.json"
    save_results(results, str(output_path))

    # Optionally run analysis
    if args.analyze:
        import json

        with open(output_path) as f:
            saved = json.load(f)
        if args.local:
            console.print(f"\n[bold]Running local analysis ({args.model})...[/bold]\n")
            from dataset_prober.agent import summarize_probe_results_local

            summary = summarize_probe_results_local(saved, model=args.model)
        else:
            console.print("\n[bold]Running Claude analysis...[/bold]\n")
            from dataset_prober.agent import summarize_probe_results

            summary = summarize_probe_results(saved)
        console.print(summary)
        summary_path = Path(__file__).parent.parent.parent / "output" / "analysis_summary.txt"
        with open(summary_path, "w") as f:
            f.write(summary)
        console.print(f"\n[green]Summary saved to {summary_path}[/green]")
