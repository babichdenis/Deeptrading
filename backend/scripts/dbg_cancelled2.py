import sys, collections
sys.path.insert(0, '.')
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

candles = _load_candles('BBG008F2T3T2', datetime(2026,7,1,tzinfo=timezone.utc), datetime(2026,7,31,tzinfo=timezone.utc))
req = {'figi':'BBG008F2T3T2','bias_mode':'info','bias':{'tf':'hour','period':50},
       'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
       'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
       'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
       'commission_rate':0.0005,'slippage_bps':2.0,'capital':10000,'lot':10,
       'use_all_setups':True,'drop_useless':True,
       'from_ts':'2026-07-01T00:00:00+00:00','to_ts':'2026-07-31T00:00:00+00:00'}
res = compute_ensemble(candles, req)
trades = res['static']['trades']
# интервалы позиций
def open_at(ts):
    ts_dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
    for t in trades:
        e = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
        x = datetime.fromisoformat(t['exit_ts'].replace('Z','+00:00'))
        if e <= ts_dt <= x:
            return t
    return None
# CANCELLED для RUAL: 2026-07-03T13:05 BUY
for cand in ['2026-07-03T13:05:00+00:00', '2026-07-10T10:10:00+00:00', '2026-07-20T10:15:00+00:00']:
    pos = open_at(cand)
    print(cand, '| позиция на decision:', pos['side'] if pos else None,
          '| entry:', pos['entry_ts'] if pos else None, '->', pos['exit_ts'] if pos else None,
          '| exit_reason:', pos['exit_reason'] if pos else None)
    # сделки за +-15 мин
    ts_dt = datetime.fromisoformat(cand.replace('Z','+00:00'))
    from datetime import timedelta
    for t in trades:
        e = datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
        if ts_dt - timedelta(minutes=15) <= e <= ts_dt + timedelta(minutes=15):
            print('   trade:', t['entry_ts'], t['side'], '->', t['exit_ts'], t['exit_reason'])
