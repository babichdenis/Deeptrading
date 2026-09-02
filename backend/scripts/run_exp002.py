"""EXP-002: entry_volatility_gate (rolling_atr_high_only).

S0: gate off (baseline). S1: gate=rolling_atr_high_only (входы только в high-vol
дни по rolling ATR 20 пред. дней; WARMUP 20 дней — без входов).
Окно: 2025-08-01..2025-12-31 (frozen, OOS от построения). 5 FIGI, capital 10k.
Критерии успеха (fixed): net>S0; netPF>=S0; median net/trade>=S0; MTM DD<=S0;
>=3/5 FIGI non-negative; S1 trades>=40% S0; max FIGI conc<=45%.
"""
import sys, json, os, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2025,8,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,1,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'

def base_req(figi, gate):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'entry_volatility_gate': gate or None,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}

def run_arm(gate):
    tag = gate or 'baseline'
    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports',
                              f'exp002_partial_{tag}.json')
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

print("=== EXP-002 S0: gate off (baseline) ===", flush=True)
s0 = run_arm(None)
print("=== EXP-002 S1: rolling_atr_high_only ===", flush=True)
s1 = run_arm('rolling_atr_high_only')

def agg(rows):
    gp = sum(r['gross'] for r in rows if r['gross'] > 0)
    gl = sum(-r['gross'] for r in rows if r['gross'] < 0)
    return {'trades':sum(r['trades'] for r in rows),
            'gross':round(sum(r['gross'] for r in rows),2),
            'commission':round(sum(r['commission'] for r in rows),2),
            'slippage':round(sum(r['slippage'] for r in rows),2),
            'costs':round(sum(r['costs'] for r in rows),2),
            'net':round(sum(r['net'] for r in rows),2),
            'pf':round(gp/gl,2) if gl>0 else None,
            'max_dd_rub':round(max(r['max_dd_rub'] for r in rows),2),
            'win_rate':round(sum(r['win_rate']*r['trades'] for r in rows)/max(sum(r['trades'] for r in rows),1),4) if rows else None}
a0, a1 = agg(s0), agg(s1)
nonneg_s1 = sum(1 for r in s1 if r['net'] > 0)
cov = round(a1['trades']/max(a0['trades'],1)*100,1)
# концентрация: max FIGI net share (по abs net)
tot_abs = sum(abs(r['net']) for r in s1)
max_conc = round(max(abs(r['net']) for r in s1)/max(tot_abs,1)*100,1) if s1 else None
passed = (a1['net']>a0['net'] and (a1['pf'] or 0)>=(a0['pf'] or 0)
          and a1['max_dd_rub']<=a0['max_dd_rub'] and nonneg_s1>=3
          and cov>=40.0 and (max_conc or 999)<=45.0)
report = {'experiment':'EXP-002_vol_gate','status':'RUNNING_COMPLETED',
          'period':['2025-08-01','2025-12-31'],'figis':FIGIS,'capital':CAPITAL,
          'baseline_config_hash':BASELINE_HASH,
          'switch':'entry_volatility_gate off -> rolling_atr_high_only',
          's0_gate_off':{'per_figi':s0,'total':a0},'s1_high_only':{'per_figi':s1,'total':a1},
          'metrics':{'coverage_s1_of_s0_pct':cov,'non_negative_figi_s1':nonneg_s1,
                     'max_figi_concentration_pct':max_conc},
          'pre_registered_success':{'net_gte_s0':a1['net']>a0['net'],
              'pf_gte_s0':(a1['pf'] or 0)>=(a0['pf'] or 0),
              'dd_not_worse':a1['max_dd_rub']<=a0['max_dd_rub'],
              'nonneg_figi_3of5':nonneg_s1>=3,
              'coverage_gte_40pct':cov>=40.0,
              'max_conc_lte_45pct':(max_conc or 999)<=45.0},
          'passed':passed,'generated_at':datetime.now(timezone.utc).isoformat()}
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
out = os.path.join(REPORTS_DIR, 'exp002_vol_gate_202508_202512_DRAFT.json')
with open(out,'w') as f: json.dump(report,f,ensure_ascii=False,indent=2)
for tag in ('baseline', 'rolling_atr_high_only'):
    p = os.path.join(REPORTS_DIR, f'exp002_partial_{tag}.json')
    if os.path.exists(p): os.remove(p)
print(json.dumps({'S0':a0,'S1':a1,'coverage':cov,'nonneg':nonneg_s1,'conc':max_conc,'passed':passed},ensure_ascii=False,indent=1))
print('saved:',out)
