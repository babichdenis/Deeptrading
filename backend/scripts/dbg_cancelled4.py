import sys, json
sys.path.insert(0, '.')
from datetime import datetime, timezone, timedelta
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble, TF_SECONDS, micro_breakout, resample as _r
from app.services.audit_engine import replay_engine_audit

figi = 'BBG004S68CP5'  # MVID — все no_pos
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
life, summ = replay_engine_audit(candles, res['static']['entries'],
    micro_breakout(_r(candles, TF_SECONDS['5min']), 1), req)
cancelled = [x for x in life if x['terminal'] == 'CANCELLED_BEFORE_FILL']
print('MVID cancelled:', len(cancelled))
for x in cancelled:
    ts_dt = datetime.fromisoformat(x['decision_time'].replace('Z','+00:00'))
    pos = None
    for t in trades:
        e = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
        ex = datetime.fromisoformat(t['exit_ts'].replace('Z','+00:00'))
        if e <= ts_dt <= ex:
            pos = t; break
    if pos is None:
        # ближайшая сделка
        nearest = None
        for t in trades:
            e = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
            if e > ts_dt:
                d = (e - ts_dt).total_seconds()
                if nearest is None or d < nearest[0]:
                    nearest = (d, t)
        print(' ', x['decision_time'], x['side'], '| нет позиции; ближайший вход через',
              f"{(nearest[1]['entry_ts'])}" if nearest else '?', nearest[1]['side'] if nearest else '')
