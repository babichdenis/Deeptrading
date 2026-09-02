"""БЛОК E — capital allocation (H-077), SHADOW. Симуляция на H1-сделках (gold_window, n=3481).

Baseline: fixed 10k/поз. Rule: per-name cap 25% + vol-target sizing (size ~ 1/N-ATR) + dry-powder 10%.
total=30k и 50k. Метрики: portfolio net, PF, max DD (peak-to-trough), % idle, max funded names,
opportunity-cost. H-059 MC (block bootstrap по неделям) сравнивает rule vs baseline.
Допущение overlap: сделки в ОДИН день конкурируют за капитал (держатся одновременно);
капитал освобождается на след. день (conservative capacity-оценка, т.к. exit_time неизвестен).
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
    """mode='baseline' (fixed 10k) или 'rule' (cap25%+voltarget+dry-powder)."""
    avail_total = total * 0.9 if mode == "rule" else total
    per_name_cap = 0.25 * total if mode == "rule" else total
    natr_med = statistics.median([v for v in natr_dec.values() if v is not None]) if mode == "rule" else None
    by_day = {}
    for t in trades:
        d = t["dt"].date()
        by_day.setdefault(d, []).append(t)
    equity = [0.0]; taken = []; missed = []
    idle_list = []; funded_names = []
    for d in sorted(by_day):
        used = 0.0; used_figi = {f: 0.0 for f in FIGIS}; day_funded = set()
        for t in sorted(by_day[d], key=lambda x: x["dt"]):
            net = t["net"]; figi = t["figi"]
            if mode == "baseline":
                alloc = 10000.0
                if used + alloc <= avail_total:
                    used += alloc; equity.append(equity[-1] + net)
                    taken.append(net); day_funded.add(figi)
                else:
                    missed.append(net)
            else:
                natr = natr_dec.get((figi, t["dt"]))
                if natr is None or natr <= 0:
                    natr = natr_med
                size = 10000.0 * (natr_med / natr) if natr_med else 10000.0
                cap_left = per_name_cap - used_figi[figi]
                alloc = min(cap_left, size)
                if alloc > 0 and used + alloc <= avail_total:
                    used += alloc; used_figi[figi] += alloc; day_funded.add(figi)
                    pnl = net * (alloc / 10000.0)
                    equity.append(equity[-1] + pnl); taken.append(pnl)
                else:
                    missed.append(net)
        idle_list.append((avail_total - used) / total)
        funded_names.append(len(day_funded))
    taken = [x for x in taken if x != 0]
    pos = sum(x for x in taken if x > 0); neg = abs(sum(x for x in taken if x < 0))
    return {
        "mode": mode, "total": total, "n_taken": len(taken), "n_missed": len(missed),
        "portfolio_net": round(sum(taken), 2),
        "PF": round(pos / neg, 3) if neg else None,
        "max_DD": round(max_dd(equity), 2),
        "pct_idle_capital": round(100 * statistics.mean(idle_list), 1),
        "max_funded_names": max(funded_names),
        "opportunity_cost_net": round(sum(missed), 2),
    }


def mc_compare(trades, natr_dec, total, n_boot=2000, seed=42):
    import numpy as np
    rng = np.random.default_rng(seed)
    base = simulate(trades, natr_dec, total, "baseline")
    rule = simulate(trades, natr_dec, total, "rule")
    # block bootstrap по неделям на net-последовательности взятых (по времени)
    by_day = {}
    for t in trades:
        by_day.setdefault(t["dt"].date(), []).append(t)
    days = sorted(by_day)
    # базовая net по дням (baseline take-all)
    day_net = []
    for d in days:
        s = 0.0
        for t in by_day[d]:
            # baseline всегда берёт (если капитал позволяет); для MC берём все (идеальный)
            s += t["net"]
        day_net.append(s)
    day_net = np.asarray(day_net)
    n = len(day_net)
    block = max(1, n // 12)
    rule_net = rule["portfolio_net"]; base_net = base["portfolio_net"]
    rule_dd = rule["max_DD"]; base_dd = base["max_DD"]
    boot_net = []; boot_dd = []
    for _ in range(n_boot):
        idx = rng.integers(0, n - block + 1, size=(n + block - 1) // block)
        sel = np.concatenate([day_net[i:i+block] for i in idx])[:n]
        eq = np.cumsum(sel); dd = min(0, np.min(eq - np.maximum.accumulate(eq)))
        boot_net.append(eq[-1]); boot_dd.append(dd)
    boot_net = np.asarray(boot_net); boot_dd = np.asarray(boot_dd)
    return {
        "total": total,
        "baseline_net": base_net, "rule_net": rule_net,
        "rule_net_pct_vs_baseline": round(100 * (rule_net - base_net) / abs(base_net), 1) if base_net else None,
        "rule_net_mc_p90": round(float(np.percentile(boot_net, 10)), 1),
        "rule_net_mc_p10": round(float(np.percentile(boot_net, 90)), 1),
        "baseline_maxDD": base_dd, "rule_maxDD": rule_dd,
        "rule_dd_better_p": round(float((boot_dd <= rule_dd).mean()), 4),
    }


def main():
    import numpy as np
    rows = list(csv.DictReader(open(TRADES)))
    trades = []
    for t in rows:
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        trades.append({"figi": t["figi"], "dt": dt, "net": float(t["net_rub"])})
    print(f"H1 trades={len(trades)}", flush=True)
    natr_lookup = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5 = [b[0] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; c = [b[4] for b in bars]
        atr = atr14(h, l, c)
        for j in range(14, len(c)):
            dtb = ts5[j] + timedelta(minutes=5)
            natr_lookup[(figi, dtb)] = (atr[j] / c[j]) if (atr[j] and c[j]) else None
        print(f"  {figi}: {len(c)} баров", flush=True)
    # для каждой сделки найдём natr по decision_time (last-closed бар)
    natr_dec = {}
    stock_ts = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        stock_ts[figi] = [b[0] for b in bars]
    for t in trades:
        figi = t["figi"]
        if figi not in stock_ts:
            continue
        j = bisect.bisect_right(stock_ts[figi], t["dt"] - timedelta(minutes=5)) - 1
        if j >= 14:
            natr_dec[(figi, t["dt"])] = natr_lookup.get((figi, stock_ts[figi][j] + timedelta(minutes=5)))
    res = {"schema": "batch_block_E", "method": "day-capacity model; vol-target size~1/N-ATR; dry-powder 10%; H-059 block-bootstrap MC"}
    for total in (30000, 50000):
        res[f"total_{total}"] = {
            "baseline": simulate(trades, natr_dec, total, "baseline"),
            "rule": simulate(trades, natr_dec, total, "rule"),
            "mc": mc_compare(trades, natr_dec, total),
        }
    for k, v in res.items():
        if k == "schema" or k == "method":
            continue
        for mode in ("baseline", "rule"):
            print(k, mode, v[mode])
        print(k, "mc", v["mc"])
    json.dump(res, open(os.path.join(REPORTS, "batch_block_E.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_E.json", flush=True)


if __name__ == "__main__":
    main()
