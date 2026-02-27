# Multi-MCP Hub

**Multi-MCP**는 여러 MCP(Model Context Protocol) 서버를 단일 허브/라우터로 통합하는 게이트웨이입니다. 클라이언트(LangGraph 등)에는 단일 엔드포인트처럼 보이며, 내부에서 정책 강제, 비밀 관리, 감사 로그를 처리합니다.

> 최우선 기준: [`.github/AGENTS.md`](.github/AGENTS.md)

---

## 핵심 기능

| 기능 | 설명 |
|------|------|
| **Hub/Router** | 여러 MCP sub-server를 등록하고 tool 호출을 라우팅 |
| **Policy Enforcement** | allowed_root, cwd, timeout, output cap, denylist, quota — 항상 강제 |
| **Secret Management** | Fernet 암호화 저장, alias 기반 접근, 클라이언트에 평문 미노출 |
| **Audit + Exec Logs** | 감사 로그와 실행 로그를 별도 파일에 분리 저장 |
| **Web GUI** | FastAPI 기반 관리 콘솔 (환경/서버/alias/정책/프로파일/로그) |
| **Multi-Environment** | dev / stage / prod 환경별 독립 설정 |
| **Client Profiles** | Researcher / Coder / Ops 등 역할별 도구 노출 분리 |

---

## 아키텍처

```
Client (LangGraph)
  │  alias 기반 호출만 허용
  ▼
Multi-MCP Hub (/mcp/call/{env})
  ├── EnforcementMiddleware  ← 정책 강제 (우회 불가)
  ├── SubServerRegistry      ← sub-server 등록/조회
  ├── AuditLogger            ← 감사 로그 (logs/audit/)
  ├── ExecutionLogger        ← 실행 로그 (logs/execution/)
  └── Adapters
       ├── FilesystemAdapter  (allowed_root 강제)
       ├── ExecAdapter        (timeout/output/concurrency 강제)
       ├── SearchAdapter      (Tavily, quota/cost guard)
       ├── SSHAdapter         (alias-only, no raw credentials)
       ├── LogsAdapter        (read-only, masking)
       └── ArtifactAdapter    (artifact_root 고정)
```

자세한 아키텍처: [`docs/architecture.md`](docs/architecture.md)

---

## 설치 및 실행

### 요구사항

- Python 3.11+

### 설치

```bash
# 1. 리포지토리 클론
git clone https://github.com/manbavaran/Multi-MCP.git
cd Multi-MCP

# 2. 가상환경 생성 (권장)
python3.11 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. 패키지 설치
pip install -e .

# 4. 환경 변수 설정 (선택)
cp .env.example .env
# .env 파일에서 필요한 값 수정
```

### 서버 실행

```bash
# 개발 모드 (자동 재시작)
uvicorn multi_mcp.main:app --reload --host 0.0.0.0 --port 8765

# 프로덕션 모드
uvicorn multi_mcp.main:app --host 0.0.0.0 --port 8765 --workers 1
```

서버 시작 후 브라우저에서 `http://localhost:8765` 접속 → 관리 콘솔

---

## 빠른 시작 (API)

### 1. 환경 생성

```bash
curl -X POST http://localhost:8765/api/environments/dev
```

### 2. Sub-server 등록

```bash
curl -X POST http://localhost:8765/api/environments/dev/servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "filesystem-main",
    "server_type": "filesystem",
    "command": "npx @modelcontextprotocol/server-filesystem /workspace",
    "exposed_tools": ["read_file", "list_directory", "write_file"],
    "allowed_profiles": ["*"],
    "enabled": true
  }'
```

### 3. Tavily API Key 등록 (alias 기반)

```bash
curl -X POST http://localhost:8765/api/environments/dev/aliases/search \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "tavily_default",
    "provider": "tavily",
    "api_key": "tvly-YOUR-KEY-HERE"
  }'
```

> API 키는 암호화 저장됩니다. 응답에는 마스킹된 미리보기만 반환됩니다.

### 4. 도구 호출 (클라이언트 방식)

```bash
# 파일 읽기
curl -X POST http://localhost:8765/mcp/call/dev \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "read_file",
    "args": {"path": "README.md"},
    "client_profile": "Researcher"
  }'

# 웹 검색 (alias 기반, 키 미노출)
curl -X POST http://localhost:8765/mcp/call/dev \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "web_search",
    "args": {"alias": "tavily_default", "query": "MCP protocol"},
    "client_profile": "Researcher"
  }'
```

### 5. 경로 탈출 시도 → 자동 차단

```bash
curl -X POST http://localhost:8765/mcp/call/dev \
  -H "Content-Type: application/json" \
  -d '{"tool_name":"read_file","args":{"path":"../../../etc/passwd"},"client_profile":"Researcher"}'
# → {"success": false, "error": "Policy violation: Path escapes allowed_root"}
```

---

## 코어 서버 자동 등록 (Built-in Core Servers)

Multi-MCP는 **첫 실행 시 6개의 코어 서버를 자동으로 등록**합니다. 별도 설정 없이 Sub-servers 페이지에서 바로 확인할 수 있습니다.

| 코어 서버 | 타입 | 상태 조건 | 필수 설정 |
|---|---|---|---|
| `core-filesystem` | filesystem | ✅ 즉시 Ready | 없음 (allowed_root 기본값 사용) |
| `core-exec` | exec | ✅ 즉시 Ready | 없음 (timeout/cwd 기본값 사용) |
| `core-ssh` | ssh | ⚠️ SSH alias 등록 필요 | Aliases → SSH Remotes |
| `core-logs` | logs | ✅ 즉시 Ready | 없음 |
| `core-search` | search | ⚠️ Tavily API Key 등록 필요 | Aliases → Search API Keys |
| `core-artifact` | artifact | ✅ 즉시 Ready | 없음 |

**상태 표시:**
- **✅ Ready** — 즉시 사용 가능
- **⚠️ Not Configured** — 자격증명 미설정. GUI에서 설정 링크 클릭
- **⏸ Disabled** — 사용자가 비활성화

**규칙:**
- 코어 서버는 **삭제 불가** (Disable 토글만 허용)
- 코어 서버는 `Built-in` 배지로 표시되며 항상 목록 상단에 위치
- Not Configured 상태에서 도구 호출 시 `4xx` 오류 반환 + audit log 기록
- 자격증명은 서버(GUI)에서만 입력·저장·사용 (클라이언트 미노출)

---

## Sub-server Registry

Multi-MCP의 핵심 기능은 기존 MCP 서버를 **Sub-server**로 등록하여 확장하는 것입니다. GUI의 `Sub-servers` 페이지에서 다음을 관리할 수 있습니다:

- **등록/수정/삭제**: `name`, `type`, `transport`(`builtin`, `stdio`, `http`), `command`/`endpoint` 등을 포함한 서버 정보를 관리합니다.
- **자동 디스커버리**: 등록된 서버의 `tools/list`를 호출하여 사용 가능한 도구 목록을 자동으로 가져옵니다.
- **라우팅 테이블**: 디스커버리 결과를 바탕으로 `tool -> sub-server` 라우팅 테이블을 동적으로 구성하고, `Routing Table` 페이지에서 확인할 수 있습니다.
- **프로파일 노출 제어**: 각 서버의 상세 페이지에서 프로파일별로 노출할 도구를 세밀하게 제어할 수 있습니다.

---

## GUI 관리 콘솔

`http://localhost:8765` 접속 시 다음 기능을 제공합니다:

| 메뉴 | 기능 |
|------|------|
| **환경** | dev/stage/prod 생성 및 관리 |
| **Sub-Servers** | MCP 서버 등록/활성화/비활성화 |
| **Aliases** | SSH 원격 서버, Tavily API Key (암호화 저장) |
| **정책** | Exec/Filesystem/Search/Logs 정책 (추천값+이유 표시) |
| **프로파일** | 클라이언트별 도구 노출 범위 설정 |
| **로그** | 감사 로그 / 실행 로그 탭 분리 조회 |

---

## 보안 원칙

1. **비밀은 서버가 소유**: API 키, SSH 자격증명은 Fernet 암호화 저장. 클라이언트에 평문 미노출.
2. **정책은 강제**: 클라이언트가 우회할 수 없음. `EnforcementMiddleware`가 모든 호출 전에 검사.
3. **read/write 분리**: Filesystem, SSH는 읽기/쓰기 도구 분리. 쓰기는 기본 OFF.
4. **로그 분리**: 감사 로그(who/what/when)와 실행 로그(output) 별도 저장.
5. **비밀 파일 gitignore**: `.secrets/`, `config/*.json`, `.env` 는 절대 커밋 금지.

---

## 프로젝트 구조

```
Multi-MCP/
├── src/multi_mcp/
│   ├── main.py                  # FastAPI 진입점
│   ├── hub/                     # Hub/Router 코어
│   ├── enforcement/             # 정책 강제 미들웨어
│   ├── logging/                 # 감사/실행 로그
│   ├── models/                  # 설정 스키마, 비밀 저장소
│   ├── adapters/                # Filesystem/Exec/SSH/Search/Logs/Artifact
│   └── gui/                     # FastAPI GUI + MCP 엔드포인트
├── docs/                        # 아키텍처 문서
├── tests/                       # 테스트
├── config/                      # 환경 설정 (gitignored)
├── logs/                        # 로그 파일 (gitignored)
├── .secrets/                    # 암호화 비밀 (gitignored)
├── pyproject.toml
├── .env.example
└── .gitignore
```

---

## 개발 의존성 업데이트 (Repo Hygiene)

새 기능 추가 시 `pyproject.toml`의 `dependencies`를 반드시 업데이트하세요.

```bash
pip install -e ".[dev]"
pytest tests/
```

---

## 로드맵

- [ ] GitHub MCP 연동 (스캐폴딩 준비됨, 기본 비활성)
- [ ] Docs/RAG MCP 연동 (스캐폴딩 준비됨, 기본 비활성)
- [ ] 설정 export/import (비밀 제외 또는 암호화 포함 옵션)
- [ ] 기본 템플릿 (Research-only, Coder safe, Prod locked-down)
- [ ] Docker Compose 지원
