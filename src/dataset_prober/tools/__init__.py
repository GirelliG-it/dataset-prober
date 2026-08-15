"""
src/tools/__init__.py

Tool factory — instantiates the correct DataSourceTool implementation
based on the catalog adapter from validated profile configuration.

Adding a new data source:
    1. Create a new tool class in src/tools/your_tool.py
    2. Inherit from DataSourceTool and implement all abstract methods
    3. Register it in TOOL_REGISTRY below
    4. Add a catalog entry in the relevant profile YAML

That's it. The agent, config loader, and prompt interpreter need no changes.
"""

from dataset_prober.tools.base import DatasetResult, DataSourceTool
from dataset_prober.tools.cbs_tool import CBSTool
from dataset_prober.tools.ckan_tool import CKANTool
from dataset_prober.tools.tavily_tool import TavilyTool

# Registry mapping adapter name → tool class
# Add new adapters here as new sources are implemented
TOOL_REGISTRY: dict[str, type[DataSourceTool]] = {
    "cbs": CBSTool,
    "ckan": CKANTool,
    "tavily": TavilyTool,
}


def create_tool(catalog_type: str, config: dict) -> DataSourceTool:
    """
    Instantiate a tool by catalog type.

    Args:
        catalog_type: Validated adapter string from profile YAML (e.g. "cbs", "ckan")
        config: Full catalog config dict from profile

    Returns:
        Configured DataSourceTool instance

    Raises:
        ValueError: If catalog_type is not registered
    """
    tool_class = TOOL_REGISTRY.get(catalog_type)
    if not tool_class:
        available = ", ".join(TOOL_REGISTRY.keys())
        raise ValueError(f"Unknown catalog type: '{catalog_type}'. Available types: {available}")
    return tool_class(config)


__all__ = [
    "DataSourceTool",
    "DatasetResult",
    "CBSTool",
    "CKANTool",
    "TavilyTool",
    "TOOL_REGISTRY",
    "create_tool",
]
