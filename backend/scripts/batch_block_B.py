"""БЛОК B — объёмное семейство (H-066..H-070), SHADOW. H-058 permutation (signal vs rest).

B1 CSI/CSC cluster sentiment; B2 VFI (Volume Flow); B3 VPCI; B4 VWMA; B5 BMP (Bull-Bear Balance).
Сигналы на last-closed 5m баре сделки. disjoint July(517)+H1-rest(2936).
"""
import sys, os, json, csv, bisect, asyncio, math, statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
JULY = os.path.join(REPORTS, "5b44f3b383df", "trades.csv")
H1 = os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv")
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


def rsum(arr, n):
    out = [None] * len(arr)
    for i in range(n - 1, len(arr)):
        out[i] = sum(arr[i - n + 1:i + 1])
    return out


def sma(arr, n):
    out = [None] * len(arr)
    for i in range(n - 1, len(arr)):
        out[i] = sum(arr[i - n + 1:i + 1]) / n
    return out


def ema(arr, n):
    out = [None] * len(arr)
    a = 2 / (n + 1)
    prev = None
    for i in range(len(arr)):
        if arr[i] is None:
            continue
        prev = arr[i] if prev is None else a * arr[i] + (1 - a) * prev
        out[i] = prev
    return out


def perm_test(a, b, n_perm=5000, seed=42):
    import numpy as np
    a = np.asarray(a, float); b = np.asarray(b, float)
    if len(a) < 20 or len(b) < 20:
        return {"n_sig": len(a), "n_rest": len(b), "note": "insufficient"}
    actual = a.mean() - b.mean()
    rng = np.random.default_rng(seed)
    g = np.r_[np.ones(len(a)), np.zeros(len(b))]; nets = np.r_[a, b]
    perms = np.empty(n_perm)
    for i in range(n_perm):
        gi = rng.permutation(g); perms[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    return {"n_sig": int(len(a)), "n_rest": int(len(b)), "mean_sig": round(float(a.mean()), 3),
            "mean_rest": round(float(b.mean()), 3), "actual_diff": round(float(actual), 4),
            "p_one_sided": round(float((perms >= actual).mean()), 4), "p_two_sided": round(float((np.abs(perms) >= abs(actual)).mean()), 4)}


def compute_indicators(bars):
    o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
    c = [b[4] for b in bars]; v = [b[5] for b in bars]; n = len(c)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    vol_signed = [v[i] if c[i] >= o[i] else -v[i] for i in range(n)]
    # B1 CSI cluster (rolling 20)
    rsig = rsum(vol_signed, 20); rvol = rsum(v, 20)
    B1 = [rsig[i] / rvol[i] if rvol[i] else 0 for i in range(n)]
    # B2 VFI
    dv = [None] * n
    for i in range(20, n):
        dv[i] = (sum((tp[j] - sum(tp[i-20:i+1])/20) ** 2 for j in range(i-19, i+1)) / 20) ** 0.5
    vfi_raw = [0.0] * n
    for i in range(1, n):
        if dv[i] and dv[i] > 0:
            vfi_raw[i] = v[i] * math.tanh((tp[i] - tp[i-1]) / (0.2 * dv[i]))
    B2 = ema(vfi_raw, 3)
    # B3 VPCI (price confirm * vol confirm)
    sc = sma(c, 20); sv = sma(v, 20)
    B3 = [None] * n
    for i in range(20, n):
        if sc[i-1] and sv[i-1]:
            B3[i] = (sc[i]/sc[i-1] - 1) * (sv[i]/sv[i-1])
    # B4 VWMA
    cv = [c[i]*v[i] for i in range(n)]
    rsum_cv = rsum(cv, 20); rsum_v = rsum(v, 20)
    B4 = [rsum_cv[i]/rsum_v[i] if rsum_v[i] else None for i in range(n)]
    # B5 BMP (bull-bear balance rolling 20)
    B5 = [rsig[i] / rvol[i] if rvol[i] else 0 for i in range(n)]
    return {"B1": B1, "B2": B2, "B3": B3, "B4": B4, "B5": B5, "close": c}


def main():
    import numpy as np
    july = list(csv.DictReader(open(JULY)))
    h1 = [t for t in csv.DictReader(open(H1)) if not (
        datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00")).year == 2026 and
        datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00")).month == 7)]
    print(f"July={len(july)} H1-rest={len(h1)}", flush=True)
    stock = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5 = [b[0] for b in bars]
        stock[figi] = (ts5, compute_indicators(bars))
        print(f"  {figi}: {len(bars)} баров", flush=True)

    def mark(trades):
        recs = {"B1_bull": [], "B2_vfi_up": [], "B3_vpci_up": [], "B4_above_vwma": [], "B5_bull": []}
        for t in trades:
            figi = t["figi"]
            if figi not in stock:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ts5, ind = stock[figi]
            j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
            if j < 60:
                continue
            net = float(t["net_rub"])
            if ind["B1"][j] is not None and ind["B1"][j] > 0.2: recs["B1_bull"].append(net)
            if ind["B2"][j] is not None and ind["B2"][j] > 0: recs["B2_vfi_up"].append(net)
            if ind["B3"][j] is not None and ind["B3"][j] > 0: recs["B3_vpci_up"].append(net)
            if ind["B4"][j] is not None and ind["close"][j] > ind["B4"][j]: recs["B4_above_vwma"].append(net)
            if ind["B5"][j] is not None and ind["B5"][j] > 0: recs["B5_bull"].append(net)
        return recs

    def run_block(trades):
        recs = mark(trades)
        alln = [float(t["net_rub"]) for t in trades if t["figi"] in stock]
        res = {}
        for k, sig in recs.items():
            rest = [x for x in alln if x not in sig]
            res[k] = perm_test(np.asarray(sig), np.asarray(rest))
        return res

    out = {"schema": "batch_block_B", "method": "last-closed bar; disjoint July & H1-rest; H-058 permutation",
           "July": run_block(july), "H1_rest": run_block(h1)}
    for w in ("July", "H1_rest"):
        print(f"== {w} ==")
        for k, v in out[w].items():
            print(f"  {k}: {v}")
    json.dump(out, open(os.path.join(REPORTS, "batch_block_B.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_B.json", flush=True)


if __name__ == "__main__":
    main()
