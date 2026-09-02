"""P0-2(b) H-058 на 2026-H1: волатильность INSIGHT-002 (high-vol vs low-vol).

Режим волатильности per-акция на decision_ts: ATR(14) на 5m барах > rolling median ATR за 20д
-> high-vol, иначе low-vol. Сравнить net/trade high vs low пермутационным тестом (как H-058).
READ-ONLY; сделки НЕ трогаем. last-closed бар (ts+5min<=decision_time).

Бары акций: из БД (candles). AFLT имеет interval=5; остальные 4 — interval=1 (ресемпл в 5m).
Прогрев: грузим с 2025-12-01 (для 20д медианы + ATR).
Деливерабл: reports/h058_extended_2026h1.{json,md}
"""
import sys, os, json, csv, bisect, asyncio
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
T0 = datetime(2025, 12, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 29, tzinfo=timezone.utc)
WINDOW = os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
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
    out = []
    for b in sorted(buckets):
        o, h, l, c, v = buckets[b]
        out.append((b, o, h, l, c, v))
    return out


async def load_stock(figi):
    eng = create_async_engine(ENG)
    rows = []
    try:
        async with eng.connect() as c:
            # пробуем interval=5
            r = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f "
                "AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "a": T0, "b": T1})
            rows = r.fetchall()
            if len(rows) > 1000:  # 5m полный ряд; иначе неполный -> откат к 1m
                bars = [(x[0].replace(tzinfo=timezone.utc) if x[0].tzinfo is None else x[0],
                         float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
                return bars
            # иначе interval=1 -> ресемпл 5m
            r = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f "
                "AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "a": T0, "b": T1})
            raw = [(x[0].replace(tzinfo=timezone.utc) if x[0].tzinfo is None else x[0],
                    float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
            return aggregate_5m(raw)
    finally:
        await eng.dispose()


def atr14(highs, lows, closes):
    n = len(closes); out = [None] * n
    if n < 2:
        return out
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
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


def perm_test(high, low, n_perm=5000, seed=42):
    import numpy as np
    high = np.asarray(high, float); low = np.asarray(low, float)
    if len(high) < 30 or len(low) < 30:
        return None
    actual = high.mean() - low.mean()
    rng = np.random.default_rng(seed)
    g = np.r_[np.ones(len(high)), np.zeros(len(low))]
    nets = np.r_[high, low]
    perms = np.empty(n_perm)
    for i in range(n_perm):
        gi = rng.permutation(g)
        perms[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    p_two = float((np.abs(perms) >= abs(actual)).mean())
    p_one = float((perms >= actual).mean())
    return {"actual_diff": round(float(actual), 4), "p_two_sided": round(p_two, 4),
            "p_one_sided": round(p_one, 4), "n_high": int(len(high)), "n_low": int(len(low)),
            "mean_high": round(float(high.mean()), 3), "mean_low": round(float(low.mean()), 3)}


def econ(sub):
    n = len(sub)
    if n == 0:
        return {"count": 0}
    net = sum(sub)
    return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 3)}


def main():
    import numpy as np
    trades = list(csv.DictReader(open(WINDOW)))
    print(f"trades H1: {len(trades)}", flush=True)
    stock = {}
    for figi in FIGIS:
        bars = asyncio.new_event_loop().run_until_complete(load_stock(figi))
        closes = [b[4] for b in bars]; highs = [b[2] for b in bars]; lows = [b[3] for b in bars]
        ts5 = [b[0] for b in bars]
        a = atr14(highs, lows, closes)
        med = rolling_median(a, 20 * 78)  # ~20 торговых дней в барах 5m
        stock[figi] = (ts5, a, med)
        print(f"  {figi}: {len(bars)} 5m баров, ATR[last]={a[-1]:.2f}", flush=True)

    high_nets, low_nets = [], []
    none = 0
    for t in trades:
        figi = t["figi"]
        if figi not in stock:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        ts5, a, med = stock[figi]
        j = bisect.bisect_right(ts5, dt - timedelta(minutes=5)) - 1
        if j < 0 or med[j] is None or a[j] is None:
            none += 1
            continue
        reg = "high" if a[j] > med[j] else "low"
        if reg == "high":
            high_nets.append(float(t["net_rub"]))
        else:
            low_nets.append(float(t["net_rub"]))

    print(f"marked: high={len(high_nets)} low={len(low_nets)} none={none}", flush=True)
    pt = perm_test(high_nets, low_nets)
    print("perm:", json.dumps(pt), flush=True)

    result = {"schema": "h058_extended_2026h1_vol", "window": "2026-01-01..2026-07-31",
              "method": "ATR14(5m) vs rolling median 20d; last-closed bar",
              "n_total": len(trades), "n_none": none,
              "econ_high": econ(high_nets), "econ_low": econ(low_nets),
              "perm_high_vs_low": pt,
              "verdict": ("SIGNIFICANT (high>low)" if (pt and pt["p_one_sided"] < 0.05) else "not_significant")}
    with open(os.path.join(REPORTS, "h058_extended_2026h1.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = ["# H-058 extended 2026-H1 — волатильность INSIGHT-002 (READ-ONLY)", "",
          f"Окно: 2026-01-01..2026-07-31 (n={len(trades)}). Метод: ATR14(5m) > rolling median 20д -> high-vol.",
          "Бар сопоставления: последний закрытый (ts+5min<=decision_time).", "",
          "## High-vol vs Low-vol (net/trade)", "",
          f"| группа | n | net | net/t |", "|---|---|---|---|",
          f"| high-vol | {result['econ_high']['count']} | {result['econ_high']['net']} | {result['econ_high']['net_per_trade']} |",
          f"| low-vol | {result['econ_low']['count']} | {result['econ_low']['net']} | {result['econ_low']['net_per_trade']} |",
          "", "## Permutation (high vs low)", "",
          f"actual_diff={pt['actual_diff'] if pt else None} p_one={pt['p_one_sided'] if pt else None} "
          f"p_two={pt['p_two_sided'] if pt else None}", "",
          f"**Вердикт:** {result['verdict']}"]
    open(os.path.join(REPORTS, "h058_extended_2026h1.md"), "w").write("\n".join(md))
    print("saved h058_extended_2026h1.json/.md", flush=True)


if __name__ == "__main__":
    main()
