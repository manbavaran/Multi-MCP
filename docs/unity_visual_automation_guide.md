# AI 시각 조작 루프 가이드 (Unity MCP Bridge v2.2)

Unity MCP Bridge v2.2는 AI가 Unity 화면을 직접 보고, UI 요소를 인지하며, 마우스/키보드 입력을 시뮬레이션할 수 있는 5가지 핵심 도구를 제공합니다. 이를 통해 AI는 다음과 같은 **관찰-판단-행동(Observe-Decide-Act)** 루프를 수행할 수 있습니다.

```mermaid
graph TD
    A[1. 관찰 (Observe)] --> B(2. 판단 (Decide));
    B --> C{3. 행동 (Act)};
    C --> A;

    subgraph 관찰
        A1["unity.capture_screenshot"];
        A2["unity.find_ui_elements"];
    end

    subgraph 판단
        B1["LLM / Vision Model
(e.g., GPT-4o)"];
    end

    subgraph 행동
        C1["unity.simulate_input"];
        C2["unity.call_component_method"];
    end
```

## 1. 관찰 (Observe): AI의 눈

AI는 먼저 현재 게임 화면이 어떻게 생겼는지, 어떤 UI가 있는지 파악해야 합니다.

| 도구 | 역할 | 사용 예시 |
|---|---|---|
| `unity.capture_screenshot` | 현재 게임 화면을 base64 PNG 이미지로 캡처합니다. | AI가 이 이미지를 보고 "화면 중앙에 '시작' 버튼이 있다"고 판단합니다. |
| `unity.find_ui_elements` | 화면에 있는 모든 UI 요소(버튼, 텍스트 등)의 이름, 위치, 크기, 텍스트를 반환합니다. | AI가 "'시작' 버튼의 화면 좌표는 (400, 300)이다"라고 정확히 인지합니다. |
| `unity.ui_raycast` | 특정 화면 좌표에 어떤 UI 또는 3D 오브젝트가 있는지 확인합니다. | AI가 "(400, 300) 좌표에는 'StartButton'이라는 UI 요소가 있다"고 확신합니다. |

## 2. 판단 (Decide): AI의 뇌

관찰 단계에서 얻은 시각 정보(스크린샷)와 UI 요소 목록을 LLM(e.g., GPT-4o)에 전달합니다. LLM은 이 정보를 바탕으로 다음에 수행할 행동을 결정합니다.

**예시 프롬프트:**

```
너는 게임을 플레이하는 AI 에이전트다. 현재 게임 화면은 아래와 같고, 화면에는 다음과 같은 UI 요소들이 있다.

[스크린샷 이미지]

UI 요소 목록:
- name: StartButton, type: Button, text: "시작", screen_x: 400, screen_y: 300
- name: TitleText, type: Text, text: "My Awesome Game"

목표: 게임을 시작하라.

다음에 호출할 도구와 파라미터를 JSON 형식으로 응답하라.
```

**LLM 응답:**

```json
{
  "tool_name": "unity.simulate_input",
  "parameters": {
    "action": "click",
    "x": 400,
    "y": 300
  }
}
```

## 3. 행동 (Act): AI의 손

LLM이 결정한 행동을 MCP 도구를 통해 실제로 실행합니다.

| 도구 | 역할 | 사용 예시 |
|---|---|---|
| `unity.simulate_input` | 마우스 클릭, 이동, 드래그, 키보드 입력을 시뮬레이션합니다. | AI가 `simulate_input(action="click", x=400, y=300)`을 호출하여 '시작' 버튼을 클릭합니다. |
| `unity.call_component_method` | 게임 로직을 직접 호출합니다. | 시각적 조작이 어려운 경우, `call_component_method(path="GameManager", method="StartGame")`를 호출할 수도 있습니다. |

## 전체 루프 예시

1.  **[관찰]** `unity.capture_screenshot` 호출 → 게임 화면 이미지 획득
2.  **[관찰]** `unity.find_ui_elements` 호출 → `[{"name": "StartButton", ...}]` 정보 획득
3.  **[판단]** LLM에 이미지와 UI 목록 전달 → "StartButton을 클릭하라"는 결정 받음
4.  **[행동]** `unity.simulate_input(action="click", x=..., y=...)` 호출
5.  **[관찰]** 다시 `unity.capture_screenshot` 호출 → 게임이 시작된 화면 확인
6.  루프 반복...
