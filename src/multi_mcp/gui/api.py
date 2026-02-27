"""
GUI API — Multi-MCP

FastAPI router that powers the management GUI.
Provides REST endpoints for:
  - Environment management (dev/stage/prod)
  - Sub-server registry
  - Aliases (SSH, Search)
  - Policies
  - Client profiles
  - Log viewing (audit + execution)
  - Secret management (write-only; never returns plain text)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from multi_mcp.models.config import (
    ClientProfile,
    Environment,
    EnvironmentConfig,
    ExecPolicy,
    FilesystemPolicy,
    SearchAlias,
    SearchPolicy,
    ServerPolicy,
    SSHAlias,
    SSHPolicy,
    SubServerConfig,
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
# Sub-server registry
# ---------------------------------------------------------------------------

@router.get("/environments/{env}/servers")
def list_servers(env: str) -> list[dict[str, Any]]:
    cfg = _get_env(env)
    return [s.model_dump(exclude={"adapter"}) for s in cfg.sub_servers]


@router.post("/environments/{env}/servers")
def add_server(env: str, server: SubServerConfig) -> dict[str, str]:
    cfg = _get_env(env)
    # Replace if name already exists
    cfg.sub_servers = [s for s in cfg.sub_servers if s.name != server.name]
    cfg.sub_servers.append(server)
    _save_env(cfg)
    return {"status": "ok", "server": server.name}


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
