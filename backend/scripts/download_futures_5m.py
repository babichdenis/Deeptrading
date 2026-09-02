"""P1-4 H-063 DATA_PREP: фьючерсы 5m (1m->5m) через MOEX ISS.

Фьючерсы на акции РФ + индексные (IMOEXF/RTS). ISS НЕ отдаёт interval=5 для фьючерсов ->
качаем interval=1 и агрегируем в 5m (как в download_macro_5m_2025.py).
Контракты: для 2026-01..07 берём основной ликвидный контракт U6 (экспирация сент.2026);
до сент.2025 (не нужно, окно с 2026-01) — U6 покрывает.
OI: отдельный запрос /iss/statistics/engines/futures/... (если доступно), иначе отмечаем недоступность.
Сохранение: backend/data/futures_5m_{secid}.csv (ts_utc;open;high;low;close;volume)
Деливерабл: reports/futures_dataprep.{json,md}
"""
import os, time, json, urllib.request
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MSK = timezone(timedelta(hours=3))
D0 = datetime(2026, 1, 1)
D1 = datetime(2026, 8, 1)

# secid фьючерсов (контракт U6 = сент.2026, покрывает 2026-01..07)
FUT = {
    "RUAL": "RLU6", "SNGP": "SGU6", "AFLT": "AFU6",
    "MVID": "MVU6", "NLMK": "NMU6", "IMOEXF": "IMOEXF", "RTS": "RTU6",
}


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


def fetch_1m(secid, day):
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
    buckets = {}
    for b, o, h, l, c, v in raw:
        bucket = b - timedelta(minutes=b.minute % 5, seconds=b.second, microseconds=b.microsecond)
        if bucket not in buckets:
            buckets[bucket] = [o, h, l, c, v]
        else:
            s = buckets[bucket]
            s[1] = max(s[1], h); s[2] = min(s[2], l); s[3] = c; s[4] += v
    out = []
    for bucket in sorted(buckets):
        s = buckets[bucket]  # [o, h, l, c, v]
        out.append((bucket.isoformat().replace("+00:00", "Z"), s[1], s[2], s[0], s[3], s[4]))
    return out


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    manifest = {}
    for name, secid in FUT.items():
        path = os.path.join(DATA_DIR, f"futures_5m_{name}.csv")
        total = 0
        with open(path, "w") as f:
            day = D0
            while day < D1:
                try:
                    raw = fetch_1m(secid, day)
                    bars = aggregate_5m(raw)
                except Exception as e:  # noqa: BLE001
                    print(f"{name}/{secid} {day.date()}: ERR {str(e)[:60]}", flush=True)
                    day += timedelta(days=1); time.sleep(1); continue
                for c in bars:
                    f.write(f"{c[0]};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]};\n")
                total += len(bars)
                if day.day == 1:
                    print(f"{name}/{secid} ... {day.date()} баров {total}", flush=True)
                day += timedelta(days=1)
                time.sleep(0.12)
        manifest[name] = {"secid": secid, "bars": total, "path": f"data/futures_5m_{name}.csv"}
        print(f"{name}: {total} баров 5m -> {path}", flush=True)
    with open(os.path.join(DATA_DIR, "futures_manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("manifest saved", flush=True)


if __name__ == "__main__":
    main()
