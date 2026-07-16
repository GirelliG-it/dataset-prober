import os
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rich.console import Console

sys.path.insert(0, os.path.dirname(__file__))
from prober import probe_url, save_results

console = Console()

DATASET_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".parquet"}
KEYWORDS = {"data", "statistics", "statistical", "dataset", "download", "cijfers"}
MAX_DEPTH = 5


def is_dataset_link(url: str) -> bool:
    """Check if a URL points directly to a dataset file."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DATASET_EXTENSIONS)


def is_relevant_page(url: str, text: str) -> bool:
    """Check if a link is worth following based on URL or anchor text."""
    combined = (url + " " + text).lower()
    return any(kw in combined for kw in KEYWORDS)


def same_domain(base_url: str, url: str) -> bool:
    """Check if a URL belongs to the same domain as the base."""
    return urlparse(base_url).netloc == urlparse(url).netloc


def crawl(base_url: str, max_depth: int = 3) -> list[dict]:
    """
    Crawl a website looking for dataset files.
    Stays on domain for page links, follows cross-domain file links.
    Returns a list of found dataset URLs with names.
    """
    visited = set()
    found_datasets = []

    def _crawl(url: str, depth: int):
        if depth > max_depth or url in visited:
            return
        visited.add(url)

        console.print(f"  [dim]Depth {depth}:[/dim] {url}")

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
        except Exception as e:
            console.print(f"  [red]Failed:[/red] {e}")
            return

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            full_url = urljoin(url, href)
            link_text = tag.get_text(strip=True)

            # Skip already visited or empty
            if not full_url.startswith("http") or full_url in visited:
                continue

            if is_dataset_link(full_url):
                # Found a dataset file
                name = link_text if link_text else full_url.split("/")[-1]
                if not any(d["url"] == full_url for d in found_datasets):
                    console.print(f"  [green]Found:[/green] {name} → {full_url}")
                    found_datasets.append({"name": name, "url": full_url})

            elif same_domain(base_url, full_url) and is_relevant_page(full_url, link_text):
                # Follow relevant pages within the same domain
                _crawl(full_url, depth + 1)

    _crawl(base_url, depth=0)
    return found_datasets


if __name__ == "__main__":
    console.print("\n[bold cyan]Dataset Crawler[/bold cyan]\n")

    url = console.input("[cyan]URL to crawl:[/cyan] ").strip()

    depth_input = console.input("[cyan]Max depth (default 3, max 5):[/cyan] ").strip()
    max_depth = int(depth_input) if depth_input.isdigit() else 3
    max_depth = min(max_depth, MAX_DEPTH)

    console.print(f"\nCrawling [bold]{url}[/bold] to depth {max_depth}...\n")
    datasets = crawl(url, max_depth)

    if not datasets:
        console.print("[yellow]No datasets found.[/yellow]")
    else:
        console.print(f"\n[bold]Found {len(datasets)} dataset(s). Probing...[/bold]\n")
        results = [probe_url(d["name"], d["url"]) for d in datasets]

        from run import display_results
        display_results(results)

        from prober import save_results
        save_results(results, "output/probe_results.json")
