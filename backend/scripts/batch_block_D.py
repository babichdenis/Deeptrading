"""БЛОК D — futures lead-lag (H-063), SHADOW. Корреляция 5m доходностей фьючерс vs базовый.

Фьючерс (CSV) vs базовая акция (БД) для RUAL/SNGP/AFLT/MVID/NLMK; IMOEXF/RTS vs IMOEX cash (CSV).
Lead-lag: corr(fut_ret[t], base_ret[t+lag]), lag=-15..+15 баров (5m). lag>0 => фьючерс лидирует.
OI не скачан (candles API), зафиксировано.
"""
import sys, os, json, bisect
from datetime import datetime, timezone, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else os.getcwd()
if "__file__" not in globals():
    BACKEND = "/Users/Denis/Dev/Deeptrading/backend"
REPORTS = os.path.join(BACKEND, "reports")
DATA = os.path.join(BACKEND, "data")
FIGIS = {"RUAL": "BBG008F2T3T2", "SNGP": "BBG004S681M2", "AFLT": "BBG004S683W7",
         "MVID": "BBG004S68CP5", "NLMK": "BBG004S681B4"}
FUT_FILES = {"RUAL": "futures_5m_RUAL.csv", "SNGP": "futures_5m_SNGP.csv", "AFLT": "futures_5m_AFLT.csv",
             "MVID": "futures_5m_MVID.csv", "NLMK": "futures_5m_NLMK.csv", "IMOEXF": "futures_5m_IMOEXF.csv",
             "RTS": "futures_5m_RTS.csv"}


def parse_ts(s):
    s = s.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def load_fut(path):
    d = {}
    with open(path) as f:
        for line in f:
            p = line.strip().split(";")
            if len(p) < 5:
                continue
            ts = parse_ts(p[0]); c = float(p[4])
            d[ts] = c
    return d


def load_imoex(path):
    d = {}
    with open(path) as f:
        for line in f:
            p = line.strip().split(";")
            if len(p) < 5:
                continue
            ts = parse_ts(p[0]); c = float(p[4])
            d[ts] = c
    return d


def load_stock(figi):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    ENG = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"
    async def run():
        eng = create_async_engine(ENG)
        try:
            async with eng.connect() as c:
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"),
                                    {"f": figi, "a": datetime(2026, 1, 1, tzinfo=timezone.utc), "b": datetime(2026, 8, 1, tzinfo=timezone.utc)})
                rows = r.fetchall()
                if len(rows) > 1000:
                    return {x[0].replace(tzinfo=timezone.utc): float(x[4]) for x in rows}
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                                    {"f": figi, "a": datetime(2026, 1, 1, tzinfo=timezone.utc), "b": datetime(2026, 8, 1, tzinfo=timezone.utc)})
                raw = [(x[0].replace(tzinfo=timezone.utc), float(x[4])) for x in r.fetchall()]
                d = {}
                for ts, c in raw:
                    b = ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)
                    d[b] = c
                return d
        finally:
            await eng.dispose()
    return asyncio.new_event_loop().run_until_complete(run())


def lag_corr(fut, base, maxlag=15):
    common = sorted(set(fut) & set(base))
    if len(common) < 200:
        return None
    frets = []; crets = []
    for i in range(1, len(common)):
        frets.append((fut[common[i]] - fut[common[i-1]]) / fut[common[i-1]])
        crets.append((base[common[i]] - base[common[i-1]]) / base[common[i-1]])
    best = None
    for lag in range(-maxlag, maxlag + 1):
        xs = []; ys = []
        for i in range(maxlag, len(frets) - maxlag):
            fi = i
            ci = i + lag
            if 0 <= ci < len(crets):
                xs.append(frets[fi]); ys.append(crets[ci])
        if len(xs) < 100:
            continue
        n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
        cov = sum((xs[k]-mx)*(ys[k]-my) for k in range(n))
        sx = (sum((x-mx)**2 for x in xs))**0.5; sy = (sum((y-my)**2 for y in ys))**0.5
        r = cov/(sx*sy) if sx*sy else 0
        if best is None or abs(r) > abs(best[1]):
            best = (lag, r)
    return {"n_common": len(common), "best_lag": best[0], "best_corr": round(best[1], 4),
            "interpretation": ("фьючерс лидирует" if best[0] > 0 else "акция/индекс лидирует" if best[0] < 0 else "синхронно")}


def main():
    out = {"schema": "batch_block_D", "note": "OI недоступен через candles API; lead-lag только по ценам"}
    # акции
    stock = {k: load_stock(v) for k, v in FIGIS.items()}
    imoex = load_imoex(os.path.join(DATA, "imoex_5m.csv"))
    print("loaded bases", flush=True)
    for name, fn in FUT_FILES.items():
        fut = load_fut(os.path.join(DATA, fn))
        base = stock.get(name, imoex if name in ("IMOEXF", "RTS") else None)
        if base is None:
            continue
        res = lag_corr(fut, base)
        out[name] = res
        print(name, res, flush=True)
    json.dump(out, open(os.path.join(REPORTS, "batch_block_D.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_D.json", flush=True)


if __name__ == "__main__":
    main()
