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
    get_shared_snapshot,          # () -> (balance: dict, watch_list: dict)
    stop_event: threading.Event,
) -> None:
    """
    getUpdates 롱폴링으로 명령어를 수신합니다.
    stop_event가 set되면 루프를 종료합니다.
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

                balance, watch_list = get_shared_snapshot()

                if text == "/잔고":
                    _send(_format_balance(balance))
                elif text == "/목록":
                    _send(_format_watchlist(watch_list))

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
    get_shared_snapshot: () -> (balance: dict, watch_list: dict) 콜백
    """
    t = threading.Thread(
        target=_poll_loop,
        args=(get_shared_snapshot, stop_event),
        daemon=True,
        name="telegram-listener",
    )
    t.start()
    return t
