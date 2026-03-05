# ==============================================================
#  pages/2_스캘핑.py — 볼린저밴드 낙폭과대 스캘핑 백테스트 + 자동매매
#
#  전략 핵심 (3가지 조건 동시 충족 - 빠르고 강한 반등 노림):
#    1. 가격: 볼린저 밴드 하단선(Lower Band) 터치 또는 이탈 (낙폭 과대)
#    2. RSI: 35 이하 (과매도 구간 진입)
#    3. Stochastic(스토캐스틱): %K가 25 이하 (초단기 과매도)
#
#  청산 조건 (먼저 충족되는 것):
#    - 단기 슈팅: 볼린저 밴드 상단 터치 또는 RSI 65 이상 도달 시
#    - 시간 청산: N일 경과
#    - 손절: 진입가 대비 -N%
#
#  자동매매:
#    - 매일 15:10 전종목 스캔 (pykrx 거래량 상위 500종목)
#    - 15:20 시장가 주문 실행 (모의투자 전용)
# ==============================================================

import csv
import json
import threading
import time
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands

st.set_page_config(page_title="스캘핑 백테스트", page_icon="⚡", layout="wide")
st.title("⚡ 낙폭과대 반전 스캘핑 전략 백테스트")
st.caption("볼린저 밴드 하단과 스토캐스틱/RSI 과매도를 활용한 승률 높은 단기 반등 전략입니다.")

WATCHLIST_FILE   = Path("watchlist.json")
POSITIONS_FILE   = Path("scalping_positions.json")   # 현재 보유 포지션
TRADES_LOG_FILE  = Path("scalping_trades.csv")        # 거래 이력
MAX_DAILY_TRADES = 3    # 하루 최대 매매 횟수
MAX_CASH_RATIO   = 0.10 # 예수금 대비 최대 투자 비율 (10%)
SCALP_ENTRY_HOUR = 15   # 진입 체크 시각 (15:10)
SCALP_ENTRY_MIN  = 10
SCALP_ORDER_HOUR = 15   # 실제 주문 시각 (15:20)
SCALP_ORDER_MIN  = 20
SCALP_SCAN_TIMEOUT_MIN = 18  # 스캔 타임아웃 (15:18 이후 중단)


# ================================================================
# 지표 계산
# ================================================================
def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 20:
        return df
    df = df.copy()

    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_lower"]  = bb.bollinger_lband()
    df["bb_mid"]    = bb.bollinger_mavg()
    df["bb_upper"]  = bb.bollinger_hband()

    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()

    stoch = StochasticOscillator(
        high=df["high"], low=df["low"], close=df["close"], window=14, smooth_window=3
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    return df


# ================================================================
# 스캘핑 신호 탐지
# ================================================================
def detect_signals(
    df: pd.DataFrame,
    rsi_threshold: float = 35.0,
    stoch_threshold: float = 25.0,
) -> pd.DataFrame:
    """조건: 가격이 BB하단 근처/이탈 + RSI 과매도 + 스토캐스틱 과매도"""
    df = df.reset_index(drop=True)
    signals = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        required = ("rsi", "bb_lower", "stoch_k")
        if any(pd.isna(row.get(c)) for c in required):
            continue

        if row["close"] > row["bb_lower"] * 1.02:
            continue
        if row["rsi"] > rsi_threshold:
            continue
        if row["stoch_k"] > stoch_threshold:
            continue

        signals.append({
            "_idx":   i,
            "신호일":  row.get("date", ""),
            "진입가":  int(row["close"]),
            "RSI":    round(row["rsi"], 1),
            "Stoch_K": round(row["stoch_k"], 1),
        })

    return pd.DataFrame(signals)


# ================================================================
# P&L 시뮬레이션
# ================================================================
def simulate_trades(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    exit_days: int   = 5,
    fee_pct: float   = 0.30,
    stoploss_pct: float = 3.0,
) -> pd.DataFrame:
    """진입 후 BB 상단/RSI 과매수 익절, 손절가 터치, N거래일 경과 시 청산."""
    if signals.empty:
        return pd.DataFrame()

    df = df.reset_index(drop=True)
    trades = []
    blocked_until = -1

    for _, sig in signals.iterrows():
        entry_idx = int(sig["_idx"])
        if entry_idx <= blocked_until:
            continue

        entry_price = float(sig["진입가"])
        stop_price  = entry_price * (1 - stoploss_pct / 100)
        exit_idx    = None
        exit_reason = "기간 만료"

        for j in range(1, exit_days + 1):
            ci = entry_idx + j
            if ci >= len(df):
                exit_idx    = len(df) - 1
                exit_reason = "기간 만료"
                break

            cur = df.iloc[ci]

            if float(cur["close"]) <= stop_price:
                exit_idx    = ci
                exit_reason = f"손절 -{stoploss_pct:.1f}%"
                break

            if float(cur["high"]) >= float(cur["bb_upper"]):
                exit_idx    = ci
                exit_reason = "BB 상단 도달 (익절)"
                break

            if not pd.isna(cur.get("rsi")) and float(cur["rsi"]) >= 65.0:
                exit_idx    = ci
                exit_reason = "RSI 과매수 (익절)"
                break

        if exit_idx is None:
            exit_idx    = min(entry_idx + exit_days, len(df) - 1)
            exit_reason = "기간 만료"

        blocked_until = exit_idx
        exit_price    = float(df.iloc[exit_idx]["close"])
        gross = (exit_price - entry_price) / entry_price * 100
        net   = gross - fee_pct

        trades.append({
            "진입일":    sig["신호일"],
            "청산일":    df.iloc[exit_idx].get("date", ""),
            "보유일":    exit_idx - entry_idx,
            "진입가":    entry_price,
            "청산가":    exit_price,
            "수익률(%)": round(net, 2),
            "청산사유":  exit_reason,
        })

    return pd.DataFrame(trades)


# ================================================================
# 사이드바 설정
# ================================================================
def _load_watchlist() -> dict:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


wl = _load_watchlist()

with st.sidebar:
    st.header("⚙️ 전략 파라미터")

    if wl:
        sym_options    = {f"{v.get('name', k)} ({k})": k for k, v in wl.items()}
        selected_label = st.selectbox("종목 선택", list(sym_options.keys()))
        selected_sym   = sym_options[selected_label]

        days_back = st.slider("조회 기간 (캘린더 일)", 90, 730, 365, 30)

        st.divider()
        st.markdown("**📊 진입 조건**")
        rsi_thresh   = st.slider("RSI 과매도 기준", 20.0, 45.0, 35.0, 1.0)
        stoch_thresh = st.slider("Stoch_K 과매도 기준", 10.0, 40.0, 25.0, 1.0)

        st.divider()
        st.markdown("**🚪 청산 조건**")
        exit_days    = st.slider("최대 보유 기간 (거래일)", 1, 10, 5)
        stoploss_pct = st.slider("손절 기준 (%)", 1.0, 10.0, 3.5, 0.5)
        fee_pct      = st.number_input("왕복 수수료 (%)", 0.0, 1.0, 0.30, 0.05)

        run_btn = st.button("▶ 백테스트 실행", type="primary", use_container_width=True)
    else:
        st.warning("감시 종목 없음. 메인 페이지에서 추가하세요.\n(자동매매 봇은 watchlist 없이도 전종목 스캔 가능)")
        run_btn      = False
        rsi_thresh   = 35.0
        stoch_thresh = 25.0
        selected_sym = None

    st.divider()
    st.markdown("**🤖 자동매매 (모의투자 전용)**")
    scalp_enabled     = st.toggle("스캘핑 봇 활성화", value=False, key="scalp_enabled")
    scalp_bull_filter = st.checkbox("상승장 필터 사용", value=True, key="scalp_bull")
    scalp_rsi_exit    = st.slider("RSI 익절 기준", 55.0, 80.0, 65.0, 1.0, key="scalp_rsi_exit")
    scalp_sl_pct      = st.slider("자동매매 손절 기준 (%)", 1.0, 10.0, 3.5, 0.5, key="scalp_sl")

    if scalp_enabled:
        start_btn = st.button("▶ 봇 시작", type="primary",   use_container_width=True, key="scalp_start")
        stop_btn  = st.button("⏹ 봇 정지", type="secondary", use_container_width=True, key="scalp_stop")
    else:
        start_btn = False
        stop_btn  = False


# ================================================================
# 자동매매 봇 — 포지션/로그 파일 I/O
# ================================================================
def _load_positions() -> dict:
    """scalping_positions.json에서 보유 포지션을 읽습니다."""
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_positions(positions: dict) -> None:
    """보유 포지션을 scalping_positions.json에 저장합니다."""
    try:
        POSITIONS_FILE.write_text(
            json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  ⚠️  포지션 저장 실패: {e}")


def _append_trade_log(record: dict) -> None:
    """거래 이력을 scalping_trades.csv에 한 줄 추가합니다."""
    fieldnames = ["date", "symbol", "name", "action", "price", "qty",
                  "rsi", "stoch_k", "reason", "pnl_pct"]
    write_header = not TRADES_LOG_FILE.exists()
    try:
        with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow({k: record.get(k, "") for k in fieldnames})
    except Exception as e:
        print(f"  ⚠️  거래 로그 저장 실패: {e}")


# ================================================================
# 자동매매 봇 — 봇 루프
# ================================================================
@st.cache_resource
def _get_scalping_bot_state() -> dict:
    """봇 상태 싱글톤 — Streamlit 리런 간에도 스레드/이벤트 유지."""
    return {
        "thread":      None,
        "stop_event":  threading.Event(),
        "lock":        threading.Lock(),
        "log":         [],       # UI용 로그 (최근 50줄)
        "status":      "정지",   # "정지" / "대기중" / "실행중"
        "daily_count": 0,        # 오늘 매매 횟수
        "last_date":   "",       # 날짜 변경 감지용
    }


def _scalping_bot_loop(
    state: dict,
    rsi_thr: float,
    stoch_thr: float,
    sl_pct: float,
    rsi_exit: float,
    use_bull_filter: bool,
) -> None:
    """백그라운드 스케줄러 루프.
    매일 15:10에 전종목 스캔으로 진입 체크, 15:20에 시장가 주문 실행.
    보유 중이면 매일 15:10에 청산 조건도 함께 체크합니다."""
    from api_handler import (
        get_mock_broker,
        get_ohlcv_dataframe, get_current_price,
        place_market_order, get_deposit,
    )
    from strategy import (
        calculate_indicators, scan_all_stocks_for_signals, check_exit_condition,
    )
    from utils import send_telegram

    def slog(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        with state["lock"]:
            state["log"].append(line)
            if len(state["log"]) > 50:
                state["log"] = state["log"][-50:]

    mock_broker = None

    with state["lock"]:
        state["status"] = "대기중"

    while not state["stop_event"].is_set():
        now = datetime.now()

        # 날짜 변경 시 일일 카운터 초기화
        today_str = now.strftime("%Y-%m-%d")
        with state["lock"]:
            if state["last_date"] != today_str:
                state["daily_count"] = 0
                state["last_date"]   = today_str

        # 브로커 초기화
        if mock_broker is None:
            try:
                mock_broker = get_mock_broker()
                slog("모의투자 broker 연결 완료")
            except Exception as e:
                slog(f"⛔ 모의투자 broker 초기화 실패: {e}")
                time.sleep(60)
                continue

        # ── 15:10~15:20 진입/청산 체크 시간 ─────────────────────────
        is_check_time = (
            now.weekday() < 5 and
            now.hour == SCALP_ENTRY_HOUR and
            SCALP_ENTRY_MIN <= now.minute < SCALP_ORDER_MIN
        )
        if not is_check_time:
            time.sleep(30)
            continue

        with state["lock"]:
            state["status"] = "실행중"

        positions = _load_positions()

        # ── 1단계: 보유 포지션 청산 체크 ────────────────────────────
        for symbol, pos in list(positions.items()):
            name = pos.get("name", symbol)
            try:
                df_pos = get_ohlcv_dataframe(mock_broker, symbol, days=120)
                if df_pos.empty:
                    slog(f"[{name}] 청산 체크 데이터 없음, 스킵")
                    continue
                df_pos = calculate_indicators(df_pos)
                if len(df_pos) >= 14:
                    stoch = StochasticOscillator(
                        high=df_pos["high"], low=df_pos["low"], close=df_pos["close"],
                        window=14, smooth_window=3,
                    )
                    df_pos["stoch_k"] = stoch.stoch()
                    df_pos["stoch_d"] = stoch.stoch_signal()
            except Exception as e:
                slog(f"[{name}] 청산 체크 오류: {e}")
                continue

            entry_price = float(pos["entry_price"])
            reason = check_exit_condition(df_pos, entry_price, sl_pct, rsi_exit)

            if reason:
                wait_secs = max(0, (SCALP_ORDER_MIN - now.minute) * 60 - now.second)
                slog(f"[{name}] 청산 조건({reason}) 감지 — {wait_secs}초 후 주문")
                time.sleep(wait_secs)

                qty  = int(pos.get("qty", 1))
                resp = place_market_order(mock_broker, symbol, "sell", qty)
                if resp:
                    cur_price = get_current_price(mock_broker, symbol) or entry_price
                    pnl = (cur_price - entry_price) / entry_price * 100
                    slog(f"[{name}] 매도 완료 {cur_price:,}원 ({pnl:+.2f}%) — {reason}")
                    send_telegram(
                        f"🤖 [스캘핑봇] {name} 청산\n"
                        f"사유: {reason}\n"
                        f"매도가: {cur_price:,}원 / 손익: {pnl:+.2f}%"
                    )
                    _append_trade_log({
                        "date": now.strftime("%Y-%m-%d %H:%M"),
                        "symbol": symbol, "name": name,
                        "action": "sell", "price": cur_price,
                        "qty": qty, "reason": reason, "pnl_pct": round(pnl, 2),
                    })
                    del positions[symbol]
                    _save_positions(positions)
                else:
                    slog(f"[{name}] 매도 주문 실패")
            else:
                hold_days = (date.today() - date.fromisoformat(pos["entry_date"])).days
                slog(f"[{name}] 보유 중 ({hold_days}일차) — 청산 조건 미충족")

        # ── 2단계: 전종목 스캔 + 신규 진입 ─────────────────────────
        with state["lock"]:
            daily_count = state["daily_count"]

        if daily_count >= MAX_DAILY_TRADES:
            slog(f"오늘 최대 매매 횟수({MAX_DAILY_TRADES}회) 도달 — 신규 진입 스캔 스킵")
        else:
            scan_timeout = now.replace(
                hour=SCALP_ENTRY_HOUR, minute=SCALP_SCAN_TIMEOUT_MIN,
                second=0, microsecond=0,
            )
            slog("전종목 스캔 시작...")
            candidates = scan_all_stocks_for_signals(
                mock_broker, rsi_thr, stoch_thr, use_bull_filter,
                log_fn=slog, timeout_at=scan_timeout,
            )
            slog(f"스캔 완료 — {len(candidates)}개 종목 신호 발견")

            for candidate in candidates:
                with state["lock"]:
                    daily_count = state["daily_count"]
                if daily_count >= MAX_DAILY_TRADES:
                    slog(f"오늘 최대 매매 횟수 도달 — 추가 진입 중단")
                    break

                symbol = candidate["symbol"]
                name   = candidate["name"]

                if symbol in positions:
                    continue

                cur_price = get_current_price(mock_broker, symbol)
                if cur_price is None:
                    slog(f"[{name}] 현재가 조회 실패 — 스킵")
                    continue

                deposit = get_deposit(mock_broker)
                if deposit is None:
                    slog(f"[{name}] 예수금 조회 실패 — 스킵")
                    continue

                max_invest = int(deposit * MAX_CASH_RATIO)
                qty = max_invest // cur_price
                if qty < 1:
                    slog(f"[{name}] 예수금 부족 (가능={max_invest:,}원, 주가={cur_price:,}원) — 스킵")
                    continue

                # 15:20까지 대기 후 시장가 매수
                wait_secs = max(0, (SCALP_ORDER_MIN - now.minute) * 60 - now.second)
                slog(
                    f"[{name}] 진입 신호! RSI={candidate['rsi']} "
                    f"Stoch={candidate['stoch_k']} — {wait_secs}초 후 매수"
                )
                time.sleep(wait_secs)

                resp = place_market_order(mock_broker, symbol, "buy", qty)
                if resp:
                    slog(f"[{name}] 매수 완료 {cur_price:,}원 × {qty}주")
                    send_telegram(
                        f"🤖 [스캘핑봇] {name} 매수\n"
                        f"매수가: {cur_price:,}원 × {qty}주\n"
                        f"RSI={candidate['rsi']} / Stoch={candidate['stoch_k']}"
                    )
                    _append_trade_log({
                        "date": now.strftime("%Y-%m-%d %H:%M"),
                        "symbol": symbol, "name": name,
                        "action": "buy", "price": cur_price,
                        "qty": qty,
                        "rsi": candidate["rsi"],
                        "stoch_k": candidate["stoch_k"],
                    })
                    positions[symbol] = {
                        "entry_price": cur_price,
                        "entry_date":  now.strftime("%Y-%m-%d"),
                        "qty":         qty,
                        "name":        name,
                    }
                    _save_positions(positions)
                    with state["lock"]:
                        state["daily_count"] += 1
                else:
                    slog(f"[{name}] 매수 주문 실패")

                time.sleep(1)  # Rate Limit 방지

        with state["lock"]:
            state["status"] = "대기중"

        time.sleep(60)  # 다음 분 체크

    with state["lock"]:
        state["status"] = "정지"
    slog("스캘핑 봇 정지")


# ================================================================
# 자동매매 봇 — UI (항상 렌더링, 백테스트 실행 여부와 무관)
# ================================================================
st.divider()
st.subheader("🤖 자동매매 봇 (모의투자 전용)")

_state = _get_scalping_bot_state()

# 봇 시작/정지 처리
if start_btn:
    if _state["thread"] is None or not _state["thread"].is_alive():
        _state["stop_event"].clear()
        _state["thread"] = threading.Thread(
            target=_scalping_bot_loop,
            args=(
                _state,
                rsi_thresh,        # 진입 RSI 임계값 (백테스트와 동일 파라미터 공유)
                stoch_thresh,      # 진입 Stoch 임계값
                scalp_sl_pct,      # 손절 기준 %
                scalp_rsi_exit,    # RSI 익절 기준
                scalp_bull_filter, # 상승장 필터 여부
            ),
            daemon=True,
        )
        _state["thread"].start()
        st.success("스캘핑 봇 시작 완료 — 15:10에 전종목 스캔을 시작합니다")
    else:
        st.warning("이미 실행 중입니다.")

if stop_btn:
    _state["stop_event"].set()
    if _state["thread"] and _state["thread"].is_alive():
        _state["thread"].join(timeout=5.0)
    st.success("스캘핑 봇 정지 완료")

# 봇 상태 표시
with _state["lock"]:
    bot_status  = _state["status"]
    bot_log     = list(_state["log"])
    daily_count = _state["daily_count"]

status_color = {"대기중": "🟡", "실행중": "🟢", "정지": "🔴"}.get(bot_status, "⚪")
st.markdown(
    f"**봇 상태:** {status_color} {bot_status} &nbsp;|&nbsp; "
    f"**오늘 매매:** {daily_count}/{MAX_DAILY_TRADES}회 &nbsp;|&nbsp; "
    f"**스캔 범위:** KOSPI/KOSDAQ 거래량 상위 500종목"
)

# 현재 포지션 표시
positions = _load_positions()
if positions:
    st.markdown("**📦 현재 포지션**")
    pos_rows = []
    for sym, pos in positions.items():
        pos_rows.append({
            "종목":   f"{pos.get('name', sym)} ({sym})",
            "수량":   pos["qty"],
            "진입가": f"{int(pos['entry_price']):,}원",
            "진입일": pos["entry_date"],
        })
    st.dataframe(pd.DataFrame(pos_rows), use_container_width=True, hide_index=True)
else:
    st.info("현재 보유 포지션 없음")

# 거래 로그
if TRADES_LOG_FILE.exists():
    st.markdown("**📋 거래 이력**")
    try:
        log_df = pd.read_csv(TRADES_LOG_FILE, encoding="utf-8")
        st.dataframe(log_df.tail(20), use_container_width=True, hide_index=True)
    except Exception:
        pass

# 봇 실행 로그
if bot_log:
    with st.expander("🖥️ 봇 실행 로그", expanded=False):
        st.code("\n".join(bot_log[-20:]), language=None)


# ================================================================
# 백테스트 섹션 (run_btn 클릭 시만 실행)
# ================================================================
st.divider()

if not run_btn:
    st.info("사이드바에서 파라미터를 설정한 뒤 **[▶ 백테스트 실행]** 버튼을 누르세요.")
    st.stop()

# ── 데이터 수집 ──────────────────────────────────────────────────
_err = ""
df_raw = pd.DataFrame()
with st.spinner("데이터 수집 중..."):
    try:
        from api_handler import get_broker, get_ohlcv_dataframe
        broker = get_broker()
        df_raw = get_ohlcv_dataframe(broker, selected_sym, days=days_back)
        if df_raw.empty:
            _err = f"**{selected_label}** 데이터 없음"
    except Exception as e:
        _err = f"**API 오류:** `{e}`"

if _err:
    st.error(_err)
    st.stop()

df = _add_indicators(df_raw)
df["_dt"] = pd.to_datetime(df["date"].astype(str))

# ── 신호 탐지 & P&L 시뮬 ────────────────────────────────────────
signals = detect_signals(df, rsi_threshold=rsi_thresh, stoch_threshold=stoch_thresh)
trades  = simulate_trades(df, signals, exit_days=exit_days, fee_pct=fee_pct, stoploss_pct=stoploss_pct)

st.subheader(f"📊 {selected_label} — {len(df)}일 데이터 / 신호 {len(signals)}건 / 매매 {len(trades)}건")

if trades.empty:
    st.warning("해당 기간/파라미터에서 매매 신호가 없습니다.")
    st.stop()

# ================================================================
# 섹션 1: 신호 차트
# ================================================================
with st.expander("📈 신호 차트 (진입 ▲ / 청산 ▼)", expanded=True):
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.70, 0.30],
        subplot_titles=("가격 및 진입/청산 시점", "RSI & Stochastic")
    )

    fig.add_trace(go.Scatter(x=df["_dt"], y=df["bb_upper"], line=dict(color="rgba(253,203,110,0.4)", width=1), showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["_dt"], y=df["bb_lower"], name="볼린저밴드", line=dict(color="rgba(253,203,110,0.4)", width=1), fill="tonexty", fillcolor="rgba(253,203,110,0.07)"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["_dt"], y=df["close"], name="종가", line=dict(color="#FFFFFF", width=2)), row=1, col=1)

    sig_dt = pd.to_datetime(signals["신호일"].astype(str))
    fig.add_trace(go.Scatter(x=sig_dt, y=signals["진입가"], mode="markers", name="진입 신호", marker=dict(symbol="triangle-up", size=13, color="#26de81")), row=1, col=1)

    exit_dt    = pd.to_datetime(trades["청산일"].astype(str))
    exit_color = ["#FF4444" if "손절" in r else "#26de81" if "익절" in r else "#FF9F43" for r in trades["청산사유"]]
    fig.add_trace(go.Scatter(x=exit_dt, y=trades["청산가"], mode="markers", name="청산", marker=dict(symbol="triangle-down", size=11, color=exit_color)), row=1, col=1)

    fig.add_trace(go.Scatter(x=df["_dt"], y=df["rsi"],     name="RSI",      line=dict(color="#A29BFE", width=1.5)), row=2, col=1)
    fig.add_trace(go.Scatter(x=df["_dt"], y=df["stoch_k"], name="Stoch %K", line=dict(color="#FFD166", width=1.5)), row=2, col=1)

    # RSI/Stoch 기준선
    fig.add_hline(y=rsi_thresh,   line=dict(color="#55EFC4", width=1, dash="dot"), row=2, col=1)
    fig.add_hline(y=stoch_thresh, line=dict(color="#FFD166", width=1, dash="dot"), row=2, col=1)

    fig.update_layout(height=500, hovermode="x unified", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(17,17,27,0.6)")
    st.plotly_chart(fig, use_container_width=True)

# ================================================================
# 섹션 2: 성과 통계
# ================================================================
st.divider()
st.subheader("📊 성과 통계")

rets     = trades["수익률(%)"]
wins     = rets[rets > 0]
losses   = rets[rets <= 0]
win_rate = len(wins) / len(trades) * 100

c1, c2, c3, c4 = st.columns(4)
c1.metric("총 매매 횟수",         f"{len(trades)}건")
c2.metric("승률",                 f"{win_rate:.1f}%", f"{len(wins)}승 {len(losses)}패")
c3.metric("누적 수익률 (단리 합산)", f"{rets.sum():+.2f}%")
c4.metric("평균 보유일",           f"{trades['보유일'].mean():.1f}일")

st.dataframe(
    trades.style.format({"진입가": "{:,.0f}", "청산가": "{:,.0f}", "수익률(%)": "{:+.2f}"}),
    use_container_width=True,
)
