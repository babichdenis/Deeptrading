"""БЛОК C — индикаторные пробелы (H-071..H-074 + C4 adopt), SHADOW. H-058 (signal vs rest).

C1 Vortex; C2 TDI(RSI+SMA); C3 FRAMA; C4 Stochastic/CCI/Williams%R/Aroon/PSAR/Ichimoku/OBV/MFI/ADX-trend.
Переиспользуем mfi/obv/aroon/adx из p2_5_indicator_attribution. disjoint July(517)+H1-rest(2936).
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


def sma(a, n):
    out = [None] * len(a)
    for i in range(n - 1, len(a)):
        seg = [x for x in a[i - n + 1:i + 1] if x is not None]
        if len(seg) == n:
            out[i] = sum(seg) / n
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        g = max(d, 0) if i == 1 else (g*(n-1) + max(d, 0)) / n
        l = -min(d, 0) if i == 1 else (l*(n-1) + (-min(d, 0))) / n
        if i >= n:
            out[i] = 100 - 100/(1 + (g/l if l else 99))
    return out


def mfi(h, l, c, v, p=14):
    out = [None] * len(c); tp = [(h[i]+l[i]+c[i])/3 for i in range(len(c))]
    for i in range(p, len(c)):
        mf = [tp[j]*v[j] for j in range(i-p+1, i+1)]
        pos = sum(mf[j] for j in range(p) if tp[i-p+1+j] >= tp[i-p+j])
        neg = sum(mf[j] for j in range(p) if tp[i-p+1+j] < tp[i-p+j])
        out[i] = 100 - 100/(1 + (pos/neg if neg else 99))
    return out


def obv(c, v):
    out = [0.0] * len(c)
    for i in range(1, len(c)):
        out[i] = out[i-1] + (v[i] if c[i] > c[i-1] else (-v[i] if c[i] < c[i-1] else 0))
    return out


def adx14(h, l, c, n=14):
    out = [None] * len(c)
    tr = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    pdi = [0.0]*len(c); mdi = [0.0]*len(c)
    for i in range(1, len(c)):
        up = h[i]-h[i-1]; dn = l[i-1]-l[i]
        pdi[i] = (max(up, 0) if i == 1 else (pdi[i-1]*(n-1) + max(up, 0))/n)
        mdi[i] = (max(dn, 0) if i == 1 else (mdi[i-1]*(n-1) + max(dn, 0))/n)
    pdi = [100*x/(tr[i] if tr[i] else 1) for i, x in enumerate(pdi)]
    mdi = [100*x/(tr[i] if tr[i] else 1) for i, x in enumerate(mdi)]
    dx = [100*abs(pdi[i]-mdi[i])/(pdi[i]+mdi[i]) if (pdi[i]+mdi[i]) else 0 for i in range(len(c))]
    for i in range(n*2, len(c)):
        out[i] = (dx[i] if i == n*2 else (out[i-1]*(n-1)+dx[i])/n)
    return out


def aroon(h, l, p=25):
    out = [None] * len(h)
    for i in range(p, len(h)):
        win_h = h[i-p+1:i+1]; win_l = l[i-p+1:i+1]
        ah = win_h.index(max(win_h)); al = win_l.index(min(win_l))
        out[i] = (p - ah)/(p) * 100 - (p - al)/(p) * 100  # aroon_up - aroon_down
    return out


def stochastic(c, h, l, n=14):
    out = [None] * len(c)
    for i in range(n-1, len(c)):
        ll = min(l[i-n+1:i+1]); hh = max(h[i-n+1:i+1])
        out[i] = 100*(c[i]-ll)/(hh-ll) if hh > ll else 50
    return out


def cci(h, l, c, n=20):
    out = [None] * len(c); tp = [(h[i]+l[i]+c[i])/3 for i in range(len(c))]
    for i in range(n-1, len(c)):
        m = sum(tp[i-n+1:i+1])/n; md = sum(abs(x-m) for x in tp[i-n+1:i+1])/n
        out[i] = (tp[i]-m)/(0.015*md) if md else 0
    return out


def williams_r(h, l, c, n=14):
    out = [None] * len(c)
    for i in range(n-1, len(c)):
        hh = max(h[i-n+1:i+1]); ll = min(l[i-n+1:i+1])
        out[i] = -100*(hh-c[i])/(hh-ll) if hh > ll else -50
    return out


def vortex(h, l, c, n=14):
    vip = [None]*len(c); vim = [None]*len(c)
    for i in range(1, len(c)):
        vmp = abs(h[i]-l[i-1]); vmm = abs(l[i]-h[i-1])
        tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        vip[i] = vmp; vim[i] = vmm
    for i in range(n, len(c)):
        svp = sum(vip[i-n+1:i+1]); svm = sum(vim[i-n+1:i+1])
        str_ = sum(max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(i-n+1, i+1))
        vip[i] = svp/str_ if str_ else 0; vim[i] = svm/str_ if str_ else 0
    return vip, vim


def frama(c, n=10):
    out = [None]*len(c)
    for i in range(n, len(c)):
        seg = c[i-n+1:i+1]
        N1 = n//2
        num = sum(abs(seg[j]-seg[j-N1]) for j in range(N1, n)) + sum(abs(seg[j]-seg[j+N1]) for j in range(0, n-N1))
        den = abs(seg[-1]-seg[0]) or 1
        fd = (math.log(n)-math.log(2))*num/den
        fd = min(max(fd, 1), 2)
        alpha = math.exp(-4.6*(fd-1))
        out[i] = c[i] if out[i-1] is None else alpha*c[i] + (1-alpha)*out[i-1]
    return out


def psar(h, l, af=0.02, maxaf=0.2):
    out = [None]*len(h); is_long = True; sar = l[0]; ep = h[0]; a = af
    for i in range(1, len(h)):
        sar = sar + a*(ep-sar)
        if is_long:
            if l[i] < sar:
                is_long = False; sar = ep; ep = l[i]; a = af
            else:
                if h[i] > ep: ep = h[i]; a = min(a+af, maxaf)
        else:
            if h[i] > sar:
                is_long = True; sar = ep; ep = h[i]; a = af
            else:
                if l[i] < ep: ep = l[i]; a = min(a+af, maxaf)
        out[i] = sar
    return out


def ichimoku(c, h, l):
    tenkan = [None]*len(c); kijun = [None]*len(c)
    for i in range(8, len(c)):
        tenkan[i] = (max(h[i-8:i+1])+min(l[i-8:i+1]))/2
    for i in range(25, len(c)):
        kijun[i] = (max(h[i-25:i+1])+min(l[i-25:i+1]))/2
    return tenkan, kijun


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


def compute(bars):
    o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
    c = [b[4] for b in bars]; v = [b[5] for b in bars]
    vip, vim = vortex(h, l, c)
    tenkan, kijun = ichimoku(c, h, l)
    r = rsi(c, 13); r_sma = sma(r, 7)
    fr = frama(c); st = stochastic(c, h, l); cc = cci(h, l, c); wr = williams_r(h, l, c)
    ar = aroon(h, l); ps = psar(h, l); ob = obv(c, v); ob_sma = sma(ob, 20); mf = mfi(h, l, c, v); ad = adx14(h, l, c)
    n = len(c)
    ind = {
        "C1_vortex_up": [bool(vip[i] > vim[i]) if vip[i] is not None else None for i in range(n)],
        "C2_tdi_up": [bool(r[i] > r_sma[i]) if r_sma[i] is not None else None for i in range(n)],
        "C3_frama_up": [bool(c[i] > fr[i]) if fr[i] is not None else None for i in range(n)],
        "C4_stoch_lo": [bool(st[i] < 20) if st[i] is not None else None for i in range(n)],
        "C4_cci_lo": [bool(cc[i] < -100) if cc[i] is not None else None for i in range(n)],
        "C4_williams_lo": [bool(wr[i] < -80) if wr[i] is not None else None for i in range(n)],
        "C4_aroon_up": [bool(ar[i] > 0) if ar[i] is not None else None for i in range(n)],
        "C4_psar_up": [bool(c[i] > ps[i]) if ps[i] is not None else None for i in range(n)],
        "C4_ichimoku_up": [bool(tenkan[i] is not None and kijun[i] is not None and tenkan[i] > kijun[i]) for i in range(n)],
        "C4_obv_up": [bool(ob[i] > ob_sma[i]) if ob_sma[i] is not None else None for i in range(n)],
        "C4_mfi_lo": [bool(mf[i] < 20) if mf[i] is not None else None for i in range(n)],
        "C4_adx_trend": [bool(ad[i] > 25) if ad[i] is not None else None for i in range(n)],
    }
    return ind


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
        stock[figi] = (ts5, compute(bars))
        print(f"  {figi}: {len(bars)} баров", flush=True)

    def mark(trades):
        recs = {k: [] for k in stock[FIGIS[0]][1]}
        for t in trades:
            figi = t["figi"]
            if figi not in stock:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ts5, ind = stock[figi]
            j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
            if j < 80:
                continue
            net = float(t["net_rub"])
            for k in recs:
                val = ind[k][j]
                if val is True:
                    recs[k].append(net)
        return recs

    def run_block(trades):
        recs = mark(trades)
        alln = [float(t["net_rub"]) for t in trades if t["figi"] in stock]
        res = {}
        for k, sig in recs.items():
            rest = [x for x in alln if x not in sig]
            res[k] = perm_test(np.asarray(sig), np.asarray(rest))
        return res

    out = {"schema": "batch_block_C", "method": "last-closed bar; disjoint July & H1-rest; H-058 permutation",
           "July": run_block(july), "H1_rest": run_block(h1)}
    for w in ("July", "H1_rest"):
        print(f"== {w} ==")
        for k, v in out[w].items():
            print(f"  {k}: {v}")
    json.dump(out, open(os.path.join(REPORTS, "batch_block_C.json"), "w"), ensure_ascii=False, indent=2)
    print("saved batch_block_C.json", flush=True)


if __name__ == "__main__":
    main()
