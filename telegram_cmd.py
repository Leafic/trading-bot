# ==============================================================
#  telegram_cmd.py — 텔레그램 양방향 명령어 리스너
#  /잔고, /목록 명령어를 수신해 봇이 답장합니다.
#  requests 롱폴링 방식 — 추가 패키지 불필요.
#  보안: TELEGRAM_CHAT_ID와 일치하는 발신자만 처리합니다.
# ==============================================================

import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


# ================================================================
# 내부 헬퍼
# ================================================================
def _send(text: str) -> None:
    """CHAT_ID로 텔레그램 메시지를 발송합니다."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"  ⚠️ [TG] 답장 실패: {e}")


def _format_balance(balance: dict) -> str:
    """잔고 dict를 가독성 높은 텍스트로 변환합니다."""
    if not balance:
        return "잔고 데이터가 없습니다.\n봇을 시작하거나 장 중에 다시 시도하세요."

    lines = [
        f"💰 잔고 현황",
        f"📅 기준: {balance.get('last_updated', '—')}",
        f"총 평가금액: {balance.get('tot_evlu_amt', 0):,}원",
        f"총 수익률:   {balance.get('profit_rate', 0):.2f}%",
        "",
    ]
    for h in balance.get("holdings", []):
        sign = "+" if h["profit_rate"] >= 0 else ""
        lines.append(
            f"• {h['name']} ({h['symbol']})\n"
            f"  보유 {h['qty']}주 | 평단 {h['avg_price']:,.0f}원\n"
            f"  현재 {h['current_price']:,}원 ({sign}{h['profit_rate']:.2f}%)"
        )
    if balance.get("is_cached"):
        lines.append("\n🌙 야간/휴장: 마지막 장 마감 기준 스냅샷")
    return "\n".join(lines)


def _format_analysis(stocks_data: dict, query: str) -> str:
    """종목코드 또는 종목명으로 기술적 지표를 조회합니다. 감시 목록 캐시에서 반환합니다."""
    # 코드로 직접 조회 → 없으면 이름으로 검색
    data   = stocks_data.get(query)
    sym    = query
    if data is None:
        for s, d in stocks_data.items():
            if d.get("name", "") == query:
                data, sym = d, s
                break
    if data is None:
        return (
            f"'{query}' 종목이 감시 목록에 없습니다.\n"
            "사이드바에서 추가하면 다음 체크 후 분석 가능합니다."
        )
    name     = data.get("name", sym)
    price    = data.get("price", 0)
    rsi      = data.get("rsi")
    sma20    = data.get("sma20")
    bb_lower = data.get("bb_lower")
    vol_pct  = data.get("vol_pct")
    updated  = data.get("last_updated", "—")

    sma20_diff = f"{(price - sma20) / sma20 * 100:+.1f}%" if sma20 else "N/A"
    rsi_str    = f"{rsi}"
    if rsi:
        if rsi <= 30:   rsi_str += " ⚠️ 과매도"
        elif rsi >= 70: rsi_str += " ⚠️ 과매수"
    return "\n".join([
        f"📊 [{name}] ({sym}) 기술적 분석",
        f"📅 {updated}",
        "",
        f"현재가:  {price:,}원",
        f"RSI(14): {rsi_str}",
        f"SMA20:   {f'{sma20:,.0f}원 ({sma20_diff})' if sma20 else 'N/A'}",
        f"BB하단:  {f'{bb_lower:,.0f}원' if bb_lower else 'N/A'}",
        f"거래량:  {f'{vol_pct:.0f}% (평균대비)' if vol_pct else 'N/A'}",
    ])


def _format_watchlist(watch_list: dict) -> str:
    """감시 종목 dict를 텍스트로 변환합니다."""
    if not watch_list:
        return "감시 중인 종목이 없습니다."
    lines = [f"👁 감시 종목 ({len(watch_list)}개)"]
    for sym, info in watch_list.items():
        tag = "🏦" if info.get("is_holding") else "👁"
        tgt = f" | 목표: {info['target_price']:,}원" if info.get("target_price") else ""
        lines.append(f"{tag} {info.get('name', sym)} ({sym}){tgt}")
    return "\n".join(lines)


# ================================================================
# 폴링 루프
# ================================================================
def _poll_loop(
    get_shared_snapshot,          # () -> (balance: dict, watch_list: dict, stocks_data: dict)
    stop_event: threading.Event,
) -> None:
    """
    getUpdates 롱폴링으로 명령어를 수신합니다.
    stop_event가 set되면 루프를 종료합니다.
    지원 명령어: /잔고, /목록, /분석 {종목코드|종목명}
    """
    last_id = 0
    print("[TG] 텔레그램 명령어 리스너 시작")

    while not stop_event.is_set():
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_id + 1, "timeout": 30},
                timeout=35,
            )
            updates = resp.json().get("result", [])

            for update in updates:
                last_id = update["update_id"]
                msg     = update.get("message", {})
                from_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                # 보안: 등록된 CHAT_ID 이외의 발신자는 무시
                if from_id != str(CHAT_ID):
                    print(f"[TG] 미등록 발신자 무시: {from_id}")
                    continue

                balance, watch_list, stocks_data = get_shared_snapshot()

                if text == "/잔고":
                    _send(_format_balance(balance))
                elif text == "/목록":
                    _send(_format_watchlist(watch_list))
                elif text.startswith("/분석"):
                    parts = text.split(maxsplit=1)
                    query = parts[1].strip() if len(parts) > 1 else ""
                    if query:
                        _send(_format_analysis(stocks_data, query))
                    else:
                        _send("사용법: /분석 {종목코드 또는 종목명}\n예시: /분석 005930")

        except Exception as e:
            print(f"  ⚠️ [TG] 폴링 오류: {e}")
            # 오류 시 짧게 대기 후 재시도
            for _ in range(5):
                if stop_event.is_set():
                    break
                time.sleep(1)

    print("[TG] 텔레그램 명령어 리스너 종료")


# ================================================================
# 공개 진입점
# ================================================================
def start_telegram_listener(
    get_shared_snapshot,
    stop_event: threading.Event,
) -> threading.Thread:
    """
    텔레그램 명령어 리스너 스레드를 시작합니다.
    get_shared_snapshot: () -> (balance: dict, watch_list: dict, stocks_data: dict) 콜백
    """
    t = threading.Thread(
        target=_poll_loop,
        args=(get_shared_snapshot, stop_event),
        daemon=True,
        name="telegram-listener",
    )
    t.start()
    return t
