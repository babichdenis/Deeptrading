"""B4-RUN v2: entry_pullback_depth = require_medium (thr=0.25*ATR5m).

По плану 2026-08-28 (My3/owner): B4-run перенесён в P1 с вариантом require medium+
(отсечь shallow pullback, НЕ требовать deep). S0 = gate off (baseline) переиспользуем
из b4run_raw.json (уже посчитан); S1 = require_medium (порог 0.25*ATR5m на decision_ts).
Окно: 2026-05-01..2026-07-01 (disjoint от July baseline и 2025-08..12 EXP-002b).
Критерии (B4-RUN, gate КАК ФИЛЬТР): S1 net/trade >= 1.15*S0; PF >= S0; win >= S0;
coverage >= 30%; DD <= 0.8*S0.
"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,5,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,7,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'
WINDOW_LABEL = '2026-05-01..2026-07-01'
GATE = 'require_medium'


def base_req(figi, gate):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'entry_pullback_depth': gate or None,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}


def run_arm(gate):
    tag = gate or 'baseline'
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports',
                              f'b4run_partial_{tag}.json')
    done = {}
    if os.path.exists(cache_path):
        done = {r['figi']: r for r in json.load(open(cache_path))}
    rows = list(done.values())
    for figi in FIGIS:
        if figi in done:
            print(f"  {figi}: из кэша net={done[figi]['net']:.0f} trades={done[figi]['trades']}", flush=True)
            continue
        candles = _load_candles(figi, T_FROM, T_TO)
        if candles:
            first = candles[0].ts.isoformat(); last = candles[-1].ts.isoformat()
            print(f"  WINDOW {figi[:12]}: {len(candles)} свечей 1m, {first} .. {last}", flush=True)
        else:
            print(f"  WINDOW {figi[:12]}: ПУСТО (нет данных за окно!)", flush=True)
        res = compute_ensemble(candles, base_req(figi, gate))
        if 'error' in res:
            print(f"  {figi}: {res['error']}", flush=True); continue
        stt = res['static']; e = stt['economic']; trades = stt['trades']
        nets = [t['net'] for t in trades]
        gp = sum(max(t['gross'],0) for t in trades); gl = sum(max(-t['gross'],0) for t in trades)
        cum, peak, dd = 0.0, 0.0, 0.0
        for t in sorted(trades, key=lambda x: x['entry_ts']):
            cum += t['net']; peak = max(peak, cum); dd = max(dd, peak-cum)
        wins = sum(1 for n in nets if n > 0)
        rows.append({'figi':figi,'trades':len(trades),'gross':round(e['gross'],2),
                     'commission':round(e['commission'],2),'slippage':round(e['slippage'],2),
                     'costs':round(e['costs'],2),'net':round(e['net'],2),
                     'pf':round(gp/gl,2) if gl>0 else None,
                     'win_rate':round(wins/max(len(nets),1),4),'max_dd_rub':round(dd,2)})
        with open(cache_path, 'w') as _f: json.dump(rows, _f, ensure_ascii=False, indent=1)
        print(f"  {figi}: net={e['net']:.0f} trades={len(trades)} pf={rows[-1]['pf']}", flush=True)
    return rows


RAW_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports', 'b4run_raw.json')
if os.path.exists(RAW_PATH):
    _raw = json.load(open(RAW_PATH))
    s0 = _raw['s0']
    print("=== S0: из b4run_raw.json (gate off, baseline) ===", flush=True)
else:
    print("=== S0: gate off (baseline, пересчёт) ===", flush=True)
    s0 = run_arm(None)
print(f"=== S1: {GATE} (thr=0.25*ATR5m) ===", flush=True)
s1 = run_arm(GATE)


def agg(rows):
    gp = sum(r['gross'] for r in rows if r['gross'] > 0)
    gl = sum(-r['gross'] for r in rows if r['gross'] < 0)
    return {'trades':sum(r['trades'] for r in rows),
            'gross':round(sum(r['gross'] for r in rows),2),
            'commission':round(sum(r['commission'] for r in rows),2),
            'slippage':round(sum(r['slippage'] for r in rows),2),
            'costs':round(sum(r['costs'] for r in rows),2),
            'net':round(sum(r['net'] for r in rows),2),
            'net_per_trade':round(sum(r['net'] for r in rows)/max(sum(r['trades'] for r in rows),1),2),
            'pf':round(gp/gl,2) if gl>0 else None,
            'max_dd_rub':round(max(r['max_dd_rub'] for r in rows),2),
            'win_rate':round(sum(r['win_rate']*r['trades'] for r in rows)/max(sum(r['trades'] for r in rows),1),4) if rows else None}

a0, a1 = agg(s0), agg(s1)
cov = round(a1['trades']/max(a0['trades'],1)*100,1)
npt0 = round(a0['net']/max(a0['trades'],1),2)
npt1 = round(a1['net']/max(a1['trades'],1),2)

checks = {
    'net_per_trade_gte_115': npt1 >= 1.15 * npt0,
    'pf_gte_s0': (a1['pf'] or 0) >= (a0['pf'] or 0),
    'win_gte_s0': a1['win_rate'] >= a0['win_rate'],
    'coverage_gte_30': cov >= 30.0,
    'dd_lte_080_s0': a1['max_dd_rub'] <= 0.80 * a0['max_dd_rub'],
}
passed = all(checks.values())

report = {'experiment':'B4-RUN_pullback_depth_medium','status':'RUNNING_COMPLETED',
          'window':WINDOW_LABEL,'figis':FIGIS,'capital':CAPITAL,
          'baseline_config_hash':BASELINE_HASH,
          'switch':'entry_pullback_depth off -> require_medium (thr=0.25*ATR5m @ decision_ts; reject shallow, keep medium+deep)',
          'note':'single variable; quorum/exit/sizing unchanged; realised peak-to-trough DD used (MTM proxy)',
          's0_gate_off':{'per_figi':s0,'total':a0},'s1_require_medium':{'per_figi':s1,'total':a1},
          'metrics':{'net_per_trade_s0':npt0,'net_per_trade_s1':npt1,
                     'coverage_pct':cov,'max_dd_s0':a0['max_dd_rub'],'max_dd_s1':a1['max_dd_rub'],
                     'checks':checks},
          'passed':passed,'generated_at':datetime.now(timezone.utc).isoformat()}
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
out = os.path.join(REPORTS_DIR, 'b4run_pullback_depth_medium_202605_202606.json')
with open(out,'w') as f: json.dump(report,f,ensure_ascii=False,indent=2)
for tag in ('baseline', GATE):
    p = os.path.join(REPORTS_DIR, f'b4run_partial_{tag}.json')
    if os.path.exists(p): os.remove(p)

md = ["# B4-RUN v2 — entry_pullback_depth (require_medium)", "",
      f"config_hash: {BASELINE_HASH} | window: {WINDOW_LABEL} | switch: off → require_medium (thr=0.25*ATR5m)",
      "(reject shallow, keep medium+deep)", "",
      "| критерий | S0 | S1 | порог | статус |", "|---|---|---|---|---|",
      f"| net/trade | {npt0} | {npt1} | ≥1.15×S0={round(1.15*npt0,2)} | {'✅' if checks['net_per_trade_gte_115'] else '❌'} |",
      f"| PF | {a0['pf']} | {a1['pf']} | ≥S0 | {'✅' if checks['pf_gte_s0'] else '❌'} |",
      f"| win | {a0['win_rate']} | {a1['win_rate']} | ≥S0 | {'✅' if checks['win_gte_s0'] else '❌'} |",
      f"| coverage | {a0['trades']} | {a1['trades']} ({cov}%) | ≥30% | {'✅' if checks['coverage_gte_30'] else '❌'} |",
      f"| max DD (realised) | {a0['max_dd_rub']} | {a1['max_dd_rub']} | ≤0.80×S0={round(0.80*a0['max_dd_rub'],1)} | {'✅' if checks['dd_lte_080_s0'] else '❌'} |",
      "", f"**ИТОГ: {'PASSES' if passed else 'REJECTED-for-now'}** (нужно ВСЕ 5).",
      "", "## Per-FIGI", "", "| FIGI | S0 net | S1 net | S0 n | S1 n | S0 PF | S1 PF | S0 win | S1 win |",
      "|---|---|---|---|---|---|---|---|---|"]
for x in s0:
    y = next(i for i in s1 if i["figi"] == x["figi"])
    md.append(f"| {x['figi'][:12]} | {x['net']} | {y['net']} | {x['trades']} | {y['trades']} | {x['pf']} | {y['pf']} | {x['win_rate']} | {y['win_rate']} |")
md += ["", "## Инварианты (DEEPSEEK_EXECUTOR)", "- single variable entry_pullback_depth; oracle off; no look-ahead (ATR/swing point-in-time).",
       "- session main 10:00-18:45 MSK, quorum=2, exit atr_stop, capital 10k, sizing unchanged.",
       "- realised peak-to-trough DD как MTM proxy."]
with open(os.path.join(REPORTS_DIR, "b4run_pullback_depth_medium_202605_202606_MY3_review.md"), "w") as f:
    f.write("\n".join(md))
print(json.dumps({'S0':a0,'S1':a1,'coverage':cov,'net_per_trade':[npt0,npt1],'checks':checks,'passed':passed},ensure_ascii=False,indent=1))
print('saved:',out)
