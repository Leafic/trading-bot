# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# 프로젝트 개요
한국투자증권(KIS) OpenAPI + Streamlit 기반 주식 알림 봇. 자동매매 없음 — 데이터 조회 + 지표 분석 + 텔레그램 알림 + AI 브리핑.

---

# 🚨 협업 룰 (엄격히 준수)

1. **코드 출력 최소화:** 수정되는 함수/블록만 제공. 전체 파일 재출력 금지.
2. **라이브러리 제한:** 신규 패키지 추가 시 팀원 사전 협의. 내장 또는 기존 패키지(`pandas`, `ta`, `mojito2`, `plotly` 등) 우선.
3. **타입 힌트 필수:** 모든 함수에 Type Hint 작성.
4. **주석 원칙:** Docstring은 Why(이유)를 한국어로 작성. How는 코드로 증명.
5. **대규모 변경 전 계획 먼저:** 파일 구조 변경이나 대량 수정 전에는 반드시 마크다운으로 계획을 먼저 출력할 것.

---

# ⚠️ Python 3.9 호환성 필수

- `X | Y` 유니온 타입 **절대 금지** (Python 3.10+)
- ❌ `def foo(x: int | None) -> dict | None`
- ✅ `def foo(x: Optional[int]) -> Optional[dict]` + `from typing import Optional`

---

# 📂 파일 구조 및 역할

```
app.py              ← Streamlit UI + bot_loop 스레드 진입점
api_handler.py      ← KIS API 통신 (mojito 래핑, 싱글톤 broker)
strategy.py         ← 기술적 지표 계산 + 9가지 알림 규칙 (check_and_alert)
utils.py            ← 텔레그램, 파일 I/O, 에러 헬퍼 (최하단 레이어)
telegram_cmd.py     ← 텔레그램 양방향 명령어 리스너 (/잔고, /목록, /분석)
ai_analyst.py       ← Claude AI 일일 브리핑 (평일 16:00)
bot.py              ← 독립 실행 봇 (app.py 없이 백그라운드 전용)
pages/1_백테스트.py  ← 알림 규칙 백테스트 (Plotly 차트, 적중률 분석)
pages/2_스캘핑.py   ← 스캘핑 전략 백테스트 (P&L 시뮬레이션, 에쿼티 커브)
```

**모듈 의존성 (import 방향, 순환 금지):**
```
utils.py ← (외부 의존 없음)
api_handler.py ← utils
strategy.py ← api_handler, utils
app.py ← utils, api_handler, strategy
pages/*.py ← api_handler (get_broker, get_ohlcv_dataframe)
```

---

# 🔑 KIS API / Broker 핵심 패턴

## Broker 싱글톤 (필수)
KIS 토큰은 계정당 1개만 유효(24시간). `create_broker()`를 여러 곳에서 독립 호출하면 토큰이 서로 무효화됨.

```python
# ✅ 올바른 사용 — 모든 페이지/모듈에서 동일 인스턴스 공유
from api_handler import get_broker
broker = get_broker()

# ❌ 금지 — 새 토큰 발급으로 기존 토큰 무효화
broker = create_broker()
```

토큰 만료 시에는 `refresh_broker()`를 호출해 싱글톤을 교체:
```python
from api_handler import refresh_broker
broker = refresh_broker()  # 전역 싱글톤 교체 → 이후 get_broker() 전역 적용
```

## broker.access_token 형식 주의
mojito2는 `access_token`을 **이미 `"Bearer ..."` 형식으로 저장**함.
직접 헤더를 구성할 때 `f"Bearer {broker.access_token}"` 이중 접두어 금지.

```python
# ✅ 올바름
"authorization": broker.access_token

# ❌ 금지 — "Bearer Bearer eyJ..." 가 되어 토큰 무효
"authorization": f"Bearer {broker.access_token}"
```

## OHLCV 날짜 범위 제한
`broker.fetch_ohlcv()`는 날짜 범위 파라미터 없이 ~80건 고정 반환.
날짜 지정이 필요하면 `_fetch_ohlcv_direct(broker, symbol, since, market_code)` 사용.
KOSPI(J) 실패 시 자동으로 KOSDAQ(Q) 재시도. `days=120` 기본값.

---

# 🏗 앱 아키텍처

## 스레드 구조
```
Streamlit 메인 스레드 (UI)
├── bot_loop() — daemon thread, _bot_state["shared"]에 결과 저장
└── telegram_cmd 리스너 — daemon thread, getUpdates 롱폴링
```

## 상태 영속화
- `status.json` — alert_flags, 당일 P&L 기준가(`open_amt`), 로그 (봇 재시작 시 복구)
- `balance_snapshot.json` — 마지막 장 마감 잔고 (야간/휴장 시 복구)
- `watchlist.json` — 관심 종목 (`{symbol: {name, target_price, stop_loss_price, trailing_stop_pct}}`)

## Streamlit 세션 상태
`@st.cache_resource`로 `_bot_state` 영속화 → Streamlit 리런 간에도 스레드/락/이벤트 유지.
UI는 `_bot_state["shared"]`에서 Lock으로 스냅샷 읽기.

## 위젯 키 리셋 패턴
폼 제출 후 입력 필드 초기화가 필요하면 form version counter 사용:
```python
st.session_state["add_form_v"] = st.session_state.get("add_form_v", 0) + 1
st.rerun()
# 위젯 key를 f"field_{st.session_state['add_form_v']}"로 설정
```

---

# 📊 알림 규칙 (strategy.py)

| 규칙 | 조건 |
|------|------|
| A | RSI ≤ 30 (과매도) |
| B | 현재가 > target_price |
| C | RSI ≤ 30 AND 현재가 ≤ BB하단 |
| D | 거래량 ≥ 평균 300% AND 현재가 > SMA20 |
| E | SMA5 하향 돌파 SMA20 + 거래량 증가 (데드크로스) |
| F | 목표가 초과 후 고점 대비 trailing_stop_pct% 하락 |
| G | 외국인+기관 순매수 합산 ≥ 5일 평균 거래량의 5% |
| H | SMA5 상향 돌파 SMA20 + 거래량 증가 (골든크로스) |
| SL | 현재가 ≤ stop_loss_price |

`check_and_alert()`는 `(stocks_data: dict, log_lines: list)` 튜플 반환.
alert_flags는 `status.json`에 영속 저장 — 조건 해소 전까지 재발송 없음.

---

# 🛠 실행 명령어

```bash
# 대시보드 실행
streamlit run app.py

# 의존성 설치
pip install -r requirements.txt
```

## 환경 변수 (.env)
```
APP_KEY, APP_SECRET, ACC_NO       # KIS API 키
IS_MOCK=False                     # True=모의투자, False=실계좌
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
ANTHROPIC_API_KEY                 # AI 브리핑용 (없으면 브리핑만 스킵)
```

---

# 🛡 봇 설계 제약

1. **1종목 1포지션:** 무한 매수 루프 방지 — 최대 매수 한도 변수로 엄격히 체크.
2. **장 시간:** 평일 09:00~15:30. 장 휴장 시 평일 10분, 주말 1시간 대기.
3. **Rate Limit(429):** 60초 대기 후 최대 3회 재시도.
4. **토큰 만료:** `refresh_broker()` 호출 후 재시도. `_is_token_error(e)` / `_is_rate_limit(e)` 헬퍼는 `utils.py`에 있음.
