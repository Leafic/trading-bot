# 프로젝트 요약
한국투자증권 API 기반 Python Streamlit 주식 자동 알림 봇 (팀 프로젝트)

# 🚨 협업 룰 (Claude 행동 지침 - 엄격하게 지킬 것)
- 코드를 제안할 때는 전체 코드를 다시 쓰지 말고, [수정되는 함수나 블록]만 제공할 것.
- 새로운 라이브러리(패키지) 추가는 팀원과 사전 협의가 필요하므로, 함부로 `pip install`을 추천하지 말고 내장 라이브러리를 우선 고려할 것.
- 반드시 Type Hint (예: `def get_balance(broker) -> dict:`)를 추가하여 함수 입력/출력을 명확히 할 것.
- 주석과 Docstring은 "이 코드가 왜(Why) 필요한지"를 한국어로 간결하게 작성할 것.
- 작업 내용은 항상 README.md파일에 업데이트 될 것

# 📂 파일 구조 및 역할 분담 (여기에 맞춰 코딩할 것)
- `app.py`: Streamlit 기반 UI 및 메인 루틴 (UI 관련 수정만 여기서 진행)
- `api_handler.py`: 한국투자증권 API 통신 (mojito 래핑, 잔고 조회, 예외 처리 등)
- `strategy.py`: 트레이딩 지표 계산(RSI, MA 등) 및 매수/매도 로직
- `utils.py`: 텔레그램 알림, 로그 기록, JSON 파일 I/O 등 범용 기능
- `requirements.txt`: 프로젝트 종속성 목록 (추가 시 반드시 업데이트)
- `.env`: 개인 보안 키 (절대 Git에 커밋하지 않음)

# 코딩 규칙 (명령어)
- 실행: `streamlit run app.py`
- 의존성 설치: `pip install -r requirements.txt`

# ⚠️ Python 버전 호환성 (필수)
- 이 프로젝트는 **Python 3.9 이상**을 지원해야 한다 (macOS 기본 venv가 3.9일 수 있음).
- **절대 사용 금지**: `X | Y` 유니온 타입 문법 (Python 3.10+에서만 동작)
  - ❌ `def foo(x: int | None)` → ✅ `def foo(x: Optional[int])`
  - ❌ `-> dict | None` → ✅ `-> Optional[dict]`
- 타입 힌트에 `Optional`, `Union`, `List`, `Dict` 등을 사용할 때는 반드시 `from typing import ...`로 임포트할 것.

# 🛠 대규모 작업 전 필수 단계
- 코드를 대량으로 수정하거나 파일 구조를 변경하기 전, 반드시 "Plan(계획)을 먼저 텍스트로 작성해서 보여달라"고 요청받은 것으로 간주하고 계획부터 출력할 것.
