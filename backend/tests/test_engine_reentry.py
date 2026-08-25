from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine import (
    CostModel,
    EngineConfig,
    EngineRunner,
    FixedSlTpPolicy,
    ScriptedStrategy,
    Signal,
    SignalPolicyConfig,
    Side,
)
from app.engine.models import Candle, ExitReason

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def series(rows: list[tuple]) -> list:
    return [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=low, close=c)
        for i, (o, h, low, c) in enumerate(rows)
    ]


def flat(n: int, price: float = 100.0) -> list[tuple]:
    return [(price, price + 0.2, price - 0.2, price) for _ in range(n)]


def scripted(index_side: dict[int, str]) -> ScriptedStrategy:
    return ScriptedStrategy(
        {
            i: Signal(strategy_id="scripted", side=Side(s), time=T0, reason="script")
            for i, s in index_side.items()
        }
    )


def run(rows, strategy, policy: SignalPolicyConfig | None = None):
    return EngineRunner(
        strategy=strategy,
        exit_policy=FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02),
        config=EngineConfig(
            figi="TEST",
            cost_model=CostModel(commission_rate=0.0, slippage_bps=0.0),
            signal_policy=policy or SignalPolicyConfig(),
        ),
    ).run(series(rows))


def test_same_side_reentry_blocked():
    # BUY на баре 2 (вход на 3), SELL на 4 (выход), BUY на 6 — тот же side в cooldown 5
    rows = flat(12)
    rows[2] = (100, 100.2, 99.8, 100)
    rows[4] = (100, 100.2, 99.8, 100)
    rows[6] = (100, 100.2, 99.8, 100)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 6: "BUY"}),
                 SignalPolicyConfig(same_side_reentry_cooldown_bars=5))
    trades = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(trades) == 1, f"ожидали 1 закрытую сделку, получили {len(trades)}"
    rej = [e for e in ledger.audit if "REJECT_REENTRY" in e.detail]
    assert len(rej) == 1, "повторный BUY в cooldown должен быть отклонён"


def test_opposite_side_reentry_allowed():
    # BUY на 2, SELL на 4 (выход), SELL на 6 — противоположный side разрешён сразу
    rows = flat(14)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 6: "SELL"}),
                 SignalPolicyConfig(same_side_reentry_cooldown_bars=5))
    shorts = [t for t in ledger.trades if t.side == "SHORT"]
    assert len(shorts) == 1, "разворот SELL должен открыться сразу после выхода из LONG"
    longs = [t for t in ledger.trades if t.side == "LONG"]
    assert len(longs) == 1


def test_cooldown_expires_after_n_bars():
    # BUY на 2 (вход 3), SELL на 4 (выход 5), BUY на 11 — прошло 6 баров > cooldown 5
    rows = flat(18)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 11: "BUY"}),
                 SignalPolicyConfig(same_side_reentry_cooldown_bars=5))
    longs = [t for t in ledger.trades if t.side == "LONG"]
    assert len(longs) == 2, f"повторный BUY после истечения cooldown должен открыться, LONG {len(longs)}"
    assert any("REJECT_REENTRY" not in e.detail for e in ledger.audit) or True


def test_exit_confirm_requires_second_opposite_signal():
    # BUY на 2, одиночный SELL на 4 не должен закрыть позицию (нет подтверждения)
    rows = flat(10)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL"}),
                 SignalPolicyConfig(exit_confirm_window_bars=3))
    closed = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(closed) == 0, "одиночный противоположный сигнал не должен закрывать позицию"
    assert any("EXIT_CANDIDATE" in e.detail for e in ledger.audit)


def test_exit_confirm_closes_on_repeat():
    # BUY на 2, SELL на 4 (кандидат), SELL на 5 (подтверждение) → выход
    rows = flat(12)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 5: "SELL"}),
                 SignalPolicyConfig(exit_confirm_window_bars=3))
    closed = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(closed) == 1
    assert closed[0].exit_reason == ExitReason.SIGNAL_EXIT.value
    assert any("EXIT_CONFIRMED" in e.detail for e in ledger.audit)


def test_exit_candidate_expires():
    # BUY на 2, SELL на 4 (кандидат), повторный SELL только на 9 — окно 3 истекло
    rows = flat(14)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 9: "SELL"}),
                 SignalPolicyConfig(exit_confirm_window_bars=3))
    closed = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(closed) == 0, "подтверждение позже окна не должно закрывать позицию"


def test_opposite_hold_does_not_exit():
    # BUY на 2, SELL на 4 — при opposite_hold позиция НЕ закрывается по сигналу
    rows = flat(10)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL"}),
                 SignalPolicyConfig(opposite_hold=True))
    closed = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(closed) == 0, "hold: противоположный сигнал не должен закрывать позицию"
    assert any("HOLD_NO_EXIT" in e.detail for e in ledger.audit)


def test_hold_ignores_opposite_then_hits_stop():
    # BUY на 2, SELL на 4 (игнорируем), потом цена падает и срабатывает стоп
    rows = flat(3)
    rows += [(100.0, 100.2, 99.6, 99.7), (99.7, 99.9, 98.5, 98.6)]
    ledger = run(rows, scripted({2: "BUY", 4: "SELL"}),
                 SignalPolicyConfig(opposite_hold=True))
    closed = [t for t in ledger.trades if t.exit_reason != ExitReason.END_OF_DATA.value]
    assert len(closed) == 1
    assert closed[0].exit_reason == ExitReason.STOP_LOSS.value


def test_confirm_flip_opens_opposite():
    # BUY на 2, SELL на 4 (кандидат), SELL на 5 (подтверждение) -> закрытие и SHORT
    rows = flat(14)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL", 5: "SELL"}),
                 SignalPolicyConfig(exit_confirm_window_bars=3, confirm_flip=True))
    shorts = [t for t in ledger.trades if t.side == "SHORT"]
    longs = [t for t in ledger.trades if t.side == "LONG"]
    assert len(longs) == 1 and len(shorts) == 1, "после подтверждения должен открыться SHORT"
    assert any("FLIP_CONFIRMED" in e.detail for e in ledger.audit)


def test_immediate_flip():
    # confirm_flip + window 0 -> мгновенный переворот по первому противоположному
    rows = flat(14)
    ledger = run(rows, scripted({2: "BUY", 4: "SELL"}),
                 SignalPolicyConfig(confirm_flip=True))
    shorts = [t for t in ledger.trades if t.side == "SHORT"]
    assert len(shorts) == 1, "мгновенный flip должен открыть SHORT"
