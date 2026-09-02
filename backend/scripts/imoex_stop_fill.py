"""B2b-II IMOEX-INFORMED STOP — fill simulation (SHADOW, read-only).

Для каждой июльской сделки моделируем выход, когда IMOEX разворачивается >= threshold
(10/20/30 bps) от локального пика (LONG) / впадины (SHORT); fill на следующем 1m баре.
Сравниваем net/MAE/win/hold vs baseline. Доп: для сделок со стопом — фактический stock MAE
ПОСЛЕ IMOEX-разворота (опережает ли IMOEX adverse-движение акции).
No look-ahead (IMOEX-path <= baseline exit); IMOEX НЕ в execution.
Артефакты: reports/imoex_stop_fill.{json,md}
"""
import sys, os, json, csv, statistics, bisect
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
IMOEX = "BBG00KDWPPW2"
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)


def main() -> None:
    from app.services.research_pack import _load_candles
    print("loading candles ...", flush=True)
    im = _load_candles(IMOEX, T_FROM, T_TO)
    im_ts = [c.ts for c in im]
    stock = {f: _load_candles(f, T_FROM, T_TO) for f in FIGIS}
    print(f"IMOEX {len(im)}, stocks loaded", flush=True)

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    out = []
    for t in trades:
        figi = t["figi"]
        if figi not in stock:
            continue
        qty = float(t["qty"])
        entry_px = float(t["entry_price"])
        side = 1.0 if t["side"] == "LONG" else -1.0
        entry_dt = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
        exit_dt = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
        sc = stock[figi]
        sc_ts = [c.ts for c in sc]
        je = bisect.bisect_right(im_ts, entry_dt) - 1
        jx = bisect.bisect_right(im_ts, exit_dt) - 1
        if jx <= je or je < 0:
            continue
        im_path = im[je:jx + 1]
        sc_se = bisect.bisect_right(sc_ts, entry_dt) - 1
        sc_ex = bisect.bisect_right(sc_ts, exit_dt) - 1
        if sc_se < 0:
            continue
        baseline_net = float(t["net_rub"])
        baseline_mae = float(t["mae_r"]) if t["mae_r"] else None
        baseline_hold = float(t["hold_bars"]) if t["hold_bars"] else None
        for thr in (10, 20, 30):
            peak = im_path[0].close
            stop_i = None
            for k in range(1, len(im_path)):
                px = im_path[k].close
                if side == 1.0:
                    if px > peak:
                        peak = px
                    elif (peak - px) / peak * 10000 >= thr:
                        stop_i = k
                        break
                else:
                    if px < peak:
                        peak = px
                    elif (px - peak) / peak * 10000 >= thr:
                        stop_i = k
                        break
            if stop_i is None:
                continue
            # fill на следующем 1m баре акции после сигнала IMOEX-разворота
            stop_ts = im_path[stop_i].ts
            fill_k = bisect.bisect_right(sc_ts, stop_ts)  # следующий бар акции после сигнала
            if fill_k >= len(sc):
                continue
            fill_px = sc[fill_k].open
            exit_px_sim = fill_px
            # commission+slippage как в baseline (5+2 bps) на один выход
            cost_bps = 5 + 2
            notional = entry_px * qty
            costs = notional * cost_bps / 10000.0
            net_sim = (exit_px_sim - entry_px) * side * qty - costs
            # hold: от entry до fill в барах (1m)
            hold_sim = fill_k - sc_se
            # MAE до стоп-момента (stock path от entry до fill)
            mae_sim = None
            path_stock = sc[sc_se:fill_k + 1]
            if path_stock:
                if side == 1.0:
                    mae_sim = (entry_px - min(c.low for c in path_stock)) / entry_px * 10000 if entry_px else None
                else:
                    mae_sim = (max(c.high for c in path_stock) - entry_px) / entry_px * 10000 if entry_px else None
            # фактический stock MAE ПОСЛЕ IMOEX-разворота (до baseline exit)
            mae_after = None
            path_after = sc[fill_k + 1:sc_ex + 1]
            if path_after:
                if side == 1.0:
                    mae_after = (min(c.low for c in path_after) - exit_px_sim) / exit_px_sim * 10000 if exit_px_sim else None
                else:
                    mae_after = (exit_px_sim - max(c.high for c in path_after)) / exit_px_sim * 10000 if exit_px_sim else None
            out.append({"trade_id": t["trade_id"], "figi": figi, "side": t["side"],
                        "threshold": thr, "baseline_net": baseline_net, "net_sim": round(net_sim, 2),
                        "baseline_mae_r": baseline_mae, "mae_sim_bps": round(mae_sim, 2) if mae_sim is not None else None,
                        "baseline_hold": baseline_hold, "hold_sim_bars": hold_sim,
                        "mae_after_reversal_bps": round(mae_after, 2) if mae_after is not None else None,
                        "baseline_exit_reason": t["exit_reason"]})
    print(f"sim rows: {len(out)}", flush=True)

    results = {"baseline": {"n": len({r['trade_id'] for r in out}), "note": "из trades.csv: net 10446, net/trade 20.69, win 71.1%, median MAE 0.27R, hold 7 (по спеце My3)"}}
    for thr in (10, 20, 30):
        sub = [r for r in out if r["threshold"] == thr]
        n = len(sub)
        if n == 0:
            continue
        net = sum(r["net_sim"] for r in sub)
        wins = sum(1 for r in sub if r["net_sim"] > 0)
        mae_after = [r["mae_after_reversal_bps"] for r in sub if r["mae_after_reversal_bps"] is not None]
        hold = [r["hold_sim_bars"] for r in sub]
        results[f"threshold_{thr}"] = {
            "n_stop": n, "net_sim_sum": round(net, 2),
            "net_per_trade_sim": round(net / n, 2) if n else None,
            "win_rate_sim": round(wins / n, 4) if n else None,
            "median_hold_sim_bars": round(statistics.median(hold), 1) if hold else None,
            "median_stock_mae_after_reversal_bps": round(statistics.median(mae_after), 2) if mae_after else None,
            "note": "только сделки со сработавшим стопом; fill на след. 1m open; costs 5+2 bps"}
    report = {"schema": "imoex_stop_fill_v1", "run_id": "5b44f3b383df", "config_hash": "1c7f75dc44c2aa67",
              "period": "2026-07-01..2026-07-31",
              "note": "SHADOW counterfactual; no look-ahead; IMOEX НЕ в execution",
              "results": results, "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "imoex_stop_fill.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = ["# IMOEX-INFORMED STOP — fill simulation (SHADOW)", "",
          f"Сделок: {len({r['trade_id'] for r in out})} | baseline: net 10446, net/t 20.69, win 71.1%, med MAE 0.27R, hold 7", "",
          "| threshold | n_stop | net_sim | net/t sim | win sim | med hold | med stock MAE after reversal |",
          "|---|---|---|---|---|---|---|"]
    for k, v in results.items():
        if k == "baseline":
            continue
        md.append(f"| {k} | {v['n_stop']} | {v['net_sim_sum']} | {v['net_per_trade_sim']} | {v['win_rate_sim']} | "
                  f"{v['median_hold_sim_bars']} | {v['median_stock_mae_after_reversal_bps']} |")
    md += ["", "## Вывод", "IMOEX-stop сокращает hold и, если median stock MAE после разворота < 0, "
           "опережает adverse-движение акции. Полный эффект на portfolio net требует взвешивания "
           "(часть сделок без стопа). SHADOW — не policy."]
    with open(os.path.join(REPORTS, "imoex_stop_fill.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"n_sim": len(out), "results": {k: v for k, v in results.items() if k != "baseline"}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
