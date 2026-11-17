"""DATA GAP: поиск и скачивание Brent/Gold/USD-RUB (MOEX фьючерсы BR/GOLD/Si) через T-Invest REST.

Локальный запуск. Шаг 1: FindInstrument -> uid. Шаг 2: GetCandles 1m по дням за 2026.
Сохранение: backend/data/raw_tinvest/{brent,gold,usdrub}_1m.csv
Формат: ts;open;close;high;low;volume;
Пишет только в свой каталог и атомарно (tmp + replace), поэтому не смешивается
с выгрузкой MOEX ISS и не оставляет половину файла при сбое.
"""
import json, os, ssl, sys, time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import get_settings

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw_tinvest")
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

INSTRUMENTS = [("BR", "Brent futures"), ("GOLD", "Gold futures"), ("Si", "USD-RUB futures")]
QUERIES = {"BR": ["BR", "Brent", "BR-"], "GOLD": ["GOLD", "Gold"], "Si": ["Si", "Si-"]}
OUT = {"BR": "brent_1m.csv", "GOLD": "gold_1m.csv", "Si": "usdrub_1m.csv"}


def api(path, body, token):
    url = "https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1." + path
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.load(r)


def find_uid(query, token, kind="INSTRUMENT_TYPE_FUTURES"):
    try:
        d = api("InstrumentsService/FindInstrument", {"query": query, "instrumentKind": kind}, token)
    except Exception as e:  # noqa: BLE001
        print(f"  find {query}: ERR {str(e)[:80]}")
        return None
    for ins in d.get("instruments") or []:
        if ins.get("currency") == "rub" and ins.get("exchange") in ("MOEX", "MOEX_PLACE_MARKET") or True:
            return {"uid": ins.get("uid"), "ticker": ins.get("ticker"), "name": ins.get("name"),
                    "classCode": ins.get("classCode")}
    return None


def get_candles(uid, day, token):
    url = ("https://invest-public-api.tinkoff.ru/rest/"
           "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles")
    body = {"instrumentId": uid, "from": day.isoformat().replace("+00:00", "Z"),
            "to": (day + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "interval": "CANDLE_INTERVAL_1_MIN"}
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        d = json.load(r)
    out = []
    for c in d.get("candles") or []:
        def px(field):
            q = c.get(field, {})
            return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9
        out.append((c["time"].replace("Z", "+00:00"), round(px("open"), 4), round(px("close"), 4),
                    round(px("high"), 4), round(px("low"), 4), int(c.get("volume", 0) or 0)))
    return out


def write_atomic(path, rows: dict):
    """Пишет отсортированные строки через временный файл: половины файла не будет."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for stamp in sorted(rows):
            handle.write(rows[stamp] + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main():
    settings = get_settings()
    os.makedirs(DATA_DIR, exist_ok=True)
    for key, label in INSTRUMENTS:
        uid_info = None
        for q in QUERIES[key]:
            r = find_uid(q, settings.tinkoff_token)
            if r:
                uid_info = r
                break
        if not uid_info:
            print(f"{label}: uid НЕ найден")
            continue
        print(f"{label}: uid={uid_info['uid'][:12]} ticker={uid_info['ticker']}", flush=True)
        # 2026-01-01 .. 2026-08-28
        d0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        d1 = datetime(2026, 8, 29, tzinfo=timezone.utc)
        day = d0
        path = os.path.join(DATA_DIR, OUT[key])
        rows: dict[str, str] = {}
        dropped = 0
        while day < d1:
            day_start = day.isoformat().replace("+00:00", "Z")
            day_end = (day + timedelta(days=1)).isoformat().replace("+00:00", "Z")
            try:
                candles = get_candles(uid_info["uid"], day, settings.tinkoff_token)
            except Exception as e:  # noqa: BLE001
                print(f"  {day.date()}: ERR {str(e)[:60]}", flush=True)
                time.sleep(2); day += timedelta(days=1); continue
            for c in candles:
                stamp = c[0]
                if not (day_start <= stamp < day_end):
                    dropped += 1
                    continue
                rows[stamp] = f"{c[0]};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]};"
            day += timedelta(days=1)
            if day.day % 10 == 1:
                print(f"  ... до {day.date()}, баров {len(rows)}", flush=True)
            time.sleep(0.3)
        write_atomic(path, rows)
        print(f"{label}: сохранено {len(rows)} баров (граничных отброшено {dropped}) -> {path}", flush=True)


if __name__ == "__main__":
    main()
