"""B4 ENTRY-QUALITY MICRO-ANALYSIS (SHADOW, read-only).

Для каждой сделки July (5b44f3b383df) на decision_ts (без look-ahead):
  (a) цена vs EMA_20 (5m) — trend-alignment со стороной сделки;
  (b) pullback depth перед breakout (макс. adverse retrace от недавнего swing high/low до decision, в bps);
  (c) MAE_early — макс. adverse экскурсия в первые 3 бара 1m после входа (в bps).
Группы: trend-aligned vs not; shallow/medium/deep pullback (терции); low/high early-noise (медиана).
Артефакты: reports/entry_quality.{json,csv,md}
"""
import sys, os, json, csv, statistics, bisect
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)


def ema20_at(closes: list[float], idx: int) -> float | None:
    if idx < 19 or idx >= len(closes):
        return None
    k = 2 / 21
    e = closes[max(0, idx - 19)]
    for i in range(max(0, idx - 19) + 1, idx + 1):
        e = closes[i] * k + e * (1 - k)
    return e


def main() -> None:
    from app.services.research_pack import _load_candles
    from app.services.ensemble import resample, TF_SECONDS
    print("loading candles ...", flush=True)
    stock_5m = {}
    stock_1m = {}
    for figi in FIGIS:
        cs = _load_candles(figi, T_FROM, T_TO)
        stock_1m[figi] = cs
        stock_5m[figi] = resample(cs, TF_SECONDS["5min"])
    print("candles loaded", flush=True)

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    rows = []
    for t in trades:
        figi = t["figi"]
        if figi not in stock_5m:
            continue
        side = t["side"]  # LONG/SHORT
        sign = 1.0 if side == "LONG" else -1.0
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        c5 = stock_5m[figi]
        c5ts = [c.ts for c in c5]
        j5 = bisect.bisect_right(c5ts, dt) - 1
        if j5 < 25:
            continue
        closes5 = [c.close for c in c5]
        px = float(t["entry_price"])
        ema = ema20_at(closes5, j5)
        trend_aligned = None
        if ema is not None:
            trend_aligned = (px > ema) if side == "LONG" else (px < ema)
        # pullback depth: для LONG — откат от максимума последних 20 5m-баров до decision;
        # для SHORT — от минимума
        look = c5[max(0, j5 - 20):j5 + 1]
        if side == "LONG":
            swing = max(c.high for c in look)
            pullback_bps = (swing - px) / swing * 10000 if swing > 0 else None
        else:
            swing = min(c.low for c in look)
            pullback_bps = (px - swing) / swing * 10000 if swing > 0 else None
        # MAE_early: первые 3 бара 1m после входа
        c1 = stock_1m[figi]
        c1ts = [c.ts for c in c1]
        e1 = bisect.bisect_right(c1ts, dt)
        mae_early = None
        if e1 + 2 < len(c1):
            if side == "LONG":
                mae_early = (px - min(c.low for c in c1[e1:e1 + 3])) / px * 10000 if px > 0 else None
            else:
                mae_early = (max(c.high for c in c1[e1:e1 + 3]) - px) / px * 10000 if px > 0 else None
        rows.append({
            "trade_id": t["trade_id"], "figi": figi, "side": side,
            "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
            "mfe_r": float(t["mfe_r"]) if t["mfe_r"] else None,
            "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
            "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None,
            "trend_aligned": trend_aligned, "pullback_bps": round(pullback_bps, 2) if pullback_bps is not None else None,
            "mae_early_bps": round(mae_early, 2) if mae_early is not None else None,
        })
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
                "win_rate": round(sum(1 for r in sub if r["net"] > 0) / n, 4),
                "median_mfe_r": round(statistics.median([r["mfe_r"] for r in sub if r["mfe_r"] is not None]), 3),
                "median_mae_r": round(statistics.median([r["mae_r"] for r in sub if r["mae_r"] is not None]), 3),
                "median_hold": round(statistics.median([r["hold_bars"] for r in sub if r["hold_bars"] is not None]), 1)}

    # терции pullback
    pbs = sorted([r["pullback_bps"] for r in rows if r["pullback_bps"] is not None])
    q1 = pbs[len(pbs) // 3] if pbs else 0
    q2 = pbs[2 * len(pbs) // 3] if pbs else 0
    # медиана early noise
    maes = sorted([r["mae_early_bps"] for r in rows if r["mae_early_bps"] is not None])
    med_noise = maes[len(maes) // 2] if maes else 0

    groups = {
        "trend_aligned": econ([r for r in rows if r["trend_aligned"] is True]),
        "trend_not_aligned": econ([r for r in rows if r["trend_aligned"] is False]),
        "pullback_shallow": econ([r for r in rows if r["pullback_bps"] is not None and r["pullback_bps"] <= q1]),
        "pullback_medium": econ([r for r in rows if r["pullback_bps"] is not None and q1 < r["pullback_bps"] <= q2]),
        "pullback_deep": econ([r for r in rows if r["pullback_bps"] is not None and r["pullback_bps"] > q2]),
        "early_noise_low": econ([r for r in rows if r["mae_early_bps"] is not None and r["mae_early_bps"] <= med_noise]),
        "early_noise_high": econ([r for r in rows if r["mae_early_bps"] is not None and r["mae_early_bps"] > med_noise]),
    }
    report = {"schema": "entry_quality_v1", "run_id": "5b44f3b383df", "config_hash": "1c7f75dc44c2aa67",
              "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; признаки <= decision_ts (без look-ahead); НЕ предлагать entry filter без повторяемости на >=2 периодах",
              "groups": groups, "pullback_q1_bps": round(q1, 2), "pullback_q2_bps": round(q2, 2),
              "early_noise_median_bps": round(med_noise, 2), "n_rows": len(rows),
              "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "entry_quality.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if rows:
        with open(os.path.join(REPORTS, "entry_quality.csv"), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader(); wr.writerows(rows)
    md = ["# ENTRY-QUALITY MICRO-ANALYSIS (SHADOW, read-only)", "",
          "Признаки на decision_ts (EMA_20 5m, pullback от swing 20×5m, MAE первые 3 бара 1m).", "",
          f"Строк: {len(rows)} | pullback Q1: {round(q1,2)} bps, Q2: {round(q2,2)} bps | early-noise median: {round(med_noise,2)} bps", "",
          "| группа | n | net | net/trade | PF | win | MFE R | MAE R | hold |",
          "|---|---|---|---|---|---|---|---|---|"]
    for g, v in groups.items():
        if v["count"] == 0:
            continue
        md.append(f"| {g} | {v['count']} | {v['net']} | {v['net_per_trade']} | {v['pf']} | {v['win_rate']} | "
                  f"{v['median_mfe_r']} | {v['median_mae_r']} | {v['median_hold']} |")
    md += ["", "## Вывод", "НЕ предлагать entry filter без повторяемости на ≥2 независимых периодах."]
    with open(os.path.join(REPORTS, "entry_quality.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows), "groups": {k: v["count"] for k, v in groups.items()},
                      "q": [round(q1, 2), round(q2, 2)], "noise_med": round(med_noise, 2)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
