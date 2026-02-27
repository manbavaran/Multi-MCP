"""
Audit Logger — Multi-MCP

Records WHO called WHAT tool via WHICH alias, and whether it SUCCEEDED or FAILED.
Sensitive values (credentials, keys) are never written to the audit log.

Log format: JSON-lines (one JSON object per line) for easy parsing.
Default location: logs/audit/audit.jsonl
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from multi_mcp.models.config import ToolCallRequest

_std_logger = logging.getLogger(__name__)


class AuditLogger:
    """
    Append-only audit log writer.

    Each entry contains:
      - timestamp (ISO-8601 UTC)
      - event: "tool_call_success" | "tool_call_failure"
      - tool_name
      - client_profile
      - server_name (if resolved)
      - alias (if present in request args — value masked)
      - error (if failure)
    """

    def __init__(self, log_dir: str | Path = "logs/audit") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_dir / "audit.jsonl"

    def _write(self, entry: dict[str, Any]) -> None:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            with self._log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            _std_logger.error("Failed to write audit log: %s", exc)

    def log_success(
        self,
        request: ToolCallRequest,
        client_profile: str,
        server_name: str,
    ) -> None:
        self._write(
            {
                "event": "tool_call_success",
                "tool_name": request.tool_name,
                "client_profile": client_profile,
                "server_name": server_name,
                "alias": self._extract_alias(request),
            }
        )

    def log_failure(
        self,
        request: ToolCallRequest,
        client_profile: str,
        error: str,
    ) -> None:
        self._write(
            {
                "event": "tool_call_failure",
                "tool_name": request.tool_name,
                "client_profile": client_profile,
                "error": error,
                "alias": self._extract_alias(request),
            }
        )

    @staticmethod
    def _extract_alias(request: ToolCallRequest) -> str | None:
        """Return the alias from the request args, if present (never the raw value)."""
        return request.args.get("alias")

    def read_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last *limit* audit entries (newest last)."""
        if not self._log_file.exists():
            return []
        lines = self._log_file.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
