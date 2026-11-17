from datetime import datetime,timedelta,timezone
from app.engine.strategy_window import StrategyWindow
from app.engine.models import Candle,Signal,Side


def test_hidden_warmup_updates_state_and_end_is_exclusive():
    at=datetime(2026,8,1,tzinfo=timezone.utc)
    class CounterStrategy:
        def __init__(self): self.count=0
        def reset(self): self.count=0
        def on_bar(self,bars):
            self.count+=1
            return Signal('fixture',Side.BUY,bars[-1].ts,features={'count':self.count})
    strategy=CounterStrategy();window=StrategyWindow(strategy,start=at,end=at+timedelta(minutes=1))
    before=Candle(at-timedelta(minutes=1),1,1,1,1,1)
    now=Candle(at,1,1,1,1,1)
    after=Candle(at+timedelta(minutes=1),1,1,1,1,1)
    assert window.on_bar([before]) is None
    assert window.on_bar([before,now]).features['count']==2
    assert window.on_bar([before,now,after]) is None
    assert strategy.count==3
    window.reset();assert strategy.count==0
