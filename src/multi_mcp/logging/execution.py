"""
Execution Logger — Multi-MCP

Records the actual stdout/stderr/result of each tool call.
This is SEPARATE from the audit log:
  - Audit log = who/what/when/success-or-fail  (always retained, minimal)
  - Execution log = raw output / artifacts      (may be large, shorter retention)

Log format: JSON-lines.
Default location: logs/execution/execution.jsonl
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


class ExecutionLogger:
    """
    Append-only execution log writer.

    Each entry contains:
      - timestamp (ISO-8601 UTC)
      - tool_name
      - server_name
      - result (truncated/masked by enforcement middleware before reaching here)
      - error (if any)
    """

    def __init__(self, log_dir: str | Path = "logs/execution") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_dir / "execution.jsonl"

    def _write(self, entry: dict[str, Any]) -> None:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            with self._log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            _std_logger.error("Failed to write execution log: %s", exc)

    def log(
        self,
        request: ToolCallRequest,
        server_name: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "tool_name": request.tool_name,
            "server_name": server_name,
        }
        if result is not None:
            entry["result"] = result
        if error is not None:
            entry["error"] = error
        self._write(entry)

    def read_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last *limit* execution log entries."""
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
