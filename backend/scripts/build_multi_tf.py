"""A3: 10m/30m/1h/4h bars за 2025-08..2026-06 (5 акций + IMOEX).

Агрегация из 1m свечей БД. Бар группируется по floor(ts/period) в UTC
(т.е. бар начинается ровно на границе 10m/30m/1h/4h от эпохи — детерминированно).
Выход: reports/{tf}_bars.csv + manifest (immutable).
"""
import asyncio, csv, hashlib, json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import text
from app.database import SessionLocal

FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4", "BBG00KDWPPW2"]
TICKERS = {"BBG008F2T3T2":"RUAL","BBG004S681M2":"SNGSP","BBG004S683W7":"AFLT",
           "BBG004S68CP5":"MVID","BBG004S681B4":"NLMK","BBG00KDWPPW2":"IMOEX"}
PERIODS = {"10m":600, "30m":1800, "1h":3600, "4h":14400}
T_FROM = datetime(2025,8,1,tzinfo=timezone.utc)
T_TO = datetime(2026,7,1,tzinfo=timezone.utc)

async def main():
    async with SessionLocal() as db:
        out_dir = os.path.join(os.path.dirname(__file__), "..", "reports")
        os.makedirs(out_dir, exist_ok=True)
        for tf, sec in PERIODS.items():
            rows_all = []
            for figi in FIGIS:
                rows = await db.execute(text("""
                    SELECT ts, open, high, low, close, volume FROM candles
                    WHERE figi=:figi AND interval=1 AND ts>=:tf AND ts<:tt ORDER BY ts
                """), {"figi":figi, "tf":T_FROM, "tt":T_TO})
                buckets = {}
                order = []
                for r in rows.fetchall():
                    ts = r[0]
                    b = int(ts.timestamp()) // sec * sec
                    if b not in buckets:
                        buckets[b] = {"open":r[1],"high":r[2],"low":r[3],"close":r[4],
                                      "volume":float(r[5]) or 0,"n":1}
                        order.append(b)
                    else:
                        g = buckets[b]
                        g["high"]=max(g["high"],r[2]); g["low"]=min(g["low"],r[3])
                        g["close"]=r[4]; g["volume"]+=float(r[5]) or 0; g["n"]+=1
                for b in order:
                    g = buckets[b]
                    rows_all.append({
                        "figi":figi,"ticker":TICKERS[figi],
                        "bar_ts": datetime.fromtimestamp(b,tz=timezone.utc).isoformat(),
                        "open":round(g["open"],4),"high":round(g["high"],4),
                        "low":round(g["low"],4),"close":round(g["close"],4),
                        "volume":int(g["volume"]),"bars_1m":g["n"],
                    })
                print(f"{tf} {TICKERS[figi]}: {len(order)} баров", flush=True)
            rows_all.sort(key=lambda r:(r["bar_ts"], r["ticker"]))
            path = os.path.join(out_dir, f"{tf}_bars.csv")
            with open(path,"w",newline="",encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["figi","ticker","bar_ts","open","high","low","close","volume","bars_1m"])
                w.writeheader(); w.writerows(rows_all)
            h = hashlib.sha256()
            with open(path,"rb") as f:
                for chunk in iter(lambda:f.read(65536),b""): h.update(chunk)
            manifest = {"artifact":f"{tf}_bars.csv","period":["2025-08-01","2026-07-01"],
                        "rows":len(rows_all),"timezone":"UTC (bar boundary)","sha256":h.hexdigest(),
                        "source":"1m candles, bucket floor(ts/period)"}
            with open(os.path.join(out_dir, f"{tf}_bars_manifest.json"),"w",encoding="utf-8") as f:
                json.dump(manifest,f,ensure_ascii=False,indent=2)
            print(f"{tf}: готово {len(rows_all)} строк sha256={h.hexdigest()[:12]}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
