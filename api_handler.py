# ==============================================================
#  api_handler.py — 한국투자증권 API 통신 래퍼
#  mojito 라이브러리를 감싸 잔고·OHLCV·현재가 조회와
#  장 시간 확인, 에러 방어 로직을 제공합니다.
# ==============================================================

import json
import os
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import mojito
import pandas as pd
import requests
from dotenv import load_dotenv

from utils import _is_token_error, _is_rate_limit

BALANCE_SNAPSHOT_FILE = Path("balance_snapshot.json")

load_dotenv()

APP_KEY    = os.getenv("APP_KEY", "")
APP_SECRET = os.getenv("APP_SECRET", "")
ACC_NO     = os.getenv("ACC_NO", "")
IS_MOCK    = os.getenv("IS_MOCK", "True").strip().lower() == "true"


# ================================================================
# 장 운영 시간
# ================================================================
def is_market_open() -> bool:
    """한국 주식 시장 운영 시간 여부를 반환합니다 (평일 09:00~15:30)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 0) <= t <= dtime(15, 30)


def market_closed_reason() -> str:
    """장 휴장 이유를 한국어 문자열로 반환합니다."""
    now = datetime.now()
    if now.weekday() >= 5:
        days = ["월", "화", "수", "목", "금", "토", "일"]
        return f"주말 ({days[now.weekday()]}요일)"
    if now.time() < dtime(9, 0):
        return f"장 시작 전 ({now.strftime('%H:%M')})"
    return f"장 마감 후 ({now.strftime('%H:%M')})"


# ================================================================
# 브로커 팩토리
# ================================================================
def create_broker() -> mojito.KoreaInvestment:
    """한국투자증권 broker 인스턴스를 생성합니다."""
    return mojito.KoreaInvestment(
        api_key=APP_KEY,
        api_secret=APP_SECRET,
        acc_no=ACC_NO,
        mock=IS_MOCK,
    )


# ================================================================
# 잔고 조회
# ================================================================
def get_balance(broker: mojito.KoreaInvestment) -> dict | None:
    """
    잔고를 조회하고 정제된 dict를 반환합니다.
    성공 시 balance_snapshot.json에 저장, 실패 시 스냅샷에서 복구합니다.
    스냅샷 복구 데이터에는 is_cached=True 플래그가 포함됩니다.
    """
    try:
        resp    = broker.fetch_balance()
        out1    = resp.get("output1") or [{}]
        summary = out1[0] if out1 else {}
        out2    = resp.get("output2") or []

        holdings = []
        for item in out2:
            qty = int(item.get("hldg_qty", 0) or 0)
            if qty <= 0:
                continue
            holdings.append({
                "name":          item.get("prdt_name", "").strip(),
                "symbol":        item.get("pdno", "").strip(),
                "qty":           qty,
                "avg_price":     float(item.get("pchs_avg_pric", 0) or 0),
                "current_price": int(item.get("prpr", 0) or 0),
                "eval_amt":      int(item.get("evlu_amt", 0) or 0),
                "profit_amt":    int(item.get("evlu_pfls_amt", 0) or 0),
                "profit_rate":   float(item.get("evlu_erng_rt", 0) or 0),
            })

        result = {
            "tot_evlu_amt": int(summary.get("tot_evlu_amt", 0) or 0),
            "pchs_amt":     int(summary.get("pchs_amt_smtl_amt", 0) or 0),
            "profit_amt":   int(summary.get("evlu_pfls_smtl_amt", 0) or 0),
            "profit_rate":  float(summary.get("asst_icdc_erng_rt", 0) or 0),
            "holdings":     holdings,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 정상 조회 성공 시 스냅샷 저장 (야간 복구용)
        try:
            with open(BALANCE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        return result

    except Exception as e:
        print(f"  ⏳ 야간/휴장으로 인한 잔고 조회 대기: {e}")

        # 스냅샷에서 마지막 장 마감 잔고 복구 시도
        if BALANCE_SNAPSHOT_FILE.exists():
            try:
                with open(BALANCE_SNAPSHOT_FILE, encoding="utf-8") as f:
                    cached = json.load(f)
                cached["is_cached"] = True
                return cached
            except Exception:
                pass

        return None


# ================================================================
# 감시 리스트 구성
# ================================================================
def build_watch_list(holdings: list, extra_wl: dict) -> dict:
    """
    계좌 보유 종목 + watchlist.json 합집합을 반환합니다.
    반환: {symbol: {"name": str, "target_price": int|None, "is_holding": bool}}
    """
    result: dict = {}

    # 1. 계좌 보유 종목 (자동)
    for h in holdings:
        sym = h.get("symbol", "").strip()
        if not sym:
            continue
        result[sym] = {
            "name":         h.get("name", sym),
            "target_price": None,
            "is_holding":   True,
        }

    # 2. watchlist.json 종목 병합 (계좌에도 있으면 target_price만 덮어씀)
    for sym, data in extra_wl.items():
        sym = sym.strip()
        if not sym:
            continue
        if sym in result:
            result[sym]["target_price"] = data.get("target_price")
        else:
            result[sym] = {
                "name":         data.get("name", sym),
                "target_price": data.get("target_price"),
                "is_holding":   False,
            }

    return result


# ================================================================
# 개별 종목 API
# ================================================================
def get_stock_name(broker: mojito.KoreaInvestment, symbol: str) -> str:
    """API로 종목명을 조회합니다. 실패 시 종목코드 반환."""
    try:
        resp = broker.fetch_price(symbol)
        return resp["output"].get("hts_kor_isnm", symbol).strip()
    except Exception:
        return symbol


def get_ohlcv_dataframe(
    broker: mojito.KoreaInvestment, symbol: str, days: int = 60
) -> pd.DataFrame:
    """일봉 OHLCV 데이터를 DataFrame으로 반환합니다. Rate Limit 시 3회 재시도합니다."""
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
            print(f"  ❌ [{symbol}] OHLCV 오류: {e}")
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


def get_current_price(broker: mojito.KoreaInvestment, symbol: str) -> int | None:
    """현재가를 조회합니다. Rate Limit 시 3회 재시도합니다."""
    for attempt in range(3):
        try:
            return int(broker.fetch_price(symbol)["output"]["stck_prpr"])
        except Exception as e:
            if _is_token_error(e):
                raise
            if _is_rate_limit(e):
                time.sleep(60)
                continue
            return None
    return None

def get_investor_trend(
    broker: mojito.KoreaInvestment, symbol: str
) -> dict | None:
    """당일 외국인·기관 가집계 순매수량 조회 — 쌍끌이 수급 감지에 필요.
    모의투자 환경에서는 API 미지원으로 None을 반환할 수 있음."""
    try:
        base = (
            "https://openapivts.koreainvestment.com:29443"
            if IS_MOCK
            else "https://openapi.koreainvestment.com:9443"
        )
        url     = f"{base}/uapi/domestic-stock/v1/quotations/inquire-investor"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {broker.access_token}",
            "appkey":        broker.api_key,
            "appsecret":     broker.api_secret,
            "tr_id":         "FHKST01010900",
        }
        params  = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":          symbol,
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 429:
            raise RuntimeError("429 Too Many Requests")
        rows = resp.json().get("output")
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        return {
            "frgn_ntby_qty": int(row.get("frgn_ntby_qty", 0) or 0),  # 외국인 순매수량
            "orgn_ntby_qty": int(row.get("orgn_ntby_qty", 0) or 0),  # 기관 순매수량
        }
    except Exception as e:
        if _is_rate_limit(e):
            print(f"  ⚠️ [{symbol}] 수급 조회 Rate Limit — 60초 대기")
            time.sleep(60)
        else:
            print(f"  ⚠️ [{symbol}] 투자자 동향 조회 실패: {e}")
        return None