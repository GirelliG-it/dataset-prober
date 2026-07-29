"""
src/tools/__init__.py

Tool factory — instantiates the correct DataSourceTool implementation
based on catalog type from profile configuration.

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

# Registry mapping catalog type → tool class
# Add new tool types here as new sources are implemented
TOOL_REGISTRY: dict[str, type[DataSourceTool]] = {
    "cbs": CBSTool,
    "ckan": CKANTool,
    "tavily": TavilyTool,
}


def create_tool(catalog_type: str, config: dict) -> DataSourceTool:
    """
    Instantiate a tool by catalog type.

    Args:
        catalog_type: Type string from profile YAML (e.g. "cbs", "ckan")
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


def tools_for_profile(profile) -> list[DataSourceTool]:
    """
    Instantiate all tools for a given profile, in priority order.
    Skips tools that are not available (missing dependencies or API keys).

    Args:
        profile: A Profile object from config_loader

    Returns:
        List of DataSourceTool instances sorted by catalog priority
    """
    tools = []
    catalogs = sorted(profile.catalogs, key=lambda c: c.priority)

    for catalog in catalogs:
        try:
            # Build tool config from catalog + profile settings
            tool_config = {
                **catalog.__dict__,
                "trusted_domains": profile.trusted_domains,
                "blocked_sources": profile.blocked_sources,
                "sample_rows": profile.budget.sample_rows,
                "download_timeout_seconds": profile.budget.download_timeout_seconds,
            }
            tool = create_tool(catalog.type, tool_config)

            if tool.is_available():
                tools.append(tool)
            else:
                from rich.console import Console

                Console().print(
                    f"[yellow]⚠️  Tool '{catalog.name}' is not available "
                    f"(missing dependencies or API key)[/yellow]"
                )
        except Exception as e:
            from rich.console import Console

            Console().print(f"[red]Failed to initialize tool '{catalog.name}': {e}[/red]")

    # Always add Tavily as fallback if available
    tavily_config = {
        "name": "Web Search (Tavily)",
        "type": "tavily",
        "base_url": "",
        "api_key_env": "TAVILY_API_KEY",
        "timeout_seconds": 30,
        "priority": 99,
        "trusted_domains": profile.trusted_domains,
        "blocked_sources": profile.blocked_sources,
        "sample_rows": profile.budget.sample_rows,
        "download_timeout_seconds": profile.budget.download_timeout_seconds,
    }
    tavily = TavilyTool(tavily_config)
    if tavily.is_available():
        tools.append(tavily)

    return tools


__all__ = [
    "DataSourceTool",
    "DatasetResult",
    "CBSTool",
    "CKANTool",
    "TavilyTool",
    "TOOL_REGISTRY",
    "create_tool",
    "tools_for_profile",
]
