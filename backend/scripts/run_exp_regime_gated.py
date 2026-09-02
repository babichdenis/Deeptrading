"""REGIME-GATED EXIT (P3b): vol-gated trailing.

S0 (baseline): atr_stop(14, 2.0, RR2.0) — всегда.
S1 (test): trailing (activate 1R / dist 1R, target 2R) ТОЛЬКО для сделок,
          открытых в HIGH-VOL дни (IMOEX daily range > медианы окна);
          LOW-VOL — baseline.
Гейт: IMOEX daily range = (high-low)/open по T_observer_imoex (5m).
Окно: 2026-05-01..2026-06-30 (то же, что E5). 5 FIGI, capital 10k.
Success (pre-registered): net(S1)>=net(S0); maxDD(S1)<=S0; win(S1)>=win(S0)-5pp.
"""
import sys, json, os, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
T_FROM = datetime(2026,5,1,tzinfo=timezone.utc)
T_TO   = datetime(2026,7,1,tzinfo=timezone.utc)
CAPITAL = 10000.0
BASELINE_HASH = '1c7f75dc44c2aa67'


def imoex_daily_ranges():
    """IMOEX daily range = (high-low)/open по дате (MSK). Возвращает {date_iso: range}."""
    candles = _load_candles('BBG00KDWPPW2', T_FROM, T_TO)
    from zoneinfo import ZoneInfo
    msk = ZoneInfo('Europe/Moscow')
    day = {}
    for c in candles:
        d = c.ts.astimezone(msk).date().isoformat()
        r = (c.high - c.low) / c.open if c.open else 0.0
        g = day.get(d)
        if g is None:
            day[d] = {'high': c.high, 'low': c.low, 'open': c.open}
        else:
            g['high'] = max(g['high'], c.high); g['low'] = min(g['low'], c.low)
    return {d: (v['high']-v['low'])/v['open'] if v['open'] else 0.0 for d, v in day.items()}


def high_vol_dates(ranges, threshold):
    return {d for d, r in ranges.items() if r > threshold}


def base_req(figi, trailing, high_vol_set=None, ranges=None):
    exit_policy = {'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}}
    adaptive = None
    if trailing:
        adaptive = [
            {'name':'HIGH_VOL','config':{'mode':'both',
               'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0,
                                                        'trail_activation_r':1.0,'trail_distance_r':1.0}}}},
            {'name':'LOW_VOL','config':{'mode':'both',
               'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}}}},
        ]
    return {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
            'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
            'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
            'opposite_hold':False,'exit_policy':exit_policy,
            'commission_rate':0.0005,'slippage_bps':2.0,'capital':CAPITAL,'lot':10,
            'use_all_setups':True,'drop_useless':True,
            'from_ts':T_FROM.isoformat(),'to_ts':T_TO.isoformat(),
            'regime_gate': {'high_vol_dates': sorted(high_vol_set or []),
                            'ranges': {k: round(v,6) for k,v in (ranges or {}).items()}} if trailing else None,
            'adaptive': adaptive}


def run_arm(trailing, high_vol_set=None, ranges=None):
    rows = []
    for figi in FIGIS:
        candles = _load_candles(figi, T_FROM, T_TO)
        req = base_req(figi, trailing, high_vol_set, ranges)
        res = compute_ensemble(candles, req)
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


print("IMOEX daily ranges...", flush=True)
ranges = imoex_daily_ranges()
threshold = statistics.median(ranges.values())
hv = high_vol_dates(ranges, threshold)
print(f"дней в окне: {len(ranges)}, high-vol: {len(hv)}, threshold={threshold:.5f}", flush=True)

print("=== REGIME-GATED S0: baseline (всегда atr_stop 2R) ===", flush=True)
s0 = run_arm(False)
print("=== REGIME-GATED S1: trailing только high-vol ===", flush=True)
s1 = run_arm(True, hv, ranges)

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
passed = (a1['net']>=a0['net'] and a1['max_dd_rub']<=a0['max_dd_rub']
          and (a1['win_rate'] or 0) >= (a0['win_rate'] or 0)-0.05)
report = {'experiment':'REGIME_GATED_EXIT','status':'RUNNING_COMPLETED',
          'period':['2026-05-01','2026-06-30'],'figis':FIGIS,'capital':CAPITAL,
          'baseline_config_hash':BASELINE_HASH,
          'gate':{'imoex_daily_range_median_threshold':threshold,'high_vol_days':len(hv),'total_days':len(ranges)},
          's0_baseline':{'per_figi':s0,'total':a0},'s1_gated_trailing':{'per_figi':s1,'total':a1},
          'pre_registered_success':{'net_gte_baseline':a1['net']>=a0['net'],
              'dd_not_worse':a1['max_dd_rub']<=a0['max_dd_rub'],
              'win_not_worse_5pp':(a1['win_rate'] or 0) >= (a0['win_rate'] or 0)-0.05},
          'passed':passed,'generated_at':datetime.now(timezone.utc).isoformat()}
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
out = os.path.join(REPORTS_DIR, 'regime_gated_exit_202605_202606_DRAFT.json')
with open(out,'w') as f: json.dump(report,f,ensure_ascii=False,indent=2)
print(json.dumps({'S0':a0,'S1':a1,'passed':passed},ensure_ascii=False,indent=1))
print('saved:',out)
