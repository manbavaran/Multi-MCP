"""
MCP Tool Call Endpoint — Multi-MCP

Provides the /mcp/call endpoint that clients (LangGraph, etc.) use to invoke tools.
The hub is instantiated lazily on first request.

Security:
  - Clients pass only: tool_name, args (with alias references), client_profile
  - No secrets, no raw credentials are accepted from clients
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from multi_mcp.hub.factory import HubFactory
from multi_mcp.hub.router import MCPHub
from multi_mcp.models.config import Environment, ToolCallRequest, ToolCallResponse
from multi_mcp.models.secrets import SecretStore
from multi_mcp.models.settings_manager import SettingsManager

router = APIRouter(prefix="/mcp", tags=["mcp"])

_settings = SettingsManager()
_secrets = SecretStore()
_hubs: dict[str, MCPHub] = {}


def _get_hub(env: str) -> MCPHub:
    if env not in _hubs:
        try:
            e = Environment(env)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown environment: {env}")
        cfg = _settings.load(e)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"Environment '{env}' not configured")
        _hubs[env] = HubFactory.create(cfg, _secrets)
    return _hubs[env]


class MCPCallRequest(BaseModel):
    tool_name: str
    args: dict[str, Any] = {}
    client_profile: str = "default"
    request_id: str | None = None


@router.post("/call/{env}", response_model=ToolCallResponse)
async def call_tool(env: str, body: MCPCallRequest) -> ToolCallResponse:
    """
    Main tool-call endpoint.

    Example (Tavily search via alias):
      POST /mcp/call/dev
      {
        "tool_name": "web_search",
        "args": {"alias": "tavily_default", "query": "MCP protocol overview"},
        "client_profile": "Researcher"
      }
    """
    hub = _get_hub(env)
    request = ToolCallRequest(
        tool_name=body.tool_name,
        args=body.args,
        request_id=body.request_id,
    )
    return await hub.call_tool(request, client_profile=body.client_profile)


@router.get("/tools/{env}")
def list_tools(env: str, client_profile: str = "default") -> dict[str, Any]:
    """List all tools available for a given environment and client profile."""
    hub = _get_hub(env)
    tools = []
    for server in hub.registry.list_enabled():
        if client_profile in server.allowed_profiles or "*" in server.allowed_profiles:
            tools.extend([
                {"tool": t, "server": server.name, "type": server.server_type}
                for t in server.exposed_tools
            ])
    return {"environment": env, "profile": client_profile, "tools": tools}


@router.post("/hub/reload/{env}")
def reload_hub(env: str) -> dict[str, str]:
    """Force reload the hub for an environment (picks up config changes)."""
    if env in _hubs:
        del _hubs[env]
    _get_hub(env)
    return {"status": "reloaded", "environment": env}
