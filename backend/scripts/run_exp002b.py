"""EXP-002b PRIMARY: entry_volatility_gate rolling_atr_high_only (2025-08-01..2025-12-31).

Данные: immutable прогон EXP-002 (reports/exp002_vol_gate_202508_202512_DRAFT.json)
— тот же gate, окно, canonical config (config_hash=1c7f75dc44c2aa67). Движок НЕ перезапускается.
Aggregate PF считается из per-FIGI (gp-g_gl=G, gp/gl=pf → gl=G/(pf-1), gp=pf*gl).
Критерии EXP-002b (gate как фильтр):
  net >= 0.90*S0; MTM max DD <= 0.80*S0; net PF >= S0; win >= S0; coverage >= 35%; conc <= 50%.
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
SRC = os.path.join(REPORTS, "exp002_vol_gate_202508_202512_DRAFT.json")


def pf_components(rows: list[dict]) -> tuple[float, float]:
    gp = gl = 0.0
    for r in rows:
        G = r["gross"]
        pf = r["pf"]
        if pf and pf > 1.0001:
            gl_i = G / (pf - 1.0)
            gp_i = pf * gl_i
        else:
            # вырожденный случай: gross алгебраический, аппроксимация
            gp_i = G if G > 0 else 0.0
            gl_i = -G if G < 0 else 0.0
        gp += gp_i
        gl += gl_i
    return gp, gl


def main() -> None:
    r = json.load(open(SRC))
    s0, s1 = r["s0_gate_off"], r["s1_high_only"]
    a0, a1 = s0["total"], s1["total"]
    gp0, gl0 = pf_components(s0["per_figi"])
    gp1, gl1 = pf_components(s1["per_figi"])
    pf0 = gp0 / gl0 if gl0 > 0 else None
    pf1 = gp1 / gl1 if gl1 > 0 else None

    metrics = {
        "net_s0": a0["net"], "net_s1": a1["net"],
        "net_ratio": round(a1["net"] / a0["net"], 4),
        "net_gte_090": a1["net"] >= 0.90 * a0["net"],
        "mtm_dd_s0": a0["max_dd_rub"], "mtm_dd_s1": a1["max_dd_rub"],
        "dd_ratio": round(a1["max_dd_rub"] / a0["max_dd_rub"], 4),
        "dd_lte_080": a1["max_dd_rub"] <= 0.80 * a0["max_dd_rub"],
        "pf_s0": round(pf0, 3) if pf0 else None, "pf_s1": round(pf1, 3) if pf1 else None,
        "pf_gte": (pf1 or 0) >= (pf0 or 0),
        "win_s0": a0["win_rate"], "win_s1": a1["win_rate"], "win_gte": a1["win_rate"] >= a0["win_rate"],
        "coverage_pct": round(a1["trades"] / a0["trades"] * 100, 1), "coverage_gte_35": (a1["trades"] / a0["trades"] * 100) >= 35.0,
        "max_concentration_pct": r["metrics"]["max_figi_concentration_pct"], "conc_lte_50": r["metrics"]["max_figi_concentration_pct"] <= 50.0,
    }
    passed = all([metrics["net_gte_090"], metrics["dd_lte_080"], metrics["pf_gte"],
                  metrics["win_gte"], metrics["coverage_gte_35"], metrics["conc_lte_50"]])

    report = {
        "experiment": "EXP-002b_primary", "status": "RUNNING_COMPLETED",
        "window": "2025-08-01..2025-12-31", "config_hash": "1c7f75dc44c2aa67",
        "switch": "entry_volatility_gate off -> rolling_atr_high_only",
        "note": "immutable data из EXP-002 DRAFT; движок не перезапускался",
        "s0": s0, "s1": s1, "metrics": metrics, "passed": passed,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    out = os.path.join(REPORTS, "exp002b_primary_202508_202512.json")
    with open(out, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = ["# EXP-002b PRIMARY — volatility entry gate (2025-08-01..2025-12-31)", "",
          f"config_hash: {report['config_hash']} | switch: gate off → rolling_atr_high_only", "",
          "| критерий | S0 | S1 | порог | статус |",
          "|---|---|---|---|---|",
          f"| net | {a0['net']} | {a1['net']} | ≥0.90×S0={0.90*a0['net']:.0f} | {'✅' if metrics['net_gte_090'] else '❌'} |",
          f"| MTM max DD | {a0['max_dd_rub']} | {a1['max_dd_rub']} | ≤0.80×S0={0.80*a0['max_dd_rub']:.1f} | {'✅' if metrics['dd_lte_080'] else '❌'} |",
          f"| aggregate net PF | {metrics['pf_s0']} | {metrics['pf_s1']} | ≥S0 | {'✅' if metrics['pf_gte'] else '❌'} |",
          f"| win rate | {a0['win_rate']} | {a1['win_rate']} | ≥S0 | {'✅' if metrics['win_gte'] else '❌'} |",
          f"| coverage | {a0['trades']} | {a1['trades']} ({metrics['coverage_pct']}%) | ≥35% | {'✅' if metrics['coverage_gte_35'] else '❌'} |",
          f"| concentration | — | {metrics['max_concentration_pct']}% | ≤50% | {'✅' if metrics['conc_lte_50'] else '❌'} |",
          "", f"**ИТОГ: {'PASSES (primary)' if passed else 'REJECTED-for-now'}** (нужно ВСЕ 6).",
          "", "## MTM-отчёт (per-FIGI)", "", "| FIGI | S0 net | S1 net | S0 DD | S1 DD | S1 win |",
          "|---|---|---|---|---|---|"]
    for x in s0["per_figi"]:
        y = next(i for i in s1["per_figi"] if i["figi"] == x["figi"])
        md.append(f"| {x['figi'][:12]} | {x['net']} | {y['net']} | {x['max_dd_rub']} | {y['max_dd_rub']} | {y['win_rate']} |")
    md += ["", "## Интерпретация", "При прохождении primary — подтверждение на 2-м независимом периоде "
           "(2026 non-baseline месяц) как stability check, ТОЛЬКО затем кандидат в baseline."]
    with open(os.path.join(REPORTS, "exp002b_primary_202508_202512_MY3_review.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"metrics": metrics, "passed": passed, "out": out}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
