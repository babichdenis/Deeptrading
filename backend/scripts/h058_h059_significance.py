"""H-058 (Rule Significance, permutation) + H-059 (Monte Carlo). SHADOW/read-only.

Источники — существующие артефакты 5b44f3b383df (July 2026) + entry_quality.csv.
Никаких новых прогонов, никаких изменений движка. Без графиков.
"""
import sys, os, json
import argparse
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN = os.path.join(REPORTS, "5b44f3b383df")
N_PERM, SEED = 5000, 42


def finite_vector(values):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError('expected a finite one-dimensional sample')
    return values


def positive_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('simulation count must be a positive integer')
    return value


def perm_test(nets, group, n_perm=N_PERM, seed=SEED):
    nets = finite_vector(nets)
    g = finite_vector(group)
    positive_count(n_perm)
    if len(nets) != len(g) or not np.isin(g, [0, 1]).all():
        raise ValueError('groups must be binary and match the sample length')
    result = {'n0': int((g == 0).sum()), 'n1': int((g == 1).sum()),
              'mean0': float(nets[g == 0].mean()) if (g == 0).any() else None,
              'mean1': float(nets[g == 1].mean()) if (g == 1).any() else None,
              'diff': None, 'p_two_sided': None, 'p_one_sided': None,
              'null_interval95': None, 'status': 'insufficient_n',
              'alternative': 'two-sided', 'n_perm': n_perm, 'seed': seed,
              'p_estimator': 'plus_one'}
    if min(result['n0'], result['n1']) < 30:
        return result
    rng = np.random.default_rng(seed)
    actual = nets[g == 1].mean() - nets[g == 0].mean()
    simulated = np.empty(n_perm)
    for i in range(n_perm):
        shuffled = rng.permutation(g)
        simulated[i] = nets[shuffled == 1].mean() - nets[shuffled == 0].mean()
    p_two = float((np.count_nonzero(np.abs(simulated) >= abs(actual)) + 1) / (n_perm + 1))
    p_one = float((np.count_nonzero(simulated >= actual) + 1) / (n_perm + 1))
    result.update(diff=float(actual), p_two_sided=p_two, p_one_sided=p_one,
                  null_interval95=np.percentile(simulated, [2.5, 97.5]).tolist(),
                  status='significant' if p_two < 0.05 else 'not_significant')
    return result


def bonf_adjust(results, names):
    if len(results) != len(names) or not names or len(set(names)) != len(names):
        raise ValueError('hypothesis names must be nonempty, unique and match results')
    alpha = 0.05 / len(names)
    out = {}
    for name, result in zip(names, results):
        p = result['p_two_sided']
        if p is not None and (not np.isfinite(p) or not 0 <= p <= 1):
            raise ValueError('invalid p-value')
        out[name] = dict(result, bonferroni_alpha=alpha,
                         significant_bonf=None if p is None else bool(p < alpha))
    return out, len(names)


def max_drawdown(nets):
    cashflows = np.r_[0.0, np.cumsum(finite_vector(nets))]
    return float(np.max(np.maximum.accumulate(cashflows) - cashflows))


def profit_factor(nets):
    losses = -nets[nets < 0].sum()
    return float(nets[nets > 0].sum() / losses) if losses > 0 else None


def percentiles(values):
    return {f'p{q}': float(np.percentile(values, q)) if len(values) else None for q in (5, 50, 95)}


def monte_carlo(nets, n_mc=10000, seed=SEED):
    nets = finite_vector(nets)
    positive_count(n_mc)
    if not len(nets):
        return {'n': 0, 'status': 'insufficient_n', 'n_mc': n_mc, 'seed': seed}
    rng = np.random.default_rng(seed)
    mc_net, mc_dd, mc_pf = [], [], []
    for _ in range(n_mc):
        sample = rng.choice(nets, size=len(nets), replace=True)
        mc_net.append(float(sample.sum()))
        mc_dd.append(max_drawdown(sample))
        mc_pf.append(profit_factor(sample))
    actual_net, actual_dd, actual_pf = float(nets.sum()), max_drawdown(nets), profit_factor(nets)
    defined_pf = [p for p in mc_pf if p is not None]
    return {'n': len(nets), 'status': 'ok', 'n_mc': n_mc, 'seed': seed,
            'sampling': 'iid_trade_bootstrap', 'dependence_preserved': False,
            'actual': {'net': actual_net, 'max_dd': actual_dd, 'pf': actual_pf},
            'percentiles': {'net': percentiles(mc_net), 'max_dd': percentiles(mc_dd), 'pf': percentiles(defined_pf)},
            'pf_distribution': {'defined': len(defined_pf), 'undefined': n_mc-len(defined_pf),
                                'percentiles_condition': 'paths with losses'},
            'actual_percentile_rank': {
                'net': float(np.mean(np.asarray(mc_net) <= actual_net)*100),
                'max_dd': float(np.mean(np.asarray(mc_dd) >= actual_dd)*100),
                'pf': float(np.mean(np.asarray(defined_pf) <= actual_pf)*100) if actual_pf is not None and defined_pf else None}}


def compare_feature(frame, column, positive, negative):
    values = pd.to_numeric(frame[column], errors='raise')
    net = pd.to_numeric(frame['net'], errors='raise')
    eligible = np.isfinite(values) & np.isfinite(net)
    pos, neg = positive(values) & eligible, negative(values) & eligible
    if (pos & neg).any():
        raise ValueError('comparison groups overlap')
    selected = pos | neg
    result = perm_test(net[selected].values, pos[selected].astype(int).values)
    result['coverage'] = {'total': len(frame), 'selected': int(selected.sum()),
                          'unknown': int((~eligible).sum()), 'outside_groups': int((eligible & ~selected).sum())}
    return result

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--entry-quality', default=os.path.join(REPORTS, "entry_quality.csv"),
                        help='CSV с колонкой pullback_bps (по умолчанию исторический путь)')
    args = parser.parse_args(argv)
    destination = Path(args.output_dir)
    if destination.exists():
        parser.error('--output-dir must be a new directory')
    out = {"schema": "h058_h059_significance_v2", "n_perm": N_PERM, "seed": SEED}

    # ---- 1a. B4 pullback: deep vs shallow (entry_quality.csv) ----
    eq = pd.read_csv(args.entry_quality)
    q1, q2 = 42.66, 82.02
    r_deep_shal = compare_feature(eq, 'pullback_bps', lambda x: x > q2, lambda x: x < q1)
    r_deep_nondeep = compare_feature(eq, 'pullback_bps', lambda x: x > q2, lambda x: x <= q2)
    out["1a_b4_pullback"] = {
        "deep_vs_shallow": r_deep_shal,
        "deep_vs_nondeep": r_deep_nondeep,
        "note": "deep: pullback>82.02; shallow: <42.66 (Q2/Q1 из entry_quality.json)",
        "entry_quality_file": os.path.abspath(args.entry_quality),
        "thresholds": {"q1": q1, "q2": q2,
                       "origin": "fixed legacy thresholds, NOT quantiles of the supplied CSV"},
    }

    # ---- 1b. B3 quorum: >=3 vs ==2 ----
    ivf_path = os.path.join(RUN, "vote_attribution_dataset_v1", "intent_vote_features.csv")
    ivf = pd.read_csv(ivf_path)
    out["1b_quorum"] = {"quorum_ge3_vs_eq2": compare_feature(ivf, 'quorum_count', lambda x: x >= 3, lambda x: x == 2),
                        "note": "per-intent net, только исполненные intents"}

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import macro_regime_attribution_v3 as macro
    factors = {name: macro.load_full_db(name) for name in macro.FACTORS_CSV}
    series = {name: macro.regime_series(bars) for name, bars in factors.items()}
    timestamps = {name: [bar[0] for bar in bars] for name, bars in factors.items()}
    trades = macro.load_trades(os.path.join(RUN, 'trades.csv'))
    labels = {f'{name}_regime': [] for name in factors}
    nets_by = {f'{name}_regime': [] for name in factors}
    from datetime import datetime
    for trade in trades:
        figi = trade['figi']
        if figi not in macro.FIGI_SECTOR:
            continue
        names = [macro.SECTOR_FACTOR[macro.FIGI_SECTOR[figi][1]]]
        if figi in macro.METALS_GOLD:
            names.append('GOLD')
        for name in names:
            regime, _ = macro.regime_at(timestamps[name], datetime.fromisoformat(trade['decision_time']), series[name])
            labels[f'{name}_regime'].append(regime)
            nets_by[f'{name}_regime'].append(float(trade['net_rub']))
    out['macro_method'] = 'canonical Wilder ADX14, last closed bar; freshness unchecked'
    macro_tests = []
    macro_names = []
    for f, a, b in (("IMOEX", "up_trend", "down_trend"), ("GOLD", "down_trend", "up_trend"),
                    ("USDRUB", "down_trend", "up_trend"), ("BRENT", "up_trend", "down_trend")):
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
        fn_results[fn] = compare_feature(ivf, fn, lambda x: x == 1, lambda x: x == 0)
    out['1d_function_presence'], out['1d_n_tests'] = bonf_adjust(list(fn_results.values()), list(fn_results))

    # ---- 2. H-059 Monte Carlo ----
    july_nets = pd.read_csv(os.path.join(RUN, "trades.csv"))["net_rub"].values
    mar_nets = pd.read_csv(os.path.join(REPORTS, "57b6244ee3eb", "trades.csv"))["net_rub"].values
    out["2_monte_carlo"] = {
        "july_2026_517": monte_carlo(july_nets),
        "mar_apr_2026_main_1005": monte_carlo(mar_nets),
    }

    payloads = {
        'h058_significance_testing.json': json.dumps({'h058': out}, ensure_ascii=False, indent=2, allow_nan=False),
        'h059_monte_carlo.json': json.dumps({'h059': out['2_monte_carlo'], 'params': {'n_mc': 10000, 'seed': SEED}}, ensure_ascii=False, indent=2, allow_nan=False),
    }

    # ---- Markdown ----
    def fmt(r):
        if r.get("status") == "insufficient_n":
            return f"n0={r['n0']}/n1={r['n1']} — insufficient_n"
        return (f"n0={r['n0']}/n1={r['n1']} mean {r['mean0']}/{r['mean1']} diff={r['diff']} "
                f"p_two={r['p_two_sided']} p_one={r['p_one_sided']} null_interval95={r['null_interval95']} → {r['status']}")

    md = ["# H-058/H-059 — Rule Significance + Monte Carlo (SHADOW)", "",
          f"N_PERM={N_PERM}, seed={SEED}. Источники: артефакты July 2026 (5b44f3b383df) + entry_quality.csv.", "",
          "## 1a. B4 pullback (entry_quality.csv)", "",
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
        if v['status'] == 'insufficient_n':
            md.append('- insufficient_n')
            continue
        md.append(f"- actual: net={v['actual']['net']}, maxDD={v['actual']['max_dd']}, PF={v['actual']['pf']}")
        md.append(f"- перцентили net: p5/p50/p95 = {v['percentiles']['net']['p5']}/{v['percentiles']['net']['p50']}/{v['percentiles']['net']['p95']}")
        md.append(f"- перцентили maxDD: p5/p50/p95 = {v['percentiles']['max_dd']['p5']}/{v['percentiles']['max_dd']['p50']}/{v['percentiles']['max_dd']['p95']}")
        md.append(f"- actual percentile rank: net={v['actual_percentile_rank']['net']}%, dd={v['actual_percentile_rank']['max_dd']}%, pf={v['actual_percentile_rank']['pf']}%")
        md.append("")
    md += ["", "## Выводы (только факты, без рекомендаций по торговле)",
           "- p<0.05 (permutation, two-sided) при n>=30 в обеих группах = значимый разрыв средних.",
           "- Бонферрони для 1c (4 пары)."]
    payloads['h058_significance_testing.md'] = '\n'.join(md)
    payloads['h059_monte_carlo.md'] = '\n'.join(md)
    destination.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        with (destination/name).open('x', encoding='utf-8') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    completion = {'status': 'complete', 'outputs': macro.file_hashes(destination/name for name in payloads)}
    with (destination/'completion.json').open('x', encoding='utf-8') as output:
        json.dump(completion, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())

    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
