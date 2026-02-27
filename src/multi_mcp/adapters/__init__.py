"""
Adapters package — Multi-MCP

Each adapter wraps a specific MCP server capability.
Adapters are instantiated by the hub factory based on the SubServerConfig.
"""

from multi_mcp.adapters.artifact import ArtifactAdapter
from multi_mcp.adapters.exec import ExecAdapter
from multi_mcp.adapters.filesystem import FilesystemAdapter
from multi_mcp.adapters.search import SearchAdapter
from multi_mcp.adapters.ssh import SSHAdapter

__all__ = [
    "ArtifactAdapter",
    "ExecAdapter",
    "FilesystemAdapter",
    "SearchAdapter",
    "SSHAdapter",
]
