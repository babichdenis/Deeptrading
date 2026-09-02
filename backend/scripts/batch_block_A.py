"""БЛОК A — regime-switching indicators (H-064/H-071/H-065/H-075/H-076), SHADOW.

Индикаторы на 5m барах акции (last-closed bar, полный прогрев). Для каждой July/H1-сделки
считаем индикатор на последнем закрытом баре, размечаем сигнал/группу, H-058 permutation
(signal vs rest). Окна disjoint: July 2026 (n=517) и 2026-H1-rest (2026-01..06).
A1 FD(Ehlers) chop(<med) vs trend(>med); A2 LinReg R^2>0.20; A3 N-ATR high/low (per-stock med);
A4 Z-Score MR-сигнал в low-vol (signal внутри low-vol vs не-MR).
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
        if b not in buckets:
            buckets[b] = [o, h, l, c, v]
        else:
            s = buckets[b]
            s[1] = max(s[1], h); s[2] = min(s[2], l); s[3] = c; s[4] += v
    return [(b,) + tuple(buckets[b]) for b in sorted(buckets)]


async def load_stock(figi):
    eng = create_async_engine(ENG)
    try:
        async with eng.connect() as c:
            r = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f "
                "AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "a": T0, "b": T1})
            rows = r.fetchall()
            if len(rows) > 1000:
                return [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
            r = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f "
                "AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "a": T0, "b": T1})
            raw = [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
            return aggregate_5m(raw)
    finally:
        await eng.dispose()


def sma(s, p):
    out = [None] * len(s)
    for i in range(p - 1, len(s)):
        out[i] = sum(s[i - p + 1:i + 1]) / p
    return out


def atr14(highs, lows, closes):
    n = len(closes); out = [None] * n
    if n < 2:
        return out
    tr = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    for i in range(14, n):
        out[i] = sum(tr[i - 13:i + 1]) / 14.0
    return out


def rolling_median(arr, window):
    n = len(arr); out = [None] * n
    for i in range(window - 1, n):
        seg = [x for x in arr[i - window + 1:i + 1] if x is not None]
        if len(seg) >= window // 2:
            out[i] = statistics.median(seg)
    return out


def fd_ehlers(closes, N=20):
    n = len(closes); out = [None] * n
    if n < N:
        return out
    N1 = N // 2
    logNN = math.log(N1 + N1) - math.log(2)
    for i in range(N - 1, n):
        seg = closes[i - N + 1:i + 1]
        num = 0.0
        for j in range(N1, N):
            num += abs(seg[j] - seg[j - N1])
        for j in range(0, N - N1):
            num += abs(seg[j] - seg[j + N1])
        den = abs(seg[-1] - seg[0])
        out[i] = (logNN * num / den) if den else 2.0
    return out


def linreg_r2(closes, N=20):
    n = len(closes); out = [None] * n
    xs = list(range(N)); mx = (N - 1) / 2
    my_den = sum((x - mx) ** 2 for x in xs)
    for i in range(N - 1, n):
        seg = closes[i - N + 1:i + 1]
        my = sum(seg) / N
        num = sum((x - mx) * (y - my) for x, y in zip(xs, seg))
        b = num / my_den if my_den else 0
        a = my - b * mx
        ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, seg))
        ss_tot = sum((y - my) ** 2 for y in seg)
        out[i] = 1 - ss_res / ss_tot if ss_tot else 0
    return out


def perm_test(a, b, n_perm=5000, seed=42):
    import numpy as np
    a = np.asarray(a, float); b = np.asarray(b, float)
    if len(a) < 20 or len(b) < 20:
        return None
    actual = a.mean() - b.mean()
    rng = np.random.default_rng(seed)
    g = np.r_[np.ones(len(a)), np.zeros(len(b))]
    nets = np.r_[a, b]
    perms = np.empty(n_perm)
    for i in range(n_perm):
        gi = rng.permutation(g)
        perms[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    return {"n_sig": int(len(a)), "n_rest": int(len(b)), "mean_sig": round(float(a.mean()), 3),
            "mean_rest": round(float(b.mean()), 3), "actual_diff": round(float(actual), 4),
            "p_one_sided": round(float((perms >= actual).mean()), 4),
            "p_two_sided": round(float((np.abs(perms) >= abs(actual)).mean()), 4)}


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
        o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
        c = [b[4] for b in bars]; v = [b[5] for b in bars]; ts5 = [b[0] for b in bars]
        atr = atr14(h, l, c); med = rolling_median(atr, 20 * 78)
        ind = {"FD": fd_ehlers(c, 20), "R2": linreg_r2(c, 20),
               "NATR": [atr[i] / c[i] if (atr[i] and c[i]) else None for i in range(len(c))],
               "SMA20": sma(c, 20)}
        ind["Z"] = [None] * len(c)
        for i in range(19, len(c)):
            seg = c[i - 19:i + 1]
            m = sum(seg) / 20; sd = (sum((x - m) ** 2 for x in seg) / 20) ** 0.5
            ind["Z"][i] = (c[i] - m) / sd if sd else 0
        stock[figi] = (ts5, ind, atr, med)
        print(f"  {figi}: {len(c)} баров", flush=True)
    FD_MED = {f: statistics.median([x for x in stock[f][1]["FD"] if x is not None]) for f in FIGIS}
    NATR_MED = {f: statistics.median([x for x in stock[f][1]["NATR"] if x is not None]) for f in FIGIS}
    print("FD_MED", {k: round(v, 3) for k, v in FD_MED.items()}, flush=True)
    print("NATR_MED", {k: round(v, 5) for k, v in NATR_MED.items()}, flush=True)

    def mark(trades):
        recs = {"A1_trend": [], "A1_chop": [], "A2_r2hi": [], "A3_high": [], "A3_low": [],
                "A4_mr_in_lowvol": []}
        for t in trades:
            figi = t["figi"]
            if figi not in stock:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ts5, ind, atr, med = stock[figi]
            j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
            if j < 60:
                continue
            net = float(t["net_rub"])
            fd = ind["FD"][j]; r2 = ind["R2"][j]; natr = ind["NATR"][j]
            z = ind["Z"][j]
            fd_med = FD_MED[figi]; natr_med = NATR_MED[figi]
            if fd is not None and fd_med is not None:
                if fd > fd_med: recs["A1_trend"].append(net)
                else: recs["A1_chop"].append(net)
            if r2 is not None and r2 > 0.20: recs["A2_r2hi"].append(net)
            if natr is not None and natr_med is not None:
                if natr > natr_med: recs["A3_high"].append(net)
                else: recs["A3_low"].append(net)
            if z is not None and natr is not None and natr_med is not None and natr <= natr_med:
                if abs(z) > 2.0: recs["A4_mr_in_lowvol"].append(net)
        return recs

    def run_block(trades):
        recs = mark(trades)
        alln = [float(t["net_rub"]) for t in trades if t["figi"] in stock]
        res = {}
        for k, sig in recs.items():
            rest = [x for x in alln if x not in sig]
            res[k] = perm_test(np.asarray(sig), np.asarray(rest)) if (len(sig) >= 20 and len(rest) >= 20) else {"n_sig": len(sig), "note": "insufficient"}
        return res

    out = {"schema": "batch_block_A", "method": "last-closed bar; disjoint July & H1-rest; H-058 permutation",
           "July": run_block(july), "H1_rest": run_block(h1)}
    for w in ("July", "H1_rest"):
        print(f"== {w} ==")
        for k, v in out[w].items():
            print(f"  {k}: {v}")
    json.dump(out, open(os.path.join(REPORTS, "batch_block_A.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_A.json", flush=True)


if __name__ == "__main__":
    main()
