"""Аудит gold_regime: lookahead-проверка и пересчёт с «последним закрытым баром».

Проблема (My3): бары gold_5m.csv имеют ts = НАЧАЛО 5-мин интервала. Если decision_time
совпадает с началом бара, close/ADX/EMA этого бара известны только в конце интервала
(+5 мин) -> использование бара с ts<=decision_time даёт lookahead до 5 минут.

Правильно: «последний закрытый бар» = бар, у которого конец интервала (ts+5min) <= decision_time,
т.е. j = bisect_right(ts5, decision_time - 5min) - 1.

Скрипт пересчитывает GOLD-regime (метод B: ADX14>25, up если close>EMA50) для:
  (a) ALL 5 FIGI, (b) Metals (RUAL+NLMK)
двумя способами (старый lookahead vs последний закрытый) и сравнивает с отчётом и My3.
Плюс проверка июльского macro_regime_attribution_v2 на lookahead.
"""
import sys, os, json, bisect
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import macro_regime_attribution_v2 as M2

METALS = ("BBG008F2T3T2", "BBG004S681B4")  # RUAL, NLMK
TF = 5


def load_gold_bars():
    from datetime import datetime as _dt
    rows = []
    with open(os.path.join(BACKEND, "data", "gold_5m.csv")) as f:
        for line in f:
            p = line.strip().rstrip(";").split(";")
            if len(p) < 6:
                continue
            ts = _dt.fromisoformat(p[0].replace("Z", "+00:00"))
            rows.append((ts, float(p[3]), float(p[1]), float(p[2]), float(p[4]), float(p[5])))  # o,h,l,c,v
    return rows


def regime_series(bars):
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    ema = M2.ema(closes, 50)
    adx = M2.adx14(highs, lows, closes)
    return closes, ema, adx


def regime_at(closes, ema, adx, j):
    if j < 50 or j >= len(closes):
        return None
    a = adx[j]
    if a is None:
        return None
    if a <= 25:
        return "flat"
    return "up_trend" if closes[j] > ema[j] else "down_trend"


def run_test(trades, bars, use_last_closed, figis):
    """trades: list of dict(figi, decision_time, net_rub). bars: (ts, o,h,l,c,v)."""
    ts5 = [b[0] for b in bars]
    closes, ema, adx = regime_series(bars)
    down, up = [], []
    for t in trades:
        if t["figi"] not in figis:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        shift = timedelta(minutes=TF) if use_last_closed else timedelta(0)
        j = bisect.bisect_right(ts5, dt - shift) - 1
        rg = regime_at(closes, ema, adx, j)
        if rg == "down_trend":
            down.append(float(t["net_rub"]))
        elif rg == "up_trend":
            up.append(float(t["net_rub"]))
    import numpy as np
    rng = np.random.default_rng(42)
    def stats(grp):
        return {"n": len(grp), "mean": round(float(np.mean(grp)), 3) if grp else None}
    d, u = np.asarray(down, float), np.asarray(up, float)
    if len(d) < 30 or len(u) < 30:
        p = None
    else:
        actual = u.mean() - d.mean()
        g = np.r_[np.zeros(len(d)), np.ones(len(u))]
        nets = np.r_[d, u]
        perms = []
        for _ in range(5000):
            gi = rng.permutation(g)
            perms.append(nets[gi == 1].mean() - nets[gi == 0].mean())
        perms = np.asarray(perms)
        p = round(float((np.abs(perms) >= np.abs(actual)).mean()), 4)
    return {"down": stats(down), "up": stats(up),
            "p_two_down_vs_up": p,
            "direction": "up>down" if (np.mean(up) > np.mean(down)) else "down>up"}


def load_trades(path):
    import csv
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    bars = load_gold_bars()
    print(f"gold bars: {len(bars)}; ts[0]={bars[0][0]} ts[-1]={bars[-1][0]}")

    trades = load_trades(os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv"))
    print(f"trades 2026-01..07: {len(trades)}")

    out = {"schema": "gold_regime_audit_v1", "window": "2026-01-01..2026-08-01", "tf": TF}
    for label, figis in (("ALL_5_FIGI", set(M2.FIGI_SECTOR.keys())), ("Metals", set(METALS))):
        old = run_test(trades, bars, use_last_closed=False, figis=figis)
        new = run_test(trades, bars, use_last_closed=True, figis=figis)
        out[f"GOLD_{label}"] = {"lookahead_ts<=dt": old, "last_closed_ts+5m<=dt": new}
        print(f"\n=== GOLD {label} ===")
        print("  old (lookahead):", json.dumps(old))
        print("  new (last closed):", json.dumps(new))

    # июль: проверка lookahead в v2 (Metals)
    july = load_trades(os.path.join(REPORTS, "5b44f3b383df", "trades.csv"))
    print(f"\ntrades July: {len(july)}")
    out["july"] = {}
    for label, figis in (("Metals", set(METALS)),):
        old = run_test(july, bars, use_last_closed=False, figis=figis)
        new = run_test(july, bars, use_last_closed=True, figis=figis)
        out["july"][f"GOLD_{label}"] = {"lookahead": old, "last_closed": new}
        print(f"July GOLD {label} old:", json.dumps(old))
        print(f"July GOLD {label} new:", json.dumps(new))

    with open(os.path.join(REPORTS, "gold_regime_audit.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nsaved reports/gold_regime_audit.json")


if __name__ == "__main__":
    main()
