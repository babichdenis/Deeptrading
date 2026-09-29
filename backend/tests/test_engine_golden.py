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
from app.engine.exits import AtrStopPolicy

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
    # ENG-008: стратегия получает warmup-историю с самого начала, поэтому
    # кроссы должны быть ПОСЛЕ формального warmup — пила, затем контртренд
    # и разворот вверх дают кроссы уже на «торгуемых» барах.
    rows = []
    price = 100.0
    for i in range(120):
        if i < 40:
            step = 0.30 if i % 2 else -0.25
        elif i < 55:
            step = -0.40
        else:
            step = 0.35
        price += step
        rows.append((price - 0.1, price + 0.3, price - 0.4, price))
    ledger = run(rows, MacdCrossStrategy(), exit_policy=FixedSlTpPolicy(0.02, 0.04))
    entries = [a for a in ledger.audit if "ACCEPT_ENTRY" in a.detail]
    assert len(entries) >= 1


def test_entry_confirm_delays_and_fills():
    # сигнал BUY на баре 1; entry_confirm_bars=2 должны ждать 2 подряд восходящих
    # свечи (close>open), вход по open после подтверждения
    rows = flat(2)
    rows += [(100.0, 100.5, 99.8, 100.4)]  # бар 2: свеча 1-я подтверждающая
    rows += [(100.4, 100.9, 100.2, 100.7)]  # бар 3: свеча 2-я подтверждающая
    rows += [(100.7, 101.2, 100.6, 101.0)]  # бар 4: исполнение по open
    rows += flat(4, 101.0)
    cfg = EngineConfig(
        figi="TEST",
        signal_policy=SignalPolicyConfig(entry_confirm_bars=2),
    )
    ledger = run(rows, scripted({1: "BUY"}), cfg=cfg)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.entry_index == 4
    base_open = rows[4][0]
    assert abs(t.entry_price - (base_open + base_open * 0.0002)) < 0.001
    details = [a.detail for a in ledger.audit]
    assert any("ENTRY_WAIT_CONFIRM" in d for d in details)
    assert any("ENTRY_CONFIRM" in d for d in details)


def test_entry_confirm_reset_on_bad_candle():
    # сигнал BUY на баре 1; свеча 2 восходящая, свеча 3 нисходящая -> сброс,
    # нужна пара восходящих заново
    rows = flat(2)
    rows += [(100.0, 100.5, 99.8, 100.4)]  # бар 2: подтверждающая
    rows += [(100.4, 100.45, 99.5, 99.6)]  # бар 3: против -> reset
    rows += [(99.6, 100.0, 99.4, 99.9)]    # бар 4: подтверждающая
    rows += [(99.9, 100.4, 99.7, 100.2)]   # бар 5: подтверждающая -> confirm done
    rows += [(100.2, 100.7, 100.0, 100.5)]  # бар 6: исполнение по open
    rows += flat(4, 100.5)
    cfg = EngineConfig(
        figi="TEST",
        signal_policy=SignalPolicyConfig(entry_confirm_bars=2),
    )
    ledger = run(rows, scripted({1: "BUY"}), cfg=cfg)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.entry_index == 6
    base_open = rows[6][0]
    assert abs(t.entry_price - (base_open + base_open * 0.0002)) < 0.001
    details = [a.detail for a in ledger.audit]
    assert any("ENTRY_CONFIRM_RESET" in d for d in details)


def test_entry_confirm_no_noop_when_zero():
    # entry_confirm_bars=0 (по умолчанию): вход на open следующего бара
    rows = flat(3)
    rows += [(100.0, 100.5, 99.9, 100.3), (100.3, 101.0, 100.1, 100.8)]
    ledger = run(rows, scripted({1: "BUY"}))
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.entry_index == 2
    base_open = rows[2][0]
    assert abs(t.entry_price - (base_open + base_open * 0.0002)) < 0.001
    details = [a.detail for a in ledger.audit]
    assert not any("ENTRY_WAIT_CONFIRM" in d for d in details)


# --- Trailing stop (активация при pnl>=комиссия*4, стоп только за ценой) ---

TRAILING_POLICY = AtrStopPolicy(
    period=14,
    multiplier=1.0,
    risk_reward=1.0,
    trail_activation_comm_mult=4.0,
    trail_distance_r=1.0,
)


def test_trailing_activates_and_exits_by_stop():
    # qty=1, entry=100, комиссия=0.05 -> активация pnl>=0.20 (close>=100.20).
    # После активации TP (100.40, было бы закрыто) отключается: цена до 101.4,
    # но выходит только по подтянутому стопу (нисходящий разворот).
    rows = flat(3)
    rows += [(100.0, 100.6, 99.9, 100.5)]  # бар3 вход open 100 -> close 100.50 => активация
    rows += [(100.5, 101.0, 100.5, 100.8)]  # бар4 ратчет вверх
    rows += [(100.8, 101.4, 100.8, 101.2)]  # бар5 ратчет вверх (highest 101.4)
    rows += [(101.2, 101.2, 100.3, 100.5)]  # бар6 low 100.30 < стоп (100.40) -> выход по стопу
    ledger = run(rows, scripted({2: "BUY"}), exit_policy=TRAILING_POLICY)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.net_pnl > 0
    details = [a.detail for a in ledger.audit]
    assert any("TRAILING_ACTIVATED" in d for d in details)


def test_trailing_blocks_opposite_signal():
    rows = flat(3)
    rows += [(100.0, 100.6, 99.9, 100.5)]  # бар3 вход, close 100.50 -> активация
    rows += [(100.5, 101.0, 100.5, 100.8)]  # бар4 ратчет вверх (highest 101.0)
    rows += [(100.8, 101.4, 100.8, 101.2)]  # бар5 ратчет вверх (highest 101.4)
    rows += [(101.2, 101.2, 100.3, 100.5)]  # бар6 low 100.30 < стоп (≈100.40) -> выход по стопу
    ledger = run(rows, scripted({2: "BUY", 5: "SELL"}), exit_policy=TRAILING_POLICY)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.exit_reason == "stop_loss"  # сигнал SELL (бар5) проигнорирован
    details = [a.detail for a in ledger.audit]
    assert any("HOLD_TRAILING" in d for d in details)


def test_trailing_short_mirrors():
    # short: pnl=(entry-close)*qty >=0.20 -> close<=99.80; стоп подтягивается вниз,
    # разворот вверх (high >= стоп) -> выход по стопу.
    rows = flat(3)
    rows += [(100.0, 100.1, 99.7, 99.8)]  # бар3 вход open 100, close 99.80 -> активация
    rows += [(99.8, 99.9, 99.0, 99.2)]  # бар4 ратчет вниз (low 99.0)
    rows += [(99.2, 99.3, 98.6, 98.8)]  # бар5 ратчет вниз (low 98.6)
    rows += [(99.5, 100.0, 99.4, 99.9)]  # бар6 high 100.00 > стоп -> выход по стопу
    ledger = run(rows, scripted({2: "SELL"}), exit_policy=TRAILING_POLICY)
    assert len(ledger.trades) == 1
    t = ledger.trades[0]
    assert t.side == "SHORT"
    assert t.exit_reason == "stop_loss"
    assert t.net_pnl > 0
    details = [a.detail for a in ledger.audit]
    assert any("TRAILING_ACTIVATED" in d for d in details)


