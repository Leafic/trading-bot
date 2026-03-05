# ==============================================================
#  pages/1_백테스트.py — 과거 데이터 기반 알림 규칙 검증 뷰어
#  감시 종목의 OHLCV를 내려받아 각 알림 규칙 발동 지점과
#  발동 후 +1/+3/+5 거래일 수익률을 테이블로 표시합니다.
# ==============================================================

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from ta.momentum   import RSIIndicator
from ta.trend      import SMAIndicator
from ta.volatility import BollingerBands

# ── 환경 설정 ──────────────────────────────────────────────────
st.set_page_config(page_title="백테스트 뷰어", page_icon="📈", layout="wide")
st.title("📈 알림 규칙 백테스트")
st.caption("과거 데이터에서 각 알림 규칙이 발동된 날짜와 이후 수익률을 확인합니다.")

WATCHLIST_FILE = Path("watchlist.json")


# ================================================================
# 지표 계산 (strategy.py의 calculate_indicators와 동일 로직)
# ================================================================
def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """SMA5/20, 거래량 SMA20, BB하단, RSI를 컬럼으로 추가합니다."""
    if len(df) < 20:
        return df
    df = df.copy()
    df["sma5"]      = SMAIndicator(close=df["close"], window=5).sma_indicator()
    df["sma20"]     = SMAIndicator(close=df["close"], window=20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(window=20).mean()
    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_lower"]  = bb.bollinger_lband()
    df["rsi"]       = RSIIndicator(close=df["close"], window=14).rsi()
    return df


# ================================================================
# 규칙 판정 함수들 (행 단위)
# ================================================================
def _rule_a(row: pd.Series, _prev: Optional[pd.Series]) -> bool:
    """Rule A: RSI ≤ 30 (당일 새로 진입)"""
    if pd.isna(row.get("rsi")):
        return False
    prev_rsi = _prev["rsi"] if _prev is not None and not pd.isna(_prev.get("rsi", float("nan"))) else 31
    return bool(row["rsi"] <= 30 and prev_rsi > 30)


def _rule_c(row: pd.Series, _prev: Optional[pd.Series]) -> bool:
    """Rule C: RSI ≤ 30 AND 현재가 ≤ BB하단"""
    return bool(
        not pd.isna(row.get("rsi")) and
        not pd.isna(row.get("bb_lower")) and
        row["rsi"] <= 30 and
        row["close"] <= row["bb_lower"]
    )


def _rule_d(row: pd.Series, _prev: Optional[pd.Series]) -> bool:
    """Rule D: 거래량 ≥ 3× SMA20 AND 현재가 > SMA20"""
    return bool(
        not pd.isna(row.get("vol_sma20")) and
        not pd.isna(row.get("sma20")) and
        row["vol_sma20"] > 0 and
        row["volume"] >= row["vol_sma20"] * 3 and
        row["close"] > row["sma20"]
    )


def _rule_e(row: pd.Series, prev: Optional[pd.Series]) -> bool:
    """Rule E: 데드크로스 (SMA5 ↓ SMA20) + 거래량 전일 대비 증가"""
    if prev is None:
        return False
    for key in ("sma5", "sma20"):
        if pd.isna(row.get(key)) or pd.isna(prev.get(key)):
            return False
    dead = prev["sma5"] >= prev["sma20"] and row["sma5"] < row["sma20"]
    vol_inc = row["volume"] > prev["volume"]
    return bool(dead and vol_inc)


def _rule_h(row: pd.Series, prev: Optional[pd.Series]) -> bool:
    """Rule H: 골든크로스 (SMA5 ↑ SMA20) + 거래량 전일 대비 증가"""
    if prev is None:
        return False
    for key in ("sma5", "sma20"):
        if pd.isna(row.get(key)) or pd.isna(prev.get(key)):
            return False
    golden = prev["sma5"] < prev["sma20"] and row["sma5"] >= row["sma20"]
    vol_inc = row["volume"] > prev["volume"]
    return bool(golden and vol_inc)


RULES = {
    "A: RSI 과매도":    _rule_a,
    "C: 스나이퍼 바닥": _rule_c,
    "D: 수급 폭발":     _rule_d,
    "E: 데드크로스":    _rule_e,
    "H: 골든크로스":    _rule_h,
}


# ================================================================
# 백테스트 실행
# ================================================================
def run_backtest(df: pd.DataFrame, rule_func, rule_name: str) -> pd.DataFrame:
    """
    df 전체를 순회하며 rule_func이 True인 날을 발동일로 기록합니다.
    이후 +1/+3/+5 거래일 종가 대비 수익률을 계산합니다.
    """
    df = df.reset_index(drop=True)
    records = []

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        if not rule_func(row, prev):
            continue

        entry_price = row["close"]
        if entry_price <= 0:
            continue

        def _ret(offset: int) -> Optional[float]:
            """발동일 이후 offset 거래일 수익률(%)을 계산합니다."""
            idx = i + offset
            if idx < len(df) and not pd.isna(df.iloc[idx]["close"]):
                return round((df.iloc[idx]["close"] - entry_price) / entry_price * 100, 2)
            return None

        records.append({
            "발동일":     row.get("date", ""),
            "발동가":     f"{int(entry_price):,}원",
            "+1일(%)":   _ret(1),
            "+3일(%)":   _ret(3),
            "+5일(%)":   _ret(5),
            "RSI":        round(row["rsi"], 1) if not pd.isna(row.get("rsi", float("nan"))) else None,
        })

    return pd.DataFrame(records)


# ================================================================
# UI — 사이드바 설정
# ================================================================
@st.cache_data(show_spinner=False)
def _load_watchlist() -> dict:
    """watchlist.json에서 감시 종목을 로드합니다."""
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


wl = _load_watchlist()

with st.sidebar:
    st.header("⚙️ 설정")

    if not wl:
        st.warning("감시 종목이 없습니다.\n메인 페이지에서 종목을 추가하세요.")
        st.stop()

    sym_options = {f"{info.get('name', sym)} ({sym})": sym for sym, info in wl.items()}
    selected_label = st.selectbox("종목 선택", list(sym_options.keys()))
    selected_sym   = sym_options[selected_label]

    days_back = st.slider("조회 기간 (거래일 기준 약)", min_value=60, max_value=365, value=120, step=30)

    selected_rules = st.multiselect(
        "검증할 규칙",
        options=list(RULES.keys()),
        default=list(RULES.keys()),
    )

    run_btn = st.button("▶ 백테스트 실행", type="primary", use_container_width=True)

# ================================================================
# 메인 — 백테스트 실행
# ================================================================
if not run_btn:
    st.info("왼쪽 사이드바에서 종목과 규칙을 선택한 뒤 **[▶ 백테스트 실행]** 버튼을 누르세요.")
    st.stop()

# API를 직접 호출하는 대신, 캐싱된 api_handler를 활용
with st.spinner("과거 데이터 수집 중..."):
    try:
        from api_handler import create_broker, get_ohlcv_dataframe
        broker = create_broker()
        df_raw = get_ohlcv_dataframe(broker, selected_sym, days=days_back)
    except Exception as e:
        st.error(f"데이터 조회 실패: {e}")
        st.stop()

if df_raw.empty:
    st.warning("OHLCV 데이터를 가져오지 못했습니다. 종목 코드와 API 연결을 확인하세요.")
    st.stop()

df_ind = _add_indicators(df_raw)

st.subheader(f"📊 {selected_label} — 최근 {len(df_ind)}일 데이터")

# 가격 차트
with st.expander("가격 차트 (SMA5 / SMA20)", expanded=True):
    chart_df = df_ind[["date", "close", "sma5", "sma20"]].dropna(subset=["close"])
    chart_df = chart_df.set_index("date")
    st.line_chart(chart_df, height=280)

# ── 규칙별 결과 탭 ──────────────────────────────────────────────
if not selected_rules:
    st.info("검증할 규칙을 하나 이상 선택하세요.")
    st.stop()

tabs = st.tabs(selected_rules)
summary_rows = []

for tab, rule_name in zip(tabs, selected_rules):
    rule_func = RULES[rule_name]
    result_df = run_backtest(df_ind, rule_func, rule_name)

    with tab:
        if result_df.empty:
            st.info("해당 기간에 규칙이 발동된 적이 없습니다.")
            continue

        # 적중률 계산 (+1/+3/+5일 양수 비율)
        def _hit_rate(col: str) -> str:
            vals = result_df[col].dropna()
            if vals.empty:
                return "—"
            rate = (vals > 0).sum() / len(vals) * 100
            avg  = vals.mean()
            return f"{rate:.0f}% (평균 {avg:+.2f}%)"

        col1, col2, col3 = st.columns(3)
        col1.metric("+1일 적중률", _hit_rate("+1일(%)"))
        col2.metric("+3일 적중률", _hit_rate("+3일(%)"))
        col3.metric("+5일 적중률", _hit_rate("+5일(%)"))

        # 색상 함수: 양수=녹색, 음수=빨강
        def _color(val):
            if val is None or not isinstance(val, (int, float)):
                return ""
            return "color: green" if val > 0 else ("color: red" if val < 0 else "")

        styled = (
            result_df.style
            .applymap(_color, subset=["+1일(%)", "+3일(%)", "+5일(%)"])
            .format(na_rep="—", precision=2)
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # 요약 수집
        vals_5 = result_df["+5일(%)"].dropna()
        summary_rows.append({
            "규칙":         rule_name,
            "발동 횟수":    len(result_df),
            "+5일 적중률":  f"{(vals_5 > 0).sum() / len(vals_5) * 100:.0f}%" if len(vals_5) > 0 else "—",
            "+5일 평균(%)": round(vals_5.mean(), 2) if len(vals_5) > 0 else None,
        })

# ── 종합 요약 ───────────────────────────────────────────────────
if summary_rows:
    st.divider()
    st.subheader("📋 규칙별 종합 요약")
    st.dataframe(
        pd.DataFrame(summary_rows).set_index("규칙"),
        use_container_width=True,
    )
