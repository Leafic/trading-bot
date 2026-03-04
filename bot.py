# ==============================================================
#  한국투자증권 트레이딩 알림 봇
#  - 환경 변수는 .env 파일에서 로드 (.env.example 참조)
#  - 지표: RSI, SMA5/20, 볼린저밴드 하단, 거래량 SMA20
#  - 알림 규칙 5가지 (기존 2 + 신규 3)
# ==============================================================

import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import mojito
from ta.momentum   import RSIIndicator
from ta.trend      import SMAIndicator
from ta.volatility import BollingerBands
from dotenv        import load_dotenv

# ================================================================
# 환경 변수 로드
# ================================================================
load_dotenv()

APP_KEY            = os.getenv("APP_KEY", "")
APP_SECRET         = os.getenv("APP_SECRET", "")
ACC_NO             = os.getenv("ACC_NO", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
IS_MOCK            = os.getenv("IS_MOCK", "True").strip().lower() == "true"

# ================================================================
# 설정
# ================================================================
STATUS_FILE = Path("status.json")

STOCKS = {
    "314130": "지놈앤컴퍼니",
    "402490": "그린리소스",
}

RSI_PERIOD             = 14
RSI_OVERSOLD_THRESHOLD = 30
GREEN_RESOURCE_TARGET  = 13_000
LOOP_INTERVAL          = 180   # 루프 간격 (초)

# ================================================================
# 중복 알림 방지 플래그 기본값
# ================================================================
DEFAULT_FLAGS = {
    # 기존
    "314130_rsi_oversold":  False,
    "402490_rsi_oversold":  False,
    "402490_price_target":  False,
    # Rule C: 스나이퍼 바닥
    "314130_sniper_bottom": False,
    "402490_sniper_bottom": False,
    # Rule D: 수급 폭발
    "314130_volume_surge":  False,
    "402490_volume_surge":  False,
    # Rule E: 데드크로스
    "314130_dead_cross":    False,
    "402490_dead_cross":    False,
}

# ================================================================
# 상태 저장/복구 (status.json)
# ================================================================
def load_status() -> dict:
    """봇 시작 시 이전 alert_flags 상태를 복구합니다."""
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                return json.load(f).get("alert_flags", DEFAULT_FLAGS.copy())
        except Exception:
            pass
    return DEFAULT_FLAGS.copy()


def save_status(alert_flags: dict, stocks_data: dict = None,
                logs: list = None, balance: dict = None):
    """현재 상태 전체를 status.json에 저장합니다."""
    data = {}
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    data["alert_flags"] = alert_flags
    data["last_check"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if stocks_data is not None:
        data["stocks"] = stocks_data
    if logs is not None:
        data["logs"] = logs[-100:]
    if balance is not None:
        data["balance"] = balance

    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_bot_running(running: bool):
    """status.json에 봇 실행 여부와 PID를 기록합니다."""
    data = {}
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["bot_running"] = running
    data["bot_pid"]     = os.getpid() if running else None
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================================================================
# 장 운영 시간 체크
# ================================================================
def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 0) <= t <= dtime(15, 30)


def market_closed_reason() -> str:
    now = datetime.now()
    if now.weekday() >= 5:
        return "주말"
    if now.time() < dtime(9, 0):
        return f"장 시작 전 ({now.strftime('%H:%M')})"
    return f"장 마감 후 ({now.strftime('%H:%M')})"


# ================================================================
# 브로커 초기화
# ================================================================
def create_broker() -> mojito.KoreaInvestment:
    return mojito.KoreaInvestment(
        api_key=APP_KEY,
        api_secret=APP_SECRET,
        acc_no=ACC_NO,
        mock=IS_MOCK,
    )


# ================================================================
# 텔레그램 메시지 전송
# ================================================================
def send_telegram(message: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


# ================================================================
# 에러 판별 유틸
# ================================================================
def _is_token_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("401", "token", "expire", "unauthorized"))


def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "too many" in s


def safe_float(val) -> float | None:
    """NaN/None/빈값을 안전하게 float으로 변환합니다."""
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


# ================================================================
# 일봉 OHLCV 조회
# ================================================================
def get_ohlcv_dataframe(broker, symbol: str, days: int = 60) -> pd.DataFrame:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    for attempt in range(3):
        try:
            resp = broker.fetch_ohlcv(symbol=symbol, timeframe="D", since=since)
            break
        except Exception as e:
            if _is_token_error(e):
                raise
            if _is_rate_limit(e):
                print(f"  ⚠️  [{symbol}] Rate Limit — 60초 대기 ({attempt+1}/3)")
                time.sleep(60)
                continue
            print(f"  ❌ [{symbol}] fetch_ohlcv 오류: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

    if not resp or "output2" not in resp or not resp["output2"]:
        return pd.DataFrame()

    df = pd.DataFrame(resp["output2"])
    df.rename(columns={
        "stck_bsop_date": "date",  "stck_oprc": "open",
        "stck_hgpr":      "high",  "stck_lwpr": "low",
        "stck_clpr":      "close", "acml_vol":  "volume",
    }, inplace=True)

    available = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[available].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "date" in df.columns:
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)

    return df


# ================================================================
# 기술적 지표 계산 (SMA5/20, 거래량SMA20, 볼린저밴드 하단)
# ================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame에 기술적 지표 컬럼을 추가합니다."""
    if len(df) < 20:
        return df

    df = df.copy()
    df["sma5"]      = SMAIndicator(close=df["close"], window=5).sma_indicator()
    df["sma20"]     = SMAIndicator(close=df["close"], window=20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(window=20).mean()

    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_lower"]  = bb.bollinger_lband()

    return df


# ================================================================
# RSI 계산
# ================================================================
def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> float | None:
    if df.empty or "close" not in df.columns or len(df) < period + 1:
        return None
    latest = RSIIndicator(close=df["close"], window=period).rsi().iloc[-1]
    return None if pd.isna(latest) else round(float(latest), 2)


# ================================================================
# 현재가 조회
# ================================================================
def get_current_price(broker, symbol: str) -> int | None:
    for attempt in range(3):
        try:
            return int(broker.fetch_price(symbol)["output"]["stck_prpr"])
        except Exception as e:
            if _is_token_error(e):
                raise
            if _is_rate_limit(e):
                print(f"  ⚠️  [{symbol}] Rate Limit — 60초 대기 ({attempt+1}/3)")
                time.sleep(60)
                continue
            print(f"  ❌ [{symbol}] 현재가 오류: {e}")
            return None
    return None


# ================================================================
# 잔고 조회
# ================================================================
def get_balance(broker) -> dict | None:
    """계좌 잔고를 조회하고 정제된 dict를 반환합니다."""
    try:
        resp = broker.fetch_balance()
    except Exception as e:
        print(f"  ❌ 잔고 조회 오류: {e}")
        return None

    out1    = resp.get("output1") or [{}]
    summary = out1[0] if out1 else {}
    out2    = resp.get("output2") or []

    holdings = []
    for item in out2:
        qty = int(item.get("hldg_qty", 0) or 0)
        if qty <= 0:
            continue
        holdings.append({
            "name":          item.get("prdt_name", ""),
            "symbol":        item.get("pdno", ""),
            "qty":           qty,
            "avg_price":     float(item.get("pchs_avg_pric", 0) or 0),
            "current_price": int(item.get("prpr", 0) or 0),
            "eval_amt":      int(item.get("evlu_amt", 0) or 0),
            "profit_amt":    int(item.get("evlu_pfls_amt", 0) or 0),
            "profit_rate":   float(item.get("evlu_erng_rt", 0) or 0),
        })

    return {
        "tot_evlu_amt": int(summary.get("tot_evlu_amt", 0) or 0),
        "pchs_amt":     int(summary.get("pchs_amt_smtl_amt", 0) or 0),
        "profit_amt":   int(summary.get("evlu_pfls_smtl_amt", 0) or 0),
        "profit_rate":  float(summary.get("asst_icdc_erng_rt", 0) or 0),
        "holdings":     holdings,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ================================================================
# 핵심 로직: 지표 체크 + 알림 5가지 룰
# ================================================================
def check_and_alert(broker, alert_flags: dict, logs: list,
                    balance: dict = None) -> dict:
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stocks_data = {}

    def log(msg: str):
        print(msg)
        logs.append(f"[{now_str}] {msg}")

    log(f"{'='*55}")
    log(f"  체크 시각: {now_str}")
    log(f"{'='*55}")

    for symbol, name in STOCKS.items():
        log(f"\n[{name} ({symbol})]")

        # 1. OHLCV 조회 + 지표 계산
        df = get_ohlcv_dataframe(broker, symbol, days=60)
        if df.empty:
            log("  → 데이터 조회 실패, 이번 사이클 스킵")
            time.sleep(1)
            continue

        df = calculate_indicators(df)

        # 2. 현재가 + RSI
        current_price = get_current_price(broker, symbol)
        if current_price is None:
            log("  → 현재가 조회 실패, 이번 사이클 스킵")
            time.sleep(1)
            continue

        rsi_value = calculate_rsi(df)
        if rsi_value is None:
            log("  → RSI 계산 실패, 이번 사이클 스킵")
            time.sleep(1)
            continue

        # 3. 최신 / 직전 행 지표 값 추출
        sma5_curr  = safe_float(df["sma5"].iloc[-1])  if "sma5"      in df.columns else None
        sma20_curr = safe_float(df["sma20"].iloc[-1]) if "sma20"     in df.columns else None
        bb_lower   = safe_float(df["bb_lower"].iloc[-1]) if "bb_lower" in df.columns else None
        vol_sma20  = safe_float(df["vol_sma20"].iloc[-1]) if "vol_sma20" in df.columns else None
        vol_today  = safe_float(df["volume"].iloc[-1]) or 0

        sma5_prev  = safe_float(df["sma5"].iloc[-2])   if ("sma5"   in df.columns and len(df) >= 2) else None
        sma20_prev = safe_float(df["sma20"].iloc[-2])  if ("sma20"  in df.columns and len(df) >= 2) else None
        vol_prev   = safe_float(df["volume"].iloc[-2]) if len(df) >= 2 else None

        vol_pct = round((vol_today / vol_sma20) * 100, 1) if (vol_sma20 and vol_sma20 > 0) else None

        log(f"  현재가: {current_price:>10,}원  |  RSI({RSI_PERIOD}): {rsi_value}")
        if sma20_curr:
            log(f"  SMA20: {sma20_curr:>10,.0f}원  |  거래량(평균대비): {vol_pct or 'N/A'}%")
        if bb_lower:
            log(f"  BB하단: {bb_lower:>9,.0f}원")

        # status.json에 저장할 종목 데이터
        stocks_data[symbol] = {
            "name":         name,
            "price":        current_price,
            "rsi":          rsi_value,
            "sma20":        round(sma20_curr, 0) if sma20_curr else None,
            "bb_lower":     round(bb_lower, 0)   if bb_lower   else None,
            "vol_pct":      vol_pct,
            "last_updated": now_str,
        }

        # ================================================================
        # Rule A: RSI 과매도 (RSI ≤ 30)
        # ================================================================
        flag_rsi = f"{symbol}_rsi_oversold"
        alert_flags.setdefault(flag_rsi, False)

        if rsi_value <= RSI_OVERSOLD_THRESHOLD:
            if not alert_flags[flag_rsi]:
                if send_telegram(f"🚨 [매수경고] {name} RSI {rsi_value} 진입!\n현재가: {current_price:,}원"):
                    alert_flags[flag_rsi] = True
                    log("  → [Rule A] 과매도 알림 발송")
            else:
                log("  → [Rule A] RSI 과매도 지속 (중복 방지)")
        else:
            if alert_flags[flag_rsi]:
                alert_flags[flag_rsi] = False
                log("  → [Rule A] RSI 회복, 플래그 초기화")

        # ================================================================
        # Rule B: 그린리소스 익절 (현재가 > 13,000원)
        # ================================================================
        if symbol == "402490":
            flag_price = "402490_price_target"
            alert_flags.setdefault(flag_price, False)

            if current_price > GREEN_RESOURCE_TARGET:
                if not alert_flags[flag_price]:
                    if send_telegram(f"💰 [익절알림] 그린리소스 {GREEN_RESOURCE_TARGET:,}원 돌파!\n분할 매도를 준비하세요."):
                        alert_flags[flag_price] = True
                        log("  → [Rule B] 익절 알림 발송")
                else:
                    log("  → [Rule B] 목표가 초과 지속 (중복 방지)")
            else:
                if alert_flags[flag_price]:
                    alert_flags[flag_price] = False
                    log("  → [Rule B] 목표가 이탈, 플래그 초기화")

        # ================================================================
        # Rule C: 스나이퍼 바닥 포착 (RSI ≤ 30 AND 현재가 ≤ BB 하단)
        # ================================================================
        flag_sniper = f"{symbol}_sniper_bottom"
        alert_flags.setdefault(flag_sniper, False)

        if rsi_value <= RSI_OVERSOLD_THRESHOLD and bb_lower is not None and current_price <= bb_lower:
            if not alert_flags[flag_sniper]:
                msg = (
                    f"🎯 [바닥포착] {name} - RSI 과매도 & 볼린저 밴드 하단 이탈!\n"
                    f"분할 매수를 검토하세요. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_sniper] = True
                    log("  → [Rule C] 스나이퍼 바닥 알림 발송")
            else:
                log("  → [Rule C] 바닥 조건 지속 (중복 방지)")
        else:
            # RSI가 5포인트 이상 회복되면 플래그 리셋
            if alert_flags[flag_sniper] and rsi_value > RSI_OVERSOLD_THRESHOLD + 5:
                alert_flags[flag_sniper] = False
                log("  → [Rule C] 바닥 조건 해소, 플래그 초기화")

        # ================================================================
        # Rule D: 수급 폭발 (거래량 ≥ 평균 3배 AND 현재가 > SMA20 돌파)
        # ================================================================
        flag_surge = f"{symbol}_volume_surge"
        alert_flags.setdefault(flag_surge, False)

        surge_cond = (
            vol_sma20 is not None and vol_sma20 > 0 and
            sma20_curr is not None and
            vol_today >= vol_sma20 * 3 and
            current_price > sma20_curr
        )

        if surge_cond:
            if not alert_flags[flag_surge]:
                msg = (
                    f"🚀 [수급폭발] {name} - 거래량 {vol_pct:.0f}% 급증하며 20일선 돌파!\n"
                    f"상승 추세가 시작될 수 있습니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_surge] = True
                    log("  → [Rule D] 수급폭발 알림 발송")
            else:
                log("  → [Rule D] 수급폭발 조건 지속 (중복 방지)")
        else:
            if alert_flags[flag_surge]:
                vol_calmed   = vol_sma20 is not None and vol_today < vol_sma20 * 1.5
                price_fallen = sma20_curr is not None and current_price < sma20_curr * 0.98
                if vol_calmed or price_fallen:
                    alert_flags[flag_surge] = False
                    log("  → [Rule D] 수급폭발 조건 해소, 플래그 초기화")

        # ================================================================
        # Rule E: 데드크로스 (SMA5 ↓ SMA20 돌파 + 거래량 전일 대비 증가)
        # ================================================================
        flag_dead = f"{symbol}_dead_cross"
        alert_flags.setdefault(flag_dead, False)

        dead_cross = (
            sma5_prev is not None and sma20_prev is not None and
            sma5_curr is not None and sma20_curr is not None and
            sma5_prev >= sma20_prev and sma5_curr < sma20_curr
        )
        vol_increasing = vol_prev is not None and vol_today > vol_prev

        if dead_cross and vol_increasing:
            if not alert_flags[flag_dead]:
                msg = (
                    f"⚠️ [위험감지] {name} - 대량 거래 동반 데드크로스 발생!\n"
                    f"비중 축소나 리스크 관리가 필요합니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_dead] = True
                    log("  → [Rule E] 데드크로스 알림 발송")
            else:
                log("  → [Rule E] 데드크로스 지속 (중복 방지)")
        else:
            # 골든크로스 (SMA5가 SMA20 위로 다시 올라오면) 플래그 리셋
            if (alert_flags[flag_dead] and
                    sma5_curr is not None and sma20_curr is not None and
                    sma5_curr > sma20_curr):
                alert_flags[flag_dead] = False
                log("  → [Rule E] 골든크로스 감지, 데드크로스 플래그 초기화")

        time.sleep(1.5)   # 종목 간 Rate Limit 방지

    save_status(alert_flags, stocks_data, logs, balance)
    return stocks_data


# ================================================================
# 메인 실행
# ================================================================
def main():
    missing = [k for k, v in {"APP_KEY": APP_KEY, "APP_SECRET": APP_SECRET, "ACC_NO": ACC_NO}.items()
               if not v or "YOUR_" in v]
    if missing:
        print(f"⛔ .env 파일에서 다음 항목을 설정해주세요: {', '.join(missing)}")
        return

    alert_flags = load_status()
    logs: list  = []

    def log(msg: str):
        print(msg)
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    log("=" * 55)
    log("  한국투자증권 트레이딩 알림 봇 시작")
    log(f"  계좌 모드  : {'모의투자 (Sandbox)' if IS_MOCK else '실계좌 (LIVE)'}")
    log(f"  감시 종목  : {', '.join(f'{v}({k})' for k, v in STOCKS.items())}")
    log(f"  체크 주기  : {LOOP_INTERVAL // 60}분마다")
    log(f"  RSI 기준   : {RSI_PERIOD}일, 과매도 임계값 {RSI_OVERSOLD_THRESHOLD}")
    log(f"  익절 목표가: 그린리소스 {GREEN_RESOURCE_TARGET:,}원")
    log("=" * 55)

    log("\n[초기화] 텔레그램 연결 테스트 중...")
    if not send_telegram(
        f"✅ 트레이딩 알림 봇 시작!\n"
        f"감시: {', '.join(STOCKS.values())}\n"
        f"모드: {'모의투자' if IS_MOCK else '실계좌'}"
    ):
        log("⛔ 텔레그램 전송 실패. BOT_TOKEN/CHAT_ID를 다시 확인하세요.")
        return

    set_bot_running(True)
    broker = None

    try:
        while True:
            # 장 운영 시간 체크
            if not is_market_open():
                reason = market_closed_reason()
                log(f"장이 열리지 않은 시간 ({reason}). 1시간 대기...")
                save_status(alert_flags, logs=logs)
                time.sleep(3600)
                continue

            # 브로커 초기화 (최초 또는 토큰 만료 후 재초기화)
            if broker is None:
                log("[초기화] API 연결 중...")
                try:
                    broker = create_broker()
                    log("[초기화] API 연결 완료")
                except Exception as e:
                    log(f"⛔ API 초기화 실패: {e}")
                    time.sleep(60)
                    continue

            try:
                # 잔고 조회 (check_and_alert 전에)
                log("\n잔고 조회 중...")
                balance = get_balance(broker)
                if balance:
                    log(f"  총 평가금액: {balance['tot_evlu_amt']:,}원  |  수익률: {balance['profit_rate']:.2f}%")

                check_and_alert(broker, alert_flags, logs, balance)
                log(f"\n⏳ 다음 체크까지 {LOOP_INTERVAL // 60}분 대기...")
                time.sleep(LOOP_INTERVAL)

            except KeyboardInterrupt:
                raise

            except Exception as e:
                if _is_token_error(e):
                    log("⚠️  토큰 만료 감지 — 재로그인합니다...")
                    broker = None
                    time.sleep(5)
                elif _is_rate_limit(e):
                    log("⚠️  Rate Limit 감지 — 60초 대기...")
                    time.sleep(60)
                else:
                    log(f"❌ 예상치 못한 오류: {e}")
                    log("   60초 후 자동 재시도...")
                    time.sleep(60)

    except KeyboardInterrupt:
        log("\n🛑 사용자에 의해 종료되었습니다. (Ctrl+C)")
        send_telegram("🛑 트레이딩 알림 봇이 사용자에 의해 종료되었습니다.")

    finally:
        set_bot_running(False)


if __name__ == "__main__":
    main()
