from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine import (
    Candle,
    EngineConfig,
    EngineRunner,
    FixedSlTpPolicy,
    ScriptedStrategy,
    Signal,
    SignalPolicyConfig,
    Side,
)
from app.engine.sessions import SessionPolicy, SessionPolicyConfig
from app.engine.strategies import DonchianBreakoutStrategy, DonchianParams


def utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


T0 = utc(2026, 6, 15, 15, 0)


def series_from(times: list[datetime], price: float = 100.0) -> list[Candle]:
    return [
        Candle(ts=t, open=price, high=price + 0.3, low=price - 0.3, close=price)
        for t in times
    ]


def five_min(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(count)]


def scripted(index_side: dict[int, str]) -> ScriptedStrategy:
    return ScriptedStrategy(
        {
            i: Signal("scripted", Side(side), T0, "script")
            for i, side in index_side.items()
        }
    )


def test_session_cutoff_blocks_late_entry():
    times = five_min(T0, 9)
    candles = series_from(times)
    runner = EngineRunner(
        strategy=scripted({7: "BUY"}),
        exit_policy=FixedSlTpPolicy(),
        config=EngineConfig(
            figi="TEST",
            session_policy=SessionPolicyConfig(entry_cutoff_bars=6),
        ),
    )
    ledger = runner.run(candles)
    assert len(ledger.trades) == 0
    details = [a.detail for a in ledger.audit]
    assert any("REJECT_SESSION_CUTOFF" in d for d in details)


def test_session_cutoff_allows_early_entry():
    times = five_min(T0, 12)
    candles = series_from(times)
    runner = EngineRunner(
        strategy=scripted({2: "BUY"}),
        exit_policy=FixedSlTpPolicy(stop_pct=0.05, target_pct=0.10),
        config=EngineConfig(
            figi="TEST",
            session_policy=SessionPolicyConfig(entry_cutoff_bars=6),
        ),
    )
    ledger = runner.run(candles)
    assert len(ledger.trades) == 1
    assert ledger.trades[0].exit_reason == "end_of_data"


def test_force_close_on_new_session_when_no_overnight():
    day1 = five_min(utc(2026, 6, 15, 15, 0), 5)
    day2 = five_min(utc(2026, 6, 16, 10, 5), 4)
    candles = series_from(day1 + day2)
    runner = EngineRunner(
        strategy=scripted({1: "BUY"}),
        exit_policy=FixedSlTpPolicy(stop_pct=0.05, target_pct=0.10),
        config=EngineConfig(
            figi="TEST",
            session_policy=SessionPolicyConfig(overnight=False),
        ),
    )
    ledger = runner.run(candles)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.exit_reason == "session_close"
    assert t.exit_index == 5


def test_daily_timeframe_ignores_sessions():
    times = [
        utc(2026, 6, 15, 12, 0),
        utc(2026, 6, 16, 12, 0),
        utc(2026, 6, 17, 12, 0),
        utc(2026, 6, 18, 12, 0),
    ]
    candles = [
        Candle(ts=t, open=100, high=100.3, low=99.7, close=100) for t in times
    ]
    runner = EngineRunner(
        strategy=scripted({0: "BUY"}),
        exit_policy=FixedSlTpPolicy(stop_pct=0.05, target_pct=0.10),
        config=EngineConfig(
            figi="TEST",
            signal_policy=SignalPolicyConfig(min_hold_bars=99),
            session_policy=SessionPolicyConfig(overnight=False),
        ),
    )
    ledger = runner.run(candles)
    assert len(ledger.trades) == 1
    assert ledger.trades[0].exit_reason == "end_of_data"


def test_weekend_entry_blocked():
    saturday = utc(2026, 6, 20, 8, 0)
    times = five_min(saturday, 4)
    candles = series_from(times)
    runner = EngineRunner(
        strategy=scripted({1: "BUY"}),
        exit_policy=FixedSlTpPolicy(),
        config=EngineConfig(
            figi="TEST",
            session_policy=SessionPolicyConfig(),
        ),
    )
    ledger = runner.run(candles)
    details = [a.detail for a in ledger.audit]
    assert any("weekend" in d for d in details)
    assert len(ledger.trades) == 0


def donchian_rows(prices: list[float]) -> list[Candle]:
    rows = []
    for i, p in enumerate(prices):
        ts = T0 + timedelta(minutes=5 * i)
        rows.append(Candle(ts=ts, open=p - 0.1, high=p + 0.2, low=p - 0.3, close=p))
    return rows


def test_donchian_breakout_long():
    prices = [100.0] * 25 + [101.0, 102.0, 103.0]
    runner = EngineRunner(
        strategy=DonchianBreakoutStrategy(DonchianParams(period=10)),
        exit_policy=FixedSlTpPolicy(stop_pct=0.03, target_pct=0.06),
        config=EngineConfig(figi="TEST"),
    )
    ledger = runner.run(donchian_rows(prices))
    entries = [a for a in ledger.audit if "ACCEPT_ENTRY" in a.detail]
    assert len(entries) >= 1
    assert ledger.trades[0].side == "LONG"


def test_donchian_breakdown_short():
    prices = [100.0] * 25 + [99.0, 98.0, 97.0]
    runner = EngineRunner(
        strategy=DonchianBreakoutStrategy(DonchianParams(period=10)),
        exit_policy=FixedSlTpPolicy(stop_pct=0.03, target_pct=0.06),
        config=EngineConfig(figi="TEST"),
    )
    ledger = runner.run(donchian_rows(prices))
    assert ledger.trades[0].side == "SHORT"
