"""
Hub/Router Core — Multi-MCP

Responsibilities:
  - Maintain a registry of sub-servers (each with type, command/address, enabled flag).
  - Route tool-call requests to the correct sub-server.
  - Apply enforcement middleware (policy checks) before and after every call.
  - Record audit and execution logs for every call.
"""

from __future__ import annotations

import logging
from typing import Any

from multi_mcp.enforcement.middleware import EnforcementMiddleware
from multi_mcp.logging.audit import AuditLogger
from multi_mcp.logging.execution import ExecutionLogger
from multi_mcp.models.config import SubServerConfig, ToolCallRequest, ToolCallResponse

logger = logging.getLogger(__name__)


class SubServerRegistry:
    """Holds registered sub-servers and resolves which server owns a given tool."""

    def __init__(self) -> None:
        self._servers: dict[str, SubServerConfig] = {}

    def register(self, server: SubServerConfig) -> None:
        self._servers[server.name] = server
        logger.info("Registered sub-server: %s (enabled=%s)", server.name, server.enabled)

    def get(self, name: str) -> SubServerConfig | None:
        return self._servers.get(name)

    def list_enabled(self) -> list[SubServerConfig]:
        return [s for s in self._servers.values() if s.enabled]

    def resolve_server_for_tool(self, tool_name: str) -> SubServerConfig | None:
        """Return the first enabled sub-server that exposes the requested tool."""
        for server in self.list_enabled():
            if tool_name in server.exposed_tools:
                return server
        return None


class MCPHub:
    """
    Central hub that receives tool-call requests from clients and routes them
    to the appropriate sub-server after policy enforcement.
    """

    def __init__(
        self,
        registry: SubServerRegistry,
        enforcement: EnforcementMiddleware,
        audit_logger: AuditLogger,
        exec_logger: ExecutionLogger,
    ) -> None:
        self.registry = registry
        self.enforcement = enforcement
        self.audit_logger = audit_logger
        self.exec_logger = exec_logger

    async def call_tool(
        self,
        request: ToolCallRequest,
        client_profile: str = "default",
    ) -> ToolCallResponse:
        """
        Main entry point for all tool calls.

        Flow:
          1. Resolve which sub-server owns the tool.
          2. Run pre-call enforcement (policy checks, rate limits, quota).
          3. Dispatch the call to the sub-server adapter.
          4. Run post-call enforcement (output cap, masking).
          5. Record audit + execution logs.
          6. Return the response to the client.
        """
        tool_name = request.tool_name
        server = self.registry.resolve_server_for_tool(tool_name)

        if server is None:
            self.audit_logger.log_failure(request, client_profile, "tool_not_found")
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"No enabled sub-server exposes tool '{tool_name}'",
            )

        # --- Pre-call enforcement ---
        try:
            self.enforcement.pre_call(request, server, client_profile)
        except PermissionError as exc:
            self.audit_logger.log_failure(request, client_profile, str(exc))
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"Policy violation: {exc}",
            )

        # --- Dispatch to sub-server adapter ---
        try:
            raw_result: dict[str, Any] = await server.adapter.call(request)
        except Exception as exc:  # noqa: BLE001
            self.audit_logger.log_failure(request, client_profile, str(exc))
            self.exec_logger.log(request, server.name, error=str(exc))
            return ToolCallResponse(
                tool_name=tool_name,
                success=False,
                error=f"Sub-server error: {exc}",
            )

        # --- Post-call enforcement (output cap, masking) ---
        processed_result = self.enforcement.post_call(raw_result, server, client_profile)

        # --- Logging ---
        self.audit_logger.log_success(request, client_profile, server.name)
        self.exec_logger.log(request, server.name, result=processed_result)

        return ToolCallResponse(
            tool_name=tool_name,
            success=True,
            result=processed_result,
        )
