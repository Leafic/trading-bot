# ==============================================================
#  ai_analyst.py — Claude AI 기반 일일 장 마감 브리핑
#  보유 종목 지표를 Claude API에 전달해 매수/홀딩/매도 의견을 생성합니다.
# ==============================================================

import os
from datetime import datetime

import anthropic
from dotenv import load_dotenv

from utils import send_telegram

load_dotenv()


def daily_briefing(stocks_data: dict, watch_list: dict) -> bool:
    """
    보유 종목 지표를 Claude Haiku API로 분석해 텔레그램 브리핑을 발송합니다.
    보유 종목이 없거나 API 키 미설정 또는 호출 실패 시 False를 반환합니다.
    """
    # 1. 보유 종목만 필터 (is_holding=True)
    holding_symbols = {
        sym for sym, info in watch_list.items()
        if info.get("is_holding")
    }
    if not holding_symbols:
        print("  ⚠️  일일 브리핑: 보유 종목 없음, 건너뜀")
        return False

    # 2. 데이터를 짧은 텍스트로 압축 (Claude 토큰 절약 목적)
    lines = []
    for sym in holding_symbols:
        data = stocks_data.get(sym)
        if not data:
            continue
        name    = data.get("name", sym)
        price   = data.get("price", 0)
        sma20   = data.get("sma20")
        rsi     = data.get("rsi")
        vol_pct = data.get("vol_pct")

        sma20_pct = f"{(price - sma20) / sma20 * 100:+.1f}%" if sma20 else "N/A"
        vol_str   = f"{vol_pct:.0f}%" if vol_pct is not None else "N/A"
        lines.append(
            f"{name}({sym}): 현재가 {price:,}원, "
            f"20일선 대비 {sma20_pct}, RSI {rsi}, 거래량 {vol_str}"
        )

    if not lines:
        print("  ⚠️  일일 브리핑: stocks_data 미존재, 건너뜀")
        return False

    prompt_text = "\n".join(lines)

    # 3. Claude API 호출
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ❌ ANTHROPIC_API_KEY 미설정 — 일일 브리핑 건너뜀")
        return False

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=(
                "너는 헤지펀드의 퀀트 애널리스트다. "
                "제공된 지표 데이터를 보고, 내일 장에서 각 종목별로 "
                "매수/홀딩/매도 중 어떤 대응이 좋을지 1줄씩만 매우 날카롭게 브리핑해라. "
                "인사말 없이 결과만 도출할 것."
            ),
            messages=[{"role": "user", "content": prompt_text}],
        )
        analysis = message.content[0].text
    except Exception as e:
        print(f"  ❌ Claude API 호출 실패: {e}")
        return False

    # 4. 텔레그램 발송
    today = datetime.now().strftime("%Y-%m-%d")
    msg   = (
        f"📊 [일일 브리핑] {today} 장 마감 AI 분석\n\n"
        f"─── 지표 요약 ───\n{prompt_text}\n\n"
        f"─── AI 분석 ───\n{analysis}"
    )
    return send_telegram(msg)
