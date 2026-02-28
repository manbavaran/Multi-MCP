# Multi-MCP Hub

**Multi-MCP**는 여러 MCP(Model Context Protocol) 서버를 단일 허브/라우터로 통합하는 보안 게이트웨이입니다. 클라이언트(LangGraph 등)에는 단일 엔드포인트처럼 보이며, 내부에서 정책 강제, 비밀 관리, 감사 로그를 처리합니다.

> 최우선 기준: [`.github/AGENTS.md`](.github/AGENTS.md)

---

## 핵심 기능

| 기능 | 설명 |
|---|---|
| **Hub/Router** | 여러 MCP sub-server를 등록하고 tool 호출을 라우팅 |
| **Policy Enforcement** | allowed_root, cwd, timeout, output cap, denylist, quota — 항상 강제 |
| **Secret Management** | Fernet 암호화 저장, alias 기반 접근, 클라이언트에 평문 미노출 |
| **Audit + Exec Logs** | 감사 로그와 실행 로그를 별도 파일에 분리 저장 |
| **Web GUI** | FastAPI 기반 관리 콘솔 (환경/서버/alias/정책/프로파일/로그) |
| **Multi-Environment** | dev / stage / prod 환경별 독립 설정 |
| **Client Profiles** | Researcher / Coder / Ops 등 역할별 도구 노출 분리 |
| **Built-in Core** | Filesystem, Exec, SSH, Search 등 6개 코어 서버 자동 등록 |

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
# .env.example을 .env로 복사하여 필요한 값을 수정할 수 있습니다.
# 기본적으로는 파일 없이도 동작합니다.
```

### 서버 실행

```bash
# 개발 모드 (코드 변경 시 자동 재시작)
uvicorn multi_mcp.main:app --reload --host 0.0.0.0 --port 8765
```

서버 시작 후 브라우저에서 `http://localhost:8765` 접속 → 관리 콘솔

---

## 빠른 시작: 3단계

1.  **서버 실행**: 위 `uvicorn` 명령어로 서버를 실행합니다.
2.  **관리 콘솔 접속**: 브라우저에서 `http://localhost:8765`를 엽니다.
3.  **API Key 등록**: `Aliases` 탭 → `Search API Keys`에서 `tavily_default`의 API 키를 입력하고 저장합니다.

이제 `core-search` 서버가 **✅ Ready** 상태가 되며, 모든 코어 서버를 사용할 수 있습니다.

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

---

## Unity MCP Bridge 연동

1.  **UnityMcpBridge.cs 추가**: `extensions/unity/UnityMcpBridge.cs` 파일을 Unity 프로젝트의 `Assets/Editor/` 폴더에 넣습니다.
2.  **자동 시작**: Unity가 컴파일을 마치면 자동으로 MCP 브릿지 서버가 시작됩니다. (Unity 콘솔에 `v2.1 started` 로그 확인)
3.  **Multi-MCP에 등록**: Multi-MCP 관리 콘솔 → `Sub-servers` 탭에서 `+ 서버 등록`을 클릭하고 아래와 같이 입력합니다.
    - **Name**: `unity-editor-1`
    - **Server Type**: `other`
    - **Transport**: `http`
    - **Endpoint**: `http://127.0.0.1:23457/mcp`
4.  **디스커버리**: 등록된 `unity-editor-1` 행의 `⏳ 클릭하여 디스커버리`를 클릭하면 **✓ 12개 도구 발견**으로 상태가 변경됩니다.

자세한 내용은 [`extensions/unity/Unity MCP Bridge 등록 및 사용 가이드.md`](extensions/unity/Unity%20MCP%20Bridge%20등록%20및%20사용%20가이드.md)를 참고하세요.

---

## 트러블슈팅

- **`✗ 오류 (클릭하여 재시도)`가 표시될 때**:
  - **원인**: Sub-server가 실행 중이 아니거나, 엔드포인트 주소가 잘못되었습니다.
  - **해결**: 
    1. Unity의 경우, Unity 콘솔에 `v2.1 started` 로그가 있는지 확인합니다.
    2. Multi-MCP GUI에서 서버의 Endpoint 주소가 올바른지 확인합니다.
    3. 서버를 재시작하고 다시 Discover를 클릭합니다.

- **`httpx not installed` 오류**:
  - **원인**: Multi-MCP v0.1.0 초기 버전의 버그입니다.
  - **해결**: `git pull origin main`으로 최신 코드를 받으면 `urllib` fallback이 적용되어 추가 설치 없이 해결됩니다.

---

## 아키텍처 및 보안

- **아키텍처**: 자세한 내용은 [`docs/architecture.md`](docs/architecture.md) 참고
- **보안 원칙**:
  1. **비밀은 서버가 소유**: API 키, SSH 자격증명은 Fernet 암호화 저장. 클라이언트에 평문 미노출.
  2. **정책은 강제**: 클라이언트가 우회할 수 없음. `EnforcementMiddleware`가 모든 호출 전에 검사.
  3. **read/write 분리**: Filesystem, SSH는 읽기/쓰기 도구 분리. 쓰기는 기본 OFF.
  4. **로그 분리**: 감사 로그(who/what/when)와 실행 로그(output) 별도 저장.
  5. **비밀 파일 gitignore**: `.secrets/`, `config/*.json`, `.env` 는 절대 커밋 금지.
