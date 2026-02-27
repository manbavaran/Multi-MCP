"""
Filesystem Adapter — Multi-MCP

Thin adapter that wraps filesystem operations with policy enforcement.
The enforcement middleware (allowed_root, symlink resolution, read/write split)
is applied BEFORE this adapter is called, so this adapter can trust that
the path has already been validated.

Preferred strategy (AGENTS.md §5.3):
  Use @modelcontextprotocol/server-filesystem (Node.js) as the actual sub-server
  and proxy calls to it. This adapter provides a Python fallback for environments
  where Node.js is not available.

Tools exposed:
  - read_file(path)         → read-only
  - list_directory(path)    → read-only
  - write_file(path, content) → write (requires allow_write=True in policy)
  - delete_file(path)       → write (requires allow_write=True in policy)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from multi_mcp.models.config import FilesystemPolicy, ToolCallRequest


EXPOSED_TOOLS = [
    "read_file",
    "list_directory",
    "write_file",
    "delete_file",
]


class FilesystemAdapter:
    """
    Python-native filesystem adapter.
    All path validation is done by EnforcementMiddleware before this is called.
    """

    def __init__(self, policy: FilesystemPolicy) -> None:
        self.policy = policy
        self._root = Path(policy.allowed_root).resolve()

    def list_tools(self) -> list[str]:
        tools = ["read_file", "list_directory"]
        if self.policy.allow_write:
            tools += ["write_file", "delete_file"]
        return tools

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        args = request.args

        if tool == "read_file":
            return self._read_file(args["path"])
        elif tool == "list_directory":
            return self._list_directory(args.get("path", "."))
        elif tool == "write_file":
            return self._write_file(args["path"], args["content"])
        elif tool == "delete_file":
            return self._delete_file(args["path"])
        else:
            raise ValueError(f"Unknown tool: {tool}")

    def _resolve(self, rel_path: str) -> Path:
        """Resolve a relative path against allowed_root (already validated by middleware)."""
        return (self._root / rel_path.lstrip("/")).resolve()

    def _read_file(self, path: str) -> dict[str, Any]:
        full = self._resolve(path)
        if not full.exists():
            return {"error": f"File not found: {path}"}
        if not full.is_file():
            return {"error": f"Not a file: {path}"}
        content = full.read_text(encoding="utf-8", errors="replace")
        return {"path": str(full), "content": content, "size_bytes": full.stat().st_size}

    def _list_directory(self, path: str) -> dict[str, Any]:
        full = self._resolve(path)
        if not full.exists():
            return {"error": f"Directory not found: {path}"}
        if not full.is_dir():
            return {"error": f"Not a directory: {path}"}
        entries = []
        for entry in sorted(full.iterdir()):
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size_bytes": entry.stat().st_size if entry.is_file() else None,
            })
        return {"path": str(full), "entries": entries}

    def _write_file(self, path: str, content: str) -> dict[str, Any]:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return {"path": str(full), "written_bytes": len(content.encode())}

    def _delete_file(self, path: str) -> dict[str, Any]:
        full = self._resolve(path)
        if not full.exists():
            return {"error": f"File not found: {path}"}
        full.unlink()
        return {"path": str(full), "deleted": True}
