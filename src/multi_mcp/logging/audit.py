"""
Audit Logger — Multi-MCP

Records WHO called WHAT tool, on WHICH server, in WHICH environment,
and whether it SUCCEEDED or FAILED.

CRITICAL — this log MUST NEVER contain:
  - API keys, passwords, SSH credentials, or any secret value
  - Raw tool arguments (may contain sensitive data)
  - stdout/stderr output (that goes to ExecutionLogger)

Fields recorded per event:
  - event: "tool_call_success" | "tool_call_failure" | "discovery_ok" | "discovery_error"
  - tool_name: name of the tool called
  - client_profile: profile used by the caller
  - server_name: sub-server that handled the call (or None if routing failed)
  - env: environment (dev/stage/prod)
  - alias: alias name only — NEVER the resolved value
  - error: sanitised error message (failures only — secrets stripped)
  - timestamp: ISO 8601 UTC

Log format: JSON-lines (one JSON object per line) for easy parsing.
Default location: logs/audit/audit.jsonl
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from multi_mcp.models.config import ToolCallRequest

_std_logger = logging.getLogger(__name__)

# Patterns to strip from error messages before logging
_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"tvly-[A-Za-z0-9]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
]


def _sanitise(text: str | None) -> str | None:
    """Strip secrets from a string before writing to audit log."""
    if text is None:
        return None
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text[:1000]  # cap length


class AuditLogger:
    """
    Append-only audit log writer.

    Each entry contains only non-sensitive metadata:
      - timestamp (ISO-8601 UTC)
      - event type
      - tool_name
      - client_profile
      - server_name (sub-server that handled the call)
      - env (dev/stage/prod)
      - alias (name only, never the resolved credential)
      - error (sanitised, failures only)
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
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._write({
            "event": "tool_call_success",
            "tool_name": request.tool_name,
            "client_profile": client_profile,
            "server_name": server_name,
            "env": (extra or {}).get("env"),
            "alias": self._extract_alias(request),
            "request_id": request.request_id,
        })

    def log_failure(
        self,
        request: ToolCallRequest,
        client_profile: str,
        error: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        ex = extra or {}
        # Determine event type: separate not_configured from generic failures
        if error.startswith("core_server_not_configured:"):
            event = "core_not_configured"
        else:
            event = "tool_call_failure"

        entry: dict[str, Any] = {
            "event": event,
            "tool_name": request.tool_name,
            "client_profile": client_profile,
            "server_name": ex.get("server"),
            "env": ex.get("env"),
            "error": _sanitise(error),
            "alias": self._extract_alias(request),
            "request_id": request.request_id,
        }
        # For not_configured events, include which items are missing (no secrets)
        if event == "core_not_configured" and ex.get("missing_items"):
            entry["missing_items"] = ex["missing_items"]

        self._write(entry)

    def log_discovery(
        self,
        server_name: str,
        status: str,
        tool_count: int = 0,
        error: str | None = None,
        env: str | None = None,
    ) -> None:
        """Log the result of a tools/list discovery attempt."""
        self._write({
            "event": f"discovery_{status}",
            "server_name": server_name,
            "env": env,
            "tool_count": tool_count,
            "error": _sanitise(error) if error else None,
        })

    @staticmethod
    def _extract_alias(request: ToolCallRequest) -> str | None:
        """Return the alias name from request args (never the resolved value)."""
        return request.args.get("alias")

    def read_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the last *limit* audit entries (newest first)."""
        if not self._log_file.exists():
            return []
        lines = self._log_file.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in reversed(lines[-limit:]):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
