# ==============================================================
#  strategy.py — 기술적 지표 계산 및 알림 조건 판단
#  RSI, SMA, 볼린저밴드 기반 9가지 매매 신호를 탐지합니다.
# ==============================================================

import time
from datetime import datetime
from typing import Optional

import pandas as pd
from ta.momentum   import RSIIndicator
from ta.trend      import SMAIndicator
from ta.volatility import BollingerBands

from api_handler import get_ohlcv_dataframe, get_current_price, get_investor_trend
from utils import send_telegram, safe_float

RSI_PERIOD             = 14
RSI_OVERSOLD_THRESHOLD = 30


# ================================================================
# 지표 계산
# ================================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """SMA5/20/60/120, 거래량 SMA20, 볼린저밴드 상단/중간/하단을 추가합니다."""
    if len(df) < 20:
        return df
    df = df.copy()
    df["sma5"]      = SMAIndicator(close=df["close"], window=5).sma_indicator()
    df["sma20"]     = SMAIndicator(close=df["close"], window=20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(window=20).mean()
    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_lower"]  = bb.bollinger_lband()
    df["bb_mid"]    = bb.bollinger_mavg()
    df["bb_upper"]  = bb.bollinger_hband()
    # 중장기 이동평균 — 데이터가 충분할 때만 계산
    if len(df) >= 60:
        df["sma60"]  = SMAIndicator(close=df["close"], window=60).sma_indicator()
    if len(df) >= 120:
        df["sma120"] = SMAIndicator(close=df["close"], window=120).sma_indicator()
    return df


def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> Optional[float]:
    """RSI 값을 계산하여 반환합니다. 데이터 부족 시 None을 반환합니다."""
    if df.empty or "close" not in df.columns or len(df) < period + 1:
        return None
    latest = RSIIndicator(close=df["close"], window=period).rsi().iloc[-1]
    return None if pd.isna(latest) else round(float(latest), 2)


# ================================================================
# 알림 체크 로직 (9가지 규칙)
# ================================================================
def check_and_alert(
    broker,
    watch_list: dict,
    alert_flags: dict,
) -> tuple[dict, list]:
    """
    감시 종목 전체를 순회하며 지표 체크 + 알림 전송.
    Returns: (stocks_data dict, log_lines list)
    """
    stocks_data: dict = {}
    log_lines:   list = []

    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        log_lines.append(line)

    log(f"{'─'*50}")
    log(f"감시 종목 {len(watch_list)}개 체크 시작")

    for symbol, info in watch_list.items():
        name         = info.get("name", symbol)
        target_price = info.get("target_price")

        # ── OHLCV + 지표 ──────────────────────────────────────
        # SMA60/120 계산을 위해 최소 120일치 데이터 확보
        df = get_ohlcv_dataframe(broker, symbol, days=120)
        if df.empty:
            log(f"[{name}] 데이터 조회 실패, 스킵")
            time.sleep(1)
            continue

        df = calculate_indicators(df)

        current_price = get_current_price(broker, symbol)
        if current_price is None:
            log(f"[{name}] 현재가 조회 실패, 스킵")
            time.sleep(1)
            continue

        rsi_value = calculate_rsi(df)
        if rsi_value is None:
            log(f"[{name}] RSI 계산 실패, 스킵")
            time.sleep(1)
            continue

        # ── 최고가 갱신 (트레일링 스탑 기준선 추적) ──────────────────────
        # target_price 설정 종목만 추적 — alert_flags에 저장해 flush_status()로 자동 영속화
        if target_price and target_price > 0:
            hp_key        = f"{symbol}_highest_price"
            highest_price = max(alert_flags.get(hp_key, 0), current_price)
            alert_flags[hp_key] = highest_price
        else:
            highest_price = 0

        # ── 지표 값 추출 ────────────────────────────────────────
        sma5_curr   = safe_float(df["sma5"].iloc[-1])      if "sma5"      in df.columns else None
        sma20_curr  = safe_float(df["sma20"].iloc[-1])     if "sma20"     in df.columns else None
        sma60_curr  = safe_float(df["sma60"].iloc[-1])     if "sma60"     in df.columns else None
        sma120_curr = safe_float(df["sma120"].iloc[-1])    if "sma120"    in df.columns else None
        bb_lower    = safe_float(df["bb_lower"].iloc[-1])  if "bb_lower"  in df.columns else None
        vol_sma20   = safe_float(df["vol_sma20"].iloc[-1]) if "vol_sma20" in df.columns else None
        vol_today   = safe_float(df["volume"].iloc[-1]) or 0

        sma5_prev  = safe_float(df["sma5"].iloc[-2])   if ("sma5"   in df.columns and len(df) >= 2) else None
        sma20_prev = safe_float(df["sma20"].iloc[-2])  if ("sma20"  in df.columns and len(df) >= 2) else None
        sma60_prev = safe_float(df["sma60"].iloc[-2])  if ("sma60"  in df.columns and len(df) >= 2) else None
        vol_prev   = safe_float(df["volume"].iloc[-2]) if len(df) >= 2 else None

        vol_pct = round((vol_today / vol_sma20) * 100, 1) if (vol_sma20 and vol_sma20 > 0) else None

        log(f"[{name}({symbol})] 현재가:{current_price:,} RSI:{rsi_value}"
            f"  SMA20:{f'{sma20_curr:,.0f}' if sma20_curr else 'N/A'}"
            f"  거래량:{f'{vol_pct:.0f}%' if vol_pct else 'N/A'}")

        stocks_data[symbol] = {
            "name":         name,
            "price":        current_price,
            "rsi":          rsi_value,
            "sma20":        round(sma20_curr, 0)  if sma20_curr  else None,
            "sma60":        round(sma60_curr, 0)  if sma60_curr  else None,
            "sma120":       round(sma120_curr, 0) if sma120_curr else None,
            "bb_lower":     round(bb_lower, 0)    if bb_lower    else None,
            "vol_pct":      vol_pct,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # ── Rule A: RSI 과매도 (RSI ≤ 30) ──────────────────────
        flag_a = f"{symbol}_rsi_oversold"
        alert_flags.setdefault(flag_a, False)
        if rsi_value <= RSI_OVERSOLD_THRESHOLD:
            if not alert_flags[flag_a]:
                if send_telegram(f"🚨 [매수경고] {name} RSI {rsi_value} 진입!\n현재가: {current_price:,}원"):
                    alert_flags[flag_a] = True
                    log(f"  → [Rule A] 과매도 알림 발송")
        else:
            if alert_flags[flag_a]:
                alert_flags[flag_a] = False
                log(f"  → [Rule A] RSI 회복 ({rsi_value}), 플래그 초기화")

        # ── Rule B: 목표가 돌파 (target_price 설정 종목만) ──────
        if target_price and target_price > 0:
            flag_b = f"{symbol}_price_target"
            alert_flags.setdefault(flag_b, False)
            if current_price > target_price:
                if not alert_flags[flag_b]:
                    if send_telegram(f"💰 [익절알림] {name} {target_price:,}원 돌파!\n분할 매도를 준비하세요. (현재가: {current_price:,}원)"):
                        alert_flags[flag_b] = True
                        log(f"  → [Rule B] 익절 알림 발송")
            else:
                if alert_flags.get(flag_b):
                    alert_flags[flag_b] = False
                    log(f"  → [Rule B] 목표가 이탈, 플래그 초기화")

        # ── 손절 목표가 (stop_loss_price 설정 종목만) ──────────────────
        stop_loss_price = info.get("stop_loss_price")
        if stop_loss_price and stop_loss_price > 0:
            flag_sl = f"{symbol}_stop_loss"
            alert_flags.setdefault(flag_sl, False)
            if current_price <= stop_loss_price:
                if not alert_flags[flag_sl]:
                    msg = (
                        f"🔴 [손절경고] {name} - "
                        f"손절 목표가({stop_loss_price:,}원) 이탈!\n"
                        f"리스크 관리가 필요합니다. (현재가: {current_price:,}원)"
                    )
                    if send_telegram(msg):
                        alert_flags[flag_sl] = True
                        log(f"  → [손절] 손절 경고 알림 발송")
            else:
                if alert_flags.get(flag_sl):
                    alert_flags[flag_sl] = False
                    log(f"  → [손절] 가격 회복, 플래그 초기화")

        # ── Rule F: 🛡️ 트레일링 스탑 (목표가 돌파 후 고점 대비 하락) ────
        if target_price and target_price > 0 and highest_price > target_price:
            flag_f       = f"{symbol}_trailing_stop"
            trailing_pct = float(info.get("trailing_stop_pct", 3.0))
            drop_pct     = (highest_price - current_price) / highest_price * 100
            alert_flags.setdefault(flag_f, False)
            if drop_pct >= trailing_pct:
                if not alert_flags[flag_f]:
                    msg = (
                        f"🛡️ [트레일링 스탑] {name} - "
                        f"최고점({highest_price:,}원) 대비 {trailing_pct:.1f}% 하락!\n"
                        f"수익 보존을 위해 익절을 검토하세요. (현재가: {current_price:,}원)"
                    )
                    if send_telegram(msg):
                        alert_flags[flag_f] = True
                        log(f"  → [Rule F] 트레일링 스탑 알림 발송")
            else:
                if alert_flags.get(flag_f):
                    alert_flags[flag_f] = False
                    log(f"  → [Rule F] 하락폭 회복 ({drop_pct:.1f}%), 플래그 초기화")

        # ── Rule C: 🎯 스나이퍼 바닥 (RSI ≤ 30 AND 현재가 ≤ BB하단) ──
        flag_c = f"{symbol}_sniper_bottom"
        alert_flags.setdefault(flag_c, False)
        if rsi_value <= RSI_OVERSOLD_THRESHOLD and bb_lower is not None and current_price <= bb_lower:
            if not alert_flags[flag_c]:
                msg = (
                    f"🎯 [바닥포착] {name} - RSI 과매도 & 볼린저 밴드 하단 이탈!\n"
                    f"분할 매수를 검토하세요. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_c] = True
                    log(f"  → [Rule C] 스나이퍼 바닥 알림 발송")
        else:
            if alert_flags[flag_c] and rsi_value > RSI_OVERSOLD_THRESHOLD + 5:
                alert_flags[flag_c] = False
                log(f"  → [Rule C] 바닥 조건 해소, 플래그 초기화")

        # ── Rule D: 🚀 수급 폭발 (거래량 ≥ 3× SMA20 AND 현재가 > SMA20) ──
        flag_d = f"{symbol}_volume_surge"
        alert_flags.setdefault(flag_d, False)
        surge_cond = (
            vol_sma20 is not None and vol_sma20 > 0 and
            sma20_curr is not None and
            vol_today >= vol_sma20 * 3 and
            current_price > sma20_curr
        )
        if surge_cond:
            if not alert_flags[flag_d]:
                msg = (
                    f"🚀 [수급폭발] {name} - 거래량 {vol_pct:.0f}% 급증하며 20일선 돌파!\n"
                    f"상승 추세가 시작될 수 있습니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_d] = True
                    log(f"  → [Rule D] 수급폭발 알림 발송")
        else:
            if alert_flags.get(flag_d):
                vol_calmed   = vol_sma20 is not None and vol_today < vol_sma20 * 1.5
                price_fallen = sma20_curr is not None and current_price < sma20_curr * 0.98
                if vol_calmed or price_fallen:
                    alert_flags[flag_d] = False
                    log(f"  → [Rule D] 수급폭발 조건 해소, 플래그 초기화")

        # ── Rule E: ⚠️ 데드크로스 (SMA5 ↓ SMA20 돌파 + 거래량 증가) ──
        flag_e = f"{symbol}_dead_cross"
        alert_flags.setdefault(flag_e, False)
        dead_cross = (
            sma5_prev is not None and sma20_prev is not None and
            sma5_curr is not None and sma20_curr is not None and
            sma5_prev >= sma20_prev and sma5_curr < sma20_curr
        )
        vol_inc = vol_prev is not None and vol_today > vol_prev
        if dead_cross and vol_inc:
            if not alert_flags[flag_e]:
                msg = (
                    f"⚠️ [위험감지] {name} - 대량 거래 동반 데드크로스 발생!\n"
                    f"비중 축소나 리스크 관리가 필요합니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_e] = True
                    log(f"  → [Rule E] 데드크로스 알림 발송")
        else:
            if (alert_flags.get(flag_e) and
                    sma5_curr is not None and sma20_curr is not None and
                    sma5_curr > sma20_curr):
                alert_flags[flag_e] = False
                log(f"  → [Rule E] 골든크로스 감지, 플래그 초기화")

        # ── Rule H: ✨ 골든크로스 (SMA5 ↑ SMA20 돌파 + 거래량 증가) ──
        flag_h = f"{symbol}_golden_cross"
        alert_flags.setdefault(flag_h, False)
        golden_cross = (
            sma5_prev is not None and sma20_prev is not None and
            sma5_curr is not None and sma20_curr is not None and
            sma5_prev < sma20_prev and sma5_curr >= sma20_curr
        )
        if golden_cross and vol_inc:
            if not alert_flags[flag_h]:
                msg = (
                    f"✨ [골든크로스] {name} - 대량 거래 동반 골든크로스 발생!\n"
                    f"상승 추세 전환을 기대해 볼 수 있습니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_h] = True
                    log(f"  → [Rule H] 골든크로스 알림 발송")
        else:
            if (alert_flags.get(flag_h) and
                    sma5_curr is not None and sma20_curr is not None and
                    sma5_curr < sma20_curr):
                alert_flags[flag_h] = False
                log(f"  → [Rule H] 데드크로스 전환, 플래그 초기화")

        # ── Rule I: 🔵 중기 골든크로스 (SMA20 ↑ SMA60) ─────────────────
        # 단기(20일)선이 중기(60일)선을 상향 돌파 → 중기 상승 전환 신호
        flag_i = f"{symbol}_mid_golden"
        alert_flags.setdefault(flag_i, False)
        mid_golden = (
            sma20_prev is not None and sma60_prev is not None and
            sma20_curr is not None and sma60_curr is not None and
            sma20_prev < sma60_prev and sma20_curr >= sma60_curr
        )
        if mid_golden:
            if not alert_flags[flag_i]:
                msg = (
                    f"🔵 [중기 골든크로스] {name} — SMA20이 SMA60을 상향 돌파!\n"
                    f"중기 상승 추세 전환 가능성이 있습니다. (현재가: {current_price:,}원)"
                )
                if send_telegram(msg):
                    alert_flags[flag_i] = True
                    log(f"  → [Rule I] 중기 골든크로스 알림 발송")
        else:
            if (alert_flags.get(flag_i) and
                    sma20_curr is not None and sma60_curr is not None and
                    sma20_curr < sma60_curr):
                alert_flags[flag_i] = False
                log(f"  → [Rule I] 중기 데드크로스 전환, 플래그 초기화")

        # ── Rule J: 🟢 장기 지지선 반등 (현재가 SMA120 ±3% + RSI ≤ 45) ──
        # 120일선(주봉 24주) 근방에서 RSI 저점 → 장기 지지 매수 기회
        flag_j = f"{symbol}_sma120_support"
        alert_flags.setdefault(flag_j, False)
        if sma120_curr is not None and sma120_curr > 0:
            dist_pct = (current_price - sma120_curr) / sma120_curr * 100
            sma120_support = -3.0 <= dist_pct <= 3.0 and rsi_value <= 45
        else:
            sma120_support = False
        if sma120_support:
            if not alert_flags[flag_j]:
                msg = (
                    f"🟢 [장기지지] {name} — 120일선({sma120_curr:,.0f}원) 근방 RSI 저점!\n"
                    f"장기 지지선 반등 가능성이 있습니다. "
                    f"(현재가: {current_price:,}원, RSI: {rsi_value})"
                )
                if send_telegram(msg):
                    alert_flags[flag_j] = True
                    log(f"  → [Rule J] 장기지지선 반등 알림 발송")
        else:
            if alert_flags.get(flag_j) and (
                sma120_curr is None or current_price > sma120_curr * 1.05
            ):
                alert_flags[flag_j] = False
                log(f"  → [Rule J] 장기지지 조건 해소, 플래그 초기화")

        # ── Rule G: 🦅 쌍끌이 수급 (외국인·기관 동반 순매수) ───────────
        # 두 주체 합산 순매수 ≥ 5일 평균거래량의 5% → 기관+외국인 공동 매집 신호
        flag_g = f"{symbol}_major_buying"
        alert_flags.setdefault(flag_g, False)
        investor = get_investor_trend(broker, symbol)
        if investor is not None:
            frgn     = investor["frgn_ntby_qty"]
            orgn     = investor["orgn_ntby_qty"]
            vol5_avg = df["volume"].tail(5).mean() if len(df) >= 5 else None
            major_buy = (
                vol5_avg is not None and vol5_avg > 0
                and frgn > 0 and orgn > 0
                and (frgn + orgn) >= vol5_avg * 0.05
            )
            if major_buy:
                if not alert_flags[flag_g]:
                    msg = (
                        f"🦅 [쌍끌이 매수] {name} - "
                        f"외국인({frgn:+,}주) · 기관({orgn:+,}주)\n"
                        f"강력한 동반 매수세가 포착되었습니다! (현재가: {current_price:,}원)"
                    )
                    if send_telegram(msg):
                        alert_flags[flag_g] = True
                        log(f"  → [Rule G] 쌍끌이 수급 알림 발송")
            else:
                if alert_flags.get(flag_g) and (frgn <= 0 or orgn <= 0):
                    alert_flags[flag_g] = False
                    log(f"  → [Rule G] 수급 조건 해소, 플래그 초기화")

        time.sleep(1.5)   # Rate Limit 방지

    return stocks_data, log_lines


# ================================================================
# 스캘핑 봇 전용 함수
# ================================================================
def is_bull_market_filter(df: pd.DataFrame) -> bool:
    """개별 종목 20일 이동평균이 20일 전보다 높으면 상승 추세로 판단합니다.
    하락장에서 역추세 매수를 방지하기 위한 최소한의 방어 필터입니다."""
    if "sma20" not in df.columns or len(df) < 40:
        return True  # 데이터 부족 시 필터 통과 (너무 보수적으로 걸리지 않도록)
    sma20_now  = df["sma20"].iloc[-1]
    sma20_prev = df["sma20"].iloc[-21]  # 20거래일 전
    if pd.isna(sma20_now) or pd.isna(sma20_prev):
        return True
    return float(sma20_now) > float(sma20_prev)


def check_scalping_signal_daily(
    df: pd.DataFrame,
    rsi_threshold: float = 35.0,
    stoch_threshold: float = 25.0,
) -> bool:
    """당일 종가 기준 3중 조건 체크.
    1) 종가가 BB 하단선 대비 +2% 이내 접근 또는 이탈
    2) RSI(14) ≤ rsi_threshold
    3) Stochastic %K ≤ stoch_threshold
    Returns: 3가지 조건 모두 충족 시 True"""
    required = ("close", "bb_lower", "rsi", "stoch_k")
    for col in required:
        if col not in df.columns or df[col].empty:
            return False

    row = df.iloc[-1]
    if any(pd.isna(row.get(c)) for c in required):
        return False

    cond_bb    = float(row["close"])   <= float(row["bb_lower"]) * 1.02
    cond_rsi   = float(row["rsi"])     <= rsi_threshold
    cond_stoch = float(row["stoch_k"]) <= stoch_threshold
    return cond_bb and cond_rsi and cond_stoch


def check_exit_condition(
    df: pd.DataFrame,
    entry_price: float,
    stoploss_pct: float = 3.5,
    rsi_exit: float     = 65.0,
) -> Optional[str]:
    """당일 데이터 기준으로 청산 조건을 체크합니다.
    Returns: 청산 사유 문자열 (해당 없으면 None)"""
    if df.empty:
        return None

    row = df.iloc[-1]
    stop_price = entry_price * (1 - stoploss_pct / 100)

    # 손절: 종가가 손절가 이하
    if float(row.get("close", entry_price)) <= stop_price:
        return f"손절 -{stoploss_pct:.1f}%"

    # 익절 1: 당일 고가가 BB 상단에 터치
    if "bb_upper" in df.columns and not pd.isna(row.get("bb_upper")):
        if float(row.get("high", 0)) >= float(row["bb_upper"]):
            return "BB 상단 도달 (익절)"

    # 익절 2: RSI 과매수 도달
    if "rsi" in df.columns and not pd.isna(row.get("rsi")):
        if float(row["rsi"]) >= rsi_exit:
            return f"RSI {rsi_exit:.0f} 도달 (익절)"

    return None
