import json
from pathlib import Path
from prober import probe_all, save_results
from rich.console import Console
from rich.table import Table

console = Console()

# --- Dataset sources ---

SOURCES = [
    {
        "name": "RDW Erkende Bedrijven",
        "url": "https://opendata.rdw.nl/api/views/5k74-3jha/rows.csv?accessType=DOWNLOAD",
    },
    {
        "name": "RDW Terugroep Actie Risico",
        "url": "https://opendata.rdw.nl/api/views/9ihi-jgpf/rows.csv?accessType=DOWNLOAD",
    },
    {
        "name": "RDW Gebreken",
        "url": "https://opendata.rdw.nl/api/views/hx2c-gt7k/rows.csv?accessType=DOWNLOAD",
    },
    {
        "name": "RDW GEO Carpool",
        "url": "https://opendata.rdw.nl/api/views/9c54-cmfx/rows.csv?accessType=DOWNLOAD",
    },
]

# --- Run prober ---
console.print("\n[bold cyan]Dataset Prober[/bold cyan] — Dutch Open Data\n")
results = probe_all(SOURCES)

# --- Terminal table ---
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

# --- Save to file ---
output_path = Path(__file__).parent.parent / "output" / "probe_results.json"
save_results(results, str(output_path))
