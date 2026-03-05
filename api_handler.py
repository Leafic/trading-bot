# ==============================================================
#  api_handler.py — 한국투자증권 API 통신 래퍼
#  mojito 라이브러리를 감싸 잔고·OHLCV·현재가 조회와
#  장 시간 확인, 에러 방어 로직을 제공합니다.
# ==============================================================

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

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

# 스캘핑 봇 전용 모의투자 계좌 (실계좌와 완전 분리)
MOCK_APP_KEY    = os.getenv("KIS_MOCK_APPKEY", "")
MOCK_APP_SECRET = os.getenv("KIS_MOCK_APPSECRET", "")
MOCK_ACC_NO     = os.getenv("KIS_MOCK_ACC_NO", "")


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
# 브로커 팩토리 + 전역 싱글톤
# ================================================================
def create_broker() -> mojito.KoreaInvestment:
    """한국투자증권 broker 인스턴스를 생성합니다.
    내부적으로 issue_access_token()을 호출하므로 직접 호출 시 새 토큰이 발급됩니다.
    일반적으로 get_broker()를 통해 싱글톤을 사용하세요."""
    return mojito.KoreaInvestment(
        api_key=APP_KEY,
        api_secret=APP_SECRET,
        acc_no=ACC_NO,
        mock=IS_MOCK,
    )


# 앱 전체에서 하나의 broker 인스턴스를 공유합니다.
# KIS 토큰은 24시간 유효하며 계정당 1개만 활성화되므로,
# 여러 곳에서 create_broker()를 독립 호출하면 서로의 토큰을 무효화합니다.
_broker_instance: Optional[mojito.KoreaInvestment] = None
_broker_lock = threading.Lock()


def get_broker() -> mojito.KoreaInvestment:
    """전역 싱글톤 broker를 반환합니다. 없으면 최초 1회 생성합니다."""
    global _broker_instance
    with _broker_lock:
        if _broker_instance is None:
            _broker_instance = create_broker()
        return _broker_instance


def refresh_broker() -> mojito.KoreaInvestment:
    """토큰 만료 시 broker를 재생성하고 싱글톤을 교체합니다.
    이후 get_broker()를 호출하는 모든 곳이 새 토큰을 사용합니다."""
    global _broker_instance
    with _broker_lock:
        _broker_instance = create_broker()
        return _broker_instance


# ================================================================
# 스캘핑 봇 전용 모의투자 싱글톤 (실계좌 싱글톤과 완전 분리)
# ================================================================
def create_mock_broker() -> mojito.KoreaInvestment:
    """스캘핑 봇 전용 모의투자 broker를 생성합니다.
    KIS_MOCK_* 환경변수를 사용하며 항상 mock=True로 고정됩니다."""
    if not MOCK_APP_KEY or not MOCK_APP_SECRET or not MOCK_ACC_NO:
        raise RuntimeError(
            "모의투자 환경변수 미설정: KIS_MOCK_APPKEY / KIS_MOCK_APPSECRET / KIS_MOCK_ACC_NO"
        )
    return mojito.KoreaInvestment(
        api_key=MOCK_APP_KEY,
        api_secret=MOCK_APP_SECRET,
        acc_no=MOCK_ACC_NO,
        mock=True,  # 절대 실계좌로 전환되지 않도록 하드코딩
    )


_mock_broker_instance: Optional[mojito.KoreaInvestment] = None
_mock_broker_lock = threading.Lock()


def get_mock_broker() -> mojito.KoreaInvestment:
    """스캘핑 봇 전용 모의투자 싱글톤 broker를 반환합니다."""
    global _mock_broker_instance
    with _mock_broker_lock:
        if _mock_broker_instance is None:
            _mock_broker_instance = create_mock_broker()
        return _mock_broker_instance


def refresh_mock_broker() -> mojito.KoreaInvestment:
    """모의투자 broker 토큰 만료 시 싱글톤을 재생성합니다."""
    global _mock_broker_instance
    with _mock_broker_lock:
        _mock_broker_instance = create_mock_broker()
        return _mock_broker_instance


# ================================================================
# 주문 (모의투자 전용 안전장치 포함)
# ================================================================
def place_market_order(
    broker: mojito.KoreaInvestment,
    symbol: str,
    side: str,
    qty: int,
) -> Optional[dict]:
    """시장가 주문을 실행합니다. 모의투자 broker가 아니면 즉시 RuntimeError 발생.
    side: 'buy' 또는 'sell'
    Returns: 주문 응답 dict, 실패 시 None"""
    # 실계좌 주문 원천 차단 — is_mock 속성이 False이면 예외
    if not getattr(broker, "is_mock", False):
        raise RuntimeError("⛔ 실계좌 주문 차단: 스캘핑 봇은 모의투자 broker만 사용 가능합니다.")
    try:
        if side == "buy":
            resp = broker.create_market_buy_order(symbol=symbol, quantity=qty)
        elif side == "sell":
            resp = broker.create_market_sell_order(symbol=symbol, quantity=qty)
        else:
            raise ValueError(f"잘못된 side 값: {side!r} — 'buy' 또는 'sell' 이어야 합니다.")
        return resp
    except Exception as e:
        print(f"  ❌ [{symbol}] 주문 실패({side}): {e}")
        return None


def get_deposit(broker: mojito.KoreaInvestment) -> Optional[int]:
    """예수금(주문 가능 금액)을 조회합니다. 실패 시 None 반환."""
    try:
        resp    = broker.fetch_balance()
        out1    = resp.get("output1") or [{}]
        summary = out1[0] if out1 else {}
        # KIS 필드명: 주문가능현금 = ord_psbl_cash
        deposit = int(summary.get("ord_psbl_cash", 0) or 0)
        return deposit if deposit > 0 else None
    except Exception as e:
        print(f"  ⚠️  예수금 조회 실패: {e}")
        return None


# ================================================================
# 잔고 조회
# ================================================================
def get_balance(broker: mojito.KoreaInvestment) -> Optional[dict]:
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


def _parse_ohlcv_resp(resp: dict) -> pd.DataFrame:
    """KIS OHLCV 응답 dict를 정형화된 DataFrame으로 변환합니다."""
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


def _fetch_ohlcv_direct(
    broker: mojito.KoreaInvestment, symbol: str, since: str, market_code: str
) -> dict:
    """KIS OHLCV API를 직접 호출합니다.
    mojito 기본값이 KOSPI(J) 고정이므로, KOSDAQ(Q) 재시도 시 이 함수를 사용합니다."""
    base = (
        "https://openapivts.koreainvestment.com:29443"
        if IS_MOCK
        else "https://openapi.koreainvestment.com:9443"
    )
    url     = f"{base}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "content-type":  "application/json; charset=utf-8",
        "authorization": broker.access_token,  # mojito가 이미 "Bearer ..." 형식으로 저장
        "appkey":        broker.api_key,
        "appsecret":     broker.api_secret,
        "tr_id":         "FHKST03010100",
    }
    params  = {
        "FID_COND_MRKT_DIV_CODE": market_code,
        "FID_INPUT_ISCD":          symbol,
        "FID_INPUT_DATE_1":        since,
        "FID_INPUT_DATE_2":        datetime.now().strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE":     "D",
        "FID_ORG_ADJ_PRC":         "0",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    if resp.status_code == 429:
        raise RuntimeError("429 Too Many Requests")
    return resp.json()


def get_ohlcv_dataframe(
    broker: mojito.KoreaInvestment, symbol: str, days: int = 120
) -> pd.DataFrame:
    """일봉 OHLCV 데이터를 날짜 범위를 지정해 반환합니다.
    토큰 만료(msg1에 'token' 포함) 감지 시 create_broker()로 새 인스턴스를 생성해 재시도합니다.
    KIS는 계정당 토큰 1개만 유효하므로, 재발급 시 기존 broker 토큰이 무효화될 수 있습니다.
    KOSPI(J) 실패 시 KOSDAQ(Q)으로 자동 재시도합니다."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    # 로컬 참조 — 토큰 만료 시 create_broker()로 교체 가능
    _broker = broker
    _token_refreshed = False  # 재발급은 전체 흐름에서 1회만 수행

    # ── 날짜 범위 직접 호출: KOSPI → KOSDAQ 순서로 시도 ────────────
    last_resp: dict = {}
    for market_code, label in (("J", "KOSPI"), ("Q", "KOSDAQ")):
        resp = None
        for attempt in range(3):
            try:
                resp = _fetch_ohlcv_direct(_broker, symbol, since, market_code)
            except Exception as e:
                if _is_rate_limit(e):
                    print(f"  ⚠️  [{symbol}] Rate Limit({label}) — 60초 대기 ({attempt+1}/3)")
                    time.sleep(60)
                    continue
                print(f"  ❌ [{symbol}] OHLCV 오류({label}): {e}")
                break

            if resp and resp.get("output2"):
                return _parse_ohlcv_resp(resp)

            # 토큰 만료 감지 → 새 broker 생성(issue_access_token 자동 호출) 후 1회 재시도
            msg = (resp or {}).get("msg1", "")
            if "token" in msg.lower() and not _token_refreshed:
                print(f"  🔄 [{symbol}] 토큰 만료 — 싱글톤 broker 재발급 후 재시도")
                _broker = refresh_broker()  # 전역 싱글톤도 함께 교체
                _token_refreshed = True
                continue

            print(f"  ⚠️  [{symbol}] {label} 데이터 없음{f' ({msg})' if msg else ''}"
                  + (" → KOSDAQ으로 재시도" if market_code == "J" else ""))
            break

        if resp and resp.get("output2"):
            return _parse_ohlcv_resp(resp)
        last_resp = resp or {}

    print(f"  ❌ [{symbol}] KOSPI/KOSDAQ 모두 데이터 없음 (msg={last_resp.get('msg1', '없음')})")
    return pd.DataFrame()


def get_current_price(broker: mojito.KoreaInvestment, symbol: str) -> Optional[int]:
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
) -> Optional[dict]:
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
            "authorization": broker.access_token,  # mojito가 이미 "Bearer ..." 형식으로 저장
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


# ================================================================
# 종목명으로 종목코드 검색
# ================================================================
def search_stock_by_name(query: str) -> list:
    """종목명 → 종목코드 후보 목록 반환.
    Yahoo Finance 검색 API로 `.KS`(KOSPI) / `.KQ`(KOSDAQ) 심볼을 받아
    6자리 KIS 코드로 변환합니다. 추가 패키지 불필요."""
    _ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "lang": "ko-KR", "region": "KR",
                    "newsCount": 0, "enableFuzzyQuery": "false"},
            headers=_ua,
            timeout=6,
        )
        resp.raise_for_status()
        quotes  = resp.json().get("quotes", [])
        results = []
        for q in quotes:
            if q.get("quoteType") != "EQUITY":
                continue
            sym_yahoo = q.get("symbol", "")
            # KIS 코드: "005930.KS" → "005930",  "314130.KQ" → "314130"
            m = re.match(r"^(\d{6})\.(KS|KQ)$", sym_yahoo)
            if not m:
                continue
            symbol = m.group(1)
            market = "KOSPI" if sym_yahoo.endswith(".KS") else "KOSDAQ"
            name   = (q.get("longname") or q.get("shortname") or symbol).strip()
            results.append({"symbol": symbol, "name": name, "market": market})
        if results:
            return results
        print(f"  ⚠️ [이름검색] Yahoo Finance 결과 없음 — query={query!r}")
    except Exception as e:
        print(f"  ⚠️ [이름검색] Yahoo Finance 실패: {e}")

    return []