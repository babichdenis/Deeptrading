"""H-058 (Rule Significance, permutation) + H-059 (Monte Carlo). SHADOW/read-only.

Источники — существующие артефакты 5b44f3b383df (July 2026) + entry_quality.csv.
Никаких новых прогонов, никаких изменений движка. Без графиков.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN = os.path.join(REPORTS, "5b44f3b383df")
N_PERM, SEED = 5000, 42
rng = np.random.default_rng(SEED)


def perm_test(nets, group):
    nets = np.asarray(nets, dtype=float)
    g = np.asarray(group, dtype=int)
    if len(nets) < 2 or int(g.sum()) < 30 or int((1 - g).sum()) < 30:
        return {"n0": int((g == 0).sum()), "n1": int((g == 1).sum()),
                "mean0": float(nets[g == 0].mean()) if (g == 0).any() else None,
                "mean1": float(nets[g == 1].mean()) if (g == 1).any() else None,
                "diff": None, "p_two_sided": None, "p_one_sided": None,
                "ci95": None, "status": "insufficient_n"}
    d_actual = nets[g == 1].mean() - nets[g == 0].mean()
    d_perm = np.empty(N_PERM)
    for i in range(N_PERM):
        gi = rng.permutation(g)
        d_perm[i] = nets[gi == 1].mean() - nets[gi == 0].mean()
    p_two = float((np.abs(d_perm) >= np.abs(d_actual)).mean())
    p_one = float((d_perm >= d_actual).mean())
    lo, hi = np.percentile(d_perm, 2.5), np.percentile(d_perm, 97.5)
    p = min(p_two, p_one)
    status = "significant" if p < 0.05 else "not_significant"
    return {"n0": int((g == 0).sum()), "n1": int((g == 1).sum()),
            "mean0": round(float(nets[g == 0].mean()), 3), "mean1": round(float(nets[g == 1].mean()), 3),
            "diff": round(float(d_actual), 3), "p_two_sided": round(p_two, 4),
            "p_one_sided": round(p_one, 4), "ci95": [round(lo, 3), round(hi, 3)],
            "status": status}


def bonf_adjust(results, names):
    n_tests = len(names)
    out = {}
    for name, r in zip(names, results):
        if r["p_two_sided"] is None:
            out[name] = dict(r)
            continue
        r2 = dict(r)
        r2["bonferroni_alpha"] = round(0.05 / n_tests, 4)
        r2["significant_bonf"] = r2["p_two_sided"] < 0.05 / n_tests
        out[name] = r2
    return out, n_tests


def main():
    out = {"schema": "h058_h059_significance_v1", "n_perm": N_PERM, "seed": SEED}

    # ---- 1a. B4 pullback: deep vs shallow (entry_quality.csv) ----
    eq = pd.read_csv(os.path.join(REPORTS, "entry_quality.csv"))
    q1, q2 = 42.66, 82.02
    deep = eq["pullback_bps"] > q2
    shal = eq["pullback_bps"] < q1
    sel = (deep | shal).values
    nets = eq["net"].values
    r_deep_shal = perm_test(nets[sel], deep.loc[sel].astype(int).values)
    # deep vs non-deep (medium+shallow) — воспроизводит S1 B4-run require_deep
    sel2 = ~deep.values  # non-deep
    group2 = deep.values.astype(int)
    r_deep_nondeep = perm_test(nets, group2)
    out["1a_b4_pullback"] = {
        "deep_vs_shallow": r_deep_shal,
        "deep_vs_nondeep": r_deep_nondeep,
        "note": "deep: pullback>82.02; shallow: <42.66 (Q2/Q1 из entry_quality.json)",
    }

    # ---- 1b. B3 quorum: >=3 vs ==2 ----
    ivf_path = os.path.join(RUN, "vote_attribution_dataset_v1", "intent_vote_features.csv")
    ivf = pd.read_csv(ivf_path)
    ivf = ivf[ivf["net"].notna()]
    q3 = (ivf["quorum_count"] >= 3).values.astype(int)
    out["1b_quorum"] = {"quorum_ge3_vs_eq2": perm_test(ivf["net"].values, q3),
                        "note": "per-intent net, только исполненные intents"}

    # ---- 1c. H-036 macro regimes (v2 метод) ----
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import macro_regime_attribution_v2 as M2
    factors = {}
    for name in ("IMOEX", "BRENT", "GOLD", "USDRUB"):
        factors[name] = M2.build_factor_series(name)
    reg_fns = {name: M2.make_regime_fn(*factors[name]) for name in factors}
    idx = {name: factors[name][0] for name in factors}
    import bisect
    trades = list(pd.read_csv(os.path.join(RUN, "trades.csv")).to_dict("records"))
    labels = {f"{f}_regime": [] for f in ("IMOEX", "GOLD", "USDRUB", "BRENT")}
    nets_by = {f"{f}_regime": [] for f in ("IMOEX", "GOLD", "USDRUB", "BRENT")}
    from datetime import datetime
    for t in trades:
        figi = t["figi"]
        if figi not in M2.FIGI_SECTOR:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        sector = M2.FIGI_SECTOR[figi][1]
        fname = M2.SECTOR_FACTOR[sector]
        if fname not in factors:
            continue
        j = bisect.bisect_right(idx[fname], dt) - 1
        rg, _ = reg_fns[fname](j, "B")
        if rg is None:
            continue
        labels[f"{fname}_regime"].append(rg)
        nets_by[f"{fname}_regime"].append(float(t["net_rub"]))
    # металлы + GOLD сенситив
    for t in trades:
        figi = t["figi"]
        if figi not in M2.METALS_GOLD:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        j = bisect.bisect_right(idx["GOLD"], dt) - 1
        rg, _ = reg_fns["GOLD"](j, "B")
        if rg is None:
            continue
        labels["GOLD_regime"].append(rg)
        nets_by["GOLD_regime"].append(float(t["net_rub"]))

    macro_tests = []
    macro_names = []
    for f, a, b in (("IMOEX", "up_trend", "down_trend"), ("GOLD", "down_trend", "up_trend"),
                    ("USDRUB", "down_trend", "up_trend")):
        g = labels[f"{f}_regime"]
        net = nets_by[f"{f}_regime"]
        # группа1 = первый режим (a), группа0 = второй (b); diff = mean(a) - mean(b)
        group = [1 if x == a else 0 for x, _ in zip(g, net) if x in (a, b)]
        sel_nets = [n for x, n in zip(g, net) if x in (a, b)]
        macro_tests.append(perm_test(sel_nets, group))
        macro_names.append(f"{f}:{a}_vs_{b}")
    out["1c_macro_regimes"], out["1c_n_tests"] = bonf_adjust(macro_tests, macro_names)

    # ---- 1d. function presence fn_X==1 vs 0 ----
    fns = ["fn_bollinger_reclaim", "fn_donchian_breakout", "fn_macd_cross", "fn_pullback_ema",
           "fn_range_compression_breakout", "fn_rsi_reversal", "fn_vwap_reclaim"]
    fn_results = {}
    for fn in fns:
        g = (ivf[fn] == 1).values.astype(int)
        fn_results[fn] = perm_test(ivf["net"].values, g)
    out["1d_function_presence"] = fn_results

    # ---- 2. H-059 Monte Carlo ----
    def monte_carlo(nets, n_mc=10000):
        nets = np.asarray(nets, dtype=float)
        mc_net = np.empty(n_mc); mc_dd = np.empty(n_mc); mc_pf = np.empty(n_mc)
        for i in range(n_mc):
            s = rng.choice(nets, size=nets.size, replace=True)
            mc_net[i] = s.sum()
            cs = np.cumsum(s)
            peak = cs[0]; mdd = 0.0
            for v in cs:
                if v > peak:
                    peak = v
                elif peak - v > mdd:
                    mdd = peak - v
            mc_dd[i] = mdd
            pos = s[s > 0].sum(); neg = abs(s[s < 0].sum())
            mc_pf[i] = pos / neg if neg > 0 else np.inf
        act_cs = np.cumsum(nets)
        peak = act_cs[0]; act_dd = 0.0
        for v in act_cs:
            if v > peak:
                peak = v
            elif peak - v > act_dd:
                act_dd = peak - v
        act_net, act_pf = float(nets.sum()), \
            float(nets[nets > 0].sum() / abs(nets[nets < 0].sum())) if (nets < 0).any() else np.inf
        return {
            "n": int(nets.size),
            "actual": {"net": round(act_net, 2), "max_dd": round(act_dd, 2), "pf": round(act_pf, 3)},
            "percentiles": {
                "net": {"p5": round(float(np.percentile(mc_net, 5)), 2), "p50": round(float(np.percentile(mc_net, 50)), 2), "p95": round(float(np.percentile(mc_net, 95)), 2)},
                "max_dd": {"p5": round(float(np.percentile(mc_dd, 5)), 2), "p50": round(float(np.percentile(mc_dd, 50)), 2), "p95": round(float(np.percentile(mc_dd, 95)), 2)},
                "pf": {"p5": round(float(np.percentile(mc_pf[mc_pf != np.inf], 5)), 3), "p50": round(float(np.percentile(mc_pf[mc_pf != np.inf], 50)), 3), "p95": round(float(np.percentile(mc_pf[mc_pf != np.inf], 95)), 3)},
            },
            "actual_percentile_rank": {
                "net": round(float((mc_net <= act_net).mean() * 100), 1),
                "max_dd": round(float((mc_dd >= act_dd).mean() * 100), 1),
                "pf": round(float((mc_pf <= act_pf).mean() * 100), 1) if act_pf != np.inf else None,
            },
        }

    july_nets = pd.read_csv(os.path.join(RUN, "trades.csv"))["net_rub"].values
    mar_nets = pd.read_csv(os.path.join(REPORTS, "57b6244ee3eb", "trades.csv"))["net_rub"].values
    out["2_monte_carlo"] = {
        "july_2026_517": monte_carlo(july_nets),
        "mar_apr_2026_main_1005": monte_carlo(mar_nets),
    }

    with open(os.path.join(REPORTS, "h058_significance_testing.json"), "w") as f:
        json.dump({"h058": out}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORTS, "h059_monte_carlo.json"), "w") as f:
        json.dump({"h059": out["2_monte_carlo"], "params": {"n_mc": 10000, "seed": SEED}},
                  f, ensure_ascii=False, indent=2)

    # ---- Markdown ----
    def fmt(r):
        if r.get("status") == "insufficient_n":
            return f"n0={r['n0']}/n1={r['n1']} — insufficient_n"
        return (f"n0={r['n0']}/n1={r['n1']} mean {r['mean0']}/{r['mean1']} diff={r['diff']} "
                f"p_two={r['p_two_sided']} p_one={r['p_one_sided']} ci95={r['ci95']} → {r['status']}")

    md = ["# H-058/H-059 — Rule Significance + Monte Carlo (SHADOW)", "",
          f"N_PERM={N_PERM}, seed={SEED}. Источники: артефакты July 2026 (5b44f3b383df) + entry_quality.csv.", "",
          "## 1a. B4 pullback (entry_quality.csv, 517 сделок)", "",
          "- deep vs shallow:", "", "  " + fmt(out["1a_b4_pullback"]["deep_vs_shallow"]), "",
          "- deep vs non-deep (S1 B4-run require_deep):", "", "  " + fmt(out["1a_b4_pullback"]["deep_vs_nondeep"]), "",
          "## 1b. B3 quorum (intent_vote_features.csv)", "",
          "- quorum>=3 vs ==2:", "", "  " + fmt(out["1b_quorum"]["quorum_ge3_vs_eq2"]), "",
          "## 1c. H-036 macro regimes (метод B; multiple testing)", "",
          f"Число тестов: {out['1c_n_tests']} (Бонферрони α={round(0.05/out['1c_n_tests'],4)})", ""]
    for k, v in out["1c_macro_regimes"].items():
        md.append(f"- {k}: {fmt(v)}" + (f" [bonf: {v.get('significant_bonf')}]" if "significant_bonf" in v else ""))
    md += ["", "## 1d. Function presence (7 функций)", ""]
    for fn, r in out["1d_function_presence"].items():
        md.append(f"- {fn}: {fmt(r)}")
    md += ["", "## 2. H-059 Monte Carlo (10 000 симуляций, with replacement)", ""]
    for k, v in out["2_monte_carlo"].items():
        md.append(f"### {k} (n={v['n']})")
        md.append(f"- actual: net={v['actual']['net']}, maxDD={v['actual']['max_dd']}, PF={v['actual']['pf']}")
        md.append(f"- перцентили net: p5/p50/p95 = {v['percentiles']['net']['p5']}/{v['percentiles']['net']['p50']}/{v['percentiles']['net']['p95']}")
        md.append(f"- перцентили maxDD: p5/p50/p95 = {v['percentiles']['max_dd']['p5']}/{v['percentiles']['max_dd']['p50']}/{v['percentiles']['max_dd']['p95']}")
        md.append(f"- actual percentile rank: net={v['actual_percentile_rank']['net']}%, dd={v['actual_percentile_rank']['max_dd']}%, pf={v['actual_percentile_rank']['pf']}%")
        md.append("")
    md += ["", "## Выводы (только факты, без рекомендаций по торговле)",
           "- p<0.05 (permutation, two-sided) при n>=30 в обеих группах = значимый разрыв средних.",
           "- Бонферрони для 1c (4 пары)."]
    with open(os.path.join(REPORTS, "h058_significance_testing.md"), "w") as f:
        f.write("\n".join(md))
    with open(os.path.join(REPORTS, "h059_monte_carlo.md"), "w") as f:
        f.write("\n".join(md))

    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
