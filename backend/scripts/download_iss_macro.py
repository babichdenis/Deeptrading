"""DATA GAP FILL: Brent/Gold/USD-RUB через MOEX ISS API (free, point-in-time).

Контракты (активны на July 2026): BRU6 (BR-9.26), GDU6 (GOLD-9.26), SiU6 (Si-9.26).
Пагинация 500 свечей/запрос; timestamps begin в MSK -> конвертация в UTC.
Сохранение: backend/data/{brent,gold,usdrub}_1m.csv  (ts_utc;open;high;low;close;volume)
Запуск: локально.
"""
import os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MSK = timezone(timedelta(hours=3))
CONTRACTS = {"BRU6": "brent_1m.csv", "GDU6": "gold_1m.csv", "SiU6": "usdrub_1m.csv"}


def fetch_day(secid, day):
    """Возвращает список (ts_utc, open, high, low, close, volume) за день."""
    out = []
    start = 0
    while True:
        url = (f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"
               f"?from={day.strftime('%Y-%m-%d')}&till={(day+timedelta(days=1)).strftime('%Y-%m-%d')}"
               f"&interval=1&iss.meta=off&limit=500&start={start}")
        req = urllib.request.Request(url, headers={"User-Agent": "research"})
        with urllib.request.urlopen(req, timeout=60) as r:
            import json
            d = json.load(r)
        rows = d.get("candles", {}).get("data") or []
        if not rows:
            break
        for r in rows:
            # r = [open, close, high, low, value, volume, begin, end]
            try:
                o, c, h, l, v, vol, begin, _ = r
            except Exception:  # noqa: BLE001
                continue
            b = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc)
            out.append((b.isoformat().replace("+00:00", "Z"), float(h), float(l), float(o), float(c), int(vol or 0)))
        if len(rows) < 500:
            break
        start += 500
        time.sleep(0.2)
    return out


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    d0 = datetime(2026, 1, 1)
    d1 = datetime(2026, 8, 29)
    for secid, outfile in CONTRACTS.items():
        path = os.path.join(DATA_DIR, outfile)
        total = 0
        with open(path, "w") as f:
            day = d0
            while day < d1:
                try:
                    candles = fetch_day(secid, day)
                except Exception as e:  # noqa: BLE001
                    print(f"{secid} {day.date()}: ERR {str(e)[:60]}", flush=True)
                    day += timedelta(days=1); time.sleep(1); continue
                for c in candles:
                    f.write(f"{c[0]};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]};\n")
                total += len(candles)
                day += timedelta(days=1)
                if day.day % 5 == 1:
                    print(f"{secid} ... {day.date()} баров {total}", flush=True)
                time.sleep(0.15)
        print(f"{secid}: {total} баров -> {path}", flush=True)


if __name__ == "__main__":
    main()
