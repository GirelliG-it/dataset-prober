"""
src/tools/tavily_tool.py

Tavily-based web search and extraction tool.
Used as a fallback when catalog APIs don't find results,
and as the primary tool for the global discovery profile.

Tavily handles JavaScript-rendered pages — solving the JS wall problem
without needing Playwright or a headless browser.
"""

import os
import re

from tools.base import (
    DatasetResult,
    DataSourceTool,
    ensure_httpfs,
    load_csv_to_table,
    safe_table_name,
)

DATASET_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".parquet", ".geojson"}


class TavilyTool(DataSourceTool):
    """
    Web search and page extraction tool using Tavily AI.

    Search: Tavily search API — returns ranked web results
    Fetch:  Tavily extract API — pulls content from JS-rendered pages
            and finds direct dataset file URLs
    Download: Delegates to DuckDB httpfs for CSV files

    This tool is the fallback for sources not covered by CBS or CKAN tools.
    For the global profile, it is the primary tool.
    """

    @property
    def source_name(self) -> str:
        return "Web Search (Tavily)"

    @property
    def source_type(self) -> str:
        return "tavily"

    def is_available(self) -> bool:
        try:
            from tavily import TavilyClient  # noqa
            key = os.environ.get("TAVILY_API_KEY")
            return bool(key)
        except ImportError:
            return False

    def _client(self):
        """Lazily initialize Tavily client."""
        from tavily import TavilyClient
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            raise EnvironmentError(
                "TAVILY_API_KEY not set. Add it to your .env file."
            )
        return TavilyClient(api_key=key)

    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search the web for dataset sources using Tavily.
        Applies trusted domain filtering from profile config.
        """
        timeout = self.config.get("timeout_seconds", 30)
        trusted_domains = self.config.get("trusted_domains", [])
        blocked_sources = self.config.get("blocked_sources", [])

        try:
            client = self._client()

            # Build search kwargs
            search_kwargs = {
                "query": keyword,
                "search_depth": "advanced",
                "max_results": max_results,
                "timeout": timeout,
            }

            # Apply domain filter if trusted domains are specified
            if trusted_domains:
                # Tavily include_domains expects full domain names
                clean_domains = [
                    d.lstrip(".") for d in trusted_domains
                    if not d.startswith("http")
                ]
                if clean_domains:
                    search_kwargs["include_domains"] = clean_domains[:5]  # Tavily limit

            response = client.search(**search_kwargs)
            results = []

            for r in response.get("results", []):
                url = r.get("url", "")

                # Skip blocked sources
                if any(blocked in url for blocked in blocked_sources):
                    continue

                # Check if it's a direct dataset file
                is_direct = self._is_dataset_url(url)

                result = DatasetResult(
                    id=url,
                    title=r.get("title", url),
                    description=r.get("content", "")[:300],
                    source=self.source_type,
                    source_name=self.source_name,
                    url=url,
                    download_url=url if is_direct else None,
                    format=self._detect_format(url) if is_direct else None,
                    modified=None,
                    frequency=None,
                    license=None,
                    license_url=None,
                    row_count=None,
                    columns=None,
                    sample=None,
                    language=None,
                    tags=[],
                    status="found"
                )
                results.append(result)

            return results

        except Exception as e:
            return [self._error_result(
                id=f"tavily_search_{keyword}",
                title=f"Web search failed: {keyword}",
                error=str(e)
            )]

    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Extract content from a URL using Tavily.
        Finds direct dataset file URLs in page content.
        For direct CSV URLs, probes with DuckDB httpfs.
        """
        timeout = self.config.get("timeout_seconds", 30)
        blocked_sources = self.config.get("blocked_sources", [])

        # Check blocked
        if any(blocked in dataset_id for blocked in blocked_sources):
            return self._error_result(
                id=dataset_id,
                title=dataset_id,
                error="Source is blocked in current profile"
            )

        # If it's already a direct dataset URL, probe it
        if self._is_dataset_url(dataset_id):
            return self._probe_direct(dataset_id, sample_rows, timeout)

        # Otherwise, extract page content to find dataset URLs
        try:
            client = self._client()
            response = client.extract(urls=[dataset_id])
            extract_results = response.get("results", [])

            if not extract_results:
                return self._error_result(
                    id=dataset_id,
                    title=dataset_id,
                    error="No content extracted from page"
                )

            content = extract_results[0].get("raw_content", "")[:5000]

            # Find dataset URLs in content
            dataset_urls = self._find_dataset_urls(content)

            if not dataset_urls:
                # Return as a landing page result — agent can decide what to do
                return DatasetResult(
                    id=dataset_id,
                    title=extract_results[0].get("url", dataset_id),
                    description=content[:300],
                    source=self.source_type,
                    source_name=self.source_name,
                    url=dataset_id,
                    download_url=None,
                    format=None,
                    modified=None,
                    frequency=None,
                    license=None,
                    license_url=None,
                    row_count=None,
                    columns=None,
                    sample=None,
                    language=None,
                    tags=[],
                    status="found",
                    error=f"No direct dataset URLs found. Page content extracted ({len(content)} chars)."
                )

            # Probe the first found CSV URL
            best_url = dataset_urls[0]
            result = self._probe_direct(best_url, sample_rows, timeout)
            result.url = dataset_id  # Keep original page URL as reference
            return result

        except Exception as e:
            return self._error_result(id=dataset_id, title=dataset_id, error=str(e))

    def _probe_direct(
        self,
        url: str,
        sample_rows: int,
        timeout: int
    ) -> DatasetResult:
        """Probe a direct CSV URL using DuckDB httpfs."""
        try:
            import duckdb
            con = duckdb.connect()
            con.execute("INSTALL httpfs; LOAD httpfs;")

            describe = con.execute(
                "DESCRIBE SELECT * FROM read_csv_auto(?) LIMIT 1", [url]
            ).fetchall()
            columns = [{"name": row[0], "type": row[1]} for row in describe]

            sample_data = con.execute(
                f"SELECT * FROM read_csv_auto(?) LIMIT {int(sample_rows)}", [url]
            ).fetchall()

            try:
                count = con.execute(
                    "SELECT COUNT(*) FROM read_csv_auto(?)", [url]
                ).fetchone()[0]
            except Exception:
                count = None

            con.close()

            # Extract filename as title
            title = url.split("/")[-1].split("?")[0] or url

            return DatasetResult(
                id=url,
                title=title,
                description=f"Direct CSV file from {url}",
                source=self.source_type,
                source_name=self.source_name,
                url=url,
                download_url=url,
                format="CSV",
                modified=None,
                frequency=None,
                license=None,
                license_url=None,
                row_count=count,
                columns=columns,
                sample=[list(row) for row in sample_data[:3]],
                language=None,
                tags=[],
                status="probed"
            )

        except Exception as e:
            return self._error_result(id=url, title=url, error=f"Probe failed: {str(e)}")

    def download(self, dataset: DatasetResult, db_path: str) -> DatasetResult:
        """Download a CSV dataset into DuckDB using httpfs."""
        if not dataset.download_url:
            dataset.status = "failed"
            dataset.error = "No download URL available"
            return dataset

        try:
            import duckdb

            table_name = safe_table_name(dataset.id, dataset.title)

            con = duckdb.connect(db_path)
            ensure_httpfs(con)
            actual_rows = load_csv_to_table(con, table_name, dataset.download_url)
            con.close()

            dataset.row_count = actual_rows
            dataset.status = "downloaded"
            return dataset

        except Exception as e:
            dataset.status = "failed"
            dataset.error = str(e)
            return dataset

    def _is_dataset_url(self, url: str) -> bool:
        """Check if URL points directly to a dataset file."""
        path = url.split("?")[0].lower()
        return any(path.endswith(ext) for ext in DATASET_EXTENSIONS)

    def _detect_format(self, url: str) -> str | None:
        """Detect file format from URL extension."""
        path = url.split("?")[0].lower()
        for ext in DATASET_EXTENSIONS:
            if path.endswith(ext):
                return ext.lstrip(".").upper()
        return None

    def _find_dataset_urls(self, content: str) -> list[str]:
        """Extract direct dataset file URLs from page content."""
        pattern = r'https?://[^\s<>"\']+?\.(?:csv|xlsx|xls|json|parquet|geojson)'
        found = re.findall(pattern, content, re.IGNORECASE)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for url in found:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def _error_result(self, id: str, title: str, error: str) -> DatasetResult:
        """Create a failed DatasetResult."""
        return DatasetResult(
            id=id,
            title=title,
            description="",
            source=self.source_type,
            source_name=self.source_name,
            url=id,
            download_url=None,
            format=None,
            modified=None,
            frequency=None,
            license=None,
            license_url=None,
            row_count=None,
            columns=None,
            sample=None,
            language=None,
            tags=[],
            status="failed",
            error=error
        )
