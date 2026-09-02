"""H-036 DATA_PREP: IMOEX 5m (индекс), 2025-2026 point-in-time (MOEX ISS).

IMOEX — индекс (engine=stock, market=index, board=SNDX). ISS отдаёт interval=1
(1-минутные бары); агрегируем в 5m локально (как в download_macro_5m_2025.py).
begin в MSK -> конвертация в UTC.
Сохранение: backend/data/imoex_5m.csv (ts_utc;open;high;low;close;volume)
"""
import os, time, json, urllib.request
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MSK = timezone(timedelta(hours=3))
D0 = datetime(2025, 1, 1)
D1 = datetime(2026, 8, 29)


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


def fetch_1m(month_start, month_end):
    raw = []
    start = 0
    while True:
        url = (f"https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/"
               f"securities/IMOEX/candles.json?from={month_start.strftime('%Y-%m-%d')}"
               f"&till={month_end.strftime('%Y-%m-%d')}&interval=1&iss.meta=off&limit=500&start={start}")
        d = _get_json(url)
        rows = d.get("candles", {}).get("data") or []
        if not rows:
            break
        for r in rows:
            try:
                o, c, h, l, v, value, begin, _end = r
            except Exception:  # noqa: BLE001
                continue
            b = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc)
            raw.append((b, float(o), float(h), float(l), float(c), int(v or 0)))
        if len(rows) < 500:
            break
        start += 500
        time.sleep(0.2)
    return raw


def aggregate_5m(raw):
    buckets = {}
    for b, o, h, l, c, v in raw:
        bucket = b - timedelta(minutes=b.minute % 5, seconds=b.second, microseconds=b.microsecond)
        if bucket not in buckets:
            buckets[bucket] = {"o": o, "h": h, "l": l, "c": c, "v": v}
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
    path = os.path.join(DATA_DIR, "imoex_5m.csv")
    total = 0
    with open(path, "w") as f:
        m = D0
        while m < D1:
            m_end = (m.replace(day=1) + timedelta(days=32)).replace(day=1)
            if m_end > D1:
                m_end = D1
            try:
                raw = fetch_1m(m, m_end)
                bars = aggregate_5m(raw)
            except Exception as e:  # noqa: BLE001
                print(f"IMOEX {m.date()}: ERR {str(e)[:60]}", flush=True)
                m = m_end; time.sleep(1); continue
            for c in bars:
                f.write(f"{c[0]};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]};\n")
            total += len(bars)
            print(f"IMOEX ... {m_end.date()} баров {total}", flush=True)
            m = m_end
            time.sleep(0.15)
    print(f"IMOEX: {total} баров 5m -> {path}", flush=True)


if __name__ == "__main__":
    main()
