# Multi-MCP — Architecture

## Overview

Multi-MCP is a **Hub/Router gateway** that aggregates multiple MCP sub-servers behind a single endpoint. It enforces security policies, manages secrets, and provides a web-based management GUI.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client (LangGraph)                       │
│   tool_call(tool_name, args={alias: "remote1", ...}, profile)   │
└────────────────────────────┬────────────────────────────────────┘
                             │  POST /mcp/call/{env}
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Multi-MCP Hub                              │
│                                                                 │
│  ┌─────────────┐   ┌──────────────────┐   ┌─────────────────┐  │
│  │ SubServer   │   │  Enforcement     │   │  Audit Logger   │  │
│  │ Registry   │──▶│  Middleware      │   │  (audit.jsonl)  │  │
│  └─────────────┘   │  - allowed_root  │   └─────────────────┘  │
│                    │  - cwd/timeout   │   ┌─────────────────┐  │
│                    │  - denylist      │   │  Exec Logger    │  │
│                    │  - quota guard   │   │  (exec.jsonl)   │  │
│                    │  - masking       │   └─────────────────┘  │
│                    └──────────────────┘                         │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Adapters                              │   │
│  │  FilesystemAdapter │ ExecAdapter │ SearchAdapter         │   │
│  │  SSHAdapter        │ LogsAdapter │ ArtifactAdapter       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────┐   ┌──────────────────────────────────┐   │
│  │  SecretStore     │   │  SettingsManager                 │   │
│  │  (Fernet AES)    │   │  (config/dev.json, etc.)         │   │
│  └──────────────────┘   └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                             │
               ┌─────────────┼─────────────┐
               ▼             ▼             ▼
        Filesystem       Tavily API     SSH Remote
        (local FS)      (REST API)      (asyncssh)
```

## Directory Structure

```
Multi-MCP/
├── src/multi_mcp/
│   ├── main.py                  # FastAPI entry point
│   ├── hub/
│   │   ├── router.py            # MCPHub, SubServerRegistry
│   │   └── factory.py           # HubFactory (wires adapters)
│   ├── enforcement/
│   │   └── middleware.py        # EnforcementMiddleware (policy enforcement)
│   ├── logging/
│   │   ├── audit.py             # AuditLogger (who/what/when)
│   │   └── execution.py         # ExecutionLogger (stdout/stderr/result)
│   ├── models/
│   │   ├── config.py            # Pydantic models (policies, sub-servers, etc.)
│   │   ├── secrets.py           # SecretStore (Fernet encryption)
│   │   └── settings_manager.py  # Load/save EnvironmentConfig
│   ├── adapters/
│   │   ├── filesystem.py        # Filesystem read/write
│   │   ├── exec.py              # Local command execution
│   │   ├── ssh.py               # Remote SSH execution
│   │   ├── search.py            # Tavily web search
│   │   ├── logs.py              # Log reading
│   │   └── artifact.py          # Artifact save/read
│   └── gui/
│       ├── api.py               # GUI REST API
│       ├── mcp_endpoint.py      # MCP tool-call endpoint
│       └── templates/
│           └── index.html       # Management console UI
├── config/                      # Environment configs (gitignored except .gitkeep)
├── logs/                        # Audit + execution logs (gitignored)
├── .secrets/                    # Encrypted secrets (gitignored)
├── docs/
│   └── architecture.md
├── tests/
├── pyproject.toml
├── .env.example
└── .gitignore
```

## Key Design Decisions

### 1. Secrets are server-owned
Clients pass only `alias` references (e.g., `"alias": "tavily_default"`). The actual API key/SSH credential is stored in `.secrets/store.json` encrypted with Fernet (AES-128-CBC + HMAC). The key is in `.secrets/master.key` (gitignored, 0600 permissions).

### 2. Policies are enforced, not optional
`EnforcementMiddleware.pre_call()` runs before every adapter call and raises `PermissionError` on violation. There is no way for a client to bypass this.

| Policy | Enforcement |
|--------|-------------|
| `allowed_root` | `os.path.realpath()` + prefix check (symlink-safe) |
| `cwd` | Same realpath check for exec working directory |
| `timeout` | `asyncio.wait_for()` in ExecAdapter |
| `output cap` | Byte-level truncation in ExecAdapter + post-call in middleware |
| `concurrency` | `asyncio.Semaphore` in ExecAdapter |
| `denylist` | Regex match before exec |
| `search quota` | Counter in SearchAdapter, persisted to `logs/search_quota.json` |
| `SSH alias-only` | Alias must exist in `SSHPolicy.allowed_aliases` |

### 3. Audit vs Execution logs
- **Audit log** (`logs/audit/audit.jsonl`): who/what/when/success-or-fail. Always retained. No raw output.
- **Execution log** (`logs/execution/execution.jsonl`): stdout/stderr/result. May be large. Shorter retention.

### 4. Client profiles
Each `EnvironmentConfig` has a list of `ClientProfile` objects. The hub checks `allowed_profiles` on the sub-server before routing. Profiles define `allowed_tools` and `denied_tools`.

## MCP Tool Call Flow

```
Client → POST /mcp/call/{env}
  → MCPHub.call_tool(request, profile)
    → SubServerRegistry.resolve_server_for_tool(tool_name)
    → EnforcementMiddleware.pre_call(request, server, profile)
      → _check_tool_exposure()
      → _check_filesystem() / _check_exec() / _check_ssh() / _check_search()
    → server.adapter.call(request)
    → EnforcementMiddleware.post_call(result, server, profile)
      → _cap_exec_output()
      → _mask_sensitive()
    → AuditLogger.log_success() + ExecutionLogger.log()
    → ToolCallResponse → Client
```

## Recommended MCP Server Candidates

| Function | Recommended Server | Source |
|----------|-------------------|--------|
| Filesystem | `@modelcontextprotocol/server-filesystem` | npm (official) |
| Web Search | `mcp-tavily` / `tavily-mcp` | pip / npm |
| SSH | Community SSH MCP server | pip |
| Exec/Terminal | Built-in adapter | — |
| Logs/Process | Built-in adapter | — |
| Artifact | Built-in adapter | — |
| GitHub | `github/github-mcp-server` | Future |
| Docs/RAG | TBD | Future |


### 4. Sub-server Registry & Discovery

Multi-MCP의 확장성은 Sub-server Registry에 의존합니다. 사용자는 GUI를 통해 외부 또는 내부 MCP 서버를 등록할 수 있습니다.

- **등록**: 사용자는 서버의 `name`, `type`, `transport` (`stdio`, `http`, `websocket`, `builtin`), 그리고 `command` 또는 `endpoint`를 지정하여 새 서버를 등록합니다.
- **디스커버리**: 등록된 서버에 대해 `tools/list` 호출을 수행하여 제공하는 도구 목록을 가져옵니다. 이 결과는 `SubServerConfig` 내의 `DiscoveryCache`에 저장됩니다.
- **라우팅 테이블 빌더**: `RoutingTableBuilder`는 모든 활성화된 서버의 디스커버리 캐시와 클라이언트 프로파일 설정을 종합하여, 어떤 `tool`이 어떤 `server`에 의해 처리되어야 하는지를 결정하는 라우팅 테이블을 동적으로 생성합니다.
- **프로파일 기반 노출 제어**: 라우팅 테이블은 클라이언트 프로파일의 `allowed_tools`, `denied_tools` 및 서버의 `allowed_profiles`, `profile_tool_overrides` 설정을 모두 고려하여 최종적으로 특정 프로파일이 사용할 수 있는 도구 목록을 결정합니다.
