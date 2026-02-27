"""
GUI API — Multi-MCP

FastAPI router that powers the management GUI.
Provides REST endpoints for:
  - Environment management (dev/stage/prod)
  - Sub-server registry (full CRUD + discovery + routing table)
  - Aliases (SSH, Search)
  - Policies
  - Client profiles (with per-server tool grants)
  - Log viewing (audit + execution)
  - Secret management (write-only; never returns plain text)
  - Policy defaults with rationale
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from multi_mcp.models.config import (
    ClientProfile,
    DiscoveryStatus,
    Environment,
    EnvironmentConfig,
    ExecPolicy,
    FilesystemPolicy,
    RoutingTable,
    SearchAlias,
    SearchPolicy,
    ServerPolicy,
    ServerType,
    SSHAlias,
    SSHPolicy,
    SubServerConfig,
    TransportType,
)
from multi_mcp.models.secrets import SecretStore
from multi_mcp.models.settings_manager import SettingsManager

router = APIRouter(prefix="/api", tags=["gui"])

_settings = SettingsManager()
_secrets = SecretStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_env(env: str) -> EnvironmentConfig:
    try:
        e = Environment(env)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown environment: {env}")
    cfg = _settings.load(e)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Environment '{env}' not found")
    return cfg


def _save_env(cfg: EnvironmentConfig) -> None:
    _settings.save(cfg)


def _server_to_dict(s: SubServerConfig) -> dict[str, Any]:
    d = s.model_dump(exclude={"adapter"})
    # Convert discovery datetimes to ISO strings for JSON serialisation
    if d.get("discovery"):
        disc = d["discovery"]
        for key in ("last_attempted_at", "last_succeeded_at"):
            if disc.get(key) and hasattr(disc[key], "isoformat"):
                disc[key] = disc[key].isoformat()
    return d


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

@router.get("/environments")
def list_environments() -> list[str]:
    return [e.value for e in _settings.list_environments()]


@router.post("/environments/{env}")
def create_environment(env: str) -> dict[str, str]:
    try:
        e = Environment(env)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown environment: {env}")
    cfg = _settings.get_or_create_default(e)
    return {"status": "ok", "environment": cfg.name.value}


@router.get("/environments/{env}")
def get_environment(env: str) -> dict[str, Any]:
    cfg = _get_env(env)
    return cfg.model_dump(exclude={"sub_servers": {"__all__": {"adapter"}}})


# ---------------------------------------------------------------------------
# Sub-server Registry — full CRUD
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/servers")
def list_servers(env: str) -> list[dict[str, Any]]:
    cfg = _get_env(env)
    return [_server_to_dict(s) for s in cfg.sub_servers]


class SubServerCreate(BaseModel):
    """Request body for creating/updating a sub-server registration."""
    name: str
    server_type: ServerType
    transport: TransportType = TransportType.builtin
    command: str | None = None
    endpoint: str | None = None
    env_scope: list[str] = ["dev", "stage", "prod"]
    enabled: bool = True
    tags: list[str] = []
    description: str = ""
    version_pin: str | None = None
    exposed_tools: list[str] = []
    allowed_profiles: list[str] = ["*"]
    profile_tool_overrides: dict[str, list[str]] = {}


@router.post("/environments/{env}/servers")
def add_server(env: str, body: SubServerCreate) -> dict[str, Any]:
    """Register a new sub-server (or replace an existing one with the same name)."""
    cfg = _get_env(env)

    # Validate transport ↔ command/endpoint consistency
    if body.transport == TransportType.stdio and not body.command:
        raise HTTPException(
            status_code=422,
            detail="stdio transport requires 'command' to be set",
        )
    if body.transport in (TransportType.http, TransportType.websocket) and not body.endpoint:
        raise HTTPException(
            status_code=422,
            detail=f"{body.transport.value} transport requires 'endpoint' to be set",
        )

    server = SubServerConfig(
        name=body.name,
        server_type=body.server_type,
        transport=body.transport,
        command=body.command,
        endpoint=body.endpoint,
        env_scope=body.env_scope,
        enabled=body.enabled,
        tags=body.tags,
        description=body.description,
        version_pin=body.version_pin,
        exposed_tools=body.exposed_tools,
        allowed_profiles=body.allowed_profiles,
        profile_tool_overrides=body.profile_tool_overrides,
    )
    cfg.sub_servers = [s for s in cfg.sub_servers if s.name != body.name]
    cfg.sub_servers.append(server)
    _save_env(cfg)
    return {"status": "ok", "server": body.name}


@router.get("/environments/{env}/servers/{name}")
def get_server(env: str, name: str) -> dict[str, Any]:
    cfg = _get_env(env)
    for s in cfg.sub_servers:
        if s.name == name:
            return _server_to_dict(s)
    raise HTTPException(status_code=404, detail=f"Server '{name}' not found")


@router.put("/environments/{env}/servers/{name}")
def update_server(env: str, name: str, body: SubServerCreate) -> dict[str, Any]:
    """Update an existing sub-server registration."""
    cfg = _get_env(env)
    existing = next((s for s in cfg.sub_servers if s.name == name), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Preserve discovery cache across updates
    discovery_cache = existing.discovery

    server = SubServerConfig(
        name=body.name,  # allow rename
        server_type=body.server_type,
        transport=body.transport,
        command=body.command,
        endpoint=body.endpoint,
        env_scope=body.env_scope,
        enabled=body.enabled,
        tags=body.tags,
        description=body.description,
        version_pin=body.version_pin,
        exposed_tools=body.exposed_tools,
        allowed_profiles=body.allowed_profiles,
        profile_tool_overrides=body.profile_tool_overrides,
        discovery=discovery_cache,
    )
    cfg.sub_servers = [s for s in cfg.sub_servers if s.name != name]
    cfg.sub_servers.append(server)
    _save_env(cfg)
    return {"status": "ok", "server": body.name}


@router.delete("/environments/{env}/servers/{name}")
def delete_server(env: str, name: str) -> dict[str, str]:
    cfg = _get_env(env)
    before = len(cfg.sub_servers)
    cfg.sub_servers = [s for s in cfg.sub_servers if s.name != name]
    if len(cfg.sub_servers) == before:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    _save_env(cfg)
    return {"status": "deleted", "server": name}


@router.patch("/environments/{env}/servers/{name}/toggle")
def toggle_server(env: str, name: str, enabled: bool) -> dict[str, Any]:
    cfg = _get_env(env)
    for s in cfg.sub_servers:
        if s.name == name:
            s.enabled = enabled
            _save_env(cfg)
            return {"status": "ok", "server": name, "enabled": enabled}
    raise HTTPException(status_code=404, detail=f"Server '{name}' not found")


# ---------------------------------------------------------------------------
# Sub-server Discovery (tools/list)
# ---------------------------------------------------------------------------

@router.post("/environments/{env}/servers/{name}/discover")
async def discover_server_tools(env: str, name: str) -> dict[str, Any]:
    """
    Trigger tools/list discovery for a specific sub-server.
    Updates the discovery cache and saves the config.
    """
    from multi_mcp.hub.discovery import DiscoveryService
    from multi_mcp.hub.factory import HubFactory

    cfg = _get_env(env)
    server = next((s for s in cfg.sub_servers if s.name == name), None)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Instantiate adapter for builtin servers so discovery can query it
    if server.transport == TransportType.builtin and server.adapter is None:
        try:
            e = Environment(env)
            adapter = HubFactory._build_adapter(server, cfg, _secrets)
            server.adapter = adapter
        except Exception:  # noqa: BLE001
            pass

    svc = DiscoveryService()
    cache = await svc.discover(server)
    _save_env(cfg)

    return {
        "server": name,
        "status": cache.status.value,
        "tool_count": len(cache.tools),
        "tools": [t.name for t in cache.tools],
        "error": cache.error_message,
        "last_attempted_at": cache.last_attempted_at.isoformat() if cache.last_attempted_at else None,
    }


@router.post("/environments/{env}/discover-all")
async def discover_all_servers(env: str) -> dict[str, Any]:
    """Run tools/list discovery for all enabled sub-servers in an environment."""
    from multi_mcp.hub.discovery import DiscoveryService
    from multi_mcp.hub.factory import HubFactory

    cfg = _get_env(env)
    enabled = [s for s in cfg.sub_servers if s.enabled]

    # Instantiate adapters for builtin servers
    for server in enabled:
        if server.transport == TransportType.builtin and server.adapter is None:
            try:
                adapter = HubFactory._build_adapter(server, cfg, _secrets)
                server.adapter = adapter
            except Exception:  # noqa: BLE001
                pass

    svc = DiscoveryService()
    await svc.discover_all(enabled)
    _save_env(cfg)

    return {
        "env": env,
        "results": [
            {
                "server": s.name,
                "status": s.discovery.status.value,
                "tool_count": len(s.discovery.tools),
                "tools": [t.name for t in s.discovery.tools],
            }
            for s in enabled
        ],
    }


# ---------------------------------------------------------------------------
# Routing Table
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/routing-table")
def get_routing_table(env: str, profile: str = "*") -> dict[str, Any]:
    """
    Build and return the routing table for an environment.
    Optionally filter by client profile.
    """
    from multi_mcp.hub.discovery import RoutingTableBuilder

    cfg = _get_env(env)
    table = RoutingTableBuilder.build(
        servers=cfg.sub_servers,
        profiles=cfg.client_profiles,
        env_name=env,
    )

    if profile != "*":
        entries = [e for e in table.entries if "*" in e.profiles or profile in e.profiles]
    else:
        entries = table.entries

    return {
        "environment": env,
        "profile_filter": profile,
        "built_at": table.built_at.isoformat() if table.built_at else None,
        "total_routes": len(entries),
        "routes": [e.model_dump() for e in entries],
    }


# ---------------------------------------------------------------------------
# Profile tool grants (per-server fine control)
# ---------------------------------------------------------------------------

class ProfileToolGrant(BaseModel):
    server_name: str
    tools: list[str]  # ["*"] for all, or specific tool names


@router.put("/environments/{env}/servers/{name}/profile-grants/{profile}")
def set_profile_tool_grant(
    env: str, name: str, profile: str, body: ProfileToolGrant
) -> dict[str, Any]:
    """Set per-profile tool grants for a specific sub-server."""
    cfg = _get_env(env)
    server = next((s for s in cfg.sub_servers if s.name == name), None)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    server.profile_tool_overrides[profile] = body.tools
    _save_env(cfg)
    return {"status": "ok", "server": name, "profile": profile, "tools": body.tools}


@router.delete("/environments/{env}/servers/{name}/profile-grants/{profile}")
def delete_profile_tool_grant(env: str, name: str, profile: str) -> dict[str, str]:
    """Remove per-profile tool grants for a specific sub-server."""
    cfg = _get_env(env)
    server = next((s for s in cfg.sub_servers if s.name == name), None)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    server.profile_tool_overrides.pop(profile, None)
    _save_env(cfg)
    return {"status": "deleted", "server": name, "profile": profile}


# ---------------------------------------------------------------------------
# SSH Aliases
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/aliases/ssh")
def list_ssh_aliases(env: str) -> list[dict[str, Any]]:
    cfg = _get_env(env)
    result = []
    for alias in cfg.ssh_aliases:
        d = alias.model_dump()
        d["secret_preview"] = _secrets.get_masked_preview(alias.secret_ref)
        result.append(d)
    return result


class SSHAliasCreate(BaseModel):
    alias: str
    host: str
    port: int = 22
    username: str
    auth_type: str = "key"
    secret_value: str  # plain text — stored encrypted, never returned


@router.post("/environments/{env}/aliases/ssh")
def add_ssh_alias(env: str, body: SSHAliasCreate) -> dict[str, str]:
    cfg = _get_env(env)
    secret_ref = f"ssh:{body.alias}"
    _secrets.set(secret_ref, body.secret_value)
    alias = SSHAlias(
        alias=body.alias,
        host=body.host,
        port=body.port,
        username=body.username,
        auth_type=body.auth_type,
        secret_ref=secret_ref,
    )
    cfg.ssh_aliases = [a for a in cfg.ssh_aliases if a.alias != body.alias]
    cfg.ssh_aliases.append(alias)
    _save_env(cfg)
    return {"status": "ok", "alias": body.alias}


@router.delete("/environments/{env}/aliases/ssh/{alias}")
def delete_ssh_alias(env: str, alias: str) -> dict[str, str]:
    cfg = _get_env(env)
    before = len(cfg.ssh_aliases)
    cfg.ssh_aliases = [a for a in cfg.ssh_aliases if a.alias != alias]
    if len(cfg.ssh_aliases) == before:
        raise HTTPException(status_code=404, detail=f"SSH alias '{alias}' not found")
    _secrets.delete(f"ssh:{alias}")
    _save_env(cfg)
    return {"status": "deleted", "alias": alias}


# ---------------------------------------------------------------------------
# Search Aliases (Tavily)
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/aliases/search")
def list_search_aliases(env: str) -> list[dict[str, Any]]:
    cfg = _get_env(env)
    result = []
    for alias in cfg.search_aliases:
        d = alias.model_dump()
        d["secret_preview"] = _secrets.get_masked_preview(alias.secret_ref)
        result.append(d)
    return result


class SearchAliasCreate(BaseModel):
    alias: str
    provider: str = "tavily"
    api_key: str  # plain text — stored encrypted, never returned


@router.post("/environments/{env}/aliases/search")
def add_search_alias(env: str, body: SearchAliasCreate) -> dict[str, str]:
    cfg = _get_env(env)
    secret_ref = f"search:{body.alias}"
    _secrets.set(secret_ref, body.api_key)
    alias = SearchAlias(
        alias=body.alias,
        provider=body.provider,
        secret_ref=secret_ref,
    )
    cfg.search_aliases = [a for a in cfg.search_aliases if a.alias != body.alias]
    cfg.search_aliases.append(alias)
    _save_env(cfg)
    return {"status": "ok", "alias": body.alias}


@router.delete("/environments/{env}/aliases/search/{alias}")
def delete_search_alias(env: str, alias: str) -> dict[str, str]:
    cfg = _get_env(env)
    before = len(cfg.search_aliases)
    cfg.search_aliases = [a for a in cfg.search_aliases if a.alias != alias]
    if len(cfg.search_aliases) == before:
        raise HTTPException(status_code=404, detail=f"Search alias '{alias}' not found")
    _secrets.delete(f"search:{alias}")
    _save_env(cfg)
    return {"status": "deleted", "alias": alias}


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/policy")
def get_policy(env: str) -> dict[str, Any]:
    cfg = _get_env(env)
    return cfg.global_policy.model_dump()


@router.put("/environments/{env}/policy")
def update_policy(env: str, policy: ServerPolicy) -> dict[str, str]:
    cfg = _get_env(env)
    cfg.global_policy = policy
    _save_env(cfg)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Client Profiles
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/profiles")
def list_profiles(env: str) -> list[dict[str, Any]]:
    cfg = _get_env(env)
    return [p.model_dump() for p in cfg.client_profiles]


@router.post("/environments/{env}/profiles")
def add_profile(env: str, profile: ClientProfile) -> dict[str, str]:
    cfg = _get_env(env)
    cfg.client_profiles = [p for p in cfg.client_profiles if p.name != profile.name]
    cfg.client_profiles.append(profile)
    _save_env(cfg)
    return {"status": "ok", "profile": profile.name}


@router.delete("/environments/{env}/profiles/{name}")
def delete_profile(env: str, name: str) -> dict[str, str]:
    cfg = _get_env(env)
    before = len(cfg.client_profiles)
    cfg.client_profiles = [p for p in cfg.client_profiles if p.name != name]
    if len(cfg.client_profiles) == before:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    _save_env(cfg)
    return {"status": "deleted", "profile": name}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.get("/logs/audit")
def get_audit_logs(limit: int = 200) -> list[dict[str, Any]]:
    from multi_mcp.logging.audit import AuditLogger
    return AuditLogger().read_recent(limit=limit)


@router.get("/logs/execution")
def get_execution_logs(limit: int = 200) -> list[dict[str, Any]]:
    from multi_mcp.logging.execution import ExecutionLogger
    return ExecutionLogger().read_recent(limit=limit)


# ---------------------------------------------------------------------------
# Policy defaults with rationale (for GUI display)
# ---------------------------------------------------------------------------

@router.get("/policy-defaults")
def get_policy_defaults() -> dict[str, Any]:
    """
    Return recommended default values with human-readable rationale.
    The GUI displays these alongside each setting field.
    """
    return {
        "exec": {
            "timeout_sec": {
                "default": 60,
                "rationale": "무한 대기/블로킹 방지. 실패 징후는 보통 빠르게 드러남.",
                "warning": "값을 높이면 장시간 블로킹 위험이 증가합니다.",
            },
            "max_stdout_kb": {
                "default": 256,
                "rationale": "로그 폭주로 인한 비용·응답 지연·저장 공간 문제 방지.",
                "warning": "값을 높이면 응답 크기와 저장 비용이 증가합니다.",
            },
            "max_concurrency": {
                "default": 1,
                "rationale": "로그/상태 혼선 및 자원 폭주 방지. 순차 실행이 기본.",
                "warning": "동시성을 높이면 자원 경합과 로그 혼선이 발생할 수 있습니다.",
            },
        },
        "search": {
            "max_results": {
                "default": 10,
                "rationale": "근거 다양성 확보 + 비용 통제의 균형.",
                "warning": "값을 높이면 API 비용이 증가합니다.",
            },
            "search_depth": {
                "default": "basic",
                "rationale": "비용 대비 효율이 좋고 대부분의 조사에 충분.",
                "warning": "'advanced'는 비용이 더 높습니다.",
            },
            "daily_request_cap": {
                "default": 100,
                "rationale": "pay-as-you-go에서 비용 폭탄 방지.",
                "warning": "한도를 높이면 예상치 못한 비용이 발생할 수 있습니다.",
            },
        },
        "logs": {
            "max_lines": {
                "default": 2000,
                "rationale": "관측은 충분히 하되 전체 덤프로 폭주하지 않도록.",
                "warning": "값을 높이면 응답 크기와 처리 시간이 증가합니다.",
            },
            "time_window_minutes": {
                "default": 10,
                "rationale": "최근 이슈 진단에 충분한 시간 범위.",
                "warning": "값을 높이면 반환 데이터가 많아집니다.",
            },
        },
        "filesystem": {
            "allow_write": {
                "default": False,
                "rationale": "기본적으로 읽기 전용. 쓰기는 명시적으로 활성화해야 함.",
                "warning": "쓰기를 활성화하면 파일 변경/삭제 위험이 생깁니다.",
            },
        },
    }


# ---------------------------------------------------------------------------
# Server type / transport metadata (for GUI dropdowns)
# ---------------------------------------------------------------------------

@router.get("/server-types")
def get_server_types() -> dict[str, Any]:
    """Return available server types and transports for GUI dropdowns."""
    return {
        "server_types": [
            {"value": t.value, "label": t.value.capitalize(),
             "description": _SERVER_TYPE_DESC.get(t.value, "")}
            for t in ServerType
        ],
        "transports": [
            {"value": t.value, "label": t.value.upper(),
             "description": _TRANSPORT_DESC.get(t.value, "")}
            for t in TransportType
        ],
    }


_SERVER_TYPE_DESC = {
    "filesystem": "파일 읽기/쓰기 (allowed_root 강제)",
    "exec": "로컬 명령 실행 (timeout/output/concurrency 강제)",
    "ssh": "원격 SSH 실행 (alias 기반, 자격증명 미노출)",
    "logs": "로그/프로세스 조회 (read-only, 마스킹)",
    "search": "웹 검색 (Tavily, quota/cost guard)",
    "artifact": "결과물 저장/조회 (artifact_root 강제)",
    "github": "GitHub 작업 (추후 구현)",
    "rag": "문서 검색/RAG (추후 구현)",
    "other": "사용자 정의 MCP 서버",
}

_TRANSPORT_DESC = {
    "stdio": "자식 프로세스로 실행, stdin/stdout 통신 (MCP 표준)",
    "http": "HTTP/SSE 엔드포인트 (이미 실행 중인 서버)",
    "websocket": "WebSocket 엔드포인트",
    "builtin": "Multi-MCP 내장 Python 어댑터",
}
