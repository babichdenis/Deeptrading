"""EXP-003a (P0 RUNNING_EXPERIMENT): remove RSI from function-set (7 -> 6).

S0: all 7 setups (canonical baseline). S1: 6 setups (rsi_reversal removed),
quorum=2 среди оставшихся 6 (не меняется). Single variable: function set.
Окно: 2025-01-01..2025-08-01 (disjoint от 2025-08..12 EXP-002b и от July 2026 baseline).
5 FIGI, capital 10k, exit atr_stop (baseline), sizing unchanged.
Критерии успеха (gate КАК ФИЛЬТР фичи): S1 net >= 0.95*S0; net/trade >= S0;
PF >= S0; win >= S0; coverage >= 95% (RSI не несёт уникального edge).
"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2025,1,1,tzinfo=timezone.utc)
T_TO   = datetime(2025,8,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'
WINDOW_LABEL = '2025-01-01..2025-08-01'
ALL_7 = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim",
         "range_compression_breakout","macd_cross","donchian_breakout"]
SIX = [s for s in ALL_7 if s != "rsi_reversal"]


def base_req(figi, mode):
    req = {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
           'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
           'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
           'opposite_hold':False,
           'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
           'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
           'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}
    if mode == 'baseline':
        req['use_all_setups'] = True
        req['drop_useless'] = True
    else:  # no_rsi
        req['setups'] = [{'strategy_id':s,'tf':'5min','params':{}} for s in SIX]
        req['use_all_setups'] = False
        req['drop_useless'] = False
    return req


def run_arm(mode):
    tag = 'baseline' if mode == 'baseline' else 'no_rsi'
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports',
                              f'exp003a_partial_{tag}.json')
    done = {}
    if os.path.exists(cache_path):
        done = {r['figi']: r for r in json.load(open(cache_path))}
    rows = list(done.values())
    for figi in FIGIS:
        if figi in done:
            print(f"  {figi}: из кэша net={done[figi]['net']:.0f} trades={done[figi]['trades']}", flush=True)
            continue
        candles = _load_candles(figi, T_FROM, T_TO)
        if not candles:
            print(f"  {figi}: ПУСТО (нет данных за окно!)", flush=True); continue
        res = compute_ensemble(candles, base_req(figi, mode))
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


print("=== EXP-003a S0: 7 setups (baseline) ===", flush=True)
s0 = run_arm('baseline')
print("=== EXP-003a S1: 6 setups (no RSI) ===", flush=True)
s1 = run_arm('no_rsi')


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
    'net_gte_095': a1['net'] >= 0.95 * a0['net'],
    'net_per_trade_gte_s0': npt1 >= npt0,
    'pf_gte_s0': (a1['pf'] or 0) >= (a0['pf'] or 0),
    'win_gte_s0': a1['win_rate'] >= a0['win_rate'],
    'coverage_gte_95': cov >= 95.0,
}
passed = all(checks.values())

report = {'experiment':'EXP-003a_remove_rsi','status':'RUNNING_COMPLETED',
          'window':WINDOW_LABEL,'figis':FIGIS,'capital':CAPITAL,
          'baseline_config_hash':BASELINE_HASH,
          'switch':'function_set 7 -> 6 (remove rsi_reversal); quorum=2 of 6',
          's0_7_setups':{'per_figi':s0,'total':a0},'s1_6_no_rsi':{'per_figi':s1,'total':a1},
          'metrics':{'net_per_trade_s0':npt0,'net_per_trade_s1':npt1,
                     'coverage_pct':cov,'checks':checks},
          'passed':passed,'generated_at':datetime.now(timezone.utc).isoformat()}
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
out = os.path.join(REPORTS_DIR, 'exp003a_remove_rsi_202501_202507.json')
with open(out,'w') as f: json.dump(report,f,ensure_ascii=False,indent=2)
for tag in ('baseline', 'no_rsi'):
    p = os.path.join(REPORTS_DIR, f'exp003a_partial_{tag}.json')
    if os.path.exists(p): os.remove(p)

md = ["# EXP-003a — remove RSI (7→6 setups)", "",
      f"config_hash: {BASELINE_HASH} | window: {WINDOW_LABEL} | switch: 7 → 6 (remove rsi_reversal)",
      "quorum=2 из 6 оставшихся; exit/sizing unchanged.", "",
      "| критерий | S0 (7) | S1 (6 no RSI) | порог | статус |", "|---|---|---|---|---|",
      f"| net | {a0['net']} | {a1['net']} | ≥0.95×S0={round(0.95*a0['net'],0)} | {'✅' if checks['net_gte_095'] else '❌'} |",
      f"| net/trade | {npt0} | {npt1} | ≥S0 | {'✅' if checks['net_per_trade_gte_s0'] else '❌'} |",
      f"| PF | {a0['pf']} | {a1['pf']} | ≥S0 | {'✅' if checks['pf_gte_s0'] else '❌'} |",
      f"| win | {a0['win_rate']} | {a1['win_rate']} | ≥S0 | {'✅' if checks['win_gte_s0'] else '❌'} |",
      f"| coverage | {a0['trades']} | {a1['trades']} ({cov}%) | ≥95% | {'✅' if checks['coverage_gte_95'] else '❌'} |",
      "", f"**ИТОГ: {'PASSES' if passed else 'REJECTED-for-now'}** (нужно ВСЕ 5).",
      "", "## Per-FIGI", "", "| FIGI | S0 net | S1 net | S0 n | S1 n | S0 PF | S1 PF | S0 win | S1 win |",
      "|---|---|---|---|---|---|---|---|---|"]
for x in s0:
    y = next(i for i in s1 if i["figi"] == x["figi"])
    md.append(f"| {x['figi'][:12]} | {x['net']} | {y['net']} | {x['trades']} | {y['trades']} | {x['pf']} | {y['pf']} | {x['win_rate']} | {y['win_rate']} |")
md += ["", "## Инварианты (DEEPSEEK_EXECUTOR)",
       "- single variable: function set (rsi_reversal removed); quorum=2 of 6; oracle off; no look-ahead.",
       "- session main 10:00-18:45 MSK, exit atr_stop, capital 10k, lot sizing unchanged.",
       "- coverage = S1 trades / S0 trades; проверяет, что RSI не несёт уникального edge."]
with open(os.path.join(REPORTS_DIR, "exp003a_remove_rsi_202501_202507_MY3_review.md"), "w") as f:
    f.write("\n".join(md))
print(json.dumps({'S0':a0,'S1':a1,'coverage':cov,'net_per_trade':[npt0,npt1],'checks':checks,'passed':passed},ensure_ascii=False,indent=1))
print('saved:',out)
