"""GOLD-regime на расширенном окне 2026-01-01..2026-08-01 (H-058 подготовка pre-reg GOLD-фильтра).

Baseline backtest (canonical config, main session) -> trades.csv -> макро-разметка
(GOLD/USDRUB/IMOEX/BRENT, метод B) -> permutation-тесты GOLD down vs up и др.
Окно дисjoинт от July (5b44f3b383df) и Mar-Apr (57b6244ee3eb) частично перекрывает — это
телеметрия, не primary; движок/config_hash не меняются.
"""
import sys, json, os, bisect
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,1,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,8,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'
WINDOW_LABEL = '2026-01-01..2026-08-01'
OUT_DIR = os.path.join(REPORTS, 'gold_window_202601_202607')


def base_req(figi):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}


def main():
    import numpy as np, pandas as pd
    from app.services.research_pack import _load_candles
    from app.services.ensemble import compute_ensemble
    import csv

    os.makedirs(OUT_DIR, exist_ok=True)
    cache_path = os.path.join(OUT_DIR, 'trades.csv')
    trades = []
    if os.path.exists(cache_path):
        trades = list(csv.DictReader(open(cache_path)))
        print(f"trades из кэша: {len(trades)}", flush=True)
    else:
        all_trades = []
        for figi in FIGIS:
            candles = _load_candles(figi, T_FROM, T_TO)
            print(f"  {figi}: {len(candles)} свечей", flush=True)
            res = compute_ensemble(candles, base_req(figi))
            if 'error' in res:
                print(f"  {figi}: {res['error']}", flush=True); continue
            ts = res['static']['trades']
            for t in ts:
                all_trades.append({'trade_id': f"{figi}:{t['entry_ts']}", 'figi': figi, 'side': t.get('side'),
                                   'decision_time': t['entry_ts'],
                                   'net_rub': t['net'], 'gross_rub': t.get('gross', 0)})
            print(f"  {figi}: {len(ts)} сделок", flush=True)
        with open(cache_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['trade_id','figi','side','decision_time','net_rub','gross_rub'])
            w.writeheader()
            for t in all_trades:
                w.writerow(t)
        trades = all_trades
        print(f"всего trades: {len(trades)}", flush=True)

    # макро-разметка
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import macro_regime_attribution_v2 as M2
    M2.T_FROM = T_FROM
    M2.T_TO = T_TO
    factors = {}
    for name in ("IMOEX", "BRENT", "GOLD", "USDRUB"):
        factors[name] = M2.build_factor_series(name)
        print(f"  factor {name}: {len(factors[name][0])} баров", flush=True)
    reg_fns = {name: M2.make_regime_fn(*factors[name]) for name in factors}
    idx = {name: factors[name][0] for name in factors}

    rows = []
    for t in trades:
        figi = t['figi']
        if figi not in M2.FIGI_SECTOR:
            continue
        dt = datetime.fromisoformat(t['decision_time'].replace('Z', '+00:00'))
        sector = M2.FIGI_SECTOR[figi][1]
        fname = M2.SECTOR_FACTOR[sector]
        j = bisect.bisect_right(idx[fname], dt) - 1
        rg, rs = reg_fns[fname](j, 'B')
        net = float(t['net_rub'])
        rows.append({'factor': fname, 'regime': rg, 'net': net})
    # металлы + GOLD
    for t in trades:
        figi = t['figi']
        if figi not in M2.METALS_GOLD:
            continue
        dt = datetime.fromisoformat(t['decision_time'].replace('Z', '+00:00'))
        j = bisect.bisect_right(idx['GOLD'], dt) - 1
        rg, rs = reg_fns['GOLD'](j, 'B')
        rows.append({'factor': 'GOLD', 'regime': rg, 'net': float(t['net_rub'])})
    print(f"rows: {len(rows)}", flush=True)

    N_PERM, SEED = 5000, 42
    rng = np.random.default_rng(SEED)

    def perm_test(nets, group):
        nets = np.asarray(nets, float); g = np.asarray(group, int)
        if len(nets) < 2 or int(g.sum()) < 30 or int((1-g).sum()) < 30:
            return {"n0": int((g==0).sum()), "n1": int((g==1).sum()), "mean0": None, "mean1": None,
                    "diff": None, "p_two_sided": None, "p_one_sided": None, "ci95": None, "status": "insufficient_n"}
        d_actual = nets[g==1].mean() - nets[g==0].mean()
        d_perm = np.empty(N_PERM)
        for i in range(N_PERM):
            gi = rng.permutation(g)
            d_perm[i] = nets[gi==1].mean() - nets[gi==0].mean()
        p_two = float((np.abs(d_perm) >= np.abs(d_actual)).mean())
        p_one = float((d_perm >= d_actual).mean())
        lo, hi = np.percentile(d_perm, 2.5), np.percentile(d_perm, 97.5)
        return {"n0": int((g==0).sum()), "n1": int((g==1).sum()),
                "mean0": round(float(nets[g==0].mean()),3), "mean1": round(float(nets[g==1].mean()),3),
                "diff": round(float(d_actual),3), "p_two_sided": round(p_two,4), "p_one_sided": round(p_one,4),
                "ci95": [round(float(lo),3), round(float(hi),3)],
                "status": "significant" if min(p_two,p_one) < 0.05 else "not_significant"}

    tests = {}
    for f, a, b in (("GOLD", "down_trend", "up_trend"), ("IMOEX", "up_trend", "down_trend"),
                    ("USDRUB", "down_trend", "up_trend")):
        sub = [r for r in rows if r['factor'] == f and r['regime'] in (a, b)]
        if not sub:
            tests[f"{f}:{a}_vs_{b}"] = {"status": "no_data"}; continue
        group = [1 if r['regime'] == a else 0 for r in sub]
        nets = [r['net'] for r in sub]
        tests[f"{f}:{a}_vs_{b}"] = perm_test(nets, group)

    n_tests = len(tests)
    report = {'schema': 'gold_regime_202601_202607_v1', 'window': WINDOW_LABEL, 'baseline_hash': BASELINE_HASH,
              'figis': FIGIS, 'capital': CAPITAL, 'n_trades': len(trades),
              'bonferroni_alpha': round(0.05 / max(n_tests, 1), 4), 'tests': tests,
              'generated_at': datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, 'gold_regime_202601_202607.json'), 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = ["# GOLD-regime на расширенном окне 2026-01-01..2026-08-01 (H-058 prep)", "",
          f"baseline {BASELINE_HASH} | {WINDOW_LABEL} | 5 FIGI | cap 10k | n_trades={len(trades)}", "",
          f"Число тестов: {n_tests}, Бонферрони α={report['bonferroni_alpha']}", "",
          "| тест | n0/n1 | mean0/mean1 | diff | p_two | p_one | CI95 | статус |", "|---|---|---|---|---|---|---|---|"]
    for k, v in tests.items():
        if v.get('status') == 'no_data':
            md.append(f"| {k} | — | — | — | — | — | — | no_data |"); continue
        if v.get('status') == 'insufficient_n':
            md.append(f"| {k} | {v['n0']}/{v['n1']} | — | — | — | — | — | insufficient_n |"); continue
        md.append(f"| {k} | {v['n0']}/{v['n1']} | {v['mean0']}/{v['mean1']} | {v['diff']} | {v['p_two_sided']} | {v['p_one_sided']} | {v['ci95']} | {v['status']} |")
    md.append("")
    with open(os.path.join(REPORTS, 'gold_regime_202601_202607.md'), 'w') as f:
        f.write("\n".join(md))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
