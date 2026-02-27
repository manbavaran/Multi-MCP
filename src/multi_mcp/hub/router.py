"""
Hub/Router Core — Multi-MCP

Responsibilities:
  - Maintain a registry of sub-servers (each with type, transport, command/endpoint, enabled flag).
  - Route tool-call requests to the correct sub-server using the RoutingTable.
  - Apply enforcement middleware (policy checks) before and after every call.
  - Record audit and execution logs for every call.
    - Audit log: sub-server name / tool name / env / result (success/fail) — NO secrets
    - Execution log: stdout/stderr/result — NO secrets
"""

from __future__ import annotations

import logging
from typing import Any

from multi_mcp.enforcement.middleware import EnforcementMiddleware
from multi_mcp.logging.audit import AuditLogger
from multi_mcp.logging.execution import ExecutionLogger
from multi_mcp.models.bootstrap import compute_core_status, is_core_server
from multi_mcp.models.config import (
    EnvironmentConfig,
    RoutingTable,
    SubServerConfig,
    ToolCallRequest,
    ToolCallResponse,
    TransportType,
)

logger = logging.getLogger(__name__)


class SubServerRegistry:
    """
    Holds registered sub-servers and resolves which server owns a given tool.

    Resolution order:
      1. RoutingTable (pre-built from discovery + profile config) — fast path
      2. Direct scan of exposed_tools (fallback for servers without discovery)
    """

    def __init__(self) -> None:
        self._servers: dict[str, SubServerConfig] = {}
        self._routing_table: RoutingTable | None = None

    def register(self, server: SubServerConfig) -> None:
        self._servers[server.name] = server
        self._routing_table = None  # invalidate routing table
        logger.info(
            "Registered sub-server: name=%s type=%s transport=%s enabled=%s",
            server.name, server.server_type.value,
            server.transport.value, server.enabled,
        )

    def get(self, name: str) -> SubServerConfig | None:
        return self._servers.get(name)

    def list_all(self) -> list[SubServerConfig]:
        return list(self._servers.values())

    def list_enabled(self) -> list[SubServerConfig]:
        return [s for s in self._servers.values() if s.enabled]

    def set_routing_table(self, table: RoutingTable) -> None:
        self._routing_table = table
        logger.info(
            "Routing table updated: env=%s entries=%d",
            table.environment, len(table.entries),
        )

    def get_routing_table(self) -> RoutingTable | None:
        return self._routing_table

    def resolve_server_for_tool(
        self,
        tool_name: str,
        client_profile: str = "default",
    ) -> SubServerConfig | None:
        """
        Return the sub-server that should handle the given tool for the given profile.

        Uses the routing table if available, otherwise falls back to direct scan.
        """
        # Fast path: routing table
        if self._routing_table:
            entry = self._routing_table.resolve(tool_name, client_profile)
            if entry:
                return self._servers.get(entry.server_name)

        # Fallback: direct scan of exposed_tools
        for server in self.list_enabled():
            effective = server.get_effective_tools(client_profile)
            if tool_name in effective:
                return server

        return None

    def all_tools_for_profile(self, profile: str) -> list[dict[str, Any]]:
        """
        Return all tools accessible by a given profile, with server metadata.
        Used by /mcp/tools/{env} endpoint.
        """
        tools = []
        if self._routing_table:
            tool_names = self._routing_table.all_tools_for_profile(profile)
            for tool_name in tool_names:
                entry = self._routing_table.resolve(tool_name, profile)
                if entry:
                    server = self._servers.get(entry.server_name)
                    tools.append({
                        "tool": tool_name,
                        "server": entry.server_name,
                        "type": entry.server_type.value,
                        "transport": entry.transport.value,
                    })
            return tools

        # Fallback
        for server in self.list_enabled():
            for tool_name in server.get_effective_tools(profile):
                tools.append({
                    "tool": tool_name,
                    "server": server.name,
                    "type": server.server_type.value,
                    "transport": server.transport.value,
                })
        return tools


class MCPHub:
    """
    Central hub that receives tool-call requests from clients and routes them
    to the appropriate sub-server after policy enforcement.

    Audit log fields (never contains secrets):
      - tool_name, client_profile, server_name, env, success/fail, timestamp

    Execution log fields (never contains secrets):
      - tool_name, server_name, stdout/stderr (masked), result summary
    """

    def __init__(
        self,
        registry: SubServerRegistry,
        enforcement: EnforcementMiddleware,
        audit_logger: AuditLogger,
        exec_logger: ExecutionLogger,
        env_name: str = "dev",
        env_config: EnvironmentConfig | None = None,
    ) -> None:
        self.registry = registry
        self.enforcement = enforcement
        self.audit_logger = audit_logger
        self.exec_logger = exec_logger
        self.env_name = env_name
        self.env_config = env_config  # used for core status checks

    async def call_tool(
        self,
        request: ToolCallRequest,
        client_profile: str = "default",
    ) -> ToolCallResponse:
        """
        Main entry point for all tool calls.

        Flow:
          1. Resolve which sub-server owns the tool (routing table or direct scan).
          2. Run pre-call enforcement (policy checks, rate limits, quota).
          3. Dispatch the call to the sub-server adapter.
          4. Run post-call enforcement (output cap, masking).
          5. Record audit log (server/tool/env/result — NO secrets).
          6. Record execution log (output — NO secrets, masked).
          7. Return the response to the client.
        """
        tool_name = request.tool_name
        server = self.registry.resolve_server_for_tool(tool_name, client_profile)

        if server is None:
            self.audit_logger.log_failure(
                request, client_profile, "tool_not_found",
                extra={"env": self.env_name},
            )
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"No enabled sub-server exposes tool '{tool_name}' for profile '{client_profile}'",
                env=self.env_name,
            )

        # --- Core server Not Configured check ---
        # If the server is a core server that requires credentials and they are
        # not yet configured, block the call immediately and log to audit.
        if is_core_server(server.name) and self.env_config is not None:
            status_info = compute_core_status(server, self.env_config)
            if status_info["status"] == "not_configured":
                hint = status_info.get("credential_hint", "Credentials not configured.")
                missing = status_info.get("missing_items", [])
                self.audit_logger.log_failure(
                    request, client_profile,
                    f"core_server_not_configured: {server.name}",
                    extra={
                        "env": self.env_name,
                        "server": server.name,
                        "missing_items": missing,
                        # NOTE: no secrets are logged here
                    },
                )
                return ToolCallResponse(
                    tool_name=tool_name,
                    success=False,
                    error=(
                        f"Core server '{server.name}' is not configured. "
                        f"{hint} Missing: {', '.join(missing)}"
                    ),
                    routed_to=server.name,
                    env=self.env_name,
                )

        # --- Pre-call enforcement ---
        try:
            self.enforcement.pre_call(request, server, client_profile)
        except PermissionError as exc:
            self.audit_logger.log_failure(
                request, client_profile, str(exc),
                extra={"env": self.env_name, "server": server.name},
            )
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"Policy violation: {exc}",
                routed_to=server.name,
                env=self.env_name,
            )

        # --- Dispatch to sub-server adapter ---
        try:
            raw_result: dict[str, Any] = await server.adapter.call(request)
        except Exception as exc:  # noqa: BLE001
            self.audit_logger.log_failure(
                request, client_profile, str(exc),
                extra={"env": self.env_name, "server": server.name},
            )
            self.exec_logger.log(request, server.name, error=str(exc))
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"Sub-server error: {exc}",
                routed_to=server.name,
                env=self.env_name,
            )

        # --- Post-call enforcement (output cap, masking) ---
        processed_result = self.enforcement.post_call(raw_result, server, client_profile)

        # --- Audit log (minimal: server/tool/env/success — NO secrets) ---
        self.audit_logger.log_success(
            request, client_profile, server.name,
            extra={"env": self.env_name},
        )

        # --- Execution log (output — masked) ---
        self.exec_logger.log(request, server.name, result=processed_result)

        return ToolCallResponse(
            tool_name=tool_name,
            success=True,
            result=processed_result,
            routed_to=server.name,
            env=self.env_name,
        )
