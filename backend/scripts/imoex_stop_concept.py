"""B2b IMOEX-INFORMED STOP (SHADOW counterfactual, read-only).

Контрфактуальный выход по IMOEX-развороту на July baseline (5b44f3b383df):
для каждой сделки строим IMOEX-path от decision_ts до baseline exit; моделируем выход,
когда IMOEX разворачивается >= threshold bps от локального пика (LONG) / впадины (SHORT).
threshold ∈ {10,20,30}. Сравниваем с baseline (net, MAE, win, hold, преждевременные выходы).
ТОЛЬКО диагностика; IMOEX НЕ в execution.
"""
import sys, os, json, csv, bisect, statistics
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
IMOEX = "BBG00KDWPPW2"
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)


def main() -> None:
    from app.services.research_pack import _load_candles
    cs = _load_candles(IMOEX, T_FROM, T_TO)
    im_ts = [c.ts for c in cs]
    im_c = [c.close for c in cs]
    print(f"IMOEX 1m: {len(cs)}", flush=True)

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    rows = []
    for t in trades:
        dt = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
        j = bisect.bisect_right(im_ts, dt) - 1
        if j < 0:
            continue
        ex = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
        jx = bisect.bisect_right(im_ts, ex) - 1
        if jx <= j:
            continue
        path = im_c[j:jx + 1]
        if len(path) < 2:
            continue
        rows.append({"trade_id": t["trade_id"], "side": t["side"], "entry_time": dt.isoformat(),
                     "exit_time": t["exit_time"], "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                     "exit_reason": t["exit_reason"], "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
                     "mfe_r": float(t["mfe_r"]) if t["mfe_r"] else None,
                     "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None,
                     "path": path, "base_idx": j, "exit_idx": jx})
    print(f"rows: {len(rows)}", flush=True)

    def baseline_stats(sub):
        n = len(sub)
        if n == 0:
            return {"count": 0}
        net = sum(x["net"] for x in sub)
        return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
                "win": round(sum(1 for x in sub if x["net"] > 0) / n, 4),
                "median_mae_r": round(statistics.median([x["mae_r"] for x in sub if x["mae_r"] is not None]), 3),
                "median_hold": round(statistics.median([x["hold_bars"] for x in sub if x["hold_bars"] is not None]), 1)}

    results = {"baseline": baseline_stats(rows)}
    for thr in (10, 20, 30):
        out_rows = []
        for r in rows:
            path = r["path"]
            side = r["side"]
            # локальный пик/впадина IMOEX от entry
            peak = path[0]
            stop_at = None
            stop_price = None
            for p in path[1:]:
                if side == "LONG":
                    if p > peak:
                        peak = p
                    elif (peak - p) / peak * 10000 >= thr:
                        stop_at = p
                        break
                else:  # SHORT
                    if p < peak:
                        peak = p
                    elif (p - peak) / peak * 10000 >= thr:
                        stop_at = p
                        break
            if stop_at is None:
                # стоп не сработал до baseline exit — baseline результат
                out_rows.append({"net": r["net"], "mae": r["mae_r"], "hold": r["hold_bars"],
                                 "exit_reason": r["exit_reason"], "early": False})
            else:
                # выход по IMOEX-стопу: реализация (для простоты) = стоп-цена как exit
                out_rows.append({"net": None, "mae": None, "hold": None,
                                 "exit_reason": "IMOEX_STOP", "early": True})
        early = sum(1 for x in out_rows if x["early"])
        # оценка: сколько сделок было бы закрыто IMOEX-стопом ранее baseline выхода
        results[f"threshold_{thr}"] = {
            "n": len(out_rows), "early_exits": early,
            "pct_early": round(early / len(out_rows) * 100, 1) if out_rows else None,
            "note": "early = IMOEX разворот >= threshold до baseline exit; сетевой эффект на net требует "
                    "полной симуляции fill — здесь только частота и тайминг (SHADOW).",
        }

    report = {"schema": "imoex_stop_concept_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW counterfactual; no look-ahead (IMOEX <= baseline exit); IMOEX НЕ в execution",
              "results": results, "generated_at": datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "imoex_stop_concept.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = ["# IMOEX-INFORMED STOP (SHADOW counterfactual)", "",
          "Контрфактуальный выход по развороту IMOEX (LONG: откат от пика; SHORT: отскок от впадины)",
          f"Сделок: {len(rows)} | Период: 2026-07 | baseline: {results['baseline']}", "",
          "| threshold | n | early exits | % early | note |", "|---|---|---|---|---|"]
    for k, v in results.items():
        if k == "baseline":
            continue
        md.append(f"| {k} | {v['n']} | {v['early_exits']} | {v['pct_early']}% | {v['note']} |")
    md += ["", "## Вывод", "Диагностика: частоты ранних IMOEX-разворотов. Полная оценка net/MAE-эффекта "
           "требует симуляции fill (не выполнена здесь, SHADOW). Не предлагается как policy без "
           "отдельного pre-reg эксперимента на disjoint 2025."]
    with open(os.path.join(REPORTS, "imoex_stop_concept.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows), "results": {k: v for k, v in results.items() if k != "baseline"}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
