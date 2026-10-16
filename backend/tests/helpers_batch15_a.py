from datetime import date, timedelta

import day_strategies_core as a7

DAY = date(2026, 7, 1)
MSK = a7.MSK


def _full(day: date, label: str, open_: float, high: float, low: float,
          close: float, volume: float = 1000.0) -> a7.Bar10:
    ts = a7.label_ts(day, label)
    minutes = [ts + timedelta(minutes=i) for i in range(10)]
    return a7.Bar10(ts=ts, minutes=minutes, open=open_, high=high, low=low,
                    close=close, volume=volume)


def _tdata(bars_by_label: dict, atr: float = 5.0, n_tr: int = 14) -> a7.TickerData:
    bar_by_day_label = {(DAY, a7.label_mod(lab)): b
                        for lab, b in bars_by_label.items()}
    atr_seq = [(a7.label_ts(DAY, "10:50"), atr, n_tr)]
    return a7.TickerData(
        ticker="TEST",
        minutes_by_day={},
        minute_by_ts={},
        buckets={},
        session_dates=[DAY],
        full_session_bars=[],
        bar_by_day_label=bar_by_day_label,
        atr_seq=atr_seq,
        warmup_start=DAY,
    )
