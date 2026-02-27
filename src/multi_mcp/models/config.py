"""
Configuration Models — Multi-MCP

All Pydantic models that represent the system's configuration state.
These are used by:
  - The GUI (read/write settings)
  - The hub router (resolve sub-servers, policies)
  - The enforcement middleware (apply policies)

Secret values are NEVER stored in plain text in these models when serialised to disk.
The SecretStore handles encryption/decryption; models only hold opaque aliases or masked previews.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Environment(str, Enum):
    dev = "dev"
    stage = "stage"
    prod = "prod"


class ServerType(str, Enum):
    filesystem = "filesystem"
    exec = "exec"
    ssh = "ssh"
    logs = "logs"
    search = "search"
    artifact = "artifact"
    github = "github"      # future
    rag = "rag"            # future


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------

class FilesystemPolicy(BaseModel):
    allowed_root: str = Field(
        default="/tmp/mcp_workspace",
        description="Absolute path; all file operations are restricted to this subtree.",
    )
    allow_write: bool = Field(
        default=False,
        description="Whether write operations are permitted. Default OFF for safety.",
    )
    resolve_symlinks: bool = Field(
        default=True,
        description="Always resolve symlinks before checking allowed_root (prevents traversal).",
    )


class ExecPolicy(BaseModel):
    allowed_cwd: str = Field(
        default="/tmp/mcp_workspace",
        description="Working directory for all exec calls. Cannot be overridden by the client.",
    )
    timeout_sec: int = Field(
        default=60,
        description="Maximum execution time in seconds. Recommended: 60s to prevent infinite blocking.",
    )
    max_stdout_kb: int = Field(
        default=256,
        description="Maximum stdout size in KB. Recommended: 256 KB to prevent log flooding.",
    )
    max_stderr_kb: int = Field(
        default=256,
        description="Maximum stderr size in KB.",
    )
    max_concurrency: int = Field(
        default=1,
        description="Maximum simultaneous exec calls. Recommended: 1 to prevent resource storms.",
    )
    denylist: list[str] = Field(
        default_factory=lambda: [
            r"rm\s+-rf\s+/",
            r":(){ :|:& };:",  # fork bomb
            r"dd\s+if=",
            r"mkfs\.",
        ],
        description="Regex patterns for commands that are always blocked.",
    )
    allowlist: list[str] = Field(
        default_factory=list,
        description="If non-empty, only commands matching these patterns are permitted.",
    )


class SSHPolicy(BaseModel):
    allowed_aliases: list[str] = Field(
        default_factory=list,
        description="Only these SSH aliases may be used. Raw hosts/credentials are forbidden.",
    )
    allow_act: bool = Field(
        default=False,
        description="Whether write/mutating SSH commands are allowed. Default OFF.",
    )


class SearchPolicy(BaseModel):
    max_results: int = Field(
        default=10,
        description="Maximum search results per query. Recommended: 5-10 for cost control.",
    )
    allow_advanced_depth: bool = Field(
        default=False,
        description="Whether 'advanced' search_depth is allowed. Default OFF (basic is cheaper).",
    )
    daily_request_cap: int = Field(
        default=100,
        description="Maximum search requests per day. Prevents accidental cost overruns.",
    )
    monthly_credit_budget: float = Field(
        default=10.0,
        description="Maximum monthly spend in USD. Server blocks requests when exceeded.",
    )
    quota_exhausted: bool = Field(
        default=False,
        description="Set to True by the quota tracker when the budget/cap is reached.",
    )


class LogsPolicy(BaseModel):
    max_lines: int = Field(
        default=2000,
        description="Maximum log lines returned per request. Recommended: 2000.",
    )
    time_window_minutes: int = Field(
        default=10,
        description="Default time window for log queries in minutes. Recommended: 10.",
    )
    masking_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+",
            r"sk-[A-Za-z0-9]{20,}",
        ],
        description="Regex patterns for sensitive data that must be masked in log output.",
    )


class ArtifactPolicy(BaseModel):
    artifact_root: str = Field(
        default="/tmp/mcp_artifacts",
        description="Root directory for all artifacts. Enforced server-side.",
    )
    max_file_size_mb: int = Field(
        default=50,
        description="Maximum artifact file size in MB.",
    )
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".txt", ".md", ".json", ".csv", ".png", ".pdf"],
        description="Allowed file extensions for artifacts.",
    )


class ServerPolicy(BaseModel):
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    exec: ExecPolicy = Field(default_factory=ExecPolicy)
    ssh: SSHPolicy = Field(default_factory=SSHPolicy)
    search: SearchPolicy = Field(default_factory=SearchPolicy)
    logs: LogsPolicy = Field(default_factory=LogsPolicy)
    artifact: ArtifactPolicy = Field(default_factory=ArtifactPolicy)


# ---------------------------------------------------------------------------
# Sub-server adapter protocol
# ---------------------------------------------------------------------------

class SubServerAdapter(Protocol):
    """Interface that every sub-server adapter must implement."""

    async def call(self, request: "ToolCallRequest") -> dict[str, Any]: ...

    def list_tools(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Sub-server configuration
# ---------------------------------------------------------------------------

class SubServerConfig(BaseModel):
    name: str
    server_type: ServerType
    command: str | None = Field(
        default=None,
        description="Shell command to launch the sub-server process (for local stdio servers).",
    )
    address: str | None = Field(
        default=None,
        description="HTTP/SSE address for remote sub-servers.",
    )
    enabled: bool = True
    exposed_tools: list[str] = Field(
        default_factory=list,
        description="Tools from this server that are exposed to clients.",
    )
    allowed_profiles: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Client profiles that may use this server. '*' = all.",
    )
    policy: ServerPolicy = Field(default_factory=ServerPolicy)
    adapter: Any = Field(
        default=None,
        exclude=True,
        description="Runtime adapter instance (not persisted).",
    )

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# SSH alias
# ---------------------------------------------------------------------------

class SSHAlias(BaseModel):
    alias: str
    host: str
    port: int = 22
    username: str
    auth_type: str = Field(default="key", description="'key' or 'password'")
    # The actual key/password is stored in SecretStore under key f"ssh:{alias}"
    secret_ref: str = Field(
        description="Reference key in SecretStore (e.g. 'ssh:remote1')"
    )


# ---------------------------------------------------------------------------
# Search alias
# ---------------------------------------------------------------------------

class SearchAlias(BaseModel):
    alias: str
    provider: str = "tavily"
    # API key is stored in SecretStore under key f"search:{alias}"
    secret_ref: str = Field(
        description="Reference key in SecretStore (e.g. 'search:tavily_default')"
    )


# ---------------------------------------------------------------------------
# Client profile (tool exposure)
# ---------------------------------------------------------------------------

class ClientProfile(BaseModel):
    name: str
    description: str = ""
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Explicit list of tool names this profile may call. Empty = deny all.",
    )
    denied_tools: list[str] = Field(
        default_factory=list,
        description="Tools explicitly denied (takes precedence over allowed_tools).",
    )


# ---------------------------------------------------------------------------
# Top-level environment configuration
# ---------------------------------------------------------------------------

class EnvironmentConfig(BaseModel):
    name: Environment
    sub_servers: list[SubServerConfig] = Field(default_factory=list)
    ssh_aliases: list[SSHAlias] = Field(default_factory=list)
    search_aliases: list[SearchAlias] = Field(default_factory=list)
    client_profiles: list[ClientProfile] = Field(default_factory=list)
    global_policy: ServerPolicy = Field(default_factory=ServerPolicy)


# ---------------------------------------------------------------------------
# Request / Response models (used by the hub router)
# ---------------------------------------------------------------------------

class ToolCallRequest(BaseModel):
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ToolCallResponse(BaseModel):
    tool_name: str
    success: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    request_id: str | None = None
