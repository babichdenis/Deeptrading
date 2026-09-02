"""P1-3 H-064 пред-тест: mean-reversion в low-vol (SHADOW/read-only).

Гипотеза: в low-vol режиме momentum деградирует, а MR-сигналы (перепроданность Stoch/RSI)
дают прибыль там, где momentum молчит.
На July 2026 (517 сделок, baseline 5b44f3b383df):
  - режим волатильности: ATR14(5m) > rolling median 20д -> high, иначе low (last-closed бар).
  - MR-признаки на last-closed баре: RSI(14)<30, Stoch(14,3) K<20.
  - внутри low-vol: сравнить net/trade сделок с MR-сигналом vs без (permutation H-058).
  - контроль: high-vol (momentum должен работать -> MR не нужен).
READ-ONLY; сделки НЕ трогаем. Деливерабл: reports/h064_mr_lowvol_telemetry.{json,md}
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


def atr14(highs, lows, closes):
    n = len(closes); out = [None] * n
    if n < 2:
        return out
    tr = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    for i in range(14, n):
        out[i] = sum(tr[i - 13:i + 1]) / 14.0
    return out


def rolling_median(arr, window):
    import statistics
    n = len(arr); out = [None] * n
    for i in range(window - 1, n):
        seg = [x for x in arr[i - window + 1:i + 1] if x is not None]
        if len(seg) >= window // 2:
            out[i] = statistics.median(seg)
    return out


def rsi14(closes):
    n = len(closes); out = [None] * n
    if n < 2:
        return out
    gains = [0.0] * n; losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0); losses[i] = max(-d, 0)
    for i in range(14, n):
        ag = sum(gains[i - 13:i + 1]) / 14; al = sum(losses[i - 13:i + 1]) / 14
        out[i] = 100 - 100 / (1 + (ag / al if al else 0)) if al else 100
    return out


def stoch_k(highs, lows, closes, k=14, d=3):
    n = len(closes); out = [None] * n
    for i in range(k - 1, n):
        hh = max(highs[i - k + 1:i + 1]); ll = min(lows[i - k + 1:i + 1])
        if hh == ll:
            out[i] = 50.0
        else:
            out[i] = (closes[i] - ll) / (hh - ll) * 100
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
    return {"actual_diff": round(float(actual), 4), "p_two_sided": round(float((np.abs(perms) >= abs(actual)).mean()), 4),
            "p_one_sided": round(float((perms >= actual).mean()), 4), "n_a": int(len(a)), "n_b": int(len(b)),
            "mean_a": round(float(a.mean()), 3), "mean_b": round(float(b.mean()), 3)}


def main():
    import numpy as np
    trades = list(csv.DictReader(open(TRADES)))
    print(f"trades July: {len(trades)}", flush=True)
    stock = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        closes = [b[1] for b in bars]; highs = [b[2] for b in bars]; lows = [b[3] for b in bars]; ts5 = [b[0] for b in bars]
        a = atr14(highs, lows, closes); med = rolling_median(a, 20 * 78)
        rsi = rsi14(closes); sk = stoch_k(highs, lows, closes)
        stock[figi] = (ts5, a, med, rsi, sk)
        print(f"  {figi}: {len(bars)} баров", flush=True)

    recs = []
    for t in trades:
        figi = t["figi"]
        if figi not in stock:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        ts5, a, med, rsi, sk = stock[figi]
        j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
        if j < 50 or med[j] is None or a[j] is None or rsi[j] is None or sk[j] is None:
            continue
        vol = "high" if a[j] > med[j] else "low"
        mr = (rsi[j] < 30) or (sk[j] < 20)
        recs.append({"vol": vol, "mr": mr, "net": float(t["net_rub"])})

    print(f"marked: {len(recs)}", flush=True)
    low = [r["net"] for r in recs if r["vol"] == "low"]
    high = [r["net"] for r in recs if r["vol"] == "high"]
    low_mr = [r["net"] for r in recs if r["vol"] == "low" and r["mr"]]
    low_non = [r["net"] for r in recs if r["vol"] == "low" and not r["mr"]]
    high_mr = [r["net"] for r in recs if r["vol"] == "high" and r["mr"]]
    high_non = [r["net"] for r in recs if r["vol"] == "high" and not r["mr"]]

    tests = {
        "low_vol_MR_vs_nonMR": perm_test(np.asarray(low_mr), np.asarray(low_non)),
        "high_vol_MR_vs_nonMR": perm_test(np.asarray(high_mr), np.asarray(high_non)),
        "low_vol_vs_high_vol": perm_test(np.asarray(low), np.asarray(high)),
    }
    for k, v in tests.items():
        print(f"  {k}: {v}", flush=True)

    result = {"schema": "h064_mr_lowvol_telemetry", "window": "July 2026",
              "method": "vol=ATR14>20d-median; MR=RSI14<30 or Stoch14,3 K<20; last-closed bar",
              "n_marked": len(recs),
              "counts": {"low": len(low), "high": len(high), "low_mr": len(low_mr),
                         "low_non": len(low_non), "high_mr": len(high_mr), "high_non": len(high_non)},
              "means": {"low_mr": round(float(np.mean(low_mr)), 3) if low_mr else None,
                        "low_non": round(float(np.mean(low_non)), 3) if low_non else None,
                        "high_mr": round(float(np.mean(high_mr)), 3) if high_mr else None,
                        "high_non": round(float(np.mean(high_non)), 3) if high_non else None},
              "tests": tests}
    with open(os.path.join(REPORTS, "h064_mr_lowvol_telemetry.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = ["# H-064 пред-тест: mean-reversion в low-vol (SHADOW, July 2026)", "",
          "Гипотеза: в low-vol momentum деградирует, MR-сигналы (RSI<30 / Stoch K<20) дают прибыль.",
          f"Сделок размечено: {len(recs)}. Метод: vol=ATR14>rolling median 20д; last-closed бар.", "",
          "## Средний net/trade по бакетам", "", "| бакет | n | net/t |", "|---|---|---|",
          f"| low-vol + MR | {len(low_mr)} | {result['means']['low_mr']} |",
          f"| low-vol − MR | {len(low_non)} | {result['means']['low_non']} |",
          f"| high-vol + MR | {len(high_mr)} | {result['means']['high_mr']} |",
          f"| high-vol − MR | {len(high_non)} | {result['means']['high_non']} |",
          "", "## Permutation тесты", "",
          "| тест | n_a/n_b | mean_a | mean_b | diff | p_one | p_two |", "|---|---|---|---|---|---|---|"]
    for k, v in tests.items():
        if v:
            md.append(f"| {k} | {v['n_a']}/{v['n_b']} | {v['mean_a']} | {v['mean_b']} | {v['actual_diff']} | {v['p_one_sided']} | {v['p_two_sided']} |")
        else:
            md.append(f"| {k} | insufficient n |")
    open(os.path.join(REPORTS, "h064_mr_lowvol_telemetry.md"), "w").write("\n".join(md))
    print("saved h064_mr_lowvol_telemetry.json/.md", flush=True)


if __name__ == "__main__":
    main()
