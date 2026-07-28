#!/usr/bin/env python3
"""Fetch public A-share quotes and write a normalized dashboard payload."""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WATCHLIST = ("600519", "300750", "601318", "000858", "600036", "000333")
SOURCE_URL = "https://qt.gtimg.cn/q="


def symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def number(fields: list[str], index: int) -> float | None:
    if index >= len(fields):
        return None
    try:
        value = float(fields[index])
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def rule_score(quote: dict) -> int:
    """Transparent quote-based score; it is not an investment recommendation."""
    change = quote.get("change_pct") or 0
    volume_ratio = quote.get("volume_ratio") or 1
    turnover = quote.get("turnover_rate") or 0
    pe = quote.get("pe_ratio")
    pb = quote.get("pb_ratio")

    score = 50
    score += clamp(change * 4, -20, 20)
    score += clamp((volume_ratio - 1) * 10, -10, 15)
    score += clamp(turnover - 2, -4, 8)
    if pe and 0 < pe <= 40:
        score += 5
    elif pe and pe > 80:
        score -= 5
    if pb and 0 < pb <= 5:
        score += 3
    return round(clamp(score, 0, 100))


def signal_for(score: int) -> str:
    if score >= 75:
        return "规则评分较强"
    if score >= 60:
        return "规则评分偏强"
    if score >= 45:
        return "规则评分中性"
    return "规则评分偏弱"


def fetch_quotes(codes: tuple[str, ...]) -> list[dict]:
    url = SOURCE_URL + ",".join(symbol(code) for code in codes)
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.qq.com/",
            "User-Agent": "Mozilla/5.0 quant-dashboard/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("gbk", errors="replace")

    quotes: list[dict] = []
    for record in raw.split(";"):
        if '"' not in record:
            continue
        payload = record.split('"', 1)[1].rsplit('"', 1)[0]
        fields = payload.split("~")
        if len(fields) < 45 or not fields[2]:
            continue
        quote = {
            "code": fields[2],
            "name": fields[1],
            "price": number(fields, 3),
            "pre_close": number(fields, 4),
            "open": number(fields, 5),
            "volume": (number(fields, 6) or 0) * 100,
            "change_amount": number(fields, 31),
            "change_pct": number(fields, 32),
            "high": number(fields, 33),
            "low": number(fields, 34),
            "turnover_rate": number(fields, 38),
            "pe_ratio": number(fields, 39),
            "amplitude": number(fields, 43),
            "circ_mv_billion": number(fields, 44),
            "total_mv_billion": number(fields, 45),
            "pb_ratio": number(fields, 46),
            "volume_ratio": number(fields, 49),
            "quote_time": fields[30] if len(fields) > 30 else "",
        }
        quote["score"] = rule_score(quote)
        quote["signal"] = signal_for(quote["score"])
        quotes.append(quote)
    if not quotes:
        raise RuntimeError("Tencent quote endpoint returned no usable records")
    return quotes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/market.json")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    quotes = fetch_quotes(WATCHLIST)
    payload = {
        "schema_version": 1,
        "source": "腾讯财经公开行情",
        "source_endpoint": "qt.gtimg.cn",
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "行情字段规则评分（涨跌幅、量比、换手率、PE、PB），非AI预测",
        "disclaimer": "数据仅供研究与功能演示，不构成任何投资建议。",
        "watchlist_size": len(quotes),
        "quotes": sorted(quotes, key=lambda item: item["score"], reverse=True),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(quotes)} real quotes to {output}")


if __name__ == "__main__":
    main()
