# Unity MCP Bridge 등록 및 사용 가이드

## 1. 개요

Unity Editor 내에서 `UnityMcpBridge.cs`를 실행하면 HTTP 기반의 MCP 서버가 로컬에 열립니다. Multi-MCP는 이 서버를 **Sub-server**로 등록하여, AI 에이전트가 Unity 씬을 직접 조작할 수 있도록 단일 엔드포인트로 노출합니다.

## 2. Unity 측 설치 방법

### 2-1. 패키지 의존성 설치

Unity Package Manager에서 다음 패키지를 추가합니다.

```
com.unity.nuget.newtonsoft-json
```

`Window → Package Manager → + → Add package by name`에서 위 이름을 입력합니다.

### 2-2. 스크립트 배치

`UnityMcpBridge.cs` 파일을 Unity 프로젝트의 `Assets/Editor/` 폴더에 복사합니다. Unity가 자동으로 컴파일합니다.

### 2-3. 서버 시작

Unity 상단 메뉴에서 `Multi-MCP → Unity Bridge → Start`를 클릭합니다. 또는 `Settings`에서 **Auto Start**를 활성화하면 에디터 실행 시 자동으로 시작됩니다.

기본 포트는 **23457**입니다 (Multi-MCP Hub 포트 8765와 충돌하지 않도록 변경됨).

## 3. Multi-MCP에 등록하는 방법

Multi-MCP 관리 콘솔(`http://localhost:8765`)의 **Sub-servers** 탭에서 아래와 같이 등록합니다.

| 필드 | 값 |
| :--- | :--- |
| **Name** | `unity-editor-1` |
| **Type** | `other` |
| **Transport** | `http` |
| **Endpoint** | `http://127.0.0.1:23457/mcp` |
| **Env Scope** | `dev` |
| **Description** | Unity Editor MCP Bridge v2 |
| **Tags** | `unity`, `game-engine` |

등록 후 **Discover** 버튼을 누르면 아래 12개 도구가 자동으로 발견됩니다.

## 4. 제공되는 도구 목록 (v2)

| 도구 이름 | 설명 | AI 자율 루프 역할 |
| :--- | :--- | :--- |
| `unity.query_scene` | 씬 전체 계층 구조 스냅샷 반환 | **관찰(Observe)** |
| `unity.manage_gameobject` | 오브젝트 찾기/생성/삭제/이동 | 행동(Act) |
| `unity.manage_components` | 컴포넌트 목록/추가/제거 | 행동(Act) |
| `unity.get_component_property` | 컴포넌트 필드/속성 값 읽기 | **관찰(Observe)** |
| `unity.set_component_property` | 컴포넌트 필드/속성 값 쓰기 | 행동(Act) |
| `unity.call_component_method` | 컴포넌트 메소드 직접 호출 | **행동(Act) — 핵심** |
| `unity.send_event` | SendMessage로 이벤트 전송 | 행동(Act) |
| `unity.control_playmode` | 플레이 모드 진입/종료/일시정지/타임스케일 | **제어(Control)** |
| `unity.manage_scene` | 씬 열기/저장/목록 | 제어(Control) |
| `unity.manage_asset` | 에셋 검색 및 프리팹 인스턴스화 | 행동(Act) |
| `unity.execute_menu_item` | 에디터 메뉴 항목 실행 | 행동(Act) |
| `unity.read_console` | 에디터 로그 읽기 (필터 지원) | **관찰(Observe)** |

## 5. AI 자율 게임 조작 루프 예시

AI 에이전트가 사용자 개입 없이 Unity를 조작하는 전형적인 루프는 다음과 같습니다.

```
1. [Observe]  unity.query_scene          → 현재 씬 상태 파악
2. [Observe]  unity.get_component_property → 플레이어 HP, 위치 등 상태 확인
3. [Plan]     AI가 다음 행동 결정
4. [Act]      unity.call_component_method  → 공격, 이동, 아이템 사용 등 실행
5. [Observe]  unity.read_console          → 실행 결과 및 오류 확인
6. [Repeat]   1번으로 돌아가 루프 반복
```

### 구체적인 예시

**플레이어 체력 확인 후 포션 사용:**
```json
// 1. 체력 확인
{"tool": "unity.get_component_property",
 "arguments": {"path": "Player", "component": "PlayerHealth", "property": "currentHp"}}

// 2. 체력이 낮으면 포션 사용 메소드 호출
{"tool": "unity.call_component_method",
 "arguments": {"path": "Player", "component": "Inventory", "method": "UseItem",
               "args": ["health_potion", 1]}}
```

**적 탐색 후 공격:**
```json
// 1. 씬에서 Enemy 태그 오브젝트 탐색
{"tool": "unity.query_scene",
 "arguments": {"filter_tag": "Enemy", "include_inactive": false}}

// 2. 가장 가까운 적에게 공격 이벤트 전송
{"tool": "unity.send_event",
 "arguments": {"path": "Enemy/Goblin_01", "message": "TakeDamage", "arg": 25}}
```

## 6. 보안 고려 사항

- Unity Bridge는 `127.0.0.1`(localhost)에서만 수신합니다. 외부 네트워크에 노출되지 않습니다.
- Bearer Token을 설정한 경우, Multi-MCP의 **Aliases** 탭에서 `unity_token`이라는 이름으로 저장하고, Sub-server 등록 시 해당 alias를 참조합니다. 토큰 값은 Multi-MCP SecretStore에 암호화되어 저장됩니다.
- 모든 도구 호출은 `logs/audit/audit.jsonl`에 기록됩니다. 토큰 등 민감 정보는 로그에 절대 기록되지 않습니다.

## 7. v1 → v2 변경 사항 요약

| 항목 | v1 | v2 |
| :--- | :--- | :--- |
| 기본 포트 | 8765 (Hub와 충돌) | **23457** |
| 도구 수 | 5개 | **12개** |
| 컴포넌트 속성 읽기 | 없음 | `get_component_property` |
| 컴포넌트 속성 쓰기 | 없음 | `set_component_property` |
| 메소드 호출 | 없음 | `call_component_method` |
| 이벤트 전송 | 없음 | `send_event` |
| 플레이 모드 제어 | 없음 | `control_playmode` |
| 씬 스냅샷 | 없음 | `query_scene` |
| 에셋 관리 | 없음 | `manage_asset` |
| 콘솔 필터 | 없음 | `read_console` + `filter` 파라미터 |
| CORS 헤더 | 없음 | 추가 (Multi-MCP GUI 연동) |
| Health 엔드포인트 | 없음 | `GET /health` |
| 메인 스레드 타임아웃 | 5초 | **10초** |
| Escape 함수 | 기본 | `\n`, `\r`, `\t` 처리 추가 |
