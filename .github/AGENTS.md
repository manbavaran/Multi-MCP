# Multi-MCP — AGENTS.md

Multi-MCP는 **여러 MCP 서버 기능을 한 프로젝트에서 통합 제공**하는 “멀티 MCP 서버(=허브)”입니다.  
핵심 목표는 아래 4가지입니다.

1) **비밀/정책을 서버가 소유**하고, LangGraph(클라이언트)는 **alias로만 호출**한다.  
2) 정책은 “설정”이 아니라 **강제(enforcement)** 된다. (우회/탈출 불가)  
3) GUI(설정 페이지)로 **비밀/환경/정책/제한**을 관리하고, 실행/감사 로그를 분리하여 운영한다.  
4) **처음부터 전부 구현하지 않는다.** 가능하면 **이미 공개된/검증된 MCP 서버를 가져다 쓰고**, Multi-MCP는 이를 **통합·관리·가드레일·GUI**로 감싼다.

---

## 1. 범위와 도메인

Multi-MCP는 두 도메인을 모두 만족해야 한다.

### 1.1 기획/설계(Research/Design) 도메인
- 웹 검색(레퍼런스 확인, 근거 수집)
- 내부 문서 열람/검토 (Filesystem 기반: 문서 파일 읽기 및 필요 시 파싱)
- 실행 결과/리포트 산출물 정리(문서/아티팩트)
- (추후) 내부 문서 검색(RAG)

### 1.2 코드 개발(Engineering) 도메인
- 파일 읽기/쓰기(코드 수정)
- 로컬 실행(테스트/빌드/실행)
- 로그 수집/요약(실행 로그, 서비스 로그)
- (옵션) SSH 원격 실행
- (추후) GitHub 작업(PR/이슈 등)

---

## 2. 제공 MCP 모듈(현재/추후) — “재사용 우선” 전략

### 2.1 현재 구현 대상(기능 카테고리)
A. **Filesystem MCP**  
B. **Command / Terminal (Exec) MCP**  
C. **SSH / Remote Exec MCP**  
D. **Logs/Process MCP**  
E. **Web Search MCP (Tavily API)**  
F. **Artifact/Result MCP**

### 2.2 추후 구현(로드맵)
G. **GitHub MCP**  
H. **Docs / RAG MCP (내부 문서 검색)**

### 2.3 핵심 정책: “가능하면 기존 MCP 서버를 사용”
- 각 모듈은 기본적으로 **기존 MCP 서버 구현체(오픈소스/공식/커뮤니티)**를 **우선 채택**한다.
- Multi-MCP는 아래 역할을 한다.
  - (1) 여러 MCP 서버를 **한 GUI에서 설정/관리**
  - (2) 환경(dev/stage/prod) 및 alias를 **표준화**
  - (3) 정책(enforcement), rate limit, tool exposure를 **서버 측에서 강제**
  - (4) audit/execution 로그를 **통합 기록**
  - (5) 필요할 때만 “부족한 부분”을 **플러그인으로 보완**

> 정리: Multi-MCP는 “모든 툴을 직접 구현하는 서버”가 아니라,  
> **기존 MCP 서버들을 연결하고 안전하게 운영하도록 만드는 ‘통합 컨트롤 플레인 + GUI + 정책 게이트웨이’**다.

---

## 3. Multi-MCP의 구현 형태(권장)

Multi-MCP는 “멀티 MCP 서버”를 아래 두 방식 중 하나로 구현한다.

### 3.1 (권장) Hub/Router(게이트웨이) 방식
- 여러 MCP 서버(외부/내부)를 **Sub-server**로 등록한다.
- Multi-MCP는 클라이언트에게 **단일 엔드포인트처럼** 보이게 하되,
  내부에서 tool 호출을 해당 sub-server로 라우팅한다.
- 라우팅 전/후에 정책을 강제하고 로그를 남긴다.

### 3.2 (대안) Bundled Server(내장 플러그인) 방식
- 일부 기능은 기존 MCP 서버를 “내장/번들”하여 단일 프로세스로 배포한다.
- 다만 운영/업데이트 편의를 위해, 기본은 3.1을 우선한다.

---

## 4. 핵심 설계 원칙(요구사항 + 보완사항 통합)

### 4.1 비밀은 MCP 서버가 소유한다 (클라이언트 평문 금지)
- LangGraph는 절대 비밀번호/토큰/키를 평문으로 읽거나 보관하면 안 된다.
- 클라이언트는 **alias**만 전달한다.
  - 예: `ssh_run(alias="remote1", cmd="...")`
  - 예: `web_search(alias="tavily_default", query="...")`

### 4.2 정책은 “설정”이 아니라 “강제”다
- 설정 UI에서 값을 바꾸더라도 런타임에 항상 강제한다.
  - Filesystem: allowed_root 밖 접근 금지(정규화/심볼릭 링크 우회 방지)
  - Exec: cwd 고정/timeout/output/concurrency 제한/금지명령 차단
  - SSH: host alias만 허용/원격 작업 루트 제한(가능한 방식으로)
  - Search: 쿼터/요청 상한/옵션 상한(비용 가드)

### 4.3 read-only / write 분리
- 툴을 “읽기 전용”과 “쓰기/변경”으로 분리한다.
- 기존 MCP 서버를 쓰더라도 Multi-MCP가 **노출 범위/권한**을 분리해서 제공한다.

### 4.4 감사 로그(audit)와 실행 로그(exec/log) 분리
- Audit: 누가/언제/무슨 tool을/어떤 alias로/성공·실패
- Execution: stdout/stderr/요약/결과 아티팩트
- 민감정보 마스킹은 기본 강제

### 4.5 GUI 기반 설정 UX(핵심)
- Multi-MCP는 GUI 환경에서 설정 페이지를 제공한다.
- “비밀 입력”뿐 아니라 alias, 환경, 정책, 제한을 모두 GUI에서 다룬다.
- 각 제한값은 “추천 디폴트 + 이유”를 함께 표시한다.

### 4.6 멀티 환경(dev/stage/prod) 구분
- 환경별로 alias/정책/키/노출 tool을 분리한다.
- prod는 보수적 기본값(write/act 기본 OFF)

### 4.7 레이트리밋/쿼터/비용 가드(서버 강제)
- Web Search(Tavily)는 예산/쿼터를 반드시 서버에서 강제한다.
- Exec/Logs도 폭주 방지를 서버에서 강제한다.

### 4.8 도구 목록 노출 범위(클라이언트별 분리)
- “클라이언트 프로파일”을 통해 tool을 분리한다.
- 프로파일별로 sub-server tool들도 노출/비노출 가능해야 한다.

---

## 5. “기존 MCP 서버 재사용” 운영 정책

### 5.1 서버 선택 기준(체크리스트)
- 신뢰성: 유지관리 여부(최근 커밋/릴리즈), 이슈 대응
- 보안: 권한 범위 최소화 가능 여부, 경로/명령 제한 가능 여부
- 운영: 컨테이너/프로세스 실행 용이성, 로깅/설정의 단순성
- 라이선스: 사용/배포 가능 여부

### 5.2 “가져다 쓴 MCP 서버”의 위험 최소화 방식
- 기존 MCP 서버가 정책 강제를 충분히 제공하지 않으면:
  - Multi-MCP가 **게이트웨이에서 강제**
  - 또는 해당 서버를 “read-only 프로파일”로만 노출
- 위험도가 큰 기능(임의 command, write FS, ssh act)은:
  - 기본 비노출
  - 환경(prod)에서 자동 차단
  - 필요 시 별도 프로파일로만 노출

### 5.3 레퍼런스(가져다 쓸 후보군)
- MCP 서버 레퍼런스 모음(공식/커뮤니티): `modelcontextprotocol/servers`
- GitHub 공식 MCP 서버(추후): `github/github-mcp-server`
- Filesystem 관련 서버(레퍼런스/커뮤니티) 등

> Multi-MCP는 “어떤 MCP 서버를 선택했는지”를 문서/설정 내에서 명시하고,
> 버전 고정(핀)과 업데이트 정책을 갖는다.

---

## 6. 모듈별 설계(“재사용 우선” + “강제 정책”)

### A. Filesystem MCP
**우선 전략**
- 먼저 공개된 Filesystem MCP 서버를 채택한다.
- Multi-MCP는 해당 서버를 sub-server로 등록하고, 아래를 강제한다.
- Research/Design 도메인에서의 내부 문서 열람(README/Markdown/Word/PDF 등)은 기본적으로 Filesystem MCP(read)를 통해 수행한다.
- 문서가 .docx/.pdf인 경우, 텍스트 추출은 Exec MCP를 통한 파서 실행(또는 내장 파서)로 지원한다.

**필수 강제(게이트웨이)**
- `allowed_root` 하위만 허용(정규화/심볼릭 링크 우회 방지 포함)
- read-only / write API 분리 노출
- 환경별 allowed_root 분리(dev/stage/prod)

### B. Command / Terminal (Exec) MCP
**우선 전략**
- 공개된 exec/command MCP 서버를 채택하거나, OS 명령 실행 서버를 사용한다.
- Multi-MCP는 실행 전/후 정책을 강제한다.

**필수 강제**
- cwd allowed_root 하위 고정
- timeout/output limits/concurrency 강제
- (권장) denylist 또는 allowlist
- (권장) venv/conda 경로 고정 프로필 지원

### C. SSH / Remote Exec MCP
**우선 전략**
- 공개된 ssh/remote-exec MCP 서버(또는 exec 서버의 ssh 확장)를 우선 채택한다.
- Multi-MCP는 alias/비밀/정책을 GUI로 관리한다.

**GUI 요구**
- `remote1`, `remote2`… alias 등록
  - host, port, username
  - auth: (권장) key / (차선) password
- 클라이언트는 alias만 사용(평문 금지)

**필수 강제**
- 허용 alias만
- 환경별 alias 분리
- (권장) 원격에서도 cwd/allowed_root 강제(가능한 방식으로)
- (권장) read-only/act 분리(특히 prod)

### D. Logs/Process MCP
**우선 전략**
- 공개된 logs/process MCP 서버가 있으면 채택한다.
- 없거나 부족하면 exec를 통해 “읽기 전용 로그 명령”만 제공하는 방식으로 대체 가능.

**필수 강제**
- 최대 라인/시간 범위 제한
- 민감정보 마스킹
- 허용된 서비스/로그 소스만 접근

### E. Web Search MCP (Tavily)
**우선 전략**
- Tavily용 MCP 서버가 존재하면 우선 사용하고, 없으면 얇은 어댑터만 직접 구현한다.
- Multi-MCP는 API 키 및 예산/쿼터를 GUI에서 관리한다.

**필수 강제**
- 월/일 요청 상한(쿼터)
- search_depth 기본값 및 상한
- max_results 상한
- 비용 가드(예산 소진 시 자동 차단)

### F. Artifact/Result MCP
**우선 전략**
- 공개된 artifact/result 서버가 있으면 채택한다.
- 없으면 Multi-MCP 내장 플러그인으로 얇게 구현한다(단순 저장/정리 중심).

**필수 강제**
- `artifact_root` 고정
- 파일 크기 제한/확장자 제한
- 메타데이터(run_id/profile/env) 기록
- 민감정보 포함 금지(필터링 옵션)

---

## 7. 레이트리밋/디폴트 값(권장) + 이유

> 아래 값은 “안전한 디폴트”이며, GUI에서 변경 가능하되 서버는 항상 강제한다.

### 7.1 Exec(로컬/원격) 권장 디폴트
- `timeout_sec = 60`
  - 이유: 무한 대기/블로킹 방지. 실패 징후는 보통 빠르게 드러남.
- `max_stdout_kb = 256`, `max_stderr_kb = 256` 또는 `max_lines = 2000`
  - 이유: 로그 폭주로 비용/응답 지연/저장 공간 문제 방지.
- `max_concurrency = 1`
  - 이유: 로그/상태 혼선 및 자원 폭주 방지.

### 7.2 Logs 권장 디폴트
- `max_lines = 2000`, `time_window = 10 minutes`
  - 이유: 관측은 충분히 하되 “전체 덤프”로 폭주하지 않도록.

### 7.3 Tavily 권장 디폴트
- `search_depth = "basic"` 기본
  - 이유: 비용 대비 효율이 좋고, 대부분의 조사에 충분.
- `max_results = 5~10`
  - 이유: 근거 다양성 확보 + 비용 통제의 균형.
- `daily_request_cap` + `monthly_credit_budget`
  - 이유: pay-as-you-go에서 비용 폭탄 방지.

GUI는 각 항목에 “추천 이유”를 반드시 표시한다.

---

## 8. 클라이언트별 도구 노출(Profiles)

### 8.1 프로파일 예시
- **Researcher**
  - 노출: Web Search, Artifact(Result), (선택) Filesystem read
  - 숨김: Filesystem write, Exec, SSH act
- **Designer/Planner**
  - 노출: Filesystem read, Web Search, Artifact
  - 숨김: Exec(선택), SSH
- **Coder**
  - 노출: Filesystem read/write, Exec, Logs
  - 숨김: SSH act(기본)
- **Ops**
  - 노출: Logs, SSH read
  - 숨김: Filesystem write, Exec(선택), SSH act(기본 OFF)

### 8.2 프로파일과 환경의 결합
- prod 환경에서는 write/act 류 tool을 기본 비노출로 두는 것이 권장.
- GUI에서 “환경별 프로파일”을 구성 가능해야 함.

---

## 9. GUI 설정 페이지 요구사항(재사용 서버 포함)

### 9.1 설정 카테고리
1) **Environments**: dev/stage/prod 생성/복제/내보내기/가져오기  
2) **Sub-Servers Registry(핵심)**
   - 가져다 쓸 MCP 서버 등록
   - 서버 종류(예: filesystem/exec/ssh/logs/search/artifact)
   - 실행 방식(로컬 프로세스/도커/원격)
   - 버전/커밋 핀(가능하면)
3) **Aliases**
   - SSH remotes: remote1/remote2… (host, user, auth)
   - Tavily: tavily_default… (api key)
   - Exec profiles: python env, cwd root, allowlist 정책
4) **Policies**
   - Filesystem allowed_root
   - Exec allowlist/denylist, timeout, output limits, concurrency
   - Logs limits, masking rules
   - Search caps(쿼터/비용)
5) **Profiles (Tool Exposure)**
   - 클라이언트/역할별 노출 도구 설정
6) **Logging**
   - Audit log(보존기간, 저장 위치)
   - Execution log(보존기간, 저장 위치)
7) **Security**
   - 비밀 저장 방식(암호화, 마스킹, export 제한)
   - 관리자 권한(선택)

### 9.2 “추천 디폴트 + 이유” UI
- 설정 항목마다:
  - 추천값(기본)
  - 추천 이유(한두 줄)
  - 변경 시 부작용
을 함께 표시한다.

---

## 10. 비밀(Secrets) 처리 정책

### 10.1 저장
- 비밀은 서버 내부 안전 저장소에 저장(암호화 권장).
- 클라이언트/로그/설정 export에서 평문 노출 금지.
- 화면 표시 시 마스킹(예: `sk-****...`).

### 10.2 전달/사용
- LangGraph는 alias만 전달
- Multi-MCP는 sub-server 호출 전 내부에서만 키를 적용

### 10.3 회전/폐기
- 키 교체(rotate) 기능
- 사용 중지(disable) 기능
- 환경별 키 분리(dev/stage/prod)

---

## 11. 완료 정의(Definition of Done)

### 11.1 MVP(A~F, 재사용 우선)
- GUI에서 dev/stage/prod 생성 및 설정 가능
- Sub-server registry에서 기존 MCP 서버를 등록/활성화 가능
- SSH alias 등록(remote1/remote2) + 비밀 평문 노출 없음
- Filesystem allowed_root 강제(우회 방지 포함)
- Exec cwd/timeout/output/concurrency 강제
- Logs/Process 수집 + 제한 + 마스킹
- Tavily 키 입력 + 쿼터/가드 + 기본값/이유 표시
- 도구 노출 프로파일 적용 가능
- Audit vs Execution 로그 분리 저장 + GUI 뷰어 제공

### 11.2 안정화
- 설정 export/import(비밀 제외 또는 암호화 포함 옵션)
- 기본 템플릿 제공:
  - “Research-only profile”
  - “Coder safe profile”
  - “Prod locked-down profile”

---

## 12. 금지 사항(명시)
- 클라이언트가 비밀정보(IP/비번/API키)를 평문으로 보관하거나 프롬프트에 포함하는 것
- Filesystem allowed_root 밖 접근을 허용하는 설정
- Exec 무제한 실행/무제한 출력/무제한 동시성을 허용하는 설정
- Audit log에 민감정보가 남는 구성

---

## 운영 규칙
### Repo Hygiene (필수)
- 폴더 구조는 지속적으로 정리한다. (불필요한 중복/임시 스크립트/산발적 위치 방치 금지)
- 새 모듈/기능 추가 시, 디렉토리 배치를 먼저 결정하고 일관된 트리에 반영한다.
- 의존성은 단일 소스 오브 트루스(SSOT)로 관리한다.
  - pyproject.toml(권장) 또는 requirements.txt(선택) 중 하나를 기준으로 삼고,
  - 둘을 동시에 쓰는 경우 동기화 규칙을 문서화한다.
- 기능 추가/변경으로 import/런타임 요구사항이 바뀌면, 같은 PR/변경셋에서
  - pyproject.toml / requirements*.txt
  - (필요 시) lock 파일
  을 반드시 업데이트한다.
- 문서/예제/실행 방법(README, AGENTS.md 포함)에서 설치/실행 명령이 깨지지 않도록
  변경이 있을 때마다 함께 갱신한다.
- .gitignore는 반드시 유지/갱신하라. (Python 캐시, venv/conda, build/dist, .mypy_cache, .pytest_cache, coverage, OS 파일, IDE 설정, 로그/아티팩트 디렉토리, .env 등 비밀 파일 패턴 포함)
- 리포지토리에 민감정보(키/비번/토큰/개인키)가 들어갈 수 있는 파일은 기본적으로 ignore하고, 예제는 .env.example 같은 샘플로만 제공하라.

---

끝.