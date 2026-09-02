"""B3 ENTRY CONFLUENCE / QUORUM OPPORTUNITY-COST (SHADOW, read-only).

Часть A — исполненные сделки по quorum_count (2/3/4+): net, net/trade, PF, win, MFE/MAE, hold.
Часть B — opportunity-cost кворум-фильтра: отвергнутые intents по числу setup-сигналов;
оценка standalone EV через per-function median net_bps из vote attribution (k-независимая).
Источники: 5b44f3b383df (entry_intents.csv, trades.csv, vote_attribution.json).
Артефакты: reports/entry_confluence.{json,csv,md}
"""
import sys, os, json, csv, statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")


def parse_mask(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("["):
        return sorted(json.loads(raw.replace("'", '"')))
    return [x.strip() for x in raw.split(";") if x.strip()]


def econ(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"count": 0}
    net = sum(r["net"] for r in rows)
    gp = sum(max(r["gross"], 0) for r in rows)
    gl = sum(max(-r["gross"], 0) for r in rows)
    return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
            "pf": round(gp / gl, 3) if gl > 0 else None,
            "win_rate": round(sum(1 for r in rows if r["net"] > 0) / n, 4),
            "median_mfe_r": round(statistics.median([r["mfe_r"] for r in rows if r["mfe_r"] is not None]), 3),
            "median_mae_r": round(statistics.median([r["mae_r"] for r in rows if r["mae_r"] is not None]), 3),
            "median_hold": round(statistics.median([r["hold_bars"] for r in rows if r["hold_bars"] is not None]), 1)}


def main() -> None:
    intents = list(csv.DictReader(open(os.path.join(RUN_DIR, "entry_intents.csv"))))
    trades = {}
    with open(os.path.join(RUN_DIR, "trades.csv"), newline="") as f:
        for row in csv.DictReader(f):
            trades[(row["figi"], row["decision_time"].replace("Z", "+00:00"), row["side"])] = row

    # Part A: executed по quorum_count
    exec_rows = []
    for it in intents:
        if it["engine_decision"] != "executed":
            continue
        tr = trades.get((it["figi"], it["decision_time"].replace("Z", "+00:00"),
                         "LONG" if it["side"] == "BUY" else "SHORT"))
        if tr is None:
            continue
        exec_rows.append({"qc": int(it["quorum_count"] or 0), "fn": parse_mask(it["functions_mask"]),
                          "net": float(tr["net_rub"]), "gross": float(tr["gross_rub"]),
                          "mfe_r": float(tr["mfe_r"]) if tr["mfe_r"] else None,
                          "mae_r": float(tr["mae_r"]) if tr["mae_r"] else None,
                          "hold_bars": float(tr["hold_bars"]) if tr["hold_bars"] else None})
    part_a = {}
    for qc_label, pred in ((2, lambda r: r["qc"] == 2), (3, lambda r: r["qc"] == 3), ("4+", lambda r: r["qc"] >= 4)):
        sub = [r for r in exec_rows if pred(r)]
        part_a[str(qc_label)] = econ(sub)

    # Part B: rejected intents — число функций + оценочный EV
    attr = json.load(open(os.path.join(RUN_DIR, "vote_attribution.json")))
    fn_net = {k: v["net_bps_median"] for k, v in attr["per_function"].items()}
    rej = [it for it in intents if it["engine_decision"] == "rejected"]
    by_nfn = {}
    ev_total = 0.0
    for it in rej:
        fns = parse_mask(it["functions_mask"])
        n = len(fns)
        by_nfn.setdefault(n, []).append(it)
    part_b = {}
    for n, items in sorted(by_nfn.items()):
        ev = 0.0
        for it in items:
            fns = parse_mask(it["functions_mask"])
            vals = [fn_net.get(f) for f in fns]
            vals = [v for v in vals if v is not None]
            if vals:
                ev += sum(vals) / len(vals)
        part_b[f"rejected_{n}signals"] = {"intents": len(items),
                                          "estimated_net_bps_sum": round(ev, 1),
                                          "note": "EV = sum(per-function median net_bps)/n_signals; аппроксимация"}
    # по причинам (только intents, которые дошли до entry-кандидата с 1-2 сигналами)
    reasons = {}
    for it in rej:
        fns = parse_mask(it["functions_mask"])
        if len(fns) <= 2:
            reasons[it["primary_reject_reason"]] = reasons.get(it["primary_reject_reason"], 0) + 1
    part_b["rejected_1_2_signals_by_reason"] = dict(sorted(reasons.items(), key=lambda x: -x[1]))

    report = {"schema": "entry_confluence_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; НЕ менять quorum/weighted-vote без отдельного policy-теста",
              "part_a_executed_by_quorum": part_a, "part_b_opportunity_cost": part_b,
              "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "entry_confluence.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORTS, "entry_confluence.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["part", "group", "count", "net", "net_per_trade", "pf", "win_rate", "mfe_r", "mae_r", "hold"])
        for g, v in part_a.items():
            wr.writerow(["A", g, v["count"], v["net"], v["net_per_trade"], v["pf"], v["win_rate"],
                         v["median_mfe_r"], v["median_mae_r"], v["median_hold"]])
        for g, v in part_b.items():
            wr.writerow(["B", g, v.get("intents", ""), "", v.get("estimated_net_bps_sum", ""), "", "", "", "", ""])
    md = ["# ENTRY CONFLUENCE / QUORUM OPPORTUNITY-COST (SHADOW)", "",
          "## Часть A — качество входа по quorum_count (July)", "",
          "| quorum | n | net | net/trade | PF | win | MFE R | MAE R | hold |",
          "|---|---|---|---|---|---|---|---|---|"]
    for g in ("2", "3", "4+"):
        v = part_a[g]
        md.append(f"| {g} | {v['count']} | {v['net']} | {v['net_per_trade']} | {v['pf']} | {v['win_rate']} | "
                  f"{v['median_mfe_r']} | {v['median_mae_r']} | {v['median_hold']} |")
    md += ["", "## Часть B — opportunity-cost кворум-фильтра (rejected intents)", "",
           "| группа | intents | оценочный edge (net_bps sum) |", "|---|---|---|"]
    for g, v in part_b.items():
        md.append(f"| {g} | {v.get('intents', v)} | {v.get('estimated_net_bps_sum', '')} |")
    md += ["", "## Вывод", "Часть A: растёт ли net/trade с числом согласных сигналов — см. таблицу. "
           "НЕ предлагать quorum/weighted-vote как вывод; только будущий policy-тест при evidence."]
    with open(os.path.join(REPORTS, "entry_confluence.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"part_a": {k: v["count"] for k, v in part_a.items()},
                      "part_b": {k: v.get("intents", v) for k, v in part_b.items()}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
