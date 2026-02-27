"""
Logs Adapter — Multi-MCP

Provides read-only access to system/service logs with masking and line limits.
All output is filtered through the masking patterns defined in LogsPolicy.

Tools exposed:
  - read_log(source, lines=100, since_minutes=10)
  - list_log_sources()
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from multi_mcp.models.config import LogsPolicy, ToolCallRequest


EXPOSED_TOOLS = ["read_log", "list_log_sources"]

# Default allowed log sources (paths or service names)
_DEFAULT_SOURCES: dict[str, str] = {
    "multi-mcp-audit": "logs/audit/audit.jsonl",
    "multi-mcp-exec": "logs/execution/execution.jsonl",
    "syslog": "/var/log/syslog",
}


class LogsAdapter:
    """
    Read-only log adapter with masking and line limits.
    """

    def __init__(self, policy: LogsPolicy, allowed_sources: dict[str, str] | None = None) -> None:
        self.policy = policy
        self._sources = allowed_sources or _DEFAULT_SOURCES
        self._mask_patterns = [re.compile(p) for p in policy.masking_patterns]

    def list_tools(self) -> list[str]:
        return EXPOSED_TOOLS

    async def call(self, request: ToolCallRequest) -> dict[str, Any]:
        tool = request.tool_name
        args = request.args

        if tool == "list_log_sources":
            return {"sources": list(self._sources.keys())}
        elif tool == "read_log":
            return await self._read_log(
                source=args["source"],
                lines=min(int(args.get("lines", 100)), self.policy.max_lines),
                since_minutes=int(args.get("since_minutes", self.policy.time_window_minutes)),
            )
        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _read_log(
        self,
        source: str,
        lines: int,
        since_minutes: int,
    ) -> dict[str, Any]:
        if source not in self._sources:
            return {"error": f"Log source '{source}' not allowed. Available: {list(self._sources.keys())}"}

        path = Path(self._sources[source])
        if not path.exists():
            return {"source": source, "lines": [], "count": 0}

        # Read last N lines
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = all_lines[-lines:]

        # Apply masking
        masked = [self._mask(line) for line in recent]

        return {
            "source": source,
            "path": str(path),
            "lines": masked,
            "count": len(masked),
            "total_lines": len(all_lines),
        }

    def _mask(self, text: str) -> str:
        for pattern in self._mask_patterns:
            text = pattern.sub("[REDACTED]", text)
        return text
