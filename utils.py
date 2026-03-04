# ==============================================================
#  utils.py — 텔레그램 알림, 파일 I/O, 범용 유틸
#  다른 모듈이 공통으로 의존하는 최하단 레이어입니다.
# ==============================================================

import json
import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

STATUS_FILE    = Path("status.json")
WATCHLIST_FILE = Path("watchlist.json")


# ================================================================
# 텔레그램
# ================================================================
def send_telegram(message: str) -> bool:
    """텔레그램으로 메시지를 전송합니다. 성공 여부를 반환합니다."""
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


# ================================================================
# 에러 판별 헬퍼
# ================================================================
def _is_token_error(e: Exception) -> bool:
    """HTTP 401 / 토큰 만료 에러인지 판별합니다."""
    s = str(e).lower()
    return any(k in s for k in ("401", "token", "expire", "unauthorized"))


def _is_rate_limit(e: Exception) -> bool:
    """HTTP 429 / Rate Limit 에러인지 판별합니다."""
    s = str(e).lower()
    return "429" in s or "too many" in s


# ================================================================
# 범용 유틸
# ================================================================
def safe_float(val) -> float | None:
    """NaN·None·변환 불가 값을 안전하게 float으로 변환합니다."""
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


# ================================================================
# 파일 I/O
# ================================================================
def load_watchlist() -> dict:
    """
    watchlist.json을 로드합니다.
    반환: {code: {"name": str, "target_price": int|None}}
    """
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_watchlist(wl: dict) -> None:
    """watchlist.json에 관심 종목을 저장합니다."""
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)


def load_status_flags() -> dict:
    """status.json에서 alert_flags를 복구합니다 (봇 재시작 시 사용)."""
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                return json.load(f).get("alert_flags", {})
        except Exception:
            pass
    return {}
