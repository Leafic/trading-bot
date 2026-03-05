# 📈 한국투자증권 트레이딩 알림 봇

한국투자증권 OpenAPI + Streamlit 기반 실시간 주식 알림 봇입니다.
자동매매는 없으며 **데이터 조회 + 기술적 지표 분석 + 텔레그램 알림 + AI 브리핑**을 수행합니다.

---

## 1. 프로젝트 개요

### 주요 기능

| 기능 | 설명 |
|------|------|
| 🏦 **계좌 자동 연동** | 잔고 조회 API로 보유 종목을 자동 감시 리스트에 추가 |
| 👁 **관심 종목 추가** | 사이드바 UI에서 코드 입력 → `watchlist.json` 저장 |
| 💰 **포트폴리오 대시보드** | 총 평가금액 / 매수금액 / 수익률 실시간 표시 |
| 📊 **기술적 지표** | RSI(14), SMA5/20, 볼린저밴드 하단, 거래량 SMA20 |
| 🚨 **7가지 알림 규칙** | 아래 표 참조 |
| 📱 **텔레그램 단방향 알림** | 조건 충족 시 즉시 모바일 알림 |
| 💬 **텔레그램 양방향 명령어** | `/잔고`, `/목록` 입력 시 봇이 즉시 답장 (보안: CHAT_ID 검증) |
| 🌙 **잔고 스냅샷** | 야간/휴장 시 마지막 장 마감 잔고를 `balance_snapshot.json`에서 복원 |
| 📊 **일일 AI 브리핑** | 매일 16:00 Claude AI가 보유 종목 매수/홀딩/매도 분석 → 텔레그램 발송 |
| 🔄 **자동 새로고침** | 10 / 30 / 60 / 120초 선택 |

### 알림 규칙 (중복 방지 플래그 내장)

| 규칙 | 조건 | 메시지 |
|------|------|--------|
| **A** | RSI(14) ≤ 30 | 🚨 매수경고 — RSI 과매도 진입 |
| **B** | 현재가 > 설정 목표가 | 💰 익절알림 — 목표가 돌파 |
| **C** | RSI ≤ 30 **AND** 현재가 ≤ BB하단 | 🎯 바닥포착 — 강력 매수 경고 |
| **D** | 거래량 ≥ 평균 300% **AND** 현재가 > SMA20 | 🚀 수급폭발 — 세력 개입 감지 |
| **E** | SMA5 하향 돌파 SMA20 (데드크로스) **AND** 거래량 증가 | ⚠️ 위험감지 — 데드크로스 |
| **F** | 목표가 돌파 후, 고점 대비 `trailing_stop_pct`% 이상 하락 | 🛡️ 트레일링 스탑 — 수익 보존 |
| **G** | 외국인·기관 동반 순매수, 합산 ≥ 5일 평균 거래량의 5% | 🦅 쌍끌이 매수 — 기관+외국인 공동 매집 |

### 동작 구조

```
streamlit run app.py
       │
       ├─ Streamlit UI (메인 스레드)
       │     ├─ 포트폴리오 대시보드
       │     ├─ 감시 종목 지표 테이블
       │     ├─ 알림 플래그 상태 (7가지 규칙)
       │     └─ 실행 로그
       │
       └─ Bot Thread (백그라운드 스레드, daemon=True)
             ├─ 잔고 조회 → 보유 종목 자동 추가 (장 여부 무관)
             ├─ watchlist.json 읽기 → 관심 종목 병합
             ├─ OHLCV + 기술적 지표 계산
             ├─ 7가지 알림 규칙 체크 + 텔레그램 발송
             ├─ 매일 16:00 Claude AI 브리핑 → 텔레그램 발송
             └─ status.json / balance_snapshot.json 저장
       │
       └─ Telegram Listener Thread (telegram_cmd.py, daemon=True)
             ├─ getUpdates 롱폴링 (30초 간격)
             ├─ CHAT_ID 검증 후 명령어 처리
             ├─ /잔고 → 보유 종목 수익률 요약 답장
             └─ /목록 → 감시 종목 리스트 답장
```

### 파일 구조

```
trading_bot/
├── app.py                  ← Streamlit 대시보드 + 봇 스레드 진입점
├── api_handler.py          ← 한국투자증권 API 통신 (mojito 래핑)
├── strategy.py             ← 기술적 지표 계산 + 7가지 알림 규칙
├── ai_analyst.py           ← Claude AI 일일 브리핑 (장 마감 후 16:00)
├── telegram_cmd.py         ← 텔레그램 양방향 명령어 리스너 (/잔고, /목록)
├── utils.py                ← 텔레그램 단방향 알림, 파일 I/O, 범용 헬퍼
├── .env                    ← 환경 변수 (⚠️ 공유 금지)
├── .env.example            ← 환경 변수 예시 (공유 가능)
├── .gitignore
├── requirements.txt
├── watchlist.json          ← 관심 종목 (앱 실행 후 자동 생성)
├── status.json             ← 런타임 상태 (봇 재시작 시 복구용)
└── balance_snapshot.json   ← 마지막 정상 잔고 스냅샷 (야간 복구용)
```

---

## 2. 사전 준비 사항

### 2-1. 한국투자증권 OpenAPI 키 발급

1. [KIS Developers 포털](https://apiportal.koreainvestment.com) 접속
2. 로그인 → **앱 등록** 클릭
3. 앱 이름 입력 후 등록 완료
4. 등록된 앱에서 **APP_KEY**와 **APP_SECRET** 확인
5. **계좌번호** 확인: 한국투자증권 앱 또는 HTS에서 확인

> ⚠️ 모의투자 계좌와 실계좌의 KEY가 다릅니다. `.env`의 `IS_MOCK=True/False`로 전환하세요.

### 2-2. 텔레그램 봇 토큰 및 Chat ID 발급

**① 봇 토큰 발급**
1. 텔레그램에서 `@BotFather` 검색 → 대화 시작
2. `/newbot` 명령어 입력
3. 봇 이름 → 봇 사용자명 순서로 입력 (사용자명은 `_bot`으로 끝나야 함)
4. BotFather가 **HTTP API 토큰** 전달 → `TELEGRAM_BOT_TOKEN`에 입력

**② Chat ID 확인**
1. 발급받은 봇과 텔레그램에서 대화를 시작 (`/start` 전송)
2. 브라우저에서 아래 URL 접속 (토큰 교체 필요):
   ```
   https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates
   ```
3. JSON 응답에서 `"chat":{"id": 숫자}` 부분을 복사 → `TELEGRAM_CHAT_ID`에 입력

### 2-3. Anthropic API 키 발급 (일일 AI 브리핑용)

1. [Anthropic Console](https://console.anthropic.com) 접속 → 회원가입/로그인
2. **API Keys** 메뉴 → **Create Key**
3. 발급된 키를 `.env`의 `ANTHROPIC_API_KEY`에 입력

> 💡 AI 브리핑을 사용하지 않으려면 `ANTHROPIC_API_KEY`를 빈 값으로 두세요. 브리핑만 건너뛰고 나머지는 정상 동작합니다.

---

## 3. 설치 방법

### 3-1. 프로젝트 클론 / 다운로드

```bash
git clone https://github.com/your-id/trading_bot.git
cd trading_bot
```

### 3-2. Python 가상환경 생성 (권장)

**Windows (PowerShell / CMD)**
```powershell
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3-3. 의존성 설치

```bash
pip install -r requirements.txt
```

**requirements.txt 내용:**
```
mojito2
ta
requests
pandas
python-dotenv
streamlit
psutil
anthropic
```

### 3-4. 환경 변수 설정

**Windows**
```powershell
copy .env.example .env
```

**macOS / Linux**
```bash
cp .env.example .env
```

`.env` 파일을 열어 아래 항목을 입력합니다:

```env
# ─── 한국투자증권 실계좌 ───
APP_KEY=PS1xxxxxxxxxxxxxxxxx
APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ACC_NO=50123456

# ─── 텔레그램 봇 ───
TELEGRAM_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789

# ─── 계좌 모드 (True=모의투자, False=실계좌) ───
IS_MOCK=False

# ─── Anthropic Claude API (일일 AI 브리핑용) ───
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ `.env` 파일은 절대 Git에 커밋하지 마세요. `.gitignore`에 이미 등록되어 있습니다.

---

## 4. 실행 방법

### 4-1. Streamlit 대시보드 실행 (권장)

**Windows / macOS / Linux 공통**
```bash
streamlit run app.py
```

브라우저가 자동으로 열리거나, 터미널에 표시된 URL(`http://localhost:8501`)로 접속합니다.

**화면 구성:**
```
[▶ 봇 시작]  [⏹ 봇 정지]

💰 내 계좌 요약
  총 매수금액 | 총 평가금액 | 총 수익률
  [보유 종목 상세 테이블]

📊 감시 종목 현황
  [종목 카드: 현재가, RSI, SMA20, 거래량]
  [지표 비교 테이블]

🚨 알림 상태 (종목별 7가지 규칙)

📋 실행 로그
```

### 4-2. 관심 종목 추가

1. Streamlit 앱 실행
2. 좌측 사이드바 → **"관심 종목 추가"** 폼
3. 종목코드 입력 (예: `005930`), 선택적으로 종목명 / 익절 목표가 입력
4. `+ 추가` 버튼 클릭 → `watchlist.json`에 저장

### 4-3. 트레일링 스탑 설정 (Rule F)

`watchlist.json`에서 직접 `trailing_stop_pct` 값을 설정합니다:

```json
{
  "402490": {
    "name": "그린리소스",
    "target_price": 13000,
    "trailing_stop_pct": 3.0
  }
}
```

- `trailing_stop_pct` 미설정 시 기본값 **3.0%** 적용
- `target_price`를 넘은 이후부터만 트레일링 스탑 추적 시작

---

## 5. macOS 작업 환경 세팅 (맥북 이어서 작업 시)

### 5-1. 처음 클론하는 경우

```bash
# 1. Homebrew 설치 (없는 경우)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Python 3.11+ 설치 (없는 경우)
brew install python@3.11

# 3. 프로젝트 클론
git clone https://github.com/your-id/trading_bot.git
cd trading_bot

# 4. 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 5. 의존성 설치
pip install -r requirements.txt

# 6. 환경 변수 파일 생성
cp .env.example .env
# .env 파일을 텍스트 에디터로 열어 값 입력
open -e .env        # TextEdit으로 열기
# 또는
code .env           # VS Code로 열기 (설치된 경우)
```

### 5-2. 이미 클론된 프로젝트를 이어받는 경우

```bash
cd trading_bot

# 가상환경 활성화 (매번 필요)
source .venv/bin/activate

# 최신 코드 pull (협업 중인 경우)
git pull

# 의존성 추가 여부 확인 후 설치
pip install -r requirements.txt

# 실행
streamlit run app.py
```

### 5-3. macOS 주의사항

| 항목 | Windows | macOS |
|------|---------|-------|
| 가상환경 활성화 | `.venv\Scripts\activate` | `source .venv/bin/activate` |
| 환경변수 복사 | `copy .env.example .env` | `cp .env.example .env` |
| Python 명령어 | `python` | `python3` |
| pip 명령어 | `pip` | `pip3` (또는 venv 활성화 후 `pip`) |
| 경로 구분자 | `\` | `/` |

> 💡 가상환경 활성화 상태에서는 `python`, `pip` 모두 venv 내부 버전을 사용합니다.

### 5-4. Python 버전 호환성

이 프로젝트는 **Python 3.9 이상**을 지원합니다.

> ⚠️ macOS의 가상환경은 기본적으로 Python 3.9로 생성될 수 있습니다.
> `X | Y` 유니온 타입 문법은 Python 3.10+에서만 지원되므로, 코드 기여 시 반드시 `Optional[X]` 형태를 사용하세요.

```python
# ❌ Python 3.10+에서만 동작 (macOS 3.9 환경에서 에러 발생)
def foo(x: int | None) -> dict | None: ...

# ✅ Python 3.9 이상 호환
from typing import Optional
def foo(x: Optional[int]) -> Optional[dict]: ...
```

Python 버전 확인:
```bash
python --version   # Windows (venv 활성화 후)
python3 --version  # macOS
```

---

## 6. 자주 묻는 질문 (FAQ)

**Q. 장 마감 후에도 봇이 계속 실행되나요?**
A. 평일 09:00 ~ 15:30 범위 밖에서는 10분 대기, 주말은 1시간 대기 후 재확인합니다.
단, 잔고 조회는 장 여부와 관계없이 매 루프마다 수행됩니다.

**Q. 야간에 잔고가 빈 화면으로 보이지 않나요?**
A. 장 중 마지막으로 성공한 잔고를 `balance_snapshot.json`에 저장합니다.
야간/휴장 시 API 오류가 발생하면 이 스냅샷을 자동으로 불러오며,
UI 하단에 "🌙 야간/휴장 시간: 마지막 장 마감 기준 잔고 스냅샷" 문구가 표시됩니다.

**Q. 텔레그램에서 명령어를 보내면 어떻게 되나요?**
A. `telegram_cmd.py` 리스너가 봇 시작과 동시에 실행됩니다.
`/잔고` — 현재 보유 종목 평가금액·수익률 요약 답장
`/목록` — 감시 중인 종목 리스트 답장
등록된 `TELEGRAM_CHAT_ID` 발신자의 명령만 처리하여 보안을 유지합니다.

**Q. 같은 알림이 계속 반복 전송되지 않나요?**
A. 각 규칙마다 중복 방지 플래그가 있습니다. 조건이 해소되기 전까지 재발송하지 않습니다.
플래그 상태는 `status.json`에 저장되어 봇을 껐다 켜도 유지됩니다.

**Q. AI 브리핑은 언제 발송되나요?**
A. 매 평일 16:00~17:00 사이, 봇이 장 휴장 구간을 통과할 때 1회만 발송됩니다.
`ANTHROPIC_API_KEY`가 없으면 브리핑만 건너뛰고 나머지 기능은 정상 동작합니다.

**Q. 토큰 만료 에러가 발생하면?**
A. HTTP 401 감지 시 자동으로 재로그인(`broker` 재생성)합니다. 별도 조치 불필요.

**Q. API Rate Limit 에러가 발생하면?**
A. HTTP 429 감지 시 60초 대기 후 최대 3회 재시도합니다.

**Q. 공휴일 처리가 되나요?**
A. 현재 버전은 주말만 처리합니다. 공휴일 처리를 추가하려면
`finterstellar` 라이브러리 또는 한국천문연구원 특일 API를 연동하세요.

---

## 7. 주의사항

- 이 프로젝트는 **알림 전용**이며 자동매매를 수행하지 않습니다.
- API 키, 시크릿, 텔레그램 토큰, Anthropic API 키는 **절대 공개 저장소에 업로드하지 마세요**.
- 기술적 지표 알림 및 AI 분석은 투자 참고용이며, **투자 결과에 대한 책임은 사용자 본인**에게 있습니다.
