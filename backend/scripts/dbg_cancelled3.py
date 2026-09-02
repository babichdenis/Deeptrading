import sys, collections, json
sys.path.insert(0, '.')
from datetime import datetime, timezone, timedelta
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble
from app.services.audit_engine import replay_engine_audit

FIGIS = ['BBG008F2T3T2','BBG004S681M2','BBG004S683W7','BBG004S68CP5','BBG004S681B4']
out = {}
for figi in FIGIS:
    candles = _load_candles(figi, datetime(2026,7,1,tzinfo=timezone.utc), datetime(2026,7,31,tzinfo=timezone.utc))
    req = {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
           'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
           'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
           'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
           'commission_rate':0.0005,'slippage_bps':2.0,'capital':10000,'lot':10,
           'use_all_setups':True,'drop_useless':True,
           'from_ts':'2026-07-01T00:00:00+00:00','to_ts':'2026-07-31T00:00:00+00:00'}
    res = compute_ensemble(candles, req)
    trades = res['static']['trades']
    # CANCELLED через lifecycle
    from app.services.ensemble import TF_SECONDS, micro_breakout, resample as _r
    life, summ = replay_engine_audit(candles, res['static']['entries'],
        micro_breakout(_r(candles, TF_SECONDS['5min']), 1), req)
    cancelled = [x for x in life if x['terminal'] == 'CANCELLED_BEFORE_FILL']
    # для каждого: открыта ли позиция противоположной стороны на decision, и вышла ли по signal_exit
    matched_signal_exit = 0
    no_pos = 0
    for x in cancelled:
        ts_dt = datetime.fromisoformat(x['decision_time'].replace('Z','+00:00'))
        pos = None
        for t in trades:
            e = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
            ex = datetime.fromisoformat(t['exit_ts'].replace('Z','+00:00'))
            if e <= ts_dt <= ex:
                pos = t; break
        if pos is None:
            no_pos += 1
        elif pos['exit_reason'] == 'signal_exit' and ts_dt <= datetime.fromisoformat(pos['exit_ts'].replace('Z','+00:00')):
            matched_signal_exit += 1
    out[figi] = {'cancelled': len(cancelled), 'matched_signal_exit': matched_signal_exit, 'no_pos': no_pos}
print(json.dumps(out, indent=1))
print('TOTAL cancelled:', sum(v['cancelled'] for v in out.values()),
      '| matched signal_exit:', sum(v['matched_signal_exit'] for v in out.values()),
      '| no_pos:', sum(v['no_pos'] for v in out.values()))
