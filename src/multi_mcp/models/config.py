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

from datetime import datetime
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
    other = "other"        # user-defined / custom


class TransportType(str, Enum):
    """
    How Multi-MCP connects to the sub-server.

    - stdio:     Launch a child process, communicate over stdin/stdout (MCP spec default)
    - http:      Connect to an already-running HTTP/SSE server
    - websocket: Connect to a WebSocket server
    - builtin:   Python-native adapter bundled in Multi-MCP (no external process)
    """
    stdio = "stdio"
    http = "http"
    websocket = "websocket"
    builtin = "builtin"


class DiscoveryStatus(str, Enum):
    """Result of the last tools/list discovery attempt."""
    pending = "pending"         # never attempted
    ok = "ok"                   # succeeded
    error = "error"             # last attempt failed
    disabled = "disabled"       # server is disabled, discovery skipped


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
# Discovery cache — result of tools/list call
# ---------------------------------------------------------------------------

class DiscoveredTool(BaseModel):
    """A single tool discovered from a sub-server's tools/list response."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class DiscoveryCache(BaseModel):
    """
    Cached result of the last tools/list discovery for a sub-server.
    Stored alongside the SubServerConfig; never contains secrets.
    """
    status: DiscoveryStatus = DiscoveryStatus.pending
    tools: list[DiscoveredTool] = Field(default_factory=list)
    last_attempted_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    error_message: str | None = None

    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]


# ---------------------------------------------------------------------------
# Sub-server adapter protocol
# ---------------------------------------------------------------------------

class SubServerAdapter(Protocol):
    """Interface that every sub-server adapter must implement."""

    async def call(self, request: "ToolCallRequest") -> dict[str, Any]: ...

    def list_tools(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Sub-server configuration (extended)
# ---------------------------------------------------------------------------

class SubServerConfig(BaseModel):
    """
    Full configuration for a registered MCP sub-server.

    Transport options:
      - stdio:     command is the shell command to launch the process
      - http:      endpoint is the base URL (e.g. http://localhost:3001)
      - websocket: endpoint is the ws:// URL
      - builtin:   no command/endpoint needed; adapter is instantiated by HubFactory
    """
    name: str = Field(description="Unique identifier for this sub-server (e.g. 'filesystem-main')")
    server_type: ServerType = Field(description="Functional category of this server")
    transport: TransportType = Field(
        default=TransportType.builtin,
        description="How Multi-MCP connects to this server.",
    )
    command: str | None = Field(
        default=None,
        description="Shell command to launch the sub-server (stdio transport only).",
    )
    endpoint: str | None = Field(
        default=None,
        description="HTTP/WebSocket URL for remote sub-servers.",
    )
    env_scope: list[str] = Field(
        default_factory=lambda: ["dev", "stage", "prod"],
        description="Environments where this server is active.",
    )
    enabled: bool = Field(default=True)
    tags: list[str] = Field(
        default_factory=list,
        description="Free-form tags for filtering/grouping (e.g. ['read-only', 'unity', 'local']).",
    )
    description: str = Field(default="", description="Human-readable description.")
    version_pin: str | None = Field(
        default=None,
        description="Version or git commit hash to pin (for reproducibility).",
    )
    exposed_tools: list[str] = Field(
        default_factory=list,
        description="Tools from this server that are exposed to clients. "
                    "If empty, all discovered tools are exposed.",
    )
    allowed_profiles: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Client profiles that may use this server. '*' = all.",
    )
    # Per-server tool exposure overrides by profile
    # e.g. {"Researcher": ["read_file", "list_directory"], "Coder": ["*"]}
    profile_tool_overrides: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-profile tool allowlists. Overrides allowed_profiles at tool level.",
    )
    policy: ServerPolicy = Field(default_factory=ServerPolicy)
    discovery: DiscoveryCache = Field(
        default_factory=DiscoveryCache,
        description="Cached result of the last tools/list discovery.",
    )
    adapter: Any = Field(
        default=None,
        exclude=True,
        description="Runtime adapter instance (not persisted).",
    )

    model_config = {"arbitrary_types_allowed": True}

    def get_effective_tools(self, profile: str | None = None) -> list[str]:
        """
        Return the list of tools this server exposes for a given profile.

        Priority:
          1. profile_tool_overrides[profile] if set
          2. exposed_tools if non-empty
          3. discovery.tool_names() (all discovered tools)
        """
        # Profile-specific override
        if profile and profile in self.profile_tool_overrides:
            overrides = self.profile_tool_overrides[profile]
            if "*" in overrides:
                # wildcard: fall through to exposed_tools / discovery
                pass
            else:
                return overrides

        # Explicit exposed_tools list
        if self.exposed_tools:
            return self.exposed_tools

        # Fall back to discovered tools
        return self.discovery.tool_names()

    def is_tool_allowed_for_profile(self, tool_name: str, profile: str) -> bool:
        """Check if a specific tool is allowed for a given profile."""
        # Check profile_tool_overrides first
        if profile in self.profile_tool_overrides:
            overrides = self.profile_tool_overrides[profile]
            if "*" not in overrides:
                return tool_name in overrides

        # Check allowed_profiles
        if "*" not in self.allowed_profiles and profile not in self.allowed_profiles:
            return False

        # Check exposed_tools
        effective = self.get_effective_tools(profile)
        return tool_name in effective


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
    # Per-server exposure: {"filesystem-main": ["read_file"], "search-1": ["web_search"]}
    server_tool_grants: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-server tool grants for fine-grained control.",
    )


# ---------------------------------------------------------------------------
# Routing table entry
# ---------------------------------------------------------------------------

class RoutingEntry(BaseModel):
    """
    A single entry in the routing table.
    Maps a tool name → server name for a specific environment.
    """
    tool_name: str
    server_name: str
    server_type: ServerType
    transport: TransportType
    profiles: list[str] = Field(
        description="Profiles that can access this tool via this route."
    )


class RoutingTable(BaseModel):
    """
    The full routing table for an environment.
    Built by RoutingTableBuilder from the registered sub-servers.
    """
    environment: str
    entries: list[RoutingEntry] = Field(default_factory=list)
    built_at: datetime | None = None

    def resolve(self, tool_name: str, profile: str) -> RoutingEntry | None:
        """Find the routing entry for a tool+profile combination."""
        for entry in self.entries:
            if entry.tool_name == tool_name:
                if "*" in entry.profiles or profile in entry.profiles:
                    return entry
        return None

    def all_tools_for_profile(self, profile: str) -> list[str]:
        """List all tool names accessible by a given profile."""
        return [
            e.tool_name for e in self.entries
            if "*" in e.profiles or profile in e.profiles
        ]


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
    # Routing metadata (never contains secrets)
    routed_to: str | None = Field(
        default=None,
        description="Name of the sub-server that handled this call.",
    )
    env: str | None = None
