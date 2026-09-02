"""P2-5 Attribution новых индикаторов (H-049/H-050/H-052/H-054) — research, SHADOW.

Кандидаты-признаки на 5m барах акции (July 2026, n=517, baseline 5b44f3b383df):
  MFI(14), OBV(slope), CMF(20), Stoch(14,3) K, StochRSI(14,14,3) K, Squeeze Momentum, ADX(14), Aroon(25).
Для каждой July-сделки считаем индикатор на последнем закрытом баре (last-closed), размечаем
«сигнал есть» vs «нет» и сравниваем net/trade пермутационным тестом (как H-058 / EXP-003 presence).
Сигналы (long-agnostic, двусторонне):
  MFI<20 (oversold) / MFI>80 (overbought); CMF>0 (приток); Stoch K<20 / K>80; StochRSI K<20;
  Squeeze ON (BB внутри KC) + momentum!=0; ADX>25 (trend); Aroon up>100 / down>100; OBV slope>0.
READ-ONLY. Деливерабл: reports/indicator_attribution_v1.{json,md}
"""
import sys, os, json, csv, bisect, asyncio
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
TRADES = os.path.join(REPORTS, "5b44f3b383df", "trades.csv")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
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


def ema(s, p):
    out = []; k = 2 / (p + 1); e = None
    for v in s:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def mfi(highs, lows, closes, vols, p=14):
    n = len(closes); out = [None] * n
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    for i in range(1, n):
        mf = tp[i] * vols[i]
        if tp[i] >= tp[i - 1]:
            pos = mf; neg = 0.0
        else:
            pos = 0.0; neg = mf
        if i < p:
            continue
        pp = sum(pos if (tp[j] >= tp[j - 1]) else 0 for j in range(i - p + 1, i + 1) for _ in [0])
        # посчитаем аккуратно
        pos_sum = 0.0; neg_sum = 0.0
        for j in range(i - p + 1, i + 1):
            m = tp[j] * vols[j]
            if tp[j] >= tp[j - 1]:
                pos_sum += m
            else:
                neg_sum += m
        if neg_sum == 0:
            out[i] = 100.0
        else:
            mr = pos_sum / neg_sum
            out[i] = 100 - 100 / (1 + mr)
    return out


def obv(closes, vols):
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + vols[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - vols[i]
        else:
            out[i] = out[i - 1]
    return out


def cmf(highs, lows, closes, vols, p=20):
    n = len(closes); out = [None] * n
    for i in range(p - 1, n):
        mf = []; vol = 0.0
        for j in range(i - p + 1, i + 1):
            hl = highs[j] - lows[j]
            m = (closes[j] - lows[j] - (highs[j] - closes[j])) / hl if hl else 0
            mf.append(m * vols[j]); vol += vols[j]
        out[i] = sum(mf) / vol if vol else 0
    return out


def stoch_k(highs, lows, closes, k=14):
    n = len(closes); out = [None] * n
    for i in range(k - 1, n):
        hh = max(highs[i - k + 1:i + 1]); ll = min(lows[i - k + 1:i + 1])
        out[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    return out


def stochrsi(closes, p=14, k=3):
    n = len(closes); rsi = [None] * n
    gains = [0.0] * n; losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0); losses[i] = max(-d, 0)
    for i in range(p, n):
        ag = sum(gains[i - p + 1:i + 1]) / p; al = sum(losses[i - p + 1:i + 1]) / p
        rsi[i] = 100 - 100 / (1 + (ag / al if al else 0)) if al else 100
    # Stoch of rsi
    out = [None] * n
    for i in range(p + k - 1, n):
        vals = [r for r in rsi[i - k + 1:i + 1] if r is not None]
        if not vals:
            continue
        hh = max(vals); ll = min(vals)
        out[i] = 50.0 if hh == ll else (rsi[i] - ll) / (hh - ll) * 100
    return out


def adx14(highs, lows, closes):
    n = len(closes); out = [None] * n
    if n < 30:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    ups = [max(highs[i] - highs[i - 1], 0) for i in range(1, n)]
    dns = [max(lows[i - 1] - lows[i], 0) for i in range(1, n)]
    for i in range(14, n):
        tr14 = sum(trs[i - 13:i + 1])
        if tr14 == 0:
            out[i] = 50.0; continue
        pdi = sum(ups[i - 13:i + 1]) / tr14 * 100; mdi = sum(dns[i - 13:i + 1]) / tr14 * 100
        out[i] = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return out


def aroon(highs, lows, p=25):
    n = len(highs); up = [None] * n; dn = [None] * n
    for i in range(p, n):
        wh = max(highs[i - p + 1:i + 1]); wl = min(lows[i - p + 1:i + 1])
        th = p - 1 - [j for j in range(i - p + 1, i + 1) if highs[j] == wh][0]
        tl = p - 1 - [j for j in range(i - p + 1, i + 1) if lows[j] == wl][0]
        up[i] = (p - th) / p * 100; dn[i] = (p - tl) / p * 100
    return up, dn


def squeeze(highs, lows, closes, p=20):
    """Squeeze Momentum: Bollinger(2σ) внутри Keltner(1.5 ATR) -> squeeze on; momentum = linreg slope."""
    n = len(closes); out = [None] * n; on = [None] * n
    ema_m = ema(closes, p)
    sd = []
    for i in range(n):
        if i < p - 1:
            sd.append(None); continue
        seg = closes[i - p + 1:i + 1]
        m = sum(seg) / p
        sd.append((sum((x - m) ** 2 for x in seg) / p) ** 0.5)
    # Keltner
    atr = adx14(highs, lows, closes)
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    atr_s = [None] * n
    for i in range(14, n):
        atr_s[i] = sum(tr[i - 13:i + 1]) / 14
    for i in range(p, n):
        if sd[i] is None or atr_s[i] is None:
            continue
        bb = 2 * sd[i]; kc = 1.5 * atr_s[i]
        on[i] = bb < kc
        out[i] = closes[i] - ema_m[i]
    return out, on


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
    return {"actual_diff": round(float(actual), 4), "p_one_sided": round(float((perms >= actual).mean()), 4),
            "p_two_sided": round(float((np.abs(perms) >= abs(actual)).mean()), 4),
            "n_sig": int(len(a)), "n_nosig": int(len(b)),
            "mean_sig": round(float(a.mean()), 3), "mean_nosig": round(float(b.mean()), 3)}


def main():
    import numpy as np
    trades = list(csv.DictReader(open(TRADES)))
    print(f"trades July: {len(trades)}", flush=True)
    stock = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
        c = [b[4] for b in bars]; v = [b[5] for b in bars]; ts5 = [b[0] for b in bars]
        ind = {
            "MFI": mfi(h, l, c, v),
            "OBV": obv(c, v), "CMF": cmf(h, l, c, v), "Stoch": stoch_k(h, l, c),
            "StochRSI": stochrsi(c), "ADX": adx14(h, l, c),
        }
        au, ad = aroon(h, l); ind["AroonUp"] = au; ind["AroonDn"] = ad
        sq, sqon = squeeze(h, l, c); ind["Squeeze"] = sq; ind["SqueezeOn"] = sqon
        stock[figi] = (ts5, ind, len(c))
        print(f"  {figi}: {len(c)} баров", flush=True)

    recs = {name: [] for name in ["MFI_lo", "MFI_hi", "CMF_pos", "Stoch_lo", "Stoch_hi",
                                  "StochRSI_lo", "ADX_tr", "Aroon_up", "Aroon_dn", "OBV_up", "Squeeze_on"]}
    for t in trades:
        figi = t["figi"]
        if figi not in stock:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        ts5, ind, n = stock[figi]
        j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
        if j < 60:
            continue
        net = float(t["net_rub"])
        if ind["MFI"][j] is not None:
            if ind["MFI"][j] < 20: recs["MFI_lo"].append(net)
            elif ind["MFI"][j] > 80: recs["MFI_hi"].append(net)
        if ind["CMF"][j] is not None and ind["CMF"][j] > 0: recs["CMF_pos"].append(net)
        if ind["Stoch"][j] is not None:
            if ind["Stoch"][j] < 20: recs["Stoch_lo"].append(net)
            elif ind["Stoch"][j] > 80: recs["Stoch_hi"].append(net)
        if ind["StochRSI"][j] is not None and ind["StochRSI"][j] < 20: recs["StochRSI_lo"].append(net)
        if ind["ADX"][j] is not None and ind["ADX"][j] > 25: recs["ADX_tr"].append(net)
        if ind["AroonUp"][j] is not None and ind["AroonUp"][j] > 100: recs["Aroon_up"].append(net)
        if ind["AroonDn"][j] is not None and ind["AroonDn"][j] > 100: recs["Aroon_dn"].append(net)
        if ind["OBV"][j] is not None and j > 1 and ind["OBV"][j] > ind["OBV"][j - 1]: recs["OBV_up"].append(net)
        if ind["SqueezeOn"][j]: recs["Squeeze_on"].append(net)

    # базовая группа (все размеченные) для сравнения "signal vs rest"
    allnets = [float(t["net_rub"]) for t in trades if t["figi"] in stock]
    tests = {}
    for name, sig in recs.items():
        rest = [x for x in allnets if x not in sig]
        if len(sig) >= 20 and len(rest) >= 20:
            tests[name] = perm_test(np.asarray(sig), np.asarray(rest))
        else:
            tests[name] = {"n_sig": len(sig), "note": "insufficient"}
    for k, v in tests.items():
        print(f"  {k}: {v}", flush=True)

    result = {"schema": "indicator_attribution_v1", "window": "July 2026",
              "method": "signal vs rest net/trade permutation; last-closed bar",
              "n_trades": len(trades), "tests": tests}
    with open(os.path.join(REPORTS, "indicator_attribution_v1.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = ["# Атрибуция новых индикаторов (P2-5, July 2026, SHADOW)", "",
          "Сигнал vs остальные сделки (net/trade permutation). last-closed бар.", "",
          "| индикатор (сигнал) | n_sig | mean_sig | n_rest | mean_rest | p_one | p_two |", "|---|---|---|---|---|---|---|"]
    for k, v in tests.items():
        if v and "p_one_sided" in v:
            md.append(f"| {k} | {v['n_sig']} | {v['mean_sig']} | {v['n_nosig']} | {v['mean_nosig']} | {v['p_one_sided']} | {v['p_two_sided']} |")
        else:
            md.append(f"| {k} | {v.get('n_sig',0)} | — | — | — | — | insufficient |")
    open(os.path.join(REPORTS, "indicator_attribution_v1.md"), "w").write("\n".join(md))
    print("saved indicator_attribution_v1.json/.md", flush=True)


if __name__ == "__main__":
    main()
