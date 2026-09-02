"""ЧАСТЬ 1 — H-077 v3 allocation (БЕЗ vol-target), SHADOW. На базе batch_block_E.py.

Сценарий владельца (owner): fixed 10k/сделку, cap per name 25% (size=min(10k,0.25*total)),
dry-powder 10%, vol-target НЕТ, + risk-budget D: sum(size_i*N-ATR_i) <= 25% total одновременно.
Сравнить с baseline(v1 fixed) и v1 vol-target. total 30k/50k. Метрики + H-059 MC.
day-capacity модель (сделки в день конкурируют; капитал освобождается на след. день).
"""
import sys, os, json, csv, bisect, asyncio, statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
TRADES = os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T0 = datetime(2025, 12, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
ENG = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"


def aggregate_5m(raw):
    buckets = {}
    for ts, o, h, l, c, v in raw:
        b = ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)
        buckets[b] = [o, h, l, c, v] if b not in buckets else [buckets[b][0], max(buckets[b][1], h), min(buckets[b][2], l), c, buckets[b][4] + v]
    return [(b,) + tuple(buckets[b]) for b in sorted(buckets)]


async def load_stock(figi):
    eng = create_async_engine(ENG)
    try:
        async with eng.connect() as c:
            r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": T0, "b": T1})
            rows = r.fetchall()
            if len(rows) > 1000:
                return [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
            r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": T0, "b": T1})
            raw = [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
            return aggregate_5m(raw)
    finally:
        await eng.dispose()


def atr14(h, l, c):
    n = len(c); out = [None] * n
    tr = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, n)]
    for i in range(14, n):
        out[i] = sum(tr[i-13:i+1]) / 14.0
    return out


def max_dd(equity):
    peak = equity[0]; mdd = 0.0
    for x in equity:
        peak = max(peak, x); mdd = min(mdd, x - peak)
    return mdd


def simulate(trades, natr_dec, total, mode):
    if mode == "baseline":
        avail = total; per_name_cap = total; dry = 0.0; voltarget = False; risk_budget = None
        base_size = 10000.0
    elif mode == "v1_voltarget":
        avail = total * 0.9; per_name_cap = 0.25 * total; dry = 0.1; voltarget = True; risk_budget = None
        base_size = 10000.0
    else:  # owner
        avail = total * 0.9; per_name_cap = 0.25 * total; dry = 0.1; voltarget = False
        risk_budget = 0.25 * total; base_size = 10000.0
    natr_med = statistics.median([v for v in natr_dec.values() if v is not None]) if voltarget else None
    by_day = {}
    for t in trades:
        by_day.setdefault(t["dt"].date(), []).append(t)
    equity = [0.0]; taken = []; missed = []
    idle_list = []; funded_names = []
    for d in sorted(by_day):
        used = 0.0; used_figi = {f: 0.0 for f in FIGIS}; risk_used = 0.0; day_funded = set()
        for t in sorted(by_day[d], key=lambda x: x["dt"]):
            net = t["net"]; figi = t["figi"]
            natr = natr_dec.get((figi, t["dt"])) or (natr_med if voltarget else None)
            if voltarget and natr_med:
                size = base_size * (natr_med / natr) if natr and natr > 0 else base_size
            else:
                size = min(base_size, per_name_cap)
            cap_left = per_name_cap - used_figi[figi]
            if risk_budget is not None and natr:
                risk_left = risk_budget - risk_used
                size = min(size, risk_left / natr if natr > 0 else size)
            alloc = min(size, cap_left)
            if alloc > 0 and used + alloc <= avail:
                used += alloc; used_figi[figi] += alloc; day_funded.add(figi)
                if risk_budget is not None and natr:
                    risk_used += alloc * natr
                pnl = net * (alloc / base_size)
                equity.append(equity[-1] + pnl); taken.append(pnl)
            else:
                missed.append(net)
        idle_list.append((avail - used) / total)
        funded_names.append(len(day_funded))
    taken_v = [x for x in taken if x != 0]
    pos = sum(x for x in taken_v if x > 0); neg = abs(sum(x for x in taken_v if x < 0))
    return {"mode": mode, "total": total, "n_taken": len(taken), "n_missed": len(missed),
            "portfolio_net": round(sum(taken), 2), "PF": round(pos/neg, 3) if neg else None,
            "max_DD": round(max_dd(equity), 2), "pct_idle_capital": round(100*statistics.mean(idle_list), 1),
            "max_funded_names": max(funded_names), "opportunity_cost_net": round(sum(missed), 2)}


def mc_compare(trades, natr_dec, total, mode, n_boot=2000, seed=42):
    import numpy as np
    rng = np.random.default_rng(seed)
    base = simulate(trades, natr_dec, total, "baseline")
    rule = simulate(trades, natr_dec, total, mode)
    by_day = {}
    for t in trades:
        by_day.setdefault(t["dt"].date(), []).append(t)
    days = sorted(by_day)
    day_net = np.asarray([sum(x["net"] for x in by_day[d]) for d in days])
    n = len(day_net); block = max(1, n // 12)
    boot_net = []; boot_dd = []
    for _ in range(n_boot):
        idx = rng.integers(0, n - block + 1, size=(n + block - 1) // block)
        sel = np.concatenate([day_net[i:i+block] for i in idx])[:n]
        eq = np.cumsum(sel); dd = min(0, np.min(eq - np.maximum.accumulate(eq)))
        boot_net.append(eq[-1]); boot_dd.append(dd)
    boot_net = np.asarray(boot_net); boot_dd = np.asarray(boot_dd)
    return {"total": total, "baseline_net": base["portfolio_net"], "scenario_net": rule["portfolio_net"],
            "scenario_net_pct_vs_baseline": round(100*(rule["portfolio_net"]-base["portfolio_net"])/abs(base["portfolio_net"]), 1) if base["portfolio_net"] else None,
            "scenario_net_mc_p10": round(float(np.percentile(boot_net, 90)), 1),
            "scenario_net_mc_p90": round(float(np.percentile(boot_net, 10)), 1),
            "baseline_maxDD": base["max_DD"], "scenario_maxDD": rule["max_DD"],
            "scenario_dd_better_p": round(float((boot_dd <= rule["max_DD"]).mean()), 4)}


def main():
    import numpy as np
    rows = list(csv.DictReader(open(TRADES)))
    trades = [{"figi": t["figi"], "dt": datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00")), "net": float(t["net_rub"])} for t in rows]
    print(f"H1 trades={len(trades)}", flush=True)
    stock_ts = {}; natr_dec = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5 = [b[0] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; c = [b[4] for b in bars]
        atr = atr14(h, l, c)
        stock_ts[figi] = ts5
        for j in range(14, len(c)):
            natr_dec[(figi, ts5[j] + timedelta(minutes=5))] = (atr[j]/c[j]) if (atr[j] and c[j]) else None
    for t in trades:
        figi = t["figi"]
        if figi not in stock_ts:
            continue
        j = bisect.bisect_right(stock_ts[figi], t["dt"] - timedelta(minutes=5)) - 1
        if j >= 14:
            natr_dec[(figi, t["dt"])] = natr_dec.get((figi, stock_ts[figi][j] + timedelta(minutes=5)))
    res = {"schema": "batch_block_E3", "method": "owner: fixed10k+cap25%+dry10%+risk-budget D(sum size*NATR<=25%); no vol-target; H-059 MC"}
    for total in (30000, 50000):
        res[f"total_{total}"] = {
            "baseline": simulate(trades, natr_dec, total, "baseline"),
            "v1_voltarget": simulate(trades, natr_dec, total, "v1_voltarget"),
            "owner": simulate(trades, natr_dec, total, "owner"),
            "mc_owner": mc_compare(trades, natr_dec, total, "owner"),
        }
    for k, v in res.items():
        if k in ("schema", "method"):
            continue
        for mode in ("baseline", "v1_voltarget", "owner"):
            print(k, mode, v[mode])
        print(k, "mc", v["mc_owner"])
    json.dump(res, open(os.path.join(REPORTS, "batch_block_E3.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_E3.json", flush=True)


if __name__ == "__main__":
    main()
