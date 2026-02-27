"""
Bootstrap — Multi-MCP

Ensures that every EnvironmentConfig contains the 6 core built-in sub-servers
on first run (or whenever they are missing).

Core servers are:
  1. filesystem   — local file read/write (allowed_root enforced)
  2. exec         — local command execution (timeout/output/concurrency enforced)
  3. ssh          — remote SSH execution (alias-only, no raw credentials)
  4. logs         — log/process reading (read-only, masking)
  5. search       — Tavily web search (quota/cost guard)
  6. artifact     — result/artifact storage (artifact_root enforced)

Rules:
  - Core servers are identified by the `is_core=True` tag in their tags list.
  - They are NEVER deleted by the bootstrap (idempotent: only adds missing ones).
  - They carry the tag "core" and "builtin".
  - They cannot be deleted via the API (enforced in api.py).
  - They CAN be disabled (enabled=False) by the user.
  - Credential-dependent cores (ssh, search) start with status "not_configured"
    until the user provides the required alias/key.
"""

from __future__ import annotations

from multi_mcp.models.config import (
    EnvironmentConfig,
    ServerType,
    SubServerConfig,
    TransportType,
)

# ---------------------------------------------------------------------------
# Core server definitions
# ---------------------------------------------------------------------------

_CORE_SERVERS: list[dict] = [
    {
        "name": "core-filesystem",
        "server_type": ServerType.filesystem,
        "transport": TransportType.builtin,
        "description": "로컬 파일 읽기/쓰기. allowed_root 경계 강제 (심링크 우회 방지 포함).",
        "tags": ["core", "builtin", "filesystem"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["read_file", "list_directory", "write_file"],
        "requires_credentials": False,
        "credential_hint": None,
        "credential_setup_tab": None,
    },
    {
        "name": "core-exec",
        "server_type": ServerType.exec,
        "transport": TransportType.builtin,
        "description": "로컬 명령 실행. cwd 고정, timeout/output/concurrency 강제.",
        "tags": ["core", "builtin", "exec"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["exec_command"],
        "requires_credentials": False,
        "credential_hint": None,
        "credential_setup_tab": None,
    },
    {
        "name": "core-ssh",
        "server_type": ServerType.ssh,
        "transport": TransportType.builtin,
        "description": "원격 SSH 실행. alias 기반 호출만 허용 (자격증명 클라이언트 미노출).",
        "tags": ["core", "builtin", "ssh"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["ssh_read", "ssh_run"],
        "requires_credentials": True,
        "credential_hint": "SSH alias가 최소 1개 이상 등록되어야 합니다. (Aliases → SSH Remotes)",
        "credential_setup_tab": "aliases",
    },
    {
        "name": "core-logs",
        "server_type": ServerType.logs,
        "transport": TransportType.builtin,
        "description": "로그/프로세스 조회. 읽기 전용, 민감정보 마스킹, 라인/시간 제한 강제.",
        "tags": ["core", "builtin", "logs"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["read_logs", "list_processes"],
        "requires_credentials": False,
        "credential_hint": None,
        "credential_setup_tab": None,
    },
    {
        "name": "core-search",
        "server_type": ServerType.search,
        "transport": TransportType.builtin,
        "description": "Tavily 웹 검색. alias 기반 API 키 관리, 쿼터/비용 가드 강제.",
        "tags": ["core", "builtin", "search", "tavily"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["web_search"],
        "requires_credentials": True,
        "credential_hint": "Tavily API Key alias가 최소 1개 이상 등록되어야 합니다. (Aliases → Search Keys)",
        "credential_setup_tab": "aliases",
    },
    {
        "name": "core-artifact",
        "server_type": ServerType.artifact,
        "transport": TransportType.builtin,
        "description": "결과물 저장/조회. artifact_root 고정, 파일 크기/확장자 제한 강제.",
        "tags": ["core", "builtin", "artifact"],
        "allowed_profiles": ["*"],
        "exposed_tools": ["artifact_save", "artifact_read", "artifact_list"],
        "requires_credentials": False,
        "credential_hint": None,
        "credential_setup_tab": None,
    },
]

# Names of all core servers (used for lookup / delete-guard)
CORE_SERVER_NAMES: frozenset[str] = frozenset(s["name"] for s in _CORE_SERVERS)


def bootstrap_core_servers(cfg: EnvironmentConfig) -> bool:
    """
    Ensure all 6 core servers are present in the EnvironmentConfig.

    This function is idempotent: it only adds missing core servers and never
    modifies or removes existing ones (even if the user has customised them).

    Returns True if any servers were added (i.e., the config was modified).
    """
    existing_names = {s.name for s in cfg.sub_servers}
    added = False

    for core_def in _CORE_SERVERS:
        if core_def["name"] in existing_names:
            continue  # already present — do not overwrite user customisations

        server = SubServerConfig(
            name=core_def["name"],
            server_type=core_def["server_type"],
            transport=core_def["transport"],
            description=core_def["description"],
            tags=list(core_def["tags"]),
            allowed_profiles=list(core_def["allowed_profiles"]),
            exposed_tools=list(core_def["exposed_tools"]),
            enabled=True,
            env_scope=["dev", "stage", "prod"],
        )
        cfg.sub_servers.append(server)
        added = True

    return added


def is_core_server(name: str) -> bool:
    """Return True if the given server name is a built-in core server."""
    return name in CORE_SERVER_NAMES


def get_core_credential_hint(name: str) -> str | None:
    """Return the credential setup hint for a core server, or None."""
    for s in _CORE_SERVERS:
        if s["name"] == name:
            return s.get("credential_hint")
    return None


def get_core_credential_setup_tab(name: str) -> str | None:
    """Return the GUI tab name where credentials for this core server are configured."""
    for s in _CORE_SERVERS:
        if s["name"] == name:
            return s.get("credential_setup_tab")
    return None


def core_requires_credentials(name: str) -> bool:
    """Return True if this core server requires credentials to be usable."""
    for s in _CORE_SERVERS:
        if s["name"] == name:
            return bool(s.get("requires_credentials", False))
    return False


# ---------------------------------------------------------------------------
# Core server readiness status
# ---------------------------------------------------------------------------

class CoreStatus(str):
    """Possible readiness states for a core server."""
    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"


def compute_core_status(
    server: "SubServerConfig",
    cfg: "EnvironmentConfig",
) -> dict:
    """
    Compute the readiness status of a core server.

    Returns a dict with:
      - status: "ready" | "not_configured" | "disabled"
      - credential_hint: str | None   — what the user needs to configure
      - credential_setup_tab: str | None — which GUI tab to navigate to
      - missing_items: list[str]      — specific missing configuration items
    """
    from multi_mcp.models.config import EnvironmentConfig, SubServerConfig  # avoid circular

    name = server.name

    # Disabled takes priority
    if not server.enabled:
        return {
            "status": CoreStatus.DISABLED,
            "credential_hint": None,
            "credential_setup_tab": None,
            "missing_items": [],
        }

    # Servers that do NOT require credentials are always ready
    if not core_requires_credentials(name):
        return {
            "status": CoreStatus.READY,
            "credential_hint": None,
            "credential_setup_tab": None,
            "missing_items": [],
        }

    # SSH: needs at least one SSH alias
    if server.server_type.value == "ssh":
        if not cfg.ssh_aliases:
            return {
                "status": CoreStatus.NOT_CONFIGURED,
                "credential_hint": get_core_credential_hint(name),
                "credential_setup_tab": get_core_credential_setup_tab(name),
                "missing_items": ["SSH alias (host, user, auth)"],
            }
        return {
            "status": CoreStatus.READY,
            "credential_hint": None,
            "credential_setup_tab": None,
            "missing_items": [],
        }

    # Search (Tavily): needs at least one search alias
    if server.server_type.value == "search":
        if not cfg.search_aliases:
            return {
                "status": CoreStatus.NOT_CONFIGURED,
                "credential_hint": get_core_credential_hint(name),
                "credential_setup_tab": get_core_credential_setup_tab(name),
                "missing_items": ["Tavily API Key alias"],
            }
        return {
            "status": CoreStatus.READY,
            "credential_hint": None,
            "credential_setup_tab": None,
            "missing_items": [],
        }

    # Unknown credential-requiring server — treat as not configured
    return {
        "status": CoreStatus.NOT_CONFIGURED,
        "credential_hint": get_core_credential_hint(name),
        "credential_setup_tab": get_core_credential_setup_tab(name),
        "missing_items": ["Unknown credentials"],
    }


def enrich_server_dict(
    server_dict: dict,
    server: "SubServerConfig",
    cfg: "EnvironmentConfig",
) -> dict:
    """
    Add core-specific fields to the server dict returned by the API.

    Fields added:
      - is_core: bool
      - core_status: dict (status, hint, tab, missing_items)
    """
    is_core = is_core_server(server.name)
    server_dict["is_core"] = is_core

    if is_core:
        server_dict["core_status"] = compute_core_status(server, cfg)
    else:
        server_dict["core_status"] = None

    return server_dict
