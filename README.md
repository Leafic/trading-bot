# 📈 한국투자증권 트레이딩 알림 봇

한국투자증권 OpenAPI + Streamlit 기반 실시간 주식 알림 봇입니다.
자동매매는 없으며 **데이터 조회 + 기술적 지표 분석 + 텔레그램 알림**만 수행합니다.

---

## 1. 프로젝트 개요

### 주요 기능

| 기능 | 설명 |
|------|------|
| 🏦 **계좌 자동 연동** | 잔고 조회 API로 보유 종목을 자동 감시 리스트에 추가 |
| 👁 **관심 종목 추가** | 사이드바 UI에서 코드 입력 → `watchlist.json` 저장 |
| 💰 **포트폴리오 대시보드** | 총 평가금액 / 매수금액 / 수익률 실시간 표시 |
| 📊 **기술적 지표** | RSI(14), SMA5/20, 볼린저밴드 하단, 거래량 SMA20 |
| 🚨 **5가지 알림 규칙** | 아래 표 참조 |
| 📱 **텔레그램 알림** | 조건 충족 시 즉시 모바일 알림 |
| 🔄 **자동 새로고침** | 10 / 30 / 60 / 120초 선택 |

### 알림 규칙 (중복 방지 플래그 내장)

| 규칙 | 조건 | 메시지 |
|------|------|--------|
| **A** | RSI(14) ≤ 30 | 🚨 매수경고 — RSI 과매도 진입 |
| **B** | 현재가 > 설정 목표가 | 💰 익절알림 — 목표가 돌파 |
| **C** | RSI ≤ 30 **AND** 현재가 ≤ BB하단 | 🎯 바닥포착 — 강력 매수 경고 |
| **D** | 거래량 ≥ 평균 300% **AND** 현재가 > SMA20 | 🚀 수급폭발 — 세력 개입 감지 |
| **E** | SMA5 하향 돌파 SMA20 (데드크로스) **AND** 거래량 증가 | ⚠️ 위험감지 — 데드크로스 |

### 동작 구조

```
streamlit run app.py
       │
       ├─ Streamlit UI (메인 스레드)
       │     ├─ 포트폴리오 대시보드
       │     ├─ 감시 종목 지표 테이블
       │     ├─ 알림 플래그 상태
       │     └─ 실행 로그
       │
       └─ Bot Thread (백그라운드 스레드, daemon=True)
             ├─ 잔고 조회 → 보유 종목 자동 추가
             ├─ watchlist.json 읽기 → 관심 종목 병합
             ├─ OHLCV + 기술적 지표 계산
             ├─ 5가지 알림 규칙 체크
             └─ 텔레그램 발송 + status.json 저장
```

---

## 2. 사전 준비 사항

### 2-1. 한국투자증권 OpenAPI 키 발급

1. [KIS Developers 포털](https://apiportal.koreainvestment.com) 접속
2. 로그인 → **앱 등록** 클릭
3. 앱 이름 입력 후 등록 완료
4. 등록된 앱에서 **APP_KEY**와 **APP_SECRET** 확인
5. **계좌번호** 확인: 한국투자증권 앱 또는 HTS에서 확인 (형식: `50123456`)

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

---

## 3. 설치 방법

### 3-1. 프로젝트 클론 / 다운로드

```bash
# Git 사용 시
git clone https://github.com/your-id/trading_bot.git
cd trading_bot

# 또는 ZIP 다운로드 후 압축 해제
```

### 3-2. Python 가상환경 생성 (권장)

```bash
# Windows (PowerShell / CMD)
python -m venv .venv
.venv\Scripts\activate
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
```

### 3-4. 환경 변수 설정

프로젝트 루트에 `.env` 파일을 생성합니다. (`.env.example` 참고)

```bash
# Windows
copy .env.example .env
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
```

> ⚠️ `.env` 파일은 절대 Git에 커밋하지 마세요. `.gitignore`에 이미 등록되어 있습니다.

---

## 4. 실행 방법

### 4-1. Streamlit 대시보드 실행 (권장)

```bash
# 가상환경 활성화 후
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

🚨 알림 상태 (종목별 5가지 규칙)

📋 실행 로그
```

### 4-2. 봇 단독 실행 (대시보드 없이)

```bash
python bot.py
```

터미널에 로그가 출력되며 텔레그램으로 알림이 발송됩니다.

### 4-3. 관심 종목 추가

1. Streamlit 앱 실행
2. 좌측 사이드바 → **"관심 종목 추가"** 폼
3. 종목코드 입력 (예: `005930`), 선택적으로 종목명 / 익절 목표가 입력
4. `+ 추가` 버튼 클릭 → `watchlist.json`에 저장

---

## 5. 파일 구조

```
trading_bot/
├── app.py              ← All-in-One Streamlit 앱 (주 실행 파일)
├── bot.py              ← 봇 단독 실행용 (터미널 전용)
├── .env                ← 환경 변수 (⚠️ 공유 금지)
├── .env.example        ← 환경 변수 예시 (공유 가능)
├── .gitignore
├── requirements.txt
├── watchlist.json      ← 관심 종목 (앱 실행 후 자동 생성)
└── status.json         ← 런타임 상태 (봇 재시작 시 복구용)
```

---

## 6. 자주 묻는 질문 (FAQ)

**Q. 장 마감 후에도 봇이 계속 실행되나요?**
A. 평일 09:00 ~ 15:30 범위 밖에서는 1시간 대기 후 재확인합니다. 불필요한 API 호출이 없습니다.

**Q. 같은 알림이 계속 반복 전송되지 않나요?**
A. 각 규칙마다 중복 방지 플래그가 있습니다. 조건이 해소되기 전까지 재발송하지 않습니다.
플래그 상태는 `status.json`에 저장되어 봇을 껐다 켜도 유지됩니다.

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
- API 키, 시크릿, 텔레그램 토큰은 **절대 공개 저장소에 업로드하지 마세요**.
- 기술적 지표 알림은 투자 참고용이며, **투자 결과에 대한 책임은 사용자 본인**에게 있습니다.
