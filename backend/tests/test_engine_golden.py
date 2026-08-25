from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle

from app.engine import (
    CostModel,
    EngineConfig,
    EngineRunner,
    FixedSlTpPolicy,
    MacdCrossStrategy,
    ScriptedStrategy,
    Signal,
    SignalPolicyConfig,
    Side,
)

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def bar(k: int, o: float, h: float, low: float, c: float) -> "tuple":
    return (o, h, low, c)


def series(rows: list[tuple]) -> list:
    return [
        Candle(
            ts=T0 + timedelta(minutes=5 * i),
            open=o,
            high=h,
            low=low,
            close=c,
        )
        for i, (o, h, low, c) in enumerate(rows)
    ]


def flat(n: int, price: float = 100.0) -> list[tuple]:
    return [(price, price + 0.2, price - 0.2, price) for _ in range(n)]


def scripted(index_side: dict[int, str]) -> ScriptedStrategy:
    return ScriptedStrategy(
        {
            i: Signal(
                strategy_id="scripted",
                side=Side(side),
                time=T0,
                reason="script",
            )
            for i, side in index_side.items()
        }
    )


def run(rows, strategy, exit_policy=None, cfg=None):
    candles = series(rows)
    runner = EngineRunner(
        strategy=strategy,
        exit_policy=exit_policy or FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02),
        config=cfg or EngineConfig(figi="TEST"),
    )
    return runner.run(candles)


def test_long_target_hit():
    rows = flat(3) + [(100, 100.3, 99.8, 100)] * 1
    rows += [(100.1, 101.6, 100.0, 101.4), (101.4, 103.0, 101.2, 102.8)]
    ledger = run(rows, scripted({2: "BUY"}))
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.side == "LONG"
    assert t.exit_reason == "target"
    assert t.net_pnl > 0


def test_long_stop_loss():
    rows = flat(3)
    rows += [(100.0, 100.2, 97.5, 98.0), (98.0, 98.4, 97.8, 98.2)]
    ledger = run(rows, scripted({2: "BUY"}))
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.net_pnl < 0


def test_gap_through_stop_fills_at_open():
    rows = flat(5)
    rows += [(90.0, 91.0, 89.5, 90.5), (90.5, 91.0, 90.0, 90.8)]
    ledger = run(rows, scripted({2: "BUY"}))
    t = ledger.trades[0]
    assert t.entry_index == 3
    assert t.exit_reason == "stop_loss"
    assert abs(t.exit_price - 90.0) < 0.05


def test_stop_first_on_same_bar_conflict():
    rows = flat(3)
    rows += [(99.9, 103.5, 96.0, 100.0), (100.0, 100.4, 99.6, 100.2)]
    ledger = run(rows, scripted({2: "BUY"}))
    t = ledger.trades[0]
    assert t.exit_reason == "stop_loss"


def test_same_side_signal_ignored():
    rows = flat(12)
    ledger = run(rows, scripted({2: "BUY", 6: "BUY"}))
    assert len(ledger.trades) == 1
    details = [a.detail for a in ledger.audit]
    assert any("IGNORE_SAME_SIDE" in d for d in details)
    assert ledger.trades[0].exit_reason == "end_of_data"


def test_opposite_signal_closes_position():
    rows = flat(10)
    ledger = run(rows, scripted({2: "BUY", 6: "SELL"}))
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.exit_reason == "signal_exit"
    fill_index = t.exit_index
    assert fill_index == 7
    base_open = rows[7][0]
    assert abs(t.exit_price - (base_open - base_open * 0.0002)) < 0.01


def test_min_hold_rejects_early_opposite():
    rows = flat(14)
    cfg = EngineConfig(
        figi="TEST",
        signal_policy=SignalPolicyConfig(min_hold_bars=10),
    )
    ledger = run(rows, scripted({2: "BUY", 4: "SELL"}), cfg=cfg)
    details = [a.detail for a in ledger.audit]
    assert any("REJECT_MIN_HOLD" in d for d in details)
    assert ledger.trades[0].exit_reason == "end_of_data"


def test_costs_are_charged():
    rows = flat(3) + [(100.1, 101.6, 100.0, 101.4), (101.4, 103.0, 101.2, 102.8)]
    ledger = run(rows, scripted({2: "BUY"}))
    t = ledger.trades[0]
    assert t.commission > 0
    assert t.slippage > 0
    assert abs(t.net_pnl - (t.gross_pnl - t.commission)) < 1e-9


def test_deterministic_replay():
    rows = flat(3) + [(100.1, 101.6, 100.0, 101.4), (101.4, 103.0, 101.2, 102.8)] + flat(4, 101)
    s = scripted({2: "BUY", 7: "SELL"})
    h1 = run(rows, s).fingerprint()
    h2 = run(rows, s).fingerprint()
    assert h1 == h2


def test_short_entry_and_target():
    rows = flat(3) + [(100.0, 100.2, 98.2, 98.4), (98.4, 98.8, 97.4, 97.6)]
    ledger = run(rows, scripted({2: "SELL"}))
    t = ledger.trades[0]
    assert t.side == "SHORT"
    assert t.exit_reason == "target"
    assert t.net_pnl > 0


def test_short_rejected_when_not_allowed():
    rows = flat(8)
    cfg = EngineConfig(figi="TEST", allow_short=False)
    ledger = run(rows, scripted({2: "SELL"}), cfg=cfg)
    assert len(ledger.trades) == 0
    details = [a.detail for a in ledger.audit]
    assert any("REJECT_SHORT" in d for d in details)


def test_macd_cross_produces_signals_on_trend():
    rows = []
    price = 100.0
    for i in range(80):
        step = 0.35 if i > 30 else (-0.15 if i % 2 else 0.05)
        price += step
        rows.append((price - 0.1, price + 0.3, price - 0.4, price))
    ledger = run(rows, MacdCrossStrategy(), exit_policy=FixedSlTpPolicy(0.02, 0.04))
    entries = [a for a in ledger.audit if "ACCEPT_ENTRY" in a.detail]
    assert len(entries) >= 1


