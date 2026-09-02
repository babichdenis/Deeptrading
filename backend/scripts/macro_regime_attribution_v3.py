"""H-036 Phase-3 / H-058 §1c fix: макро-режимы С ИСПРАВЛЕННЫМ lookahead (v3).

Исправления относительно v2/H-058:
  - бар сопоставления: последний ЗАКРЫТЫЙ бар: j = bisect_right(ts5, dt - 5min) - 1
    (условие ts[j] + 5min <= decision_time; ts = начало 5m-интервала).
  - ряд фактора грузится ПОЛНОСТЬЮ (2025-01..2026-08) -> EMA50/ADX14 прогреты, нет холодного старта.
  - regime-функция B без изменений (ADX14>25, up если close>EMA50).
Пересчёт для July 2026 (517 сделок) и 2026-H1 (3481 сделка) по факторам
IMOEX / BRENT / GOLD / USDRUB (секторное сопоставление как в v2).
Применяется поправка Бонферрони к July (n_tests=4, alpha=0.0125).
Обновляется reports/h058_significance_testing.json -> блок 1c (macro_regimes).
Деливераблы: reports/macro_regime_attribution_v3.{json,md}
"""
import sys, os, json, csv, bisect
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from macro_regime_attribution_v2 import ema, adx14  # переиспользуем расчёты

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
DATA = os.path.join(BACKEND, "data")
TF = 5

FACTORS_CSV = {"BRENT": "brent_5m.csv", "GOLD": "gold_5m.csv",
               "USDRUB": "usdrub_5m.csv", "IMOEX": "imoex_5m.csv"}

FIGI_SECTOR = {
    "BBG008F2T3T2": ("RUAL", "Metals/USD-RUB"), "BBG004S681B4": ("NLMK", "Metals/USD-RUB"),
    "BBG004S681M2": ("SNGSP", "OilGas/Brent"), "BBG004S683W7": ("AFLT", "IMOEX/risk"),
    "BBG004S68CP5": ("MVID", "IMOEX/risk"),
}
SECTOR_FACTOR = {"IMOEX/risk": "IMOEX", "OilGas/Brent": "BRENT", "Metals/USD-RUB": "USDRUB"}
METALS_GOLD = ("BBG008F2T3T2", "BBG004S681B4")

WINDOWS = {
    "July_2026": os.path.join(REPORTS, "5b44f3b383df", "trades.csv"),
    "H1_2026": os.path.join(REPORTS, "gold_window_202601_202607", "trades.csv"),
}

# фактор -> (группа-гипотеза "good", направление сравнения)
# GOLD/USDRUB: ожидается down>up; IMOEX/BRENT: ожидается up>down
GOOD_GROUP = {"GOLD": "down_trend", "USDRUB": "down_trend", "IMOEX": "up_trend", "BRENT": "up_trend"}


def load_full_csv(name):
    path = os.path.join(DATA, FACTORS_CSV[name])
    out = []
    with open(path) as f:
        for line in f:
            p = line.strip().rstrip(";").split(";")
            if len(p) < 6:
                continue
            ts = datetime.fromisoformat(p[0].replace("Z", "+00:00")).astimezone(timezone.utc)
            h, l, o, c, v = (float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]))
            out.append((ts, o, h, l, c, v))
    return out


def regime_series(bars):
    closes = [b[4] for b in bars]; highs = [b[2] for b in bars]; lows = [b[3] for b in bars]
    ema_s = ema(closes, 50)
    adx = adx14(highs, lows, closes)
    return closes, ema_s, adx


def regime_B(j, closes, ema_s, adx):
    if j < 50 or j >= len(closes):
        return None
    a = adx[j]
    if a is None or a <= 25:
        return "flat"
    return "up_trend" if closes[j] > ema_s[j] else "down_trend"


def perm_test(good, bad, n_perm=5000, seed=42):
    import numpy as np
    good = np.asarray(good, float); bad = np.asarray(bad, float)
    if len(good) < 30 or len(bad) < 30:
        return None
    actual = good.mean() - bad.mean()
    rng = np.random.default_rng(seed)
    g = np.r_[np.ones(len(good)), np.zeros(len(bad))]
    nets = np.r_[good, bad]
    perms = np.empty(n_perm)
    for i in range(n_perm):
        gi = rng.permutation(g)
        perms[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    p_two = float((np.abs(perms) >= abs(actual)).mean())
    p_one = float((perms >= actual).mean())
    return {"actual_diff": round(float(actual), 4), "p_two_sided": round(p_two, 4),
            "p_one_sided": round(p_one, 4),
            "n_good": int(len(good)), "n_bad": int(len(bad)),
            "mean_good": round(float(good.mean()), 3), "mean_bad": round(float(bad.mean()), 3)}


def econ(sub):
    n = len(sub)
    if n == 0:
        return {"count": 0}
    net = sum(r["net"] for r in sub)
    gp = sum(max(r["gross"], 0) for r in sub); gl = sum(max(-r["gross"], 0) for r in sub)
    return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 3),
            "pf": round(gp / gl, 3) if gl > 0 else None,
            "win_rate": round(sum(1 for r in sub if r["win"]) / n, 4)}


def load_trades(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    factors = {n: load_full_csv(n) for n in FACTORS_CSV}
    print("loaded factors:", {n: len(v) for n, v in factors.items()}, flush=True)
    series = {n: regime_series(b) for n, b in factors.items()}
    idx = {n: [b[0] for b in factors[n]] for n in factors}

    result = {"schema": "macro_regime_attribution_v3", "method": "B: ADX14>25, up if close>EMA50",
              "bar_alignment": "last_closed (ts+5min<=decision_time)",
              "warmup": "full series 2025-01..2026-08", "note": "lookahead fixed vs v2"}

    for wname, wpath in WINDOWS.items():
        trades = load_trades(wpath)
        print(f"\n=== window {wname}: {len(trades)} trades ===", flush=True)
        rows = []
        for t in trades:
            figi = t["figi"]
            if figi not in FIGI_SECTOR:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ticker, sector = FIGI_SECTOR[figi]
            fname = SECTOR_FACTOR[sector]
            j = bisect.bisect_right(idx[fname], dt - timedelta(minutes=TF)) - 1
            rg = regime_B(j, *series[fname])
            rows.append({"ticker": ticker, "sector": sector, "factor": fname,
                         "regime": rg, "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0})
        # металлы + Gold доп.
        for t in trades:
            figi = t["figi"]
            if figi not in METALS_GOLD:
                continue
            dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
            ticker, sector = FIGI_SECTOR[figi]
            j = bisect.bisect_right(idx["GOLD"], dt - timedelta(minutes=TF)) - 1
            rg = regime_B(j, *series["GOLD"])
            rows.append({"ticker": ticker, "sector": "Metals/Gold", "factor": "GOLD",
                         "regime": rg, "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0})

        wres = {}
        for fname in ("IMOEX", "BRENT", "GOLD", "USDRUB"):
            good = GOOD_GROUP[fname]
            bad = "down_trend" if good == "up_trend" else "up_trend"
            g = [r["net"] for r in rows if r["factor"] == fname and r["regime"] == good]
            b = [r["net"] for r in rows if r["factor"] == fname and r["regime"] == bad]
            pt = perm_test(g, b)
            grp_good = econ([r for r in rows if r["factor"] == fname and r["regime"] == good])
            grp_bad = econ([r for r in rows if r["factor"] == fname and r["regime"] == bad])
            wres[fname] = {"good_group": good, "bad_group": bad, "perm": pt,
                           "econ_good": grp_good, "econ_bad": grp_bad}
            sig = pt and (pt["p_one_sided"] < 0.05)
            print(f"  {fname}: {good} n={len(g)} net/t={grp_good.get('net_per_trade')} vs "
                  f"{bad} n={len(b)} net/t={grp_bad.get('net_per_trade')} | p1={pt['p_one_sided'] if pt else None} "
                  f"{'SIGNIF' if sig else ''}", flush=True)
        result[wname] = wres

    # Бонферрони для July (4 теста)
    n_tests = 4
    alpha = 0.05 / n_tests
    july = result["July_2026"]
    bonf = {fname: (july[fname]["perm"]["p_one_sided"] < alpha if july[fname]["perm"] else None)
            for fname in july}
    result["bonferroni"] = {"n_tests": n_tests, "alpha": round(alpha, 4), "july_significant": bonf}
    print(f"\nBonferroni alpha={alpha}; July significant: {bonf}", flush=True)

    # сохранение v3
    with open(os.path.join(REPORTS, "macro_regime_attribution_v3.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # обновление h058 §1c
    hpath = os.path.join(REPORTS, "h058_significance_testing.json")
    h = json.load(open(hpath))
    h["h058"]["1c"] = {
        "note": "macro_regimes July 2026, пересчитано с last-closed баром + полный ряд (lookahead fixed). См. macro_regime_attribution_v3.json",
        "method": result["method"], "bar_alignment": result["bar_alignment"],
        "July_2026": {fname: july[fname]["perm"] for fname in july},
        "bonferroni_alpha": round(alpha, 4),
    }
    h["h058"]["1c_macro_regimes"] = [f"{f}:{GOOD_GROUP[f]}_vs_{'down_trend' if GOOD_GROUP[f]=='up_trend' else 'up_trend'}"
                                     for f in ("IMOEX", "BRENT", "GOLD", "USDRUB")]
    json.dump(h, open(hpath, "w"), ensure_ascii=False, indent=2)
    print("saved macro_regime_attribution_v3.json + updated h058 1c", flush=True)


if __name__ == "__main__":
    main()
