'''Интеграционные тесты шага 4 (план выходов v2, 2026-09-26):
breakeven_stop / early_abort_exit / wick_tol через EngineRunner.

Контракты:
  - флаги opt-in в EngineConfig; с дефолтами поведение прежнее (golden — эталон);
  - BE переносит initial_stop на вход (ratchet), выход по touch-семантике на уровне стопа;
  - abort срабатывает только если на баре не задеты стоп/TP, выход по close;
  - wick_tol прощает прокол хвоста до tol, если close вернулся за стоп.
'''
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine import (
    EngineConfig,
    EngineRunner,
    FixedSlTpPolicy,
    ScriptedStrategy,
    Signal,
    Side,
)
from app.engine.models import Candle

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def series(rows):
    return [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c)
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def flat(n, price=100.0):
    return [(price, price + 0.2, price - 0.2, price) for _ in range(n)]


def scripted(index_side):
    return ScriptedStrategy(
        {
            i: Signal(strategy_id='scripted', side=Side(side), time=T0, reason='script')
            for i, side in index_side.items()
        }
    )


def cfg(**kw):
    return EngineConfig(figi='TEST', **kw)


def run(rows, strategy, config):
    runner = EngineRunner(
        strategy=strategy,
        exit_policy=FixedSlTpPolicy(stop_pct=0.01, target_pct=0.05),
        config=config,
    )
    return runner.run(series(rows))


class TestBreakeven:
    '''be_trigger_r=1.0: при прибыли >= 1R стоп переносится на вход.'''

    def test_long_be_moves_stop_and_exits_at_entry(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),   # вход LONG по open=100; стоп 99; риск 1
            (100.05, 101.5, 100.0, 101.2),  # high 101.5 >= 101 -> BE (стоп 100 со след. бара)
            (101.2, 101.3, 99.9, 100.0),    # low задевает стоп на входе -> выход в ноль
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(be_trigger_r=1.0))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'stop_loss'
        # безубыток: пнл около нуля (за вычетом комиссий), а не полный -1R
        assert -0.2 < t.net_pnl < 0.2

    def test_long_be_not_triggered_below_threshold(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),
            (100.05, 100.9, 100.0, 100.8),  # high 100.9 < 101 -> BE молчит
            (100.8, 100.9, 100.3, 100.5),
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(be_trigger_r=1.0))
        assert not any(t.exit_reason == 'stop_loss' for t in led.trades)

    def test_short_be_exits_at_entry_level(self):
        # ENG-002 (audit 2026-09-29): BE активируется по close бара и действует
        # со СЛЕДУЮЩЕГО бара — high того же бара его не выбивает.
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 99.95),    # вход SHORT по open=100; стоп 101; риск 1
            (99.95, 100.05, 98.4, 98.6),    # close 98.6: прибыль 1.4R -> BE (стоп 100 со след. бара)
            (98.6, 100.1, 98.0, 98.2),      # high задевает стоп 100 -> выход в ноль
        ]
        led = run(rows, scripted({2: 'SELL'}), cfg(be_trigger_r=1.0))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'stop_loss'
        assert -0.2 < t.net_pnl < 0.2


class TestEarlyAbort:
    '''abort_r=0.5, abort_max_bars=2: провал на 0.5R в первые 2 бара -> выход по close.'''

    ROWS = flat(3) + [
        (100.0, 100.2, 99.8, 100.05),   # вход; стоп 99
        (100.05, 100.3, 99.2, 99.3),    # стоп не задет (99.2 > 99), close 99.3 <= 99.5 -> abort
        (99.3, 99.5, 98.5, 99.0),       # без abort этот бар задел бы стоп 99
    ]

    def test_long_abort_within_window(self):
        led = run(self.ROWS, scripted({2: 'BUY'}), cfg(abort_r=0.5, abort_max_bars=2))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'early_abort'
        assert t.net_pnl < 0

    def test_long_without_flag_stopped_later(self):
        led = run(self.ROWS, scripted({2: 'BUY'}), cfg())
        assert len(led.trades) == 1
        assert led.trades[0].exit_reason == 'stop_loss'

    def test_short_abort_within_window(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 99.95),    # вход SHORT; стоп 101
            (99.95, 100.8, 98.9, 100.6),    # high 100.8 < 101 (стоп цел), close 100.6 >= 100.5 -> abort
            (100.6, 101.2, 100.0, 101.0),   # без abort здесь был бы выход по стопу
        ]
        led = run(rows, scripted({2: 'SELL'}), cfg(abort_r=0.5, abort_max_bars=2))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'early_abort'
        assert t.net_pnl < 0


class TestWickTolerance:
    '''wick_tol=0.5: прокол стопа хвостом до 0.5 при close за стопом прощается.'''

    ROWS = flat(3) + [
        (100.0, 100.2, 99.8, 100.05),   # вход; стоп 99
        (100.05, 100.3, 98.8, 100.2),   # прокол 0.2 <= 0.5, close выше стопа -> прощено
        (100.2, 105.5, 100.1, 105.0),   # TP 105 достигнут
    ]

    def test_wick_forgiven_then_target(self):
        led = run(self.ROWS, scripted({2: 'BUY'}), cfg(wick_tol=0.5))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'target'
        assert t.net_pnl > 0

    def test_same_wick_stops_without_tol(self):
        led = run(self.ROWS, scripted({2: 'BUY'}), cfg())
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'stop_loss'
        assert t.net_pnl < 0


class TestPartialTake:
    """Интеграционные тесты шага 5 (план выходов v2, 2026-09-26): частичные выходы.

    Контракты:
      - partial_r > 0: при прибыли >= partial_r * risk закрывается доля partial_fraction;
      - partial_to_be: после частичного тейка стоп переносится на вход;
      - частичный тейк — не более одного раза на позицию; qty=1 закрывается целиком;
      - с дефолтами (partial_r=0) поведение прежнее (покрыто golden).
    """

    def test_long_partial_then_be_exit(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),   # вход LONG qty=2; стоп ~99; риск ~1
            (100.05, 101.5, 100.0, 101.2),  # high >= ~101 -> partial 1 шт; стоп -> вход
            (101.2, 101.3, 99.9, 100.1),    # low задевает вход -> полный выход ~в ноль
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(qty=2, partial_r=1.0, partial_fraction=0.5))
        assert len(led.trades) == 2
        p, f = led.trades
        assert p.exit_reason == 'partial_take' and p.qty == 1
        assert 0.3 < p.net_pnl < 1.1
        assert f.qty == 1 and f.exit_reason == 'stop_loss'
        assert -0.4 < f.net_pnl < 0.3

    def test_partial_once_per_position(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),
            (100.05, 101.5, 100.0, 101.2),  # partial; стоп -> вход
            (101.2, 102.0, 101.0, 101.5),   # снова выше уровня: повторного partial нет
            (101.5, 101.6, 99.9, 100.0),    # выход по BE-стопу
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(qty=2, partial_r=1.0))
        assert len(led.trades) == 2
        assert [t.exit_reason for t in led.trades] == ['partial_take', 'stop_loss']
        assert sum(t.qty for t in led.trades) == 2

    def test_qty1_partial_closes_full_position(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),
            (100.05, 101.5, 100.0, 101.2),  # qty=1: partial закрывает всю позицию
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(partial_r=1.0, partial_fraction=0.5))
        assert len(led.trades) == 1
        t = led.trades[0]
        assert t.exit_reason == 'partial_take' and t.qty == 1
        assert 0.3 < t.net_pnl < 1.1

    def test_no_partial_below_level(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 100.05),
            (100.05, 100.9, 100.0, 100.8),  # high < уровня -> partial молчит
            (100.8, 100.9, 100.3, 100.5),
        ]
        led = run(rows, scripted({2: 'BUY'}), cfg(qty=2, partial_r=1.0))
        assert [t.exit_reason for t in led.trades] == ['end_of_data']

    def test_short_partial(self):
        rows = flat(3) + [
            (100.0, 100.2, 99.8, 99.95),    # вход SHORT; стоп ~101; риск ~1
            (99.95, 100.1, 98.8, 99.1),     # low <= ~99 -> partial; стоп -> вход
            (99.1, 100.1, 98.9, 99.5),      # high задевает вход -> полный выход
        ]
        led = run(rows, scripted({2: 'SELL'}), cfg(qty=2, partial_r=1.0, partial_fraction=0.5))
        assert len(led.trades) == 2
        p, f = led.trades
        assert p.exit_reason == 'partial_take' and p.qty == 1
        assert 0.3 < p.net_pnl < 1.1
        assert f.exit_reason == 'stop_loss'
        assert -0.4 < f.net_pnl < 0.3
