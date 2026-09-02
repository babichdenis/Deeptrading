import sys
sys.path.insert(0, '.')
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble, TF_SECONDS, micro_breakout, resample as _r
from app.engine.costs import CostModel
from app.engine.policies import SignalPolicyConfig
from app.engine.runner import EngineConfig, EngineRunner
from app.engine.sessions import SessionPolicyConfig
from app.engine.wave1 import ReplayStrategy
from app.services.experiments import build_exit_policy

candles = _load_candles('BBG008F2T3T2', datetime(2026,7,1,tzinfo=timezone.utc), datetime(2026,7,31,tzinfo=timezone.utc))
req = {'figi':'BBG008F2T3T2','bias_mode':'info','bias':{'tf':'hour','period':50},
       'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main',
       'quorum':2,'same_side_reentry_cooldown_bars':15,'carry_overnight':True,
       'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
       'commission_rate':0.0005,'slippage_bps':2.0,'capital':10000,'lot':10,
       'use_all_setups':True,'drop_useless':True,
       'from_ts':'2026-07-01T00:00:00+00:00','to_ts':'2026-07-31T00:00:00+00:00'}
res = compute_ensemble(candles, req)
accepted = res['static']['entries']
entries_raw = micro_breakout(_r(candles, TF_SECONDS['5min']), 1)
exit_obj = build_exit_policy('atr_stop', {'period':14,'multiplier':2.0,'risk_reward':2.0})
cfg = EngineConfig(figi='BBG008F2T3T2', qty=350, allow_short=True,
    cost_model=CostModel(commission_rate=0.0005, slippage_bps=2.0),
    signal_policy=SignalPolicyConfig(same_side_reentry_cooldown_bars=15),
    session_policy=SessionPolicyConfig(overnight=True))
runner = EngineRunner(strategy=ReplayStrategy([(a['ts'], a['side']) for a in accepted],
                                              exits=[(e['ts'], e['side']) for e in entries_raw]),
                      exit_policy=exit_obj, config=cfg)
ledger = runner.run(candles)
print('trades:', len(ledger.trades))
for e in ledger.audit:
    if '2026-07-03T13:0' in str(e.time) or '2026-07-03T13:1' in str(e.time):
        print(e.time, '|', e.kind, '|', e.detail[:100])
