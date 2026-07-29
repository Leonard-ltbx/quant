#!/usr/bin/env python3
"""Fetch public A-share quotes and write a normalized dashboard payload."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WATCHLIST = ("600519", "300750", "601318", "000858", "600036", "000333")
SOURCE_URL = "https://qt.gtimg.cn/q="
HISTORY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
BENCHMARK_CODE = "000300"


def symbol(code: str) -> str:
    if code == BENCHMARK_CODE:
        return "sh000300"
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


def fetch_history(code: str, limit: int = 320) -> list[dict]:
    market_symbol = symbol(code)
    param = f"{market_symbol},day,,,{limit},qfq"
    url = f"{HISTORY_URL}?param={param}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://gu.qq.com/",
            "User-Agent": "Mozilla/5.0 quant-dashboard/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    block = payload.get("data", {}).get(market_symbol, {})
    rows = block.get("qfqday") or block.get("day") or []
    result = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            result.append(
                {
                    "date": row[0],
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        except (TypeError, ValueError):
            continue
    if len(result) < 80:
        raise RuntimeError(f"Insufficient history for {code}: {len(result)} bars")
    return result


def pct_change(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] <= 0:
        return None
    return (values[-1] / values[-periods - 1] - 1) * 100


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst * 100


def factors_from_history(bars: list[dict], quote: dict) -> dict:
    closes = [bar["close"] for bar in bars]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    recent_returns = returns[-20:]
    volatility = (
        statistics.stdev(recent_returns) * math.sqrt(252) * 100
        if len(recent_returns) >= 2
        else None
    )
    ma20 = statistics.mean(closes[-20:])
    momentum_20d = pct_change(closes, 20)
    momentum_60d = pct_change(closes, 60)
    price_vs_ma20 = (closes[-1] / ma20 - 1) * 100 if ma20 else None
    drawdown_60d = max_drawdown(closes[-60:])

    pe = quote.get("pe_ratio")
    pb = quote.get("pb_ratio")
    valuation_score = 50
    if pe:
        valuation_score += 15 if 0 < pe <= 20 else 5 if pe <= 40 else -10
    if pb:
        valuation_score += 15 if 0 < pb <= 2 else 5 if pb <= 5 else -10
    momentum_score = clamp(50 + (momentum_20d or 0) * 2, 0, 100)
    low_vol_score = clamp(100 - (volatility or 50) * 1.5, 0, 100)
    trend_score = clamp(50 + (price_vs_ma20 or 0) * 3, 0, 100)

    return {
        "momentum_20d_pct": round(momentum_20d, 2) if momentum_20d is not None else None,
        "momentum_60d_pct": round(momentum_60d, 2) if momentum_60d is not None else None,
        "annualized_volatility_20d_pct": round(volatility, 2) if volatility is not None else None,
        "price_vs_ma20_pct": round(price_vs_ma20, 2) if price_vs_ma20 is not None else None,
        "max_drawdown_60d_pct": round(drawdown_60d, 2),
        "scores": {
            "momentum": round(momentum_score),
            "trend": round(trend_score),
            "valuation": round(clamp(valuation_score, 0, 100)),
            "low_volatility": round(low_vol_score),
        },
    }


def compute_backtest(histories: dict[str, list[dict]], benchmark: list[dict]) -> dict:
    close_maps = {
        code: {bar["date"]: bar["close"] for bar in bars}
        for code, bars in histories.items()
    }
    common_dates = sorted(set.intersection(*(set(values) for values in close_maps.values())))
    if len(common_dates) < 100:
        raise RuntimeError(f"Insufficient common backtest dates: {len(common_dates)}")

    rebalance_interval = 20
    lookback = 20
    cost_rate = 0.0003
    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    holdings: list[str] = []
    rebalance_count = 0

    for index in range(lookback, len(common_dates) - 1):
        if not holdings or (index - lookback) % rebalance_interval == 0:
            momentum = []
            for code, values in close_maps.items():
                current = values[common_dates[index]]
                prior = values[common_dates[index - lookback]]
                momentum.append((current / prior - 1, code))
            new_holdings = [code for _, code in sorted(momentum, reverse=True)[:2]]
            turnover = len(set(holdings).symmetric_difference(new_holdings)) / 2
            equity *= 1 - turnover * cost_rate
            holdings = new_holdings
            rebalance_count += 1

        today = common_dates[index]
        tomorrow = common_dates[index + 1]
        portfolio_return = statistics.mean(
            close_maps[code][tomorrow] / close_maps[code][today] - 1
            for code in holdings
        )
        equity *= 1 + portfolio_return
        daily_returns.append(portfolio_return)
        equity_curve.append(equity)

    benchmark_map = {bar["date"]: bar["close"] for bar in benchmark}
    benchmark_dates = [date for date in common_dates[lookback:] if date in benchmark_map]
    benchmark_return = None
    if len(benchmark_dates) >= 2:
        benchmark_return = (
            benchmark_map[benchmark_dates[-1]] / benchmark_map[benchmark_dates[0]] - 1
        ) * 100

    trading_days = len(daily_returns)
    total_return = (equity - 1) * 100
    annualized_return = ((equity ** (252 / trading_days)) - 1) * 100 if trading_days else 0
    volatility = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0
    sharpe = statistics.mean(daily_returns) / volatility * math.sqrt(252) if volatility else 0
    win_rate = sum(value > 0 for value in daily_returns) / trading_days * 100 if trading_days else 0

    sampled_curve = []
    stride = max(1, len(equity_curve) // 60)
    curve_dates = common_dates[lookback : lookback + len(equity_curve)]
    for index in range(0, len(equity_curve), stride):
        sampled_curve.append(
            {"date": curve_dates[min(index, len(curve_dates) - 1)], "value": round(equity_curve[index], 6)}
        )
    if sampled_curve[-1]["date"] != curve_dates[-1]:
        sampled_curve.append({"date": curve_dates[-1], "value": round(equity_curve[-1], 6)})

    return {
        "strategy": "20日动量轮动",
        "universe": list(WATCHLIST),
        "rule": "每20个交易日，以过去20日收益率排序，等权持有前2只；信号只使用当时及以前数据",
        "transaction_cost_rate": cost_rate,
        "benchmark": "沪深300",
        "start_date": common_dates[lookback],
        "end_date": common_dates[-1],
        "trading_days": trading_days,
        "rebalance_count": rebalance_count,
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(annualized_return, 2),
        "benchmark_return_pct": round(benchmark_return, 2) if benchmark_return is not None else None,
        "max_drawdown_pct": round(max_drawdown(equity_curve), 2),
        "sharpe_ratio": round(sharpe, 2),
        "daily_win_rate_pct": round(win_rate, 2),
        "equity_curve": sampled_curve,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/market.json")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    quotes = fetch_quotes(WATCHLIST)
    histories = {code: fetch_history(code) for code in WATCHLIST}
    quote_by_code = {quote["code"]: quote for quote in quotes}
    for code, bars in histories.items():
        if code in quote_by_code:
            quote_by_code[code]["factors"] = factors_from_history(bars, quote_by_code[code])
    backtest = compute_backtest(histories, fetch_history(BENCHMARK_CODE))
    payload = {
        "schema_version": 1,
        "source": "腾讯财经公开行情",
        "source_endpoint": "qt.gtimg.cn",
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "行情字段规则评分（涨跌幅、量比、换手率、PE、PB），非AI预测",
        "disclaimer": "数据仅供研究与功能演示，不构成任何投资建议。",
        "watchlist_size": len(quotes),
        "factor_method": "动量20/60日、20日年化波动率、MA20偏离、60日最大回撤；估值使用实时PE/PB",
        "backtest": backtest,
        "quotes": sorted(quotes, key=lambda item: item["score"], reverse=True),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(quotes)} real quotes to {output}")


if __name__ == "__main__":
    main()
