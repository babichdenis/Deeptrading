"""H-081 — leverage (P2, contingent on H-079), SHADOW.
Gross exposure L=1.5/2.0, risk-budget frame (sum size*NATR <= 25% capital), ONLY high-vol regime,
pool = base 5 (Part C selected nothing -> base 5). H-059 MC on day-level PnL. net/DD vs no-leverage.
T-cost from H-079 included. Financing cost NOT modeled (minute-scale holds -> negligible) - caveat.
"""
import sys, os, json, csv, bisect, asyncio, statistics
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import numpy as np

REPORTS = "/Users/Denis/Dev/Deeptrading/backend/reports"
TRADES = os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv")
TCOST = os.path.join(REPORTS, "h079_tcost.json")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T0 = datetime(2025, 12, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
ENG = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"
DD_THRESH = 0.20
N_BOOT = 2000
SEED = 42


def cost_oneway_frac(figi, notional):
    d = json.load(open(TCOST))["per_ticker"].get(figi, {}).get("cost_pct_at_grid", {})
    if not d:
        d = json.load(open(TCOST))["aggregate_mean"]
    xs = np.array([float(k) for k in d]); ys = np.array([d[k] / 100.0 for k in d])
    return float(np.interp(notional, xs, ys))


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
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    for i in range(14, n):
        out[i] = sum(tr[i - 13:i + 1]) / 14.0
    return out


def max_dd(equity):
    peak = equity[0]; mdd = 0.0
    for x in equity:
        peak = max(peak, x); mdd = min(mdd, x - peak)
    return mdd


def simulate(trades, natr_full, ts_list, name_med, total, L, tcost=True):
    """L=None -> no-leverage (owner baseline). L=1.5/2.0 -> leverage, high-vol regime only, within risk-budget."""
    avail = (L * 0.9 * total) if L else (0.9 * total)
    per_name_cap = (L * 0.25 * total) if L else (0.25 * total)
    base_size = (L * 10000.0) if L else 10000.0
    risk_budget = 0.25 * total
    by_day = {}
    for t in trades:
        by_day.setdefault(t["dt"].date(), []).append(t)
    equity = [0.0]; taken = []; missed = []; day_pnl = []
    hv_funded = 0; hv_pool = 0
    for d in sorted(by_day):
        used = 0.0; used_figi = {f: 0.0 for f in FIGIS}; risk_used = 0.0; dsum = 0.0
        for t in sorted(by_day[d], key=lambda x: x["dt"]):
            net = t["net"]; figi = t["figi"]
            j = bisect.bisect_right(ts_list[figi], t["dt"] - timedelta(minutes=5)) - 1
            natr = natr_full[figi].get(ts_list[figi][j]) if j >= 0 else None
            hv = L is not None and natr is not None and natr > name_med[figi]
            if hv:
                hv_pool += 1
            eff_base = (L * 10000.0) if hv else 10000.0
            eff_cap = (L * 0.25 * total) if hv else (0.25 * total)
            size = min(eff_base, eff_cap)
            cap_left = eff_cap - used_figi[figi]
            if natr:
                risk_left = risk_budget - risk_used
                size = min(size, risk_left / natr if natr > 0 else size)
            alloc = min(size, cap_left)
            if alloc > 0 and used + alloc <= avail:
                used += alloc; used_figi[figi] += alloc
                if natr:
                    risk_used += alloc * natr
                pnl = net * (alloc / 10000.0)
                if tcost:
                    cof = cost_oneway_frac(figi, alloc)
                    pnl -= (2 * cof - 0.0014) * alloc
                if hv:
                    hv_funded += 1
                equity.append(equity[-1] + pnl); taken.append(pnl); dsum += pnl
            else:
                missed.append(net)
        day_pnl.append(dsum)
    taken_v = [x for x in taken if x != 0]
    pos = sum(x for x in taken_v if x > 0); neg = abs(sum(x for x in taken_v if x < 0))
    return {"L": L, "total": total, "n_taken": len(taken), "n_missed": len(missed),
            "portfolio_net": round(sum(taken), 2), "PF": round(pos / neg, 3) if neg else None,
            "max_DD": round(max_dd(equity), 2), "day_pnl": day_pnl,
            "hv_funded": hv_funded, "hv_pool": hv_pool}


def mc_day_bootstrap(day_pnl, total, n_boot=N_BOOT, seed=SEED):
    rng = np.random.default_rng(seed)
    day = np.asarray(day_pnl); n = len(day)
    if n == 0:
        return {}
    block = max(1, n // 12)
    nets = []; dds = []
    for _ in range(n_boot):
        idx = rng.integers(0, n - block + 1, size=(n + block - 1) // block)
        sel = np.concatenate([day[i:i + block] for i in idx])[:n]
        eq = np.cumsum(sel); dd = min(0, np.min(eq - np.maximum.accumulate(eq)))
        nets.append(eq[-1]); dds.append(dd)
    nets = np.asarray(nets); dds = np.asarray(dds)
    return {"net_p10": round(float(np.percentile(nets, 10)), 1),
            "net_p50": round(float(np.percentile(nets, 50)), 1),
            "net_p90": round(float(np.percentile(nets, 90)), 1),
            "dd_p10": round(float(np.percentile(dds, 10)), 1),
            "dd_med": round(float(np.percentile(dds, 50)), 1),
            "P_margin_call": round(float((dds <= -DD_THRESH * total).mean()), 4)}


def main():
    rows = list(csv.DictReader(open(TRADES)))
    trades = [{"figi": t["figi"], "dt": datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00")), "net": float(t["net_rub"])} for t in rows]
    print(f"H1 trades={len(trades)}", flush=True)
    natr_full = {}; ts_list = {}; name_med = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5 = [b[0] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; c = [b[4] for b in bars]
        atr = atr14(h, l, c)
        full = {}; vals = []
        for j in range(14, len(c)):
            v = (atr[j] / c[j]) if (atr[j] and c[j]) else None
            if v:
                full[ts5[j] + timedelta(minutes=5)] = v
                vals.append(v)
        natr_full[figi] = full; ts_list[figi] = sorted(full)
        name_med[figi] = statistics.median(vals)
    res = {"schema": "h081_leverage",
           "method": "gross L=1.5/2.0 high-vol-only, risk-budget sum(size*NATR)<=25% capital, pool=base5, H-059 MC day-block, T-cost on, financing not modeled (minute holds)",
           "DD_margin_call_thresh_frac": DD_THRESH}
    for total in (30000, 50000):
        sims = {"no_lev": simulate(trades, natr_full, ts_list, name_med, total, None),
                "L15": simulate(trades, natr_full, ts_list, name_med, total, 1.5),
                "L20": simulate(trades, natr_full, ts_list, name_med, total, 2.0)}
        out = {}
        for k, s in sims.items():
            mcres = mc_day_bootstrap(s["day_pnl"], total)
            out[k] = {kk: vv for kk, vv in s.items() if kk != "day_pnl"}
            out[k]["mc"] = mcres
        ref_dd = out["no_lev"]["mc"].get("dd_p10")
        rng = np.random.default_rng(SEED)
        for k in ("L15", "L20"):
            day = np.asarray(sims[k]["day_pnl"]); n = len(day); block = max(1, n // 12)
            dds = []
            for _ in range(N_BOOT):
                idx = rng.integers(0, n - block + 1, size=(n + block - 1) // block)
                sel = np.concatenate([day[i:i + block] for i in idx])[:n]
                eq = np.cumsum(sel); dds.append(min(0, np.min(eq - np.maximum.accumulate(eq))))
            if ref_dd is not None:
                out[k]["mc"]["P_dd_worse_than_no_lev"] = round(float((np.asarray(dds) <= ref_dd).mean()), 4)
        res[f"total_{total}"] = out
        for k, v in out.items():
            print(total, k, "net=", v["portfolio_net"], "DD=", v["max_DD"], "PF=", v["PF"],
                  "hv=", v["hv_funded"], "/", v["hv_pool"], "P_mc=", v["mc"].get("P_margin_call"),
                  "P_dd>no_lev=", v["mc"].get("P_dd_worse_than_no_lev"))
    json.dump(res, open(os.path.join(REPORTS, "h081_leverage.json"), "w"), ensure_ascii=False, indent=2)
    print("saved h081_leverage.json", flush=True)


if __name__ == "__main__":
    main()
