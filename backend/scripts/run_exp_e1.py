"""E1: signal_exit on/off (единственный RUNNING_EXPERIMENT).

S0 = signal_exit ON  (opposite_hold=False) — baseline
S1 = signal_exit OFF (opposite_hold=True)  — держим позицию, противоположный сигнал не закрывает
Окно: 2026-03-01..2026-04-30 (disjoint от July), 5 акций, capital 10k.
Условия успеха (pre-registered, из PROJECT_STATE):
  min 200 сделок; PF >= baseline; realised DD не хуже; coverage >= 80% S0.
Правило конвейера: единственный RUNNING_EXPERIMENT; движок/параметры не меняются
кроме одного переключателя opposite_hold.
"""
import sys, json, hashlib
sys.path.insert(0, '/Users/Denis/Dev/Deeptrading/backend')
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,3,1,tzinfo=timezone.utc)
T_TO = datetime(2026,5,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'

def base_req(figi, opposite_hold):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':opposite_hold,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}

def run_arm(opposite_hold):
    rows = []
    for figi in FIGIS:
        candles = _load_candles(figi, T_FROM, T_TO)
        res = compute_ensemble(candles, base_req(figi, opposite_hold))
        if 'error' in res:
            print(f"  {figi}: {res['error']}", flush=True); continue
        st = res['static']
        e = st['economic']
        trades = st['trades']
        nets = [t['net'] for t in trades]
        gross_pos = sum(max(t['gross'],0) for t in trades)
        gross_neg = sum(max(-t['gross'],0) for t in trades)
        cum, peak, dd = 0.0, 0.0, 0.0
        for t in sorted(trades, key=lambda x: x['entry_ts']):
            cum += t['net']; peak = max(peak, cum); dd = max(dd, peak-cum)
        wins = sum(1 for n in nets if n > 0)
        rows.append({'figi':figi, 'trades':len(trades), 'gross':round(e['gross'],2),
                     'commission':round(e['commission'],2), 'slippage':round(e['slippage'],2),
                     'costs':round(e['costs'],2), 'net':round(e['net'],2),
                     'pf':round(gross_pos/gross_neg,2) if gross_neg>0 else None,
                     'win_rate':round(wins/max(len(nets),1),4) if nets else None,
                     'max_dd_rub':round(dd,2)})
        print(f"  {figi}: net={e['net']:.0f} trades={len(trades)} pf={rows[-1]['pf']}", flush=True)
    return rows

print("=== E1 S0: signal_exit ON (baseline) ===", flush=True)
s0 = run_arm(False)
print("=== E1 S1: signal_exit OFF ===", flush=True)
s1 = run_arm(True)

def agg(rows):
    return {'trades': sum(r['trades'] for r in rows),
            'gross': round(sum(r['gross'] for r in rows),2),
            'commission': round(sum(r['commission'] for r in rows),2),
            'slippage': round(sum(r['slippage'] for r in rows),2),
            'costs': round(sum(r['costs'] for r in rows),2),
            'net': round(sum(r['net'] for r in rows),2),
            'max_dd_rub': round(max(r['max_dd_rub'] for r in rows),2),
            'win_rate': round(sum(r['win_rate']*r['trades'] for r in rows)/max(sum(r['trades'] for r in rows),1),4) if rows else None}
a0, a1 = agg(s0), agg(s1)
gp0 = sum(max(r['gross'],0) for r in s0); gl0 = sum(max(-r['gross'],0) for r in s0)
gp1 = sum(max(r['gross'],0) for r in s1); gl1 = sum(max(-r['gross'],0) for r in s1)
a0['pf'] = round(gp0/gl0,2) if gl0>0 else None
a1['pf'] = round(gp1/gl1,2) if gl1>0 else None
cov = round(a1['trades']/max(a0['trades'],1)*100, 1)
passed = (a1['trades'] >= 200 and a1['pf'] is not None and a1['pf'] >= (a0['pf'] or 0)
          and a1['max_dd_rub'] <= a0['max_dd_rub'] and cov >= 80.0)
report = {
    'experiment': 'E1_signal_exit',
    'status': 'RUNNING_EXPERIMENT',
    'period': ['2026-03-01','2026-04-30'],
    'figis': FIGIS, 'capital': CAPITAL,
    'baseline_config_hash': BASELINE_HASH,
    'switch': 'opposite_hold: False(S0=on) vs True(S1=off)',
    's0_signal_exit_on': {'per_figi': s0, 'total': a0},
    's1_signal_exit_off': {'per_figi': s1, 'total': a1},
    'coverage_s1_of_s0_pct': cov,
    'pre_registered_success': {
        'min_trades_200': a1['trades'] >= 200,
        'pf_gte_baseline': (a1['pf'] is not None and a0['pf'] is not None and a1['pf'] >= a0['pf']),
        'dd_not_worse': a1['max_dd_rub'] <= a0['max_dd_rub'],
        'coverage_gte_80pct': cov >= 80.0,
    },
    'passed': passed,
    'generated_at': datetime.now(timezone.utc).isoformat(),
}
out = '/Users/Denis/Dev/Deeptrading/backend/reports/e1_signal_exit_202603_202604.json'
with open(out,'w') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(json.dumps({'S0': a0, 'S1': a1, 'coverage_pct': cov, 'passed': passed}, ensure_ascii=False, indent=1))
print('saved:', out)
