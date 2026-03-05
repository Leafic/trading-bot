# ==============================================================
#  pages/1_백테스트.py — 과거 데이터 기반 알림 규칙 검증 뷰어
#  감시 종목의 OHLCV를 내려받아 각 알림 규칙 발동 지점과
#  발동 후 +1/+3/+5 거래일 수익률을 테이블로 표시합니다.
# ==============================================================

import json
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from ta.momentum   import RSIIndicator
from ta.trend      import SMAIndicator
from ta.volatility import BollingerBands

# ── 환경 설정 ──────────────────────────────────────────────────
st.set_page_config(page_title="백테스트 뷰어", page_icon="📈", layout="wide")
st.title("📈 알림 규칙 백테스트")
st.caption("과거 데이터에서 각 알림 규칙이 발동된 날짜와 이후 수익률을 확인합니다.")

WATCHLIST_FILE = Path("watchlist.json")


# ================================================================
# 지표 계산 — strategy.py의 calculate_indicators와 동일 로직
# ================================================================
def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """SMA5/20/60/120, 거래량 SMA20, BB 상단/중간/하단, RSI를 추가합니다."""
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
    df["rsi"]       = RSIIndicator(close=df["close"], window=14).rsi()
    if len(df) >= 60:
        df["sma60"]  = SMAIndicator(close=df["close"], window=60).sma_indicator()
    if len(df) >= 120:
        df["sma120"] = SMAIndicator(close=df["close"], window=120).sma_indicator()
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
    """Rule C: RSI ≤ 30 AND 현재가 ≤ BB하단 (스나이퍼 바닥)"""
    return bool(
        not pd.isna(row.get("rsi")) and
        not pd.isna(row.get("bb_lower")) and
        row["rsi"] <= 30 and
        row["close"] <= row["bb_lower"]
    )


def _rule_d(row: pd.Series, _prev: Optional[pd.Series]) -> bool:
    """Rule D: 거래량 ≥ 3× SMA20 AND 현재가 > SMA20 (수급 폭발)"""
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
    return bool(dead and row["volume"] > prev["volume"])


def _rule_h(row: pd.Series, prev: Optional[pd.Series]) -> bool:
    """Rule H: 골든크로스 (SMA5 ↑ SMA20) + 거래량 전일 대비 증가"""
    if prev is None:
        return False
    for key in ("sma5", "sma20"):
        if pd.isna(row.get(key)) or pd.isna(prev.get(key)):
            return False
    golden = prev["sma5"] < prev["sma20"] and row["sma5"] >= row["sma20"]
    return bool(golden and row["volume"] > prev["volume"])


def _rule_i(row: pd.Series, prev: Optional[pd.Series]) -> bool:
    """Rule I: 중기 골든크로스 (SMA20 ↑ SMA60)"""
    if prev is None:
        return False
    for key in ("sma20", "sma60"):
        if pd.isna(row.get(key)) or pd.isna(prev.get(key)):
            return False
    return bool(prev["sma20"] < prev["sma60"] and row["sma20"] >= row["sma60"])


def _rule_j(row: pd.Series, _prev: Optional[pd.Series]) -> bool:
    """Rule J: 장기 지지선 반등 (현재가 SMA120 ±3% + RSI ≤ 45)"""
    if pd.isna(row.get("sma120")) or pd.isna(row.get("rsi")):
        return False
    if row["sma120"] <= 0:
        return False
    dist = (row["close"] - row["sma120"]) / row["sma120"] * 100
    return bool(-3.0 <= dist <= 3.0 and row["rsi"] <= 45)


RULES = {
    "A: RSI 과매도":      _rule_a,
    "C: 스나이퍼 바닥":   _rule_c,
    "D: 수급 폭발":       _rule_d,
    "E: 데드크로스":      _rule_e,
    "H: 골든크로스(단기)": _rule_h,
    "I: 골든크로스(중기)": _rule_i,
    "J: 장기 지지 반등":  _rule_j,
}

# 하락 기대 전략: 수익률 음수가 적중
BEARISH_RULES = {"E: 데드크로스"}


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
            idx = i + offset
            if idx < len(df) and not pd.isna(df.iloc[idx]["close"]):
                return round((df.iloc[idx]["close"] - entry_price) / entry_price * 100, 2)
            return None

        records.append({
            "발동일":    row.get("date", ""),
            "발동가":    f"{int(entry_price):,}원",
            "_price":    int(entry_price),
            "+1일(%)":  _ret(1),
            "+3일(%)":  _ret(3),
            "+5일(%)":  _ret(5),
            "RSI":       round(row["rsi"], 1) if not pd.isna(row.get("rsi", float("nan"))) else None,
        })

    return pd.DataFrame(records)


# ================================================================
# 사이드바 설정
# ================================================================
def _load_watchlist() -> dict:
    """watchlist.json을 매 로드 시 신선하게 읽습니다."""
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

    sym_options    = {f"{info.get('name', sym)} ({sym})": sym for sym, info in wl.items()}
    selected_label = st.selectbox("종목 선택", list(sym_options.keys()))
    selected_sym   = sym_options[selected_label]

    days_back = st.slider(
        "조회 기간 (거래일 기준 약)",
        min_value=60, max_value=365, value=120, step=30,
        help="SMA60 = 60일↑, SMA120 = 120일↑ 데이터 필요",
    )

    selected_rules = st.multiselect(
        "검증할 규칙",
        options=list(RULES.keys()),
        default=list(RULES.keys()),
    )

    run_btn = st.button("▶ 백테스트 실행", type="primary", use_container_width=True)

# ================================================================
# 메인 — 데이터 수집
# ================================================================
if not run_btn:
    st.info("왼쪽 사이드바에서 종목과 규칙을 선택한 뒤 **[▶ 백테스트 실행]** 버튼을 누르세요.")
    st.stop()

_bt_error: str = ""
df_raw = pd.DataFrame()

with st.spinner("과거 데이터 수집 중... (장외 시간에도 정상 동작합니다)"):
    try:
        from api_handler import get_broker, get_ohlcv_dataframe
        broker = get_broker()
        df_raw = get_ohlcv_dataframe(broker, selected_sym, days=days_back)
        if df_raw.empty:
            _bt_error = (
                f"**{selected_label}** 데이터가 비어 있습니다.\n\n"
                "**가능한 원인:**\n"
                "- `.env` 의 `APP_KEY` / `APP_SECRET` / `ACC_NO` 확인\n"
                "- 종목코드가 잘못된 경우 (watchlist.json 직접 확인)\n"
                "- KIS API 서버 일시 오류 — 잠시 후 재시도\n\n"
                f"*종목코드: `{selected_sym}`  |  조회 기간: {days_back}일*"
            )
    except Exception as e:
        _bt_error = (
            f"**API 연결 오류:** `{e}`\n\n"
            "`.env` 파일의 인증 키와 네트워크 연결을 확인하세요."
        )

if _bt_error:
    st.error(_bt_error)
    st.stop()

df_ind = _add_indicators(df_raw)
df_ind["_dt"] = pd.to_datetime(df_ind["date"].astype(str))

st.subheader(f"📊 {selected_label} — 최근 {len(df_ind)}일 데이터")

# ================================================================
# 메인 차트: 가격 + 이동평균 + BB 밴드 / RSI 서브플롯
# ================================================================
with st.expander("📈 기술 지표 차트", expanded=True):
    has_sma60  = "sma60"  in df_ind.columns and df_ind["sma60"].notna().any()
    has_sma120 = "sma120" in df_ind.columns and df_ind["sma120"].notna().any()
    has_bb     = "bb_upper" in df_ind.columns and df_ind["bb_upper"].notna().any()
    has_rsi    = "rsi" in df_ind.columns and df_ind["rsi"].notna().any()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
        subplot_titles=("가격 · 이동평균 · 볼린저밴드", "RSI (14)"),
    )

    # ── 색상 팔레트 정의 ──────────────────────────────────────────
    # 단기선은 따뜻한 색(yellow→orange), 장기선은 차가운 색(green→blue)
    # BB는 황금색 계열로 MA와 구분
    C_PRICE  = "#FFFFFF"                     # 종가: 흰색 (가장 눈에 띄게)
    C_SMA5   = "#FFD166"                     # SMA5: 황금 노랑 (단기, dotted)
    C_SMA20  = "#FF6B6B"                     # SMA20: 코랄 레드 (단기-중기, dashed)
    C_SMA60  = "#26de81"                     # SMA60: 에메랄드 그린 (중기)
    C_SMA120 = "#45AAF2"                     # SMA120: 하늘 파랑 (장기, 굵게)
    C_BB_FILL    = "rgba(253,203,110,0.09)"  # BB 내부: 연한 황금 음영
    C_BB_BORDER  = "rgba(253,203,110,0.35)"  # BB 상단/하단 테두리
    C_BB_MID     = "rgba(253,203,110,0.65)"  # BB 중간선 (dotted)
    C_RSI        = "#A29BFE"                 # RSI: 라벤더 (보조 영역)
    C_OB         = "#FF7675"                 # 과매수 기준선
    C_OS         = "#55EFC4"                 # 과매도 기준선

    # ── BB 밴드 (황금 음영) ───────────────────────────────────────
    if has_bb:
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["bb_upper"],
            name="BB상단", line=dict(color=C_BB_BORDER, width=1),
            showlegend=False, hoverinfo="skip",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["bb_lower"],
            name="볼린저밴드",
            line=dict(color=C_BB_BORDER, width=1),
            fill="tonexty",
            fillcolor=C_BB_FILL,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["bb_mid"],
            name="BB중심", line=dict(color=C_BB_MID, width=1, dash="dot"),
        ), row=1, col=1)

    # ── 이동평균선 (장기 → 단기 순으로 추가해 단기가 위에 그려짐) ──
    if has_sma120:
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["sma120"],
            name="SMA120", line=dict(color=C_SMA120, width=2),
        ), row=1, col=1)
    if has_sma60:
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["sma60"],
            name="SMA60", line=dict(color=C_SMA60, width=1.5),
        ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_ind["_dt"], y=df_ind["sma20"],
        name="SMA20", line=dict(color=C_SMA20, width=1.2, dash="dash"),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_ind["_dt"], y=df_ind["sma5"],
        name="SMA5", line=dict(color=C_SMA5, width=1, dash="dot"),
    ), row=1, col=1)

    # ── 종가 (최상단 레이어) ──────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df_ind["_dt"], y=df_ind["close"],
        name="종가", line=dict(color=C_PRICE, width=2.5),
    ), row=1, col=1)

    # ── RSI 서브플롯 ──────────────────────────────────────────────
    if has_rsi:
        fig.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["rsi"],
            name="RSI(14)", line=dict(color=C_RSI, width=1.5),
            fill="tozeroy", fillcolor="rgba(162,155,254,0.08)",
        ), row=2, col=1)
        for y_val, color, lbl in ((70, C_OB, "과매수 70"), (30, C_OS, "과매도 30")):
            fig.add_hline(
                y=y_val, line=dict(color=color, width=1, dash="dot"),
                annotation_text=lbl, annotation_position="left",
                row=2, col=1,
            )

    fig.update_layout(
        height=520,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(
            orientation="h", y=1.05, x=0,
            font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,17,27,0.6)",
        xaxis2=dict(tickformat="%Y-%m-%d", tickangle=-30, gridcolor="rgba(255,255,255,0.07)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.07)"),
        yaxis=dict(tickformat=",", gridcolor="rgba(255,255,255,0.07)"),
        yaxis2=dict(
            range=[0, 100], tickvals=[0, 30, 50, 70, 100],
            gridcolor="rgba(255,255,255,0.07)",
        ),
    )
    st.plotly_chart(fig, use_container_width=True, key="chart_main")

# ================================================================
# 적중률 설명
# ================================================================
with st.expander("❓ 적중률(%) 읽는 법"):
    st.info(
        "**적중률** = 규칙 발동 후 N거래일째 종가가 **발동가보다 높은 비율** (매수 기대 전략 기준)  \n\n"
        "예) **+5일 적중률 70% (평균 +3.2%)** → 규칙 발동 후 5거래일째 주가가 오른 경우가 "
        "10번 중 7번, 평균 수익률은 +3.2%  \n\n"
        "⚠️ **E: 데드크로스**는 하락 기대 전략 → 수익률 **음수일수록** 예측 적중  \n"
        "⚠️ I/J 규칙은 **120일↑ 데이터**가 있어야 발동됩니다.  \n"
        "⚠️ 과거 데이터 기반 통계이며 미래 수익을 보장하지 않습니다."
    )

# ================================================================
# 백테스트 실행 (탭 이전에 모두 실행 → 비교 테이블 먼저 표시)
# ================================================================
if not selected_rules:
    st.info("검증할 규칙을 하나 이상 선택하세요.")
    st.stop()

all_results: dict = {}
for rule_name in selected_rules:
    all_results[rule_name] = run_backtest(df_ind, RULES[rule_name], rule_name)

# ================================================================
# 전략별 비교 요약 (상단)
# ================================================================
st.divider()
st.subheader("📋 전략별 비교 요약")
st.caption("발동 횟수 0 = 해당 기간 내 규칙이 한 번도 발동되지 않음")

summary_rows = []
for rule_name, result_df in all_results.items():
    v1 = result_df["+1일(%)"].dropna() if not result_df.empty else pd.Series(dtype=float)
    v3 = result_df["+3일(%)"].dropna() if not result_df.empty else pd.Series(dtype=float)
    v5 = result_df["+5일(%)"].dropna() if not result_df.empty else pd.Series(dtype=float)
    bearish = rule_name in BEARISH_RULES

    def _fmt(vals: pd.Series) -> str:
        if vals.empty:
            return "—"
        hits = (vals < 0).sum() if bearish else (vals > 0).sum()
        return f"{hits/len(vals)*100:.0f}% (평균 {vals.mean():+.1f}%)"

    summary_rows.append({
        "전략":        rule_name,
        "발동 횟수":   len(result_df),
        "+1일 적중률": _fmt(v1),
        "+3일 적중률": _fmt(v3),
        "+5일 적중률": _fmt(v5),
    })

st.dataframe(
    pd.DataFrame(summary_rows).set_index("전략"),
    use_container_width=True,
)

# ================================================================
# 전략별 상세 탭
# ================================================================
st.divider()
st.subheader("🔍 전략별 상세")

tabs = st.tabs(selected_rules)

for tab, rule_name in zip(tabs, selected_rules):
    result_df  = all_results[rule_name]
    is_bearish = rule_name in BEARISH_RULES

    with tab:
        if result_df.empty:
            st.info("해당 기간에 규칙이 발동된 적이 없습니다.")
            continue

        # ── 발동 시점 마커 차트 ─────────────────────────────────
        trigger_dt    = pd.to_datetime(result_df["발동일"].astype(str))
        trigger_price = result_df["_price"]

        fig_tab = go.Figure()

        # BB 밴드 (황금 음영 — 메인 차트와 동일 팔레트)
        if has_bb:
            fig_tab.add_trace(go.Scatter(
                x=df_ind["_dt"], y=df_ind["bb_upper"],
                line=dict(color="rgba(253,203,110,0.35)", width=1),
                showlegend=False, hoverinfo="skip",
            ))
            fig_tab.add_trace(go.Scatter(
                x=df_ind["_dt"], y=df_ind["bb_lower"],
                name="BB밴드", line=dict(color="rgba(253,203,110,0.35)", width=1),
                fill="tonexty", fillcolor="rgba(253,203,110,0.08)",
            ))

        if has_sma120:
            fig_tab.add_trace(go.Scatter(
                x=df_ind["_dt"], y=df_ind["sma120"],
                name="SMA120", line=dict(color="#45AAF2", width=1.5),
            ))
        if has_sma60:
            fig_tab.add_trace(go.Scatter(
                x=df_ind["_dt"], y=df_ind["sma60"],
                name="SMA60", line=dict(color="#26de81", width=1.2),
            ))
        fig_tab.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["sma20"],
            name="SMA20", line=dict(color="#FF6B6B", width=1, dash="dash"),
        ))
        fig_tab.add_trace(go.Scatter(
            x=df_ind["_dt"], y=df_ind["close"],
            name="종가", line=dict(color="#FFFFFF", width=2),
        ))
        fig_tab.add_trace(go.Scatter(
            x=trigger_dt, y=trigger_price,
            mode="markers", name="발동 시점",
            marker=dict(
                symbol="triangle-down" if is_bearish else "triangle-up",
                size=14,
                color="#FF4444" if is_bearish else "#FFD700",
                line=dict(color="white", width=1),
            ),
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:,}원<extra>발동</extra>",
        ))
        fig_tab.update_layout(
            height=260,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation="h", y=1.12, x=0, font=dict(size=11), bgcolor="rgba(0,0,0,0)"),
            xaxis=dict(tickformat="%Y-%m-%d", tickangle=-30, gridcolor="rgba(255,255,255,0.07)"),
            yaxis=dict(tickformat=",", gridcolor="rgba(255,255,255,0.07)"),
            hovermode="x unified",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(17,17,27,0.6)",
        )
        st.plotly_chart(fig_tab, use_container_width=True, key=f"chart_tab_{rule_name}")

        # ── 적중률 메트릭 ───────────────────────────────────────
        def _hit_rate(col: str) -> str:
            vals = result_df[col].dropna()
            if vals.empty:
                return "—"
            hits = (vals < 0).sum() if is_bearish else (vals > 0).sum()
            return f"{hits/len(vals)*100:.0f}% (평균 {vals.mean():+.2f}%)"

        col1, col2, col3 = st.columns(3)
        col1.metric("+1일 적중률", _hit_rate("+1일(%)"))
        col2.metric("+3일 적중률", _hit_rate("+3일(%)"))
        col3.metric("+5일 적중률", _hit_rate("+5일(%)"))

        if is_bearish:
            st.caption("⚠️ 하락 기대 전략 — 수익률 음수일수록 예측 적중입니다.")

        # ── 상세 테이블 ─────────────────────────────────────────
        def _color(val):
            if not isinstance(val, (int, float)):
                return ""
            return "color: green" if val > 0 else ("color: red" if val < 0 else "")

        display_df = result_df.drop(columns=["_price"])
        styled = (
            display_df.style
            .applymap(_color, subset=["+1일(%)", "+3일(%)", "+5일(%)"])
            .format(na_rep="—", precision=2)
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
