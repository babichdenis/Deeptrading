import sys, json, glob
import numpy as np

FILES = sorted(glob.glob("/Users/Denis/Dev/Deeptrading/backend/reports/_h082_*.json"))
rows = []
for f in FILES:
    rows += json.load(open(f))
print("total rows", len(rows))

def skew(x):
    x = np.asarray(x, float)
    if len(x) < 3 or x.std() == 0: return None
    z = (x - x.mean()) / x.std(ddof=0)
    return float((z**3).mean())

def dd(net_arr):
    eq = np.cumsum(np.asarray(net_arr, float))
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())

def metrics(sub):
    sub = list(sub)
    n = len(sub)
    if n == 0: return {}
    base_ret = np.array([r["base_net"]/r["base_notional"] for r in sub])
    pyr_ret = np.array([r["pyr_net"]/r["pyr_notional"] for r in sub])
    base_net = np.array([r["base_net"] for r in sub])
    pyr_net = np.array([r["pyr_net"] for r in sub])
    return dict(
        n=n,
        base_net_total=round(float(base_net.sum()),1),
        pyr_net_total=round(float(pyr_net.sum()),1),
        base_net_per_trade=round(float(base_net.mean()),3),
        pyr_net_per_trade=round(float(pyr_net.mean()),3),
        base_ret_mean=round(float(base_ret.mean()),4),
        pyr_ret_mean=round(float(pyr_ret.mean()),4),
        ret_ratio=round(float(pyr_ret.mean()/base_ret.mean()),3) if base_ret.mean()!=0 else None,
        base_right_pct=round(float((base_ret>1.0).mean()),3),
        pyr_right_pct=round(float((pyr_ret>1.0).mean()),3),
        base_skew=round(skew(base_ret),3),
        pyr_skew=round(skew(pyr_ret),3),
        base_dd=round(dd(base_net),1),
        pyr_dd=round(dd(pyr_net),1),
        avg_bars=round(float(np.mean([r["bars"] for r in sub])),1),
        n_units_dist={str(k): int(sum(1 for r in sub if r["n_units"]==k)) for k in [1,2,3]},
    )

overall = metrics(rows)
by_window = {w: metrics([r for r in rows if r["window"]==w]) for w in ["2025-H1","2025-H2","2026-H1"]}
ratio = overall.get("ret_ratio")
gate = (ratio is not None and ratio >= 1.15)
out = dict(n_total=len(rows), overall=overall, by_window=by_window,
           gate_passed=bool(gate),
           gate_rule="pyr_ret_mean / base_ret_mean >= 1.15 (coverage=1.0, pyramiding applies to all trades)")
json.dump(out, open("/Users/Denis/Dev/Deeptrading/backend/reports/h082_pyramiding.json","w"), indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2, ensure_ascii=False))
