"""Скачивание истории IMOEX (индекс МосБиржи) через REST getCandles.

Индексы не поддерживаются history-data (архивами) — только getCandles.
Скачиваем 1m по дням и сохраняем CSV в /tmp/hist_imoex/ (как акции),
затем загрузчик занесёт в БД с отдельным figi-префиксом.
"""
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import get_settings

UID_IMOEX = "4821c9aa-36e8-4743-b37c-861e58581b25"
OUT_DIR = "/tmp/hist_imoex"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def get_candles_day(day: datetime, token: str) -> list[dict]:
    url = ("https://invest-public-api.tinkoff.ru/rest/"
           "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles")
    body = {
        "instrumentId": UID_IMOEX,
        "from": day.isoformat().replace("+00:00", "Z"),
        "to": (day + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "interval": "CANDLE_INTERVAL_1_MIN",
    }
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        d = json.load(r)
    out = []
    for c in d.get("candles") or []:
        def px(field):
            q = c.get(field, {})
            return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9
        out.append({
            "ts": c["time"].replace("Z", "+00:00"),
            "open": round(px("open"), 4), "high": round(px("high"), 4),
            "low": round(px("low"), 4), "close": round(px("close"), 4),
            "volume": int(c.get("volume", 0) or 0),
        })
    return out


def main():
    settings = get_settings()
    os.makedirs(OUT_DIR, exist_ok=True)
    # диапазон: 2025-01-01 .. 2026-08-25 (как акции)
    d0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    d1 = datetime(2026, 8, 25, tzinfo=timezone.utc)
    day = d0
    total = 0
    while day < d1:
        fname = f"{UID_IMOEX}_{day.strftime('%Y%m%d')}.csv"
        path = os.path.join(OUT_DIR, fname)
        if os.path.exists(path):
            day += timedelta(days=1)
            continue
        try:
            candles = get_candles_day(day, settings.tinkoff_token)
        except Exception as e:  # noqa: BLE001
            print(f"{day.date()}: ERR {str(e)[:80]}", flush=True)
            time.sleep(2)
            day += timedelta(days=1)
            continue
        if candles:
            with open(path, "w") as f:
                for c in candles:
                    f.write(f"{UID_IMOEX};{c['ts']};{c['open']};{c['close']};"
                            f"{c['high']};{c['low']};{c['volume']};\n")
            total += len(candles)
        if day.day % 10 == 0:
            print(f"{day.date()}: {total} свечей", flush=True)
        time.sleep(0.15)  # rate limit
        day += timedelta(days=1)
    print(f"ИТОГО: {total} свечей в {OUT_DIR}")


if __name__ == "__main__":
    main()
