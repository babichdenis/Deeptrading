#!/usr/bin/env python3
"""
arbitrage_data_probe.py — разведка данных для арбитражных гипотез.

Запускает discovery → пытается скачать свечи по валютам и фьючерсам →
сохраняет results + concrete next steps.

Usage:
    cd backend && .venv/bin/python3 scripts/arbitrage_data_probe.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

from app.config import get_settings
from t_tech.invest import CandleInterval
from t_tech.invest.clients import Client
from t_tech.invest.utils import quotation_to_decimal

OUT_DIR = ROOT / "reports" / "arbitrage_data_probe"
OUT_DIR.mkdir(parents=True, exist_ok=True)

INTERVALS = {
    "1min":  CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5min":  CandleInterval.CANDLE_INTERVAL_5_MIN,
    "hour":  CandleInterval.CANDLE_INTERVAL_HOUR,
    "day":   CandleInterval.CANDLE_INTERVAL_DAY,
}

NOW = datetime.now(timezone.utc)
WINDOW = NOW - timedelta(days=7)   # неделя для quick probe


def q2f(q) -> float:
    return float(q.units + q.nano / 1e9)


def _candles(client, figi: str, interval: CandleInterval, from_: datetime, to: datetime) -> list[dict]:
    """ candles from a context-managed Services object (not a raw Client). """
    rows = {}
    cursor = from_
    while cursor < to:
        chunk_end = min(cursor + timedelta(days=1), to)
        resp = client.market_data.get_candles(figi=figi, interval=interval, from_=cursor, to=chunk_end)
        for c in resp.candles:
            key = (figi, int(interval.value), str(c.time))
            rows[key] = {
                "figi": figi,
                "interval": int(interval.value),
                "ts": str(c.time),
                "open":  q2f(c.open),
                "high":  q2f(c.high),
                "low":   q2f(c.low),
                "close": q2f(c.close),
                "volume": c.volume,
            }
        cursor = chunk_end
    return sorted(rows.values(), key=lambda r: r["ts"])


async def main():
    settings = get_settings()
    token = settings.tinkoff_token

    results = {
        "probed_at": NOW.isoformat(),
        "window_from": WINDOW.isoformat(),
        "window_to":   NOW.isoformat(),
        "token_prefix": token[:8] + "..." if token else "(empty)",
        "discovery": {},
        "fx_candles": {},
        "futures_candles": {},
        "next_steps": [],
    }

    with Client(token=token) as svc:
        # ── 1. Discovery всех типов ───────────────────────────────────────
        for method, label in [("shares", "shares"), ("currencies", "currencies"), ("futures", "futures"), ("etfs", "etfs"), ("options", "options")]:
            try:
                resp = getattr(svc.instruments, method)()
                items = [
                    {
                        "ticker": getattr(x, "ticker", None),
                        "uid":    getattr(x, "uid", None),
                        "figi":   getattr(x, "figi", None),
                        "name":   x.name,
                        # фьючерсы
                        "currency":     getattr(x, "currency", None),
                        "exchange":     getattr(x, "exchange", None),
                        "strike_price": getattr(x, "strike_price", None),
                        "option_type":  getattr(x, "option_type", None),
                        "direction":     getattr(x, "direction", None),
                        "future_type":  getattr(x, "future_type", None),
                        "short_enabled": getattr(x, "short_enabled_flag", None),
                    }
                    for x in getattr(resp, "instruments", [])
                ]
                results["discovery"][label] = {
                    "ok": True,
                    "count": len(items),
                    "sample": items[:5],
                }
                print(f"  {label}: {len(items)} instruments")
            except Exception as e:
                results["discovery"][label] = {"ok": False, "error": str(e)[:200]}
                print(f"  {label}: FAIL — {e}")

        # ── 2. FX валюты — качаем USD, EUR, CNY ──────────────────────────
        print("\n  FX candles...")
        fx_targets = [
            ("USD000UTSTOM", "USD/RUB TOD"),
            ("EUR_RUB__TOM", "EUR/RUB TOD"),
            ("CNY_RUB__TOM", "CNY/RUB TOD"),
            ("USD000UTSTOM", "USD/RUB TOM"),   # форвард
        ]
        for query, label in fx_targets:
            try:
                r = svc.instruments.find_instrument(query=query)
                if not r.instruments:
                    results["fx_candles"][label] = {"ok": False, "error": "not found"}
                    continue
                item = r.instruments[0]
                figi = item.figi
                results["fx_candles"][label] = {
                    "ticker": item.ticker,
                    "figi": figi,
                    "uid": getattr(item, "uid", None),
                    "name": item.name,
                }
                # Качаем day candles
                day_rows = _candles(svc, figi, CandleInterval.CANDLE_INTERVAL_DAY,
                                    WINDOW, NOW)
                hour_rows = _candles(svc, figi, CandleInterval.CANDLE_INTERVAL_HOUR,
                                     WINDOW, NOW)
                results["fx_candles"][label].update({
                    "day_candles": {"ok": True, "count": len(day_rows),
                                    "sample": day_rows[-3:] if day_rows else []},
                    "hour_candles": {"ok": True, "count": len(hour_rows),
                                     "sample": hour_rows[-3:] if hour_rows else []},
                })
                print(f"    {label} ({figi}): {len(day_rows)} day, {len(hour_rows)} hour candles")
            except Exception as e:
                results["fx_candles"][label] = {"ok": False, "error": str(e)[:200]}
                print(f"    {label}: FAIL — {e}")

        # ── 3. Фьючерсы — качаем ключевые ────────────────────────────────
        print("\n  Futures candles...")
        futures_targets = [
            ("Si",   "Si-12.25"),
            ("BR",   "BR-1.26"),
            ("RTS",  "RTS-12.25"),
            ("MIX",  "MIX-12.25"),
            ("VTBX", "VTBX"),
        ]
        for query, label in futures_targets:
            try:
                r = svc.instruments.find_instrument(query=query)
                if not r.instruments:
                    results["futures_candles"][label] = {"ok": False, "error": "not found"}
                    continue
                item = r.instruments[0]
                figi = item.figi
                uid = getattr(item, "uid", None)
                currency = getattr(item, "currency", None)
                results["futures_candles"][label] = {
                    "ticker": item.ticker,
                    "figi": figi,
                    "uid": uid,
                    "name": item.name,
                    "currency": currency,
                }
                day_rows = _candles(svc, figi, CandleInterval.CANDLE_INTERVAL_DAY,
                                    WINDOW, NOW)
                hour_rows = _candles(svc, figi, CandleInterval.CANDLE_INTERVAL_HOUR,
                                     WINDOW, NOW)
                results["futures_candles"][label].update({
                    "day_candles": {"ok": True, "count": len(day_rows),
                                    "sample": day_rows[-3:] if day_rows else []},
                    "hour_candles": {"ok": True, "count": len(hour_rows),
                                     "sample": hour_rows[-3:] if hour_rows else []},
                })
                print(f"    {label} ({figi}): {len(day_rows)} day, {len(hour_rows)} hour candles")
            except Exception as e:
                results["futures_candles"][label] = {"ok": False, "error": str(e)[:200]}
                print(f"    {label}: FAIL — {e}")

    # ── 4. Save ────────────────────────────────────────────────────────────
    out_file = OUT_DIR / f"{NOW.strftime('%Y%m%dT%H%M%SZ')}_probe.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # ── 5. Print summary + next steps ─────────────────────────────────────
    print(f"\n=== Probe complete ===")
    print(f"Output: {out_file}")

    discovery = results["discovery"]
    accessible = [k for k, v in discovery.items() if v.get("ok")]
    blocked    = [k for k, v in discovery.items() if not v.get("ok")]
    print(f"\nDiscovery: {accessible}")
    if blocked:
        print(f"Blocked: {blocked}")

    fx_ok = {k: v for k, v in results["fx_candles"].items() if v.get("day_candles", {}).get("count", 0) > 0}
    fut_ok = {k: v for k, v in results["futures_candles"].items() if v.get("day_candles", {}).get("count", 0) > 0}

    print(f"\nFX candles downloaded: {list(fx_ok.keys())}")
    print(f"Futures candles downloaded: {list(fut_ok.keys())}")

    if fx_ok or fut_ok:
        print("\n=== Next Steps ===")
        print("1. Добавить в БД: FX и фьючерсы через upsert_instruments + upsert_candles")
        print("2. Гипотеза 1 — FX carry: USD/RUB day candles → построить дневную доходность")
        print("   (но нужен overnight rate / ставка ЦБ для carry baseline)")
        print("3. Гипотеза 2 — Futures basis: BR/SI/RTS day candles vs equity basket")
        print("   (basis = futures - spot; mean-reversion на ~5d window)")
        print("4. Гипотеза 3 — ETF vs basket: OBLG vs OBLG constituents")
        print("   (ETF делаютArb между ETF-ценой и суммой组分)")
        print("5. Гипотеза 4 — Пары акций: SBER/SBERP, ROSN/ROSNp")
        print("   (коинтеграция на day candles, работает с уже имеющимися данными)")
        print("\n⚠ Для фьючерсов нужны также strike/expiry — есть ли в discovery.")


if __name__ == "__main__":
    asyncio.run(main())
