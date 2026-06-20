"""
src/tools/base.py

Base interface and standardized data structures for all data source tools.
Every tool implementation must inherit from DataSourceTool and implement
all abstract methods. All tools return DatasetResult objects — no tool-specific
data structures leak into the agent layer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DatasetResult:
    """
    Standardized dataset representation across all data sources.
    Every tool returns this structure — CBS, CKAN, OpenDataSoft, Tavily.
    The agent works exclusively with DatasetResult objects.
    """
    # Identity
    id: str                              # Source-specific identifier (CBS table ID, CKAN package name, URL)
    title: str                           # Human-readable dataset name
    description: str                     # What the dataset contains
    source: str                          # Tool that found this: "cbs", "ckan", "opendatasoft", "tavily"
    source_name: str                     # Human-readable source name: "CBS", "Data.gov", "Den Haag Open Data"

    # Access
    url: str                             # Catalog or landing page URL
    download_url: Optional[str]          # Direct file URL (CSV, parquet etc.) if known
    format: Optional[str]               # "CSV", "JSON", "parquet", "OData" etc.

    # Freshness
    modified: Optional[str]             # Last update date (ISO format preferred)
    frequency: Optional[str]            # Update frequency: "monthly", "annual" etc.

    # License (CCREL/ODRL)
    license: Optional[str]              # "CC0", "CC-BY", "CC-BY-SA", "CC-BY-NC", "other", "unknown"
    license_url: Optional[str]          # Full license URL

    # Schema (populated after probe/fetch)
    row_count: Optional[int]            # Number of rows — None until probed
    columns: Optional[list]             # Column definitions — None until probed
    sample: Optional[list]              # Sample rows — None until probed

    # Discovery metadata
    language: Optional[str]             # Dataset language: "nl", "en" etc.
    tags: list = field(default_factory=list)  # Keywords/topics

    # Pipeline status
    status: str = "found"               # "found" → "probed" → "downloaded" → "failed" → "skipped"
    error: Optional[str] = None         # Error message if status is "failed"

    # Cost tracking
    tokens_used: int = 0                # Tokens consumed discovering/fetching this dataset
    cost_usd: float = 0.0               # Estimated cost in USD

    def freshness_days(self) -> Optional[int]:
        """Return how many days ago this dataset was last modified. None if unknown."""
        if not self.modified:
            return None
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y"):
            try:
                dt = datetime.strptime(self.modified.strip()[:19], fmt)
                return (datetime.now() - dt).days
            except ValueError:
                continue
        return None

    def passes_freshness(self, max_days: int) -> Optional[bool]:
        """
        Check if dataset meets a freshness requirement.
        Returns None if freshness cannot be determined.
        """
        days = self.freshness_days()
        if days is None:
            return None
        return days <= max_days

    def license_grade(self) -> str:
        """
        Return a human-readable license grade.
        A = unrestricted, B = attribution required, C = restricted, ? = unknown
        """
        if not self.license:
            return "?"
        lic = self.license.upper()
        if "CC0" in lic or "PUBLIC DOMAIN" in lic or "PDDL" in lic:
            return "A"
        if "CC-BY" in lic and "NC" not in lic and "SA" not in lic:
            return "B"
        if "CC-BY-SA" in lic:
            return "B-"
        if "NC" in lic:
            return "C"
        return "?"

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON output."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "source_name": self.source_name,
            "url": self.url,
            "download_url": self.download_url,
            "format": self.format,
            "modified": self.modified,
            "frequency": self.frequency,
            "license": self.license,
            "license_url": self.license_url,
            "license_grade": self.license_grade(),
            "row_count": self.row_count,
            "columns": self.columns,
            "language": self.language,
            "tags": self.tags,
            "status": self.status,
            "error": self.error,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6)
        }


class DataSourceTool(ABC):
    """
    Abstract base class for all data source tools.

    Every tool must implement:
      - search(keyword, max_results) → list[DatasetResult]
      - fetch(dataset_id, sample_rows) → DatasetResult
      - download(dataset, db_path) → DatasetResult

    Tools receive all configuration from the profile at instantiation.
    No default values are hardcoded — all limits come from config.
    """

    def __init__(self, config: dict):
        """
        Initialize tool with profile configuration.

        Args:
            config: Dictionary from profile YAML containing all tool settings.
                    No fallback defaults — if a key is missing, raise KeyError
                    so misconfigured profiles fail loudly.
        """
        self.config = config

    @abstractmethod
    def search(self, keyword: str, max_results: int) -> list[DatasetResult]:
        """
        Search the catalog for datasets matching a keyword.

        Args:
            keyword: Search term (may be in any language)
            max_results: Maximum number of results to return (from config)

        Returns:
            List of DatasetResult objects with status="found"
        """
        pass

    @abstractmethod
    def fetch(self, dataset_id: str, sample_rows: int) -> DatasetResult:
        """
        Fetch metadata and sample data for a specific dataset.

        Args:
            dataset_id: Source-specific identifier
            sample_rows: Number of sample rows to retrieve (from config)

        Returns:
            DatasetResult with status="probed", columns and sample populated
        """
        pass

    @abstractmethod
    def download(self, dataset: DatasetResult, db_path: str) -> DatasetResult:
        """
        Download the full dataset into a local DuckDB database.

        Args:
            dataset: A DatasetResult with status="probed"
            db_path: Path to the DuckDB file

        Returns:
            DatasetResult with status="downloaded" and row_count updated,
            or status="failed" with error populated
        """
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable name of this data source (e.g. 'CBS Statistics Netherlands')"""
        pass

    @property
    @abstractmethod
    def source_type(self) -> str:
        """
        Machine-readable source type identifier.
        Used by config_loader to instantiate the correct tool class.
        Values: 'cbs', 'ckan', 'opendatasoft', 'tavily'
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this tool's dependencies and credentials are available.
        Override in subclasses that require API keys or external services.
        Returns True by default.
        """
        return True
