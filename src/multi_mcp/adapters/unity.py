"""
unity.py — Multi-MCP adapter for Unity Editor MCP Bridge (HTTP transport)

Connects to a running UnityMcpBridge inside the Unity Editor via HTTP.
The bridge exposes a JSON-RPC 2.0 endpoint at POST /mcp.

Registration example (Multi-MCP GUI → Sub-servers):
    name:      unity-editor-1
    type:      other
    transport: http
    endpoint:  http://127.0.0.1:23457/mcp
    env:       dev

Security notes:
- The bridge runs on 127.0.0.1 only (localhost-only by design).
- Optional Bearer token is stored as an alias in SecretStore, never in plaintext.
- All calls are logged to audit.jsonl (no secrets written).
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from ..logging.audit import AuditLogger

_audit = AuditLogger()

# ── Default connection settings ──────────────────────────────────────────────
DEFAULT_TIMEOUT_S = 15          # Unity main-thread dispatch can be slow
DEFAULT_MAX_RETRIES = 2
_RPC_ID_COUNTER = 0


def _next_id() -> int:
    global _RPC_ID_COUNTER
    _RPC_ID_COUNTER += 1
    return _RPC_ID_COUNTER


# ── Low-level HTTP helpers ────────────────────────────────────────────────────

def _build_headers(token: str | None = None) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _post_rpc(
    endpoint: str,
    method: str,
    params: dict | None = None,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Send a JSON-RPC 2.0 request to the Unity bridge and return the parsed response."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
    }
    if params:
        payload["params"] = params

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                headers=_build_headers(token),
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            raise RuntimeError(f"Unity bridge request failed: {e}") from e

    raise RuntimeError(
        f"Unity bridge unreachable at {endpoint} after {retries + 1} attempts. "
        f"Is UnityMcpBridge running? Last error: {last_err}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

class UnityAdapter:
    """
    Thin adapter that wraps the Unity MCP Bridge HTTP endpoint.

    Usage:
        adapter = UnityAdapter(endpoint="http://127.0.0.1:23457/mcp", token=None)
        tools   = adapter.list_tools()
        result  = adapter.call_tool("unity.manage_gameobject", {"action": "find", "query": "Player"})
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:23457/mcp",
        token: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
        server_name: str = "unity-editor",
        env: str = "dev",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token          # resolved from SecretStore alias by caller
        self.timeout = timeout
        self.server_name = server_name
        self.env = env

    # ── Discovery ────────────────────────────────────────────────────────────

    def list_tools(self) -> list[dict]:
        """Call tools/list on the Unity bridge and return the tool definitions."""
        try:
            rpc_resp = _post_rpc(
                self.endpoint,
                method="tools/list",
                token=self.token,
                timeout=self.timeout,
            )
            tools = rpc_resp.get("result", {}).get("tools", [])
            _audit.log_success(
                tool="tools/list",
                server=self.server_name,
                env=self.env,
                extra={"discovered": len(tools)},
            )
            return tools
        except Exception as e:
            _audit.log_failure(
                tool="tools/list",
                server=self.server_name,
                env=self.env,
                reason=str(e),
            )
            raise

    # ── Tool call ─────────────────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """
        Invoke a Unity tool via JSON-RPC tools/call.

        Returns the inner result dict (ok, result/error fields from Unity).
        Raises RuntimeError if the bridge is unreachable or returns an RPC error.
        """
        arguments = arguments or {}
        try:
            rpc_resp = _post_rpc(
                self.endpoint,
                method="tools/call",
                params={"name": tool_name, "arguments": arguments},
                token=self.token,
                timeout=self.timeout,
            )

            if "error" in rpc_resp:
                err = rpc_resp["error"]
                _audit.log_failure(
                    tool=tool_name,
                    server=self.server_name,
                    env=self.env,
                    reason=f"RPC error {err.get('code')}: {err.get('message')}",
                )
                raise RuntimeError(f"Unity RPC error: {err}")

            result = rpc_resp.get("result", {})
            success = result.get("ok", True)

            if success:
                _audit.log_success(
                    tool=tool_name,
                    server=self.server_name,
                    env=self.env,
                )
            else:
                _audit.log_failure(
                    tool=tool_name,
                    server=self.server_name,
                    env=self.env,
                    reason=result.get("error", "unknown"),
                )

            return result

        except RuntimeError:
            raise
        except Exception as e:
            _audit.log_failure(
                tool=tool_name,
                server=self.server_name,
                env=self.env,
                reason=str(e),
            )
            raise RuntimeError(f"Unity tool call failed: {e}") from e

    # ── Health check ─────────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """
        GET /health on the bridge (not MCP endpoint).
        Returns {"status": "ok", "version": "2", ...} or raises on failure.
        """
        health_url = self.endpoint.replace("/mcp", "") + "/health"
        try:
            resp = requests.get(
                health_url,
                headers=_build_headers(self.token),
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Unity bridge health check failed: {e}") from e


# ── Factory helper (used by hub/factory.py) ───────────────────────────────────

def create_unity_adapter(server_config: dict, env: str = "dev") -> UnityAdapter:
    """
    Create a UnityAdapter from a SubServerConfig dict.

    The `token` is resolved from the SecretStore alias stored in
    server_config["auth_alias"] — never from plaintext config.
    """
    endpoint = server_config.get("endpoint", "http://127.0.0.1:23457/mcp")
    server_name = server_config.get("name", "unity-editor")

    # Token resolution: caller (hub/factory.py) passes resolved token if alias set
    token = server_config.get("_resolved_token")  # injected by factory, never stored

    return UnityAdapter(
        endpoint=endpoint,
        token=token,
        server_name=server_name,
        env=env,
    )
