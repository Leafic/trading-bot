# ==============================================================
#  app.py — Streamlit 대시보드 & 봇 백그라운드 스레드 진입점
#  실행: streamlit run app.py
#  필수: .env 파일 (APP_KEY, APP_SECRET, ACC_NO, TELEGRAM_*)
# ==============================================================

import json
import threading
import time
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="트레이딩 알림 봇",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 외부 패키지 및 프로젝트 모듈 임포트
# 패키지 미설치 시 친절한 에러 메시지를 먼저 보여주기 위해 try-except로 감쌉니다.
try:
    from api_handler import (
        IS_MOCK,
        create_broker,
        get_balance,
        build_watch_list,
        get_stock_name,
        is_market_open,
        market_closed_reason,
    )
    from strategy import check_and_alert, RSI_PERIOD
    from ai_analyst import daily_briefing
    from telegram_cmd import start_telegram_listener
    from utils import (
        STATUS_FILE,
        load_watchlist,
        save_watchlist,
        load_status_flags,
        send_telegram,
        _is_token_error,
        _is_rate_limit,
    )
except ImportError as _imp_err:
    st.error(
        f"필수 패키지가 없습니다: {_imp_err}\n"
        "`pip install -r requirements.txt` 실행 후 재시작하세요."
    )
    st.stop()


# ================================================================
# 상수
# ================================================================
LOOP_INTERVAL = 180   # 메인 루프 간격 (초)
LOG_MAX       = 300   # 최대 로그 보관 줄 수


# ================================================================
# 영속 봇 상태 (@st.cache_resource — Streamlit 서버 수명 동안 유지)
# ================================================================
@st.cache_resource
def _create_bot_state() -> dict:
    """
    봇의 전역 상태를 생성합니다.
    @st.cache_resource 덕분에 Streamlit 리런 사이에서도 객체가 유지됩니다.
    """
    return {
        "lock":       threading.Lock(),
        "stop_event": threading.Event(),
        "thread":     None,          # 봇 메인 스레드
        "tg_thread":  None,          # 텔레그램 명령어 리스너 스레드
        "shared": {
            "balance":     {},       # 잔고 dict
            "stocks":      {},       # {symbol: {name, price, rsi, ...}}
            "alert_flags": {},       # 중복 알림 방지 플래그
            "logs":        [],       # 실행 로그
            "last_check":  "—",
            "watch_list":        {},  # 현재 감시 중인 종목 {symbol: {name, target_price, is_holding}}
            "briefing_done_date": "", # 일일 AI 브리핑 발송 완료 날짜 (YYYY-MM-DD)
        },
    }


_bot_state = _create_bot_state()


# ================================================================
# Status JSON 스냅샷
# _bot_state 전역에 직접 접근하므로 app.py에 유지합니다.
# ================================================================
def flush_status() -> None:
    """공유 상태를 status.json에 스냅샷합니다 (프로그램 재시작 시 복구용)."""
    lock   = _bot_state["lock"]
    shared = _bot_state["shared"]
    with lock:
        data = {
            "alert_flags":       shared["alert_flags"],
            "last_check":        shared["last_check"],
            "stocks":            shared["stocks"],
            "balance":           shared["balance"],
            "logs":              shared["logs"][-100:],
            "briefing_done_date": shared["briefing_done_date"],
        }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================================================================
# 봇 메인 루프 (백그라운드 스레드)
# ================================================================
def bot_loop(stop_event: threading.Event) -> None:
    """봇 메인 루프. _create_bot_state() 에서 얻은 공유 상태에 직접 씁니다."""
    lock   = _bot_state["lock"]
    shared = _bot_state["shared"]
    broker = None

    def slog(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        with lock:
            shared["logs"].append(line)
            if len(shared["logs"]) > LOG_MAX:
                del shared["logs"][:-LOG_MAX]

    slog("=" * 50)
    slog(f"봇 스레드 시작  |  모드: {'모의투자' if IS_MOCK else '실계좌'}")
    slog("=" * 50)

    # 이전 실행에서 저장된 상태 복구
    with lock:
        if not shared["alert_flags"]:
            shared["alert_flags"] = load_status_flags()
        # 봇 재시작 시 당일 브리핑 중복 방지를 위해 날짜 복구
        if not shared["briefing_done_date"] and STATUS_FILE.exists():
            try:
                with open(STATUS_FILE, encoding="utf-8") as _f:
                    shared["briefing_done_date"] = json.load(_f).get("briefing_done_date", "")
            except Exception:
                pass

    if not send_telegram(f"✅ 트레이딩 알림 봇 시작!\n모드: {'모의투자' if IS_MOCK else '실계좌'}"):
        slog("⚠️  텔레그램 전송 실패 (BOT_TOKEN / CHAT_ID 확인)")

    while not stop_event.is_set():

        # ── 브로커 초기화 (최초 또는 토큰 만료 후, 장 운영 여부 무관) ──
        if broker is None:
            slog("API 연결 중...")
            try:
                broker = create_broker()
                slog("API 연결 완료")
            except Exception as e:
                slog(f"⛔ API 초기화 실패: {e} — 60초 후 재시도")
                time.sleep(60)
                continue

        # ── 잔고 조회 (장 운영 여부 무관, 항상 수행) ─────────────────
        try:
            slog("잔고 조회 중...")
            balance = get_balance(broker)
            if balance:
                with lock:
                    shared["balance"] = balance
                slog(f"  총 평가: {balance['tot_evlu_amt']:,}원  "
                     f"수익률: {balance['profit_rate']:.2f}%  "
                     f"보유: {len(balance['holdings'])}종목")
                holdings = balance.get("holdings", [])
            else:
                slog("  ⏳ 잔고 조회 실패 — 이전 잔고 유지 (야간/휴장)")
                holdings = []
        except Exception as e:
            if _is_token_error(e):
                slog("⚠️  토큰 만료 감지 — 재로그인...")
                broker = None
                time.sleep(5)
                continue
            elif _is_rate_limit(e):
                slog("⚠️  Rate Limit — 60초 대기...")
                time.sleep(60)
                continue
            else:
                slog(f"❌ 잔고 조회 오류: {e} — 이전 잔고 유지")
                holdings = []

        # ── 장 운영 시간 확인 ────────────────────────────────────
        if not is_market_open():
            reason     = market_closed_reason()
            now_dt     = datetime.now()
            is_weekend = now_dt.weekday() >= 5
            wait_sec   = 3600 if is_weekend else 600
            wait_min   = wait_sec // 60
            flush_status()

            # ── 일일 AI 브리핑 (장 마감 후 16:00~17:00, 평일만, 하루 1회) ──
            today_str = now_dt.strftime("%Y-%m-%d")
            if not is_weekend and 16 <= now_dt.hour < 17:
                with lock:
                    already_done = shared["briefing_done_date"] == today_str
                if not already_done:
                    with lock:
                        stocks_snap = dict(shared["stocks"])
                        wl_snap     = dict(shared["watch_list"])
                    slog("📊 일일 AI 브리핑 생성 중...")
                    try:
                        ok = daily_briefing(stocks_snap, wl_snap)
                        if ok:
                            with lock:
                                shared["briefing_done_date"] = today_str
                            flush_status()
                            slog("  → 일일 브리핑 텔레그램 발송 완료")
                        else:
                            slog("  → 일일 브리핑 발송 실패 (보유 종목 없음 또는 API 오류)")
                    except Exception as e:
                        slog(f"  ❌ 일일 브리핑 오류: {e}")

            slog(f"장 휴장 중 ({reason}) — 잔고 갱신 완료, 다음 체크: {wait_min}분 후")
            for _ in range(wait_sec):
                if stop_event.is_set():
                    break
                time.sleep(1)
            continue

        # ── 메인 체크 사이클 (장 운영 중일 때만) ─────────────────────
        try:
            # 1. 감시 리스트 구성 (계좌 보유 + watchlist.json 합집합)
            extra_wl   = load_watchlist()
            watch_list = build_watch_list(holdings, extra_wl)

            # 2. watchlist 전용 종목 중 이름이 코드인 경우 자동 조회
            for sym, info in watch_list.items():
                if info["name"] == sym:   # 이름 미입력 → API로 조회
                    fetched = get_stock_name(broker, sym)
                    if fetched != sym:
                        info["name"] = fetched
                        if sym in extra_wl:
                            extra_wl[sym]["name"] = fetched
                            save_watchlist(extra_wl)

            with lock:
                shared["watch_list"] = watch_list

            if not watch_list:
                slog("감시 종목 없음 — 계좌에 보유 종목을 늘리거나 사이드바에서 추가하세요.")
                flush_status()
                time.sleep(60)
                continue

            # 3. 지표 체크 + 알림
            with lock:
                alert_flags = dict(shared["alert_flags"])

            stocks_data, new_logs = check_and_alert(broker, watch_list, alert_flags)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with lock:
                shared["stocks"]      = stocks_data
                shared["alert_flags"] = alert_flags
                shared["last_check"]  = now_str
                shared["logs"].extend(new_logs)
                if len(shared["logs"]) > LOG_MAX:
                    del shared["logs"][:-LOG_MAX]

            flush_status()
            slog(f"체크 완료. {LOOP_INTERVAL // 60}분 후 다음 체크...")

        except Exception as e:
            if _is_token_error(e):
                slog("⚠️  토큰 만료 감지 — 재로그인...")
                broker = None
                time.sleep(5)
                continue
            elif _is_rate_limit(e):
                slog("⚠️  Rate Limit — 60초 대기...")
                time.sleep(60)
                continue
            else:
                slog(f"❌ 예상치 못한 오류: {e}")
                time.sleep(60)
                continue

        # ── LOOP_INTERVAL 대기 (stop_event 1초 단위 체크) ─────────
        for _ in range(LOOP_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)

    send_telegram("🛑 트레이딩 알림 봇이 종료되었습니다.")
    slog("봇 스레드 종료")
    flush_status()


# ================================================================
# 봇 제어 함수
# ================================================================
def is_bot_running() -> bool:
    t = _bot_state.get("thread")
    return t is not None and t.is_alive()


def start_bot() -> None:
    """봇 메인 스레드 + 텔레그램 리스너 스레드를 함께 시작합니다."""
    if is_bot_running():
        return
    _bot_state["stop_event"].clear()

    # 봇 메인 스레드
    t = threading.Thread(
        target=bot_loop,
        args=(_bot_state["stop_event"],),
        daemon=True,
        name="trading-bot",
    )
    t.start()
    _bot_state["thread"] = t

    # 텔레그램 명령어 리스너 스레드 (/잔고, /목록)
    def _get_snapshot() -> tuple[dict, dict]:
        """UI 렌더링과 동일한 락 방식으로 공유 상태를 스냅샷합니다."""
        with _bot_state["lock"]:
            return (
                dict(_bot_state["shared"]["balance"]),
                dict(_bot_state["shared"]["watch_list"]),
            )

    _bot_state["tg_thread"] = start_telegram_listener(
        _get_snapshot, _bot_state["stop_event"]
    )


def stop_bot() -> None:
    """봇 스레드에 종료 신호를 보내고 완전히 종료될 때까지 대기합니다."""
    _bot_state["stop_event"].set()
    t = _bot_state.get("thread")
    if t and t.is_alive():
        t.join(timeout=8.0)   # 봇 루프가 완전히 종료될 때까지 대기 (최대 8초)


# ================================================================
# Streamlit UI 헬퍼
# ================================================================
def rsi_badge(rsi: float | None) -> str:
    if rsi is None:
        return "—"
    if rsi <= 30:
        return f":red[**{rsi}** ▼ 과매도]"
    if rsi >= 70:
        return f":orange[**{rsi}** ▲ 과매수]"
    return f":green[**{rsi}** ●]"


# ================================================================
# 공유 상태 스냅샷 (UI 렌더링용 — 락 안에서 복사)
# ================================================================
with _bot_state["lock"]:
    _snap_balance     = dict(_bot_state["shared"]["balance"])
    _snap_stocks      = dict(_bot_state["shared"]["stocks"])
    _snap_alert_flags = dict(_bot_state["shared"]["alert_flags"])
    _snap_logs        = list(_bot_state["shared"]["logs"])
    _snap_last_check  = _bot_state["shared"]["last_check"]
    _snap_watch_list  = dict(_bot_state["shared"]["watch_list"])

_bot_running = is_bot_running()

# ================================================================
# ── 헤더 & 컨트롤 ───────────────────────────────────────────────
# ================================================================
st.title("📈 한국투자증권 트레이딩 알림 봇")

c_state, c_start, c_stop = st.columns([4, 1, 1])

with c_state:
    mkt = "🟢 장 운영 중" if is_market_open() else "🔴 장 휴장"
    if _bot_running:
        st.success(f"봇 실행 중 (백그라운드 스레드)  ·  {mkt}")
    else:
        st.error(f"봇 정지됨  ·  {mkt}")

with c_start:
    if st.button("▶ 봇 시작", disabled=_bot_running, use_container_width=True, type="primary"):
        start_bot()
        st.toast("봇 시작됨", icon="✅")
        time.sleep(0.5)
        st.rerun()

with c_stop:
    if st.button("⏹ 봇 정지", disabled=not _bot_running, use_container_width=True):
        with st.spinner("봇 종료 중..."):
            stop_bot()   # thread.join(8s) 포함 — 반환 시 스레드 확실히 종료
        st.toast("봇 정지됨", icon="🛑")
        st.rerun()

st.caption(f"마지막 체크: **{_snap_last_check}**")

# ================================================================
# ── 섹션 1: 💰 내 계좌 요약 (포트폴리오) ─────────────────────────
# ================================================================
st.divider()
st.subheader("💰 내 계좌 요약")

if _snap_balance:
    tot_evlu   = _snap_balance.get("tot_evlu_amt", 0)
    pchs_amt   = _snap_balance.get("pchs_amt", 0)
    profit_amt = _snap_balance.get("profit_amt", 0)
    profit_rt  = _snap_balance.get("profit_rate", 0.0)

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("총 매수금액", f"{pchs_amt:,} 원")
    with m2:
        st.metric("총 평가금액", f"{tot_evlu:,} 원")
    with m3:
        sign = "+" if profit_amt >= 0 else ""
        st.metric("총 수익률", f"{profit_rt:.2f} %",
                  delta=f"{sign}{profit_amt:,} 원")

    holdings = _snap_balance.get("holdings", [])
    if holdings:
        df_h = pd.DataFrame(holdings)
        df_display = pd.DataFrame({
            "종목명":      df_h["name"],
            "종목코드":    df_h["symbol"],
            "보유수량":    df_h["qty"].astype(int),
            "평단가(원)":  df_h["avg_price"].astype(int),
            "현재가(원)":  df_h["current_price"].astype(int),
            "평가금액(원)": df_h["eval_amt"].astype(int),
            "수익률(%)":   df_h["profit_rate"].round(2),
        })
        st.dataframe(
            df_display,
            column_config={
                "보유수량":    st.column_config.NumberColumn(format="%d주"),
                "평단가(원)":  st.column_config.NumberColumn(format="%,d"),
                "현재가(원)":  st.column_config.NumberColumn(format="%,d"),
                "평가금액(원)": st.column_config.NumberColumn(format="%,d"),
                "수익률(%)":   st.column_config.NumberColumn(format="%.2f%%"),
            },
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("보유 종목이 없습니다.")

    st.caption(f"잔고 기준: {_snap_balance.get('last_updated', '—')}")
    if _snap_balance.get("is_cached"):
        st.caption("🌙 야간/휴장 시간: 마지막 장 마감 기준 잔고 스냅샷을 표시합니다.")
else:
    st.info("봇을 시작하면 실시간 잔고가 표시됩니다.")

# ================================================================
# ── 섹션 2: 📊 감시 종목 현황 ────────────────────────────────────
# ================================================================
st.divider()
st.subheader("📊 감시 종목 현황")

if _snap_stocks:
    items = list(_snap_stocks.items())
    cols  = st.columns(min(len(items), 4))

    for i, (symbol, data) in enumerate(items):
        price    = data.get("price", 0)
        rsi      = data.get("rsi")
        name     = data.get("name", symbol)
        sma20    = data.get("sma20")
        vol_pct  = data.get("vol_pct")
        bb_lower = data.get("bb_lower")

        with cols[i % 4]:
            st.metric(f"**{name}**  `{symbol}`", f"{price:,} 원")
            st.markdown(f"RSI: {rsi_badge(rsi)}")

            if sma20:
                diff = (price - sma20) / sma20 * 100
                clr  = ":green" if diff >= 0 else ":red"
                st.caption(f"SMA20: {sma20:,.0f}원 {clr}[({diff:+.1f}%)]")
            if vol_pct is not None:
                vc = ":red" if vol_pct >= 300 else (":orange" if vol_pct >= 150 else "")
                st.caption(f"거래량: {f'{vc}[{vol_pct:.0f}%]' if vc else f'{vol_pct:.0f}%'} (평균대비)")
            if bb_lower:
                bb_clr = ":red" if price <= bb_lower else ""
                st.caption(f"BB하단: {f'{bb_clr}[{bb_lower:,.0f}원]' if bb_clr else f'{bb_lower:,.0f}원'}")
            st.caption(f"업데이트: {data.get('last_updated', '—')}")

    # 지표 비교 테이블
    st.markdown("#### 지표 비교")
    rows = []
    for sym, d in _snap_stocks.items():
        price = d.get("price", 0)
        sma20 = d.get("sma20")
        rows.append({
            "종목명":           d.get("name", sym),
            "현재가(원)":       price,
            f"RSI({RSI_PERIOD})": d.get("rsi"),
            "SMA20(원)":        int(sma20) if sma20 else None,
            "BB하단(원)":       int(d["bb_lower"]) if d.get("bb_lower") else None,
            "거래량(평균대비%)": d.get("vol_pct"),
        })
    st.dataframe(
        pd.DataFrame(rows),
        column_config={
            "현재가(원)":       st.column_config.NumberColumn(format="%,d"),
            "SMA20(원)":        st.column_config.NumberColumn(format="%,d"),
            "BB하단(원)":       st.column_config.NumberColumn(format="%,d"),
            "거래량(평균대비%)": st.column_config.NumberColumn(format="%.1f%%"),
        },
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("봇을 시작하면 종목 데이터가 여기에 표시됩니다.")

# ================================================================
# ── 섹션 3: 🚨 알림 상태 ─────────────────────────────────────────
# ================================================================
st.divider()
st.subheader("🚨 알림 상태")

_RULE_SUFFIX_LABELS = {
    "rsi_oversold":  ("A", "RSI 과매도"),
    "price_target":  ("B", "💰 목표가 돌파"),
    "sniper_bottom": ("C", "🎯 바닥 포착"),
    "volume_surge":  ("D", "🚀 수급 폭발"),
    "dead_cross":    ("E", "⚠️ 데드크로스"),
    "trailing_stop": ("F", "🛡️ 트레일링 스탑"),
    "major_buying":  ("G", "🦅 쌍끌이 수급"),
}

if _snap_alert_flags:
    # symbol별로 플래그를 묶어서 expander로 표시
    by_sym: dict = {}
    for key, val in _snap_alert_flags.items():
        # key 형식: "{symbol}_{suffix}"  e.g. "314130_rsi_oversold"
        for suffix in _RULE_SUFFIX_LABELS:
            if key.endswith(f"_{suffix}"):
                sym = key[: -(len(suffix) + 1)]
                by_sym.setdefault(sym, []).append((suffix, val))
                break

    for sym, flag_list in by_sym.items():
        name      = _snap_stocks.get(sym, {}).get("name", sym)
        actives   = [s for s, v in flag_list if v]
        exp_label = (
            f"**{name}** ({sym})  —  ⚠️ {len(actives)}개 활성" if actives
            else f"**{name}** ({sym})  —  ✅ 정상"
        )
        with st.expander(exp_label, expanded=bool(actives)):
            fcols = st.columns(max(len(flag_list), 1))
            for ci, (suffix, val) in enumerate(flag_list):
                rid, rname = _RULE_SUFFIX_LABELS.get(suffix, ("?", suffix))
                with fcols[ci]:
                    if val:
                        st.warning(f"Rule **{rid}**\n\n{rname}\n\n✅ 발송됨")
                    else:
                        st.info(f"Rule **{rid}**\n\n{rname}\n\n⬜ 대기")
else:
    st.info("봇을 시작하면 알림 상태가 표시됩니다.")

# ================================================================
# ── 섹션 4: 📋 실행 로그 ─────────────────────────────────────────
# ================================================================
st.divider()
st.subheader("📋 실행 로그")

if _snap_logs:
    st.text_area(
        "",
        value="\n".join(reversed(_snap_logs[-80:])),
        height=260,
        disabled=True,
        label_visibility="collapsed",
    )
else:
    st.info("로그가 없습니다. 봇을 시작하면 여기에 출력됩니다.")

# ================================================================
# ── 사이드바: 감시 종목 관리 + 설정 ──────────────────────────────
# ================================================================
with st.sidebar:
    st.header("📋 감시 종목 관리")
    st.caption("계좌 보유 종목은 봇 시작 시 자동으로 추가됩니다.")

    # ── 종목 추가 폼 ─────────────────────────────────────────────
    with st.form("add_stock_form", clear_on_submit=True):
        st.markdown("**관심 종목 추가**")
        code_in = st.text_input("종목코드", placeholder="005930")
        name_in = st.text_input("종목명 (선택)", placeholder="삼성전자")
        tgt_in  = st.number_input(
            "익절 목표가 (원, 0=미설정)",
            min_value=0, step=100, value=0,
        )
        submitted = st.form_submit_button("+ 추가", use_container_width=True)

        if submitted and code_in.strip():
            sym  = code_in.strip()
            nm   = name_in.strip() if name_in.strip() else sym
            tgt  = int(tgt_in) if tgt_in > 0 else None
            wl   = load_watchlist()
            wl[sym] = {"name": nm, "target_price": tgt}
            save_watchlist(wl)
            st.toast(f"{sym} ({nm}) 추가됨", icon="✅")
            st.rerun()

    # ── 현재 감시 리스트 표시 ────────────────────────────────────
    st.divider()
    st.markdown("**현재 감시 리스트**")

    wl_current = load_watchlist()
    holdings_in_account = {
        h["symbol"] for h in _snap_balance.get("holdings", [])
    }

    # 계좌 보유 종목 표시
    if holdings_in_account:
        st.markdown("*계좌 보유 (자동)*")
        for sym in sorted(holdings_in_account):
            nm = _snap_stocks.get(sym, {}).get("name", sym)
            wl_tag = " + 감시목록" if sym in wl_current else ""
            st.markdown(f"🏦 `{sym}` **{nm}**{wl_tag}")

    # watchlist 전용 종목 (계좌에 없는 것)
    wl_only = {k: v for k, v in wl_current.items() if k not in holdings_in_account}
    if wl_only:
        st.markdown("*감시 목록 전용*")
        for sym, info in wl_only.items():
            nm      = info.get("name", sym)
            tgt_str = f"  목표: {info['target_price']:,}원" if info.get("target_price") else ""
            ca, cb  = st.columns([3, 1])
            with ca:
                st.markdown(f"👁 `{sym}` **{nm}**{tgt_str}")
            with cb:
                if st.button("삭제", key=f"del_{sym}", use_container_width=True):
                    wl_current.pop(sym)
                    save_watchlist(wl_current)
                    st.rerun()

    # watchlist에 있지만 계좌에도 있는 경우 — 삭제 버튼 제공
    wl_and_holding = {k: v for k, v in wl_current.items() if k in holdings_in_account}
    if wl_and_holding:
        st.markdown("*계좌 보유 + 감시 목록*")
        for sym, info in wl_and_holding.items():
            nm      = info.get("name", sym)
            tgt_str = f"  목표: {info['target_price']:,}원" if info.get("target_price") else ""
            ca, cb  = st.columns([3, 1])
            with ca:
                st.markdown(f"🏦👁 `{sym}` **{nm}**{tgt_str}")
            with cb:
                if st.button("삭제", key=f"delh_{sym}", use_container_width=True):
                    wl_current.pop(sym)
                    save_watchlist(wl_current)
                    st.rerun()

    if not holdings_in_account and not wl_current:
        st.info("감시 종목이 없습니다.\n위 폼에서 추가하거나 봇을 시작하세요.")

    # ── 알림 규칙 요약 ────────────────────────────────────────────
    st.divider()
    st.markdown("**알림 규칙 요약**")
    st.markdown(
        "- **A** RSI ≤ 30 (과매도)\n"
        "- **B** 목표가 돌파 (설정 시)\n"
        "- **C** 🎯 RSI ≤ 30 + BB하단 이탈\n"
        "- **D** 🚀 거래량 300%↑ + SMA20 돌파\n"
        "- **E** ⚠️ 데드크로스 + 거래량 증가\n"
        "- **F** 🛡️ 트레일링 스탑 (목표가 돌파 후)\n"
        "- **G** 🦅 쌍끌이 수급 (외국인+기관)"
    )

    # ── 자동 새로고침 ─────────────────────────────────────────────
    st.divider()
    st.header("⚙️ 설정")
    refresh_sec = st.selectbox(
        "자동 새로고침 간격",
        options=[0, 10, 30, 60, 120],
        index=2,
        format_func=lambda x: "사용 안 함" if x == 0 else f"{x}초",
    )
    if refresh_sec > 0:
        st.caption(f"{refresh_sec}초마다 화면이 자동으로 갱신됩니다.")
        time.sleep(refresh_sec)
        st.rerun()
