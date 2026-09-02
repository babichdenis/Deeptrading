"""H-036 DATA_PREP: Brent5m + Gold5m + USD-RUB 5m, 2025-2026 point-in-time (MOEX ISS).

MOEX ISS candles API НЕ отдаёт interval=5 для фьючерсов (возвращает 0 строк),
поэтому качаем interval=1 (1-минутные бары, доступно) и агрегируем в 5m локально.

MOEX фьючерсы имеют滚动ющиеся контракты: для 2025 активен контракт с
экспирацией в сент.2025 (код U5), для 2026 — U6. Качаем оба и объединяем.

Контракты: BR(U5/U6)=Brent, GD(U5/U6)=Gold, Si(U5/U6)=USD-RUB.
timestamps begin в MSK -> конвертация в UTC.
Сохранение: backend/data/{brent,gold,usdrub}_5m.csv (ts_utc;open;high;low;close;volume)
"""
import os, time, json, urllib.request
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MSK = timezone(timedelta(hours=3))

COMMODITIES = {
    "brent":  [("BRU5", 2025), ("BRU6", 2026)],
    "gold":   [("GDU5", 2025), ("GDU6", 2026)],
    "usdrub": [("SiU5", 2025), ("SiU6", 2026)],
}
D1_LAST = datetime(2026, 8, 29)


def _get_json(url):
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 + attempt)
    raise last


def fetch_day_1m(secid, day):
    """Возвращает список (ts_utc_dt, o, h, l, c, v) 1m за день (с пагинацией)."""
    raw = []
    start = 0
    while True:
        url = (f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"
               f"?from={day.strftime('%Y-%m-%d')}&till={(day+timedelta(days=1)).strftime('%Y-%m-%d')}"
               f"&interval=1&iss.meta=off&limit=500&start={start}")
        d = _get_json(url)
        rows = d.get("candles", {}).get("data") or []
        if not rows:
            break
        for r in rows:
            try:
                o, c, h, l, v, vol, begin, _ = r
            except Exception:  # noqa: BLE001
                continue
            b = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc)
            raw.append((b, float(o), float(h), float(l), float(c), int(vol or 0)))
        if len(rows) < 500:
            break
        start += 500
        time.sleep(0.2)
    return raw


def aggregate_5m(raw):
    """Группирует 1m бары в 5m (floor до 5-минутки). Возвращает список (ts_iso,h,l,o,c,v)."""
    buckets = {}
    for b, o, h, l, c, v in raw:
        bucket = b - timedelta(minutes=b.minute % 5, seconds=b.second, microseconds=b.microsecond)
        if bucket not in buckets:
            buckets[bucket] = {"o": o, "h": h, "l": l, "c": c, "v": v, "first": True}
        else:
            s = buckets[bucket]
            s["h"] = max(s["h"], h); s["l"] = min(s["l"], l); s["c"] = c; s["v"] += v
    out = []
    for bucket in sorted(buckets):
        s = buckets[bucket]
        out.append((bucket.isoformat().replace("+00:00", "Z"), s["h"], s["l"], s["o"], s["c"], s["v"]))
    return out


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    for name, contracts in COMMODITIES.items():
        path = os.path.join(DATA_DIR, f"{name}_5m.csv")
        total = 0
        with open(path, "w") as f:
            for secid, year in contracts:
                d0 = datetime(year, 1, 1)
                d1 = datetime(year + 1, 1, 1) if year < D1_LAST.year else D1_LAST
                day = d0
                while day < d1:
                    try:
                        raw = fetch_day_1m(secid, day)
                        bars = aggregate_5m(raw)
                    except Exception as e:  # noqa: BLE001
                        print(f"{name}/{secid} {day.date()}: ERR {str(e)[:60]}", flush=True)
                        day += timedelta(days=1); time.sleep(1); continue
                    for c in bars:
                        f.write(f"{c[0]};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]};\n")
                    total += len(bars)
                    day += timedelta(days=1)
                    if day.day == 1:
                        print(f"{name}/{secid} ... {day.date()} баров {total}", flush=True)
                    time.sleep(0.12)
        print(f"{name}: {total} баров 5m -> {path}", flush=True)


if __name__ == "__main__":
    main()
