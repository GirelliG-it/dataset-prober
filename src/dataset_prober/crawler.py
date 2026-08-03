import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rich.console import Console

from dataset_prober.paths import AppPaths
from dataset_prober.prober import probe_url, save_results

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


# ─── Directory descent (Apache/nginx autoindex listings) ─────────────────────
# Resolves a directory-style URL (RIVM, INSPIRE folder portals, plain
# autoindex pages) into its immediate children: subdirectories to descend into,
# and concrete dataset files to download. One level at a time — the caller
# (interactive prober, or the agent report) decides where to go next. This is
# the seam that turns "found a folder, couldn't download" into "here are the
# actual files."


def _is_subdirectory(href: str, base_url: str) -> bool:
    """
    True if `href` points to a child directory of `base_url`.

    Apache/nginx autoindex marks directories with a trailing slash. We also
    require the resolved URL to sit *below* base_url's path, so parent links
    and sort-header links (?C=N;O=D) don't masquerade as children.
    """
    full = urljoin(base_url, href)
    if "?" in full or "?" in href:  # sort/nav links: ?C=N;O=D etc.
        return False
    if not full.rstrip("/").startswith(base_url.rstrip("/")):
        return False  # not a descendant → parent/sibling
    if full.rstrip("/") == base_url.rstrip("/"):
        return False  # the folder itself
    return full.endswith("/")


def resolve_directory(url: str, timeout: int = 10) -> dict:
    """
    Fetch a directory-listing URL and split its links into subdirectories and
    dataset files. Returns:

        {
          "url": <the listing url>,
          "subdirs": [{"name": "Actueel-jaar/", "url": ".../Actueel-jaar/"}, ...],
          "files":   [{"name": "2026.csv", "url": ".../2026.csv",
                       "ext": ".csv", "modified": "2026-06-16"}, ...],
        }

    Does NOT recurse: one level per call, so the caller controls descent. File
    entries carry the modified date parsed from the listing row when present,
    so freshness can be shown per candidate without a second request.
    Raises on fetch failure; empty result if the page has no usable links.
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    subdirs, files, seen = [], [], set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full = urljoin(url, href)
        if full in seen:
            continue

        if _is_subdirectory(href, url):
            seen.add(full)
            name = tag.get_text(strip=True) or full.rstrip("/").split("/")[-1] + "/"
            subdirs.append({"name": name, "url": full})

        elif is_dataset_link(full):
            seen.add(full)
            name = tag.get_text(strip=True) or full.split("/")[-1]
            files.append(
                {
                    "name": name,
                    "url": full,
                    "ext": urlparse(full).path.lower().rsplit(".", 1)[-1],
                    "modified": _parse_listing_date(tag),
                }
            )

    return {"url": url, "subdirs": subdirs, "files": files}


def _parse_listing_date(tag) -> str | None:
    """
    Best-effort: pull a YYYY-MM-DD date from the autoindex row containing this
    link. Apache renders it as plain text after the <a>. Returns None if absent.
    """

    # The date sits in the tail text of the row — walk siblings after the link.
    tail = ""
    for sib in tag.next_siblings:
        tail += str(sib)
        if len(tail) > 80:
            break
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", tail)
    return m.group(1) if m else None


def main() -> None:
    paths = AppPaths.resolve()

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

        from dataset_prober.run import display_results

        display_results(results)

        save_results(results, str(paths.probe_results_path))


if __name__ == "__main__":
    main()
