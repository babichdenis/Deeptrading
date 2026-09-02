"""E2 (DRAFT — НЕ ЗАПУСКАТЬ БЕЗ APPROVE My3): cooldown_bars 15 -> 8.

Однофакторный switch: same_side_reentry_cooldown_bars 15 -> 8.
Всё остальное = baseline hash 1c7f75dc44c2aa67. Окно disjoint от July и Mar-Apr.
Успех (pre-registered): trades>=200; net(S1 cooldown8)>=net(S0 cooldown15) на том же
окне; maxDD(S1)<=S0; coverage>=80%. Fail -> reject.

ВНИМАНИЕ: это DRAFT. Запуск только после ревью draft-конфига и аппрува My3.
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
COOLDOWN = 15  # baseline S0; S1 = 8

def base_req(figi, cooldown):
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':cooldown,'carry_overnight':True,
            'opposite_hold':False,
            'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat()}

def run_arm(cooldown):
    rows = []
    for figi in FIGIS:
        candles = _load_candles(figi, T_FROM, T_TO)
        res = compute_ensemble(candles, base_req(figi, cooldown))
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
        print(f"  {figi}: net={e['net']:.0f} trades={len(trades)} pf={rows[-1]['pf']}", flush=True)
    return rows

print("=== E2 S0: cooldown 15 (baseline) ===", flush=True)
s0 = run_arm(COOLDOWN)
print("=== E2 S1: cooldown 8 ===", flush=True)
s1 = run_arm(8)

def agg(rows):
    return {'trades':sum(r['trades'] for r in rows),
            'gross':round(sum(r['gross'] for r in rows),2),
            'commission':round(sum(r['commission'] for r in rows),2),
            'slippage':round(sum(r['slippage'] for r in rows),2),
            'costs':round(sum(r['costs'] for r in rows),2),
            'net':round(sum(r['net'] for r in rows),2),
            'max_dd_rub':round(max(r['max_dd_rub'] for r in rows),2),
            'win_rate':round(sum(r['win_rate']*r['trades'] for r in rows)/max(sum(r['trades'] for r in rows),1),4) if rows else None}
a0, a1 = agg(s0), agg(s1)
cov = round(a1['trades']/max(a0['trades'],1)*100,1)
passed = (a1['trades']>=200 and a1['net']>=a0['net'] and a1['max_dd_rub']<=a0['max_dd_rub'] and cov>=80.0)
report = {'experiment':'E2_cooldown','status':'DRAFT_NOT_RUN','period':['2026-05-01','2026-06-30'],
          'figis':FIGIS,'capital':CAPITAL,'baseline_config_hash':BASELINE_HASH,
          'switch':'same_side_reentry_cooldown_bars 15->8',
          's0_cooldown15':{'per_figi':s0,'total':a0},'s1_cooldown8':{'per_figi':s1,'total':a1},
          'coverage_s1_of_s0_pct':cov,'pre_registered_success':{
              'min_trades_200':a1['trades']>=200,'net_gte_baseline':a1['net']>=a0['net'],
              'dd_not_worse':a1['max_dd_rub']<=a0['max_dd_rub'],'coverage_gte_80pct':cov>=80.0},
          'passed':passed,'generated_at':datetime.now(timezone.utc).isoformat()}
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
out = os.path.join(REPORTS_DIR, 'e2_cooldown_202605_202606_DRAFT.json')
with open(out,'w') as f: json.dump(report,f,ensure_ascii=False,indent=2)
print(json.dumps({'S0':a0,'S1':a1,'coverage_pct':cov,'passed':passed},ensure_ascii=False,indent=1))
print('DRAFT saved (NOT RUN):',out)
