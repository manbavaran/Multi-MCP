"""
Sub-server Discovery — Multi-MCP

Calls tools/list on registered sub-servers and caches the result.

Supported transports:
  - builtin:   Query the Python adapter directly (adapter.list_tools())
  - http:      GET {endpoint}/tools/list  (MCP HTTP SSE convention)
  - stdio:     Launch process, send {"jsonrpc":"2.0","method":"tools/list","id":1},
               read response, terminate process.
  - websocket: Connect, send tools/list request, read response.

The discovery result is stored in SubServerConfig.discovery (DiscoveryCache).
It is NOT persisted to disk automatically — call SettingsManager.save() after
running discovery if you want to persist the cache.

Security:
  - Discovery never passes secrets to sub-servers.
  - Error messages are sanitised before being stored in DiscoveryCache.
  - The discovery module does NOT modify any policy or enforcement settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from multi_mcp.models.config import (
    DiscoveredTool,
    DiscoveryCache,
    DiscoveryStatus,
    SubServerConfig,
    TransportType,
)

logger = logging.getLogger(__name__)

# Timeout for a single discovery attempt
_DISCOVERY_TIMEOUT_SEC = 15


class DiscoveryService:
    """
    Discovers tools from sub-servers and updates their DiscoveryCache.
    """

    async def discover(self, server: SubServerConfig) -> DiscoveryCache:
        """
        Run tools/list discovery for a single sub-server.
        Updates server.discovery in place and returns the new cache.
        """
        if not server.enabled:
            cache = DiscoveryCache(status=DiscoveryStatus.disabled)
            server.discovery = cache
            return cache

        now = datetime.now(timezone.utc)
        cache = DiscoveryCache(
            status=DiscoveryStatus.pending,
            last_attempted_at=now,
        )

        try:
            tools = await asyncio.wait_for(
                self._fetch_tools(server),
                timeout=_DISCOVERY_TIMEOUT_SEC,
            )
            cache.status = DiscoveryStatus.ok
            cache.tools = tools
            cache.last_succeeded_at = now
            cache.error_message = None
            logger.info(
                "Discovery OK: server=%s transport=%s tools=%d",
                server.name, server.transport.value, len(tools),
            )
        except asyncio.TimeoutError:
            cache.status = DiscoveryStatus.error
            cache.error_message = f"Discovery timed out after {_DISCOVERY_TIMEOUT_SEC}s"
            logger.warning("Discovery timeout: server=%s", server.name)
        except Exception as exc:  # noqa: BLE001
            cache.status = DiscoveryStatus.error
            cache.error_message = _sanitise_error(str(exc))
            logger.warning("Discovery error: server=%s error=%s", server.name, exc)

        server.discovery = cache
        return cache

    async def discover_all(
        self, servers: list[SubServerConfig]
    ) -> dict[str, DiscoveryCache]:
        """Run discovery for all servers concurrently."""
        tasks = {s.name: self.discover(s) for s in servers}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {name: r for name, r in zip(tasks.keys(), results)}

    # ------------------------------------------------------------------
    # Transport-specific fetch implementations
    # ------------------------------------------------------------------

    async def _fetch_tools(self, server: SubServerConfig) -> list[DiscoveredTool]:
        transport = server.transport

        if transport == TransportType.builtin:
            return self._fetch_builtin(server)
        elif transport == TransportType.http:
            return await self._fetch_http(server)
        elif transport == TransportType.stdio:
            return await self._fetch_stdio(server)
        elif transport == TransportType.websocket:
            return await self._fetch_websocket(server)
        else:
            raise ValueError(f"Unsupported transport: {transport}")

    def _fetch_builtin(self, server: SubServerConfig) -> list[DiscoveredTool]:
        """Query the Python adapter directly."""
        if server.adapter is None:
            # Adapter not yet instantiated — return empty list
            return []
        raw_tools = server.adapter.list_tools()
        return [DiscoveredTool(name=t, description="") for t in raw_tools]

    async def _fetch_http(self, server: SubServerConfig) -> list[DiscoveredTool]:
        """
        Call tools/list on an HTTP MCP server (JSON-RPC 2.0 over HTTP).

        Strategy (in order):
          1. POST {endpoint}          — MCP JSON-RPC 2.0 (Unity bridge, most HTTP servers)
          2. POST {endpoint}/         — some servers add trailing slash
          3. GET  {endpoint}/tools/list — REST-style fallback

        The endpoint field should be the full MCP path, e.g.:
          http://127.0.0.1:23457/mcp
        """
        if not server.endpoint:
            raise ValueError("HTTP transport requires 'endpoint' to be set")

        try:
            import httpx  # type: ignore[import]
        except ImportError:
            raise RuntimeError("httpx not installed. Run: pip install httpx")

        # Normalise: strip trailing slash so we control the exact URL
        base = server.endpoint.rstrip("/")
        payload = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Strategy 1: POST directly to the endpoint (Unity bridge: POST /mcp)
            try:
                resp = await client.post(base, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data or "tools" in data:
                        return _parse_tools_list_response(data)
            except Exception:  # noqa: BLE001
                pass

            # Strategy 2: POST with trailing slash
            try:
                resp = await client.post(f"{base}/", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data or "tools" in data:
                        return _parse_tools_list_response(data)
            except Exception:  # noqa: BLE001
                pass

            # Strategy 3: REST-style GET /tools/list
            # Derive base host from endpoint (strip the last path segment)
            host_base = base.rsplit("/", 1)[0] if "/" in base.split("://", 1)[-1] else base
            resp = await client.get(f"{host_base}/tools/list")
            resp.raise_for_status()
            data = resp.json()
            return _parse_tools_list_response(data)

    async def _fetch_stdio(self, server: SubServerConfig) -> list[DiscoveredTool]:
        """
        Launch the sub-server process, send a tools/list JSON-RPC request,
        read the response, then terminate the process.
        """
        if not server.command:
            raise ValueError("stdio transport requires 'command' to be set")

        proc = await asyncio.create_subprocess_shell(
            server.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # MCP initialise handshake
            init_req = json.dumps({
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "multi-mcp-discovery", "version": "0.1.0"},
                },
            }) + "\n"
            proc.stdin.write(init_req.encode())
            await proc.stdin.drain()

            # Read initialize response (with timeout)
            init_line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            init_data = json.loads(init_line.decode().strip())
            if "error" in init_data:
                raise RuntimeError(f"Initialize failed: {init_data['error']}")

            # Send initialized notification
            notif = json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }) + "\n"
            proc.stdin.write(notif.encode())
            await proc.stdin.drain()

            # Send tools/list request
            tools_req = json.dumps({
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 2,
                "params": {},
            }) + "\n"
            proc.stdin.write(tools_req.encode())
            await proc.stdin.drain()

            # Read tools/list response
            tools_line = await asyncio.wait_for(proc.stdout.readline(), timeout=8.0)
            tools_data = json.loads(tools_line.decode().strip())
            return _parse_tools_list_response(tools_data)

        finally:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:  # noqa: BLE001
                proc.kill()

    async def _fetch_websocket(self, server: SubServerConfig) -> list[DiscoveredTool]:
        """Connect to a WebSocket MCP server and call tools/list."""
        if not server.endpoint:
            raise ValueError("websocket transport requires 'endpoint' to be set")

        try:
            import websockets  # type: ignore[import]
        except ImportError:
            raise RuntimeError("websockets not installed. Run: pip install websockets")

        async with websockets.connect(server.endpoint) as ws:
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 1,
                "params": {},
            })
            await ws.send(payload)
            response_raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
            data = json.loads(response_raw)
            return _parse_tools_list_response(data)


# ---------------------------------------------------------------------------
# Routing table builder
# ---------------------------------------------------------------------------

class RoutingTableBuilder:
    """
    Builds a RoutingTable from the registered sub-servers in an EnvironmentConfig.

    The routing table maps (tool_name, profile) → SubServerConfig.
    It is rebuilt whenever:
      - A sub-server is added/removed/toggled
      - Discovery is re-run (new tools discovered)
      - A profile's tool grants change
    """

    @staticmethod
    def build(
        servers: list[SubServerConfig],
        profiles: list[Any],
        env_name: str,
    ) -> "RoutingTable":
        from multi_mcp.models.config import RoutingEntry, RoutingTable

        entries: list[RoutingEntry] = []
        seen: set[str] = set()  # tool_name → first server wins

        for server in servers:
            if not server.enabled:
                continue

            # Determine which tools this server exposes
            all_tools = server.get_effective_tools()

            for tool_name in all_tools:
                if tool_name in seen:
                    logger.debug(
                        "Tool '%s' already routed to another server; skipping %s",
                        tool_name, server.name,
                    )
                    continue

                # Determine which profiles can access this tool
                accessible_profiles: list[str] = []
                if "*" in server.allowed_profiles:
                    accessible_profiles = ["*"]
                else:
                    for profile_cfg in profiles:
                        pname = profile_cfg.name if hasattr(profile_cfg, "name") else str(profile_cfg)
                        if server.is_tool_allowed_for_profile(tool_name, pname):
                            accessible_profiles.append(pname)

                if not accessible_profiles:
                    continue

                entries.append(RoutingEntry(
                    tool_name=tool_name,
                    server_name=server.name,
                    server_type=server.server_type,
                    transport=server.transport,
                    profiles=accessible_profiles,
                ))
                seen.add(tool_name)

        from datetime import datetime, timezone
        return RoutingTable(
            environment=env_name,
            entries=entries,
            built_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_tools_list_response(data: dict[str, Any]) -> list[DiscoveredTool]:
    """
    Parse a JSON-RPC tools/list response into DiscoveredTool objects.

    Handles two schema key conventions:
      - "inputSchema"  (camelCase) — MCP official spec, Claude Desktop, etc.
      - "input_schema" (snake_case) — Unity MCP Bridge v2, some custom servers

    MCP spec response shape:
      {"jsonrpc": "2.0", "id": N, "result": {"tools": [{"name": ..., "description": ..., "inputSchema": {...}}, ...]}}
    """
    if "error" in data:
        raise RuntimeError(f"tools/list error: {data['error']}")

    result = data.get("result", data)  # handle both wrapped and unwrapped
    tools_raw = result.get("tools", [])

    tools = []
    for t in tools_raw:
        # Accept both camelCase (MCP spec) and snake_case (Unity bridge, custom servers)
        schema = t.get("inputSchema") or t.get("input_schema") or {}
        tools.append(DiscoveredTool(
            name=t.get("name", ""),
            description=t.get("description", ""),
            input_schema=schema,
        ))
    return tools


def _sanitise_error(msg: str) -> str:
    """Remove potentially sensitive information from error messages."""
    import re
    # Remove anything that looks like a key, token, or password
    msg = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+", "[REDACTED]", msg)
    msg = re.sub(r"sk-[A-Za-z0-9]{10,}", "[REDACTED]", msg)
    return msg[:500]  # Cap length
