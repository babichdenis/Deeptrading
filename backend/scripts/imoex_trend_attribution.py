"""B5 IMOEX TREND REGIME & STRENGTH ATTRIBUTION (SHADOW, read-only).

Три независимых определения тренда IMOEX (5m бары, point-in-time):
  A. EMA-slope: EMA20 vs EMA50, diff_norm = (ema_f - ema_s)/close, tau=0.0015
  B. ADX(14): ADX>25 => тренд (+DI>-DI => up)
  C. Linear regression slope (40 баров), slope_norm, tau=0.00075
strength_norm = trend_raw / vol_imoex, где vol = rolling_std(return_5m, 30).
Привязка к сделкам: entry/exit/hold. Группы A-E, сравнение методов.
Артефакты: reports/5b44f3b383df/imoex_trend_attribution.json + imoex_trend_trade_audit.csv
           + reports/imoex_trend_methodology.md (в reports/)
"""
import sys, os, json, csv, statistics, math, bisect
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
IMOEX = "BBG00KDWPPW2"
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)
TAU_A = 0.0015
TAU_C = 0.00075
ADX_THR = 25.0


def ema(series: list[float], period: int) -> list[float]:
    out = []
    k = 2 / (period + 1)
    e = None
    for v in series:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def adx14(highs, lows, closes) -> list[float]:
    n = len(closes)
    out = [None] * n
    if n < 30:
        return out
    tr_ = [highs[i] - lows[i] for i in range(n)]
    for i in range(1, n):
        tr_[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    up = [0.0] * n
    dn = [0.0] * n
    for i in range(1, n):
        up[i] = highs[i] - highs[i - 1]
        dn[i] = lows[i - 1] - lows[i]
        if up[i] < 0: up[i] = 0.0
        if dn[i] < 0: dn[i] = 0.0
    for i in range(14, n):
        if tr_[i] == 0:
            out[i] = 50.0
            continue
        tr14 = sum(tr_[i - 13:i + 1])
        up14 = sum(up[i - 13:i + 1])
        dn14 = sum(dn[i - 13:i + 1])
        pdi = up14 / tr14 * 100
        mdi = dn14 / tr14 * 100
        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
        out[i] = dx
    return out


def linreg_slope(closes: list[float], idx: int, window: int = 40) -> float | None:
    if idx < window - 1:
        return None
    seg = closes[idx - window + 1:idx + 1]
    xs = list(range(window))
    mx = sum(xs) / window
    my = sum(seg) / window
    num = sum((x - mx) * (y - my) for x, y in zip(xs, seg))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else None


def main() -> None:
    from app.services.research_pack import _load_candles
    from app.services.ensemble import resample, TF_SECONDS
    print("loading IMOEX ...", flush=True)
    im = _load_candles(IMOEX, T_FROM, T_TO)
    c5 = resample(im, TF_SECONDS["5min"])
    closes = [c.close for c in c5]
    highs = [c.high for c in c5]
    lows = [c.low for c in c5]
    ts5 = [c.ts for c in c5]
    ema_f = ema(closes, 20)
    ema_s = ema(closes, 50)
    adx = adx14(highs, lows, closes)
    rets5 = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    # rolling vol (std 30 5m returns)
    vol30 = [None] * len(closes)
    for i in range(30, len(closes)):
        vol30[i] = statistics.pstdev(rets5[i - 29:i + 1])

    def regime(j: int, method: str) -> tuple[str | None, float | None]:
        if j < 50:
            return None, None
        if method == "A":
            diff_norm = (ema_f[j] - ema_s[j]) / closes[j]
            r = "up_trend" if diff_norm > TAU_A else ("down_trend" if diff_norm < -TAU_A else "flat")
            return r, diff_norm
        if method == "B":
            a = adx[j]
            if a is None:
                return None, None
            if a <= ADX_THR:
                return "flat", a
            # направление: +DI vs -DI (пересчёт для j)
            trs = [max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]), abs(lows[k] - closes[k - 1])) for k in range(j - 13, j + 1)]
            ups = [max(highs[k] - highs[k - 1], 0) for k in range(j - 13, j + 1)]
            dns = [max(lows[k - 1] - lows[k], 0) for k in range(j - 13, j + 1)]
            tr14 = sum(trs); up14 = sum(ups); dn14 = sum(dns)
            pdi = up14 / tr14 * 100 if tr14 else 0
            mdi = dn14 / tr14 * 100 if tr14 else 0
            return ("up_trend" if pdi > mdi else "down_trend"), a
        if method == "C":
            sl = linreg_slope(closes, j, 40)
            if sl is None:
                return None, None
            slope_norm = sl / closes[j]
            r = "up_trend" if slope_norm > TAU_C else ("down_trend" if slope_norm < -TAU_C else "flat")
            return r, slope_norm

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    rows = []
    for t in trades:
        entry_dt = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
        exit_dt = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
        je = bisect.bisect_right(ts5, entry_dt) - 1
        jx = bisect.bisect_right(ts5, exit_dt) - 1
        if je < 50 or jx <= je or jx >= len(c5):
            continue
        rec = {"trade_id": t["trade_id"], "figi": t["figi"], "side": t["side"],
               "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
               "exit_reason": t["exit_reason"],
               "mfe_r": float(t["mfe_r"]) if t["mfe_r"] else None,
               "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
               "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None}
        for m in ("A", "B", "C"):
            re_, rs = regime(je, m)
            rec[f"entry_regime_{m}"] = re_
            rec[f"entry_strength_norm_{m}"] = round(rs / vol30[je], 3) if (rs is not None and vol30[je]) else None
            rx, xs = regime(jx, m)
            rec[f"exit_regime_{m}"] = rx
            rec[f"exit_strength_norm_{m}"] = round(xs / vol30[jx], 3) if (xs is not None and vol30[jx]) else None
            # hold regime = mode, strength = mean
            modes = {}
            sm = []
            for k in range(je, jx + 1):
                rk, sk = regime(k, m)
                if rk:
                    modes[rk] = modes.get(rk, 0) + 1
                if sk is not None and vol30[k]:
                    sm.append(sk / vol30[k])
            rec[f"hold_regime_{m}"] = max(modes, key=modes.get) if modes else None
            rec[f"hold_strength_norm_{m}"] = round(statistics.mean(sm), 3) if sm else None
        rows.append(rec)
    print(f"rows: {len(rows)}", flush=True)

    def econ(sub: list[dict]) -> dict:
        n = len(sub)
        if n == 0:
            return {"count": 0}
        net = sum(r["net"] for r in sub)
        gp = sum(max(r["gross"], 0) for r in sub)
        gl = sum(max(-r["gross"], 0) for r in sub)
        return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
                "pf": round(gp / gl, 3) if gl > 0 else None,
                "win_rate": round(sum(1 for r in sub if r["net"] > 0) / n, 4)}

    methods = {"A": "EMA_slope", "B": "ADX", "C": "linreg"}
    report = {"schema": "imoex_trend_attribution_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; point-in-time; IMOEX НЕ в execution",
              "methods": methods, "groups": {}, "n_rows": len(rows),
              "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    for m in ("A", "B", "C"):
        g = {}
        for rstate in ("up_trend", "down_trend", "flat"):
            for side in ("LONG", "SHORT"):
                key = f"{side}_{rstate}"
                g[key] = econ([r for r in rows if r["side"] == side and r[f"entry_regime_{m}"] == rstate])
        # aligned / counter / flat по входу
        g["aligned"] = econ([r for r in rows if (r["side"] == "LONG" and r[f"entry_regime_{m}"] == "up_trend")
                             or (r["side"] == "SHORT" and r[f"entry_regime_{m}"] == "down_trend")])
        g["counter"] = econ([r for r in rows if (r["side"] == "LONG" and r[f"entry_regime_{m}"] == "down_trend")
                             or (r["side"] == "SHORT" and r[f"entry_regime_{m}"] == "up_trend")])
        g["flat"] = econ([r for r in rows if r[f"entry_regime_{m}"] == "flat"])
        # квантили strength (entry)
        vals = sorted([r[f"entry_strength_norm_{m}"] for r in rows if r[f"entry_strength_norm_{m}"] is not None])
        if vals:
            q1 = vals[len(vals) // 3]; q2 = vals[2 * len(vals) // 3]
            g["strength_Q1_weak"] = econ([r for r in rows if r[f"entry_strength_norm_{m}"] is not None and r[f"entry_strength_norm_{m}"] <= q1])
            g["strength_Q2_medium"] = econ([r for r in rows if r[f"entry_strength_norm_{m}"] is not None and q1 < r[f"entry_strength_norm_{m}"] <= q2])
            g["strength_Q3_strong"] = econ([r for r in rows if r[f"entry_strength_norm_{m}"] is not None and r[f"entry_strength_norm_{m}"] > q2])
        # по режиму на выходе
        for rstate in ("up_trend", "down_trend", "flat"):
            g[f"exit_{rstate}"] = econ([r for r in rows if r[f"exit_regime_{m}"] == rstate])
        # смена режима entry->exit
        g["transition_up_to_flat"] = econ([r for r in rows if r[f"entry_regime_{m}"] == "up_trend" and r[f"exit_regime_{m}"] == "flat"])
        g["transition_up_to_down"] = econ([r for r in rows if r[f"entry_regime_{m}"] == "up_trend" and r[f"exit_regime_{m}"] == "down_trend"])
        g["transition_flat_to_up"] = econ([r for r in rows if r[f"entry_regime_{m}"] == "flat" and r[f"exit_regime_{m}"] == "up_trend"])
        report["groups"][m] = g
    with open(os.path.join(RUN_DIR, "imoex_trend_attribution.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(RUN_DIR, "imoex_trend_trade_audit.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader(); wr.writerows(rows)
    md = ["# IMOEX TREND REGIME & STRENGTH ATTRIBUTION (SHADOW)", "",
          "Методы: A EMA-slope (EMA20/50, tau=0.15%), B ADX14 (>25 тренд), C linreg 40 (tau=0.075%).",
          f"strength_norm = raw / rolling_vol(30×5m). Строк: {len(rows)}", "",
          "## aligned / counter / flat (по входу)", "", "| method | aligned n | aligned net/t | counter n | counter net/t | flat n | flat net/t |",
          "|---|---|---|---|---|---|---|"]
    for m in ("A", "B", "C"):
        g = report["groups"][m]
        md.append(f"| {m} | {g['aligned']['count']} | {g['aligned']['net_per_trade']} | {g['counter']['count']} | "
                  f"{g['counter']['net_per_trade']} | {g['flat']['count']} | {g['flat']['net_per_trade']} |")
    md += ["", "## Strength квантили (по входу, net/trade)", "",
           "| method | Q1 weak | Q2 medium | Q3 strong |", "|---|---|---|---|"]
    for m in ("A", "B", "C"):
        g = report["groups"][m]
        md.append(f"| {m} | {g['strength_Q1_weak']['net_per_trade']} | {g['strength_Q2_medium']['net_per_trade']} | "
                  f"{g['strength_Q3_strong']['net_per_trade']} |")
    md += ["", "## Вывод", "Предпочтительный метод тренда для будущих экспериментов — см. methodology.md. "
           "SHADOW — не policy."]
    with open(os.path.join(REPORTS, "imoex_trend_methodology.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows), "aligned": {m: report["groups"][m]["aligned"]["count"] for m in "ABC"},
                      "counter": {m: report["groups"][m]["counter"]["count"] for m in "ABC"}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
