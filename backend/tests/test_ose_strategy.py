"""Тесты адаптеров OsEngine-роботов (app/engine/ose/strategy.py).

Голос робота — смена его позиции на закрытии бара; OseAllStrategy
складывает голоса, при quorum=1 (дефолт) вход даёт один голос. Сценарии
баров повторяют tests/test_ose_robots.py — там проверена сигнальная
логика самих портов, здесь проверяется слой голосования и регистрация.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.engine.catalog import STRATEGY_CATALOG
from app.engine.models import Candle, Side
from app.engine.ose.strategy import (
    OseAllParams,
    OseAllStrategy,
    OseEnvelopTrendStrategy,
    OsePriceChannelStrategy,
    OseSmaStochParams,
    OseSmaStochStrategy,
    _decide,
)
from app.engine.strategies import (
    ParamValidationError,
    _PARAMS_BY_STRATEGY,
    STRATEGY_REGISTRY,
    build_strategy,
)

_T0 = datetime(2026, 1, 5, 10, 0)


def make_candles(closes, *, high=None, low=None, start=None):
    start = start or _T0
    candles = []
    for i, close in enumerate(closes):
        h = close if high is None else high(i)
        low_value = close if low is None else low(i)
        candles.append(
            Candle(
                ts=start + timedelta(minutes=i),
                open=close,
                high=h,
                low=low_value,
                close=close,
                volume=100.0,
            )
        )
    return candles


def drive(strategy, candles):
    """Прогон адаптера по растущим префиксам (как движок): пары (i, сигнал)."""
    out = []
    for i in range(2, len(candles) + 1):
        sig = strategy.on_bar(candles[:i])
        if sig is not None:
            out.append((i, sig))
    return out


def breakout_candles():
    return make_candles(
        [100.0] * 45 + [110.0],
        high=lambda i: 100.0 if i < 45 else 112.0,
        low=lambda i: 100.0 if i < 45 else 109.0,
    )


# --- одиночные адаптеры ------------------------------------------------------


def test_ose_price_channel_votes_buy_on_breakout():
    strat = OsePriceChannelStrategy()
    sigs = drive(strat, breakout_candles())
    assert len(sigs) == 1
    _, sig = sigs[0]
    assert sig.strategy_id == "ose_price_channel"
    assert sig.side is Side.BUY
    assert sig.reason == "ose_vote BUY (price_channel: open_long)"


def test_ose_envelop_trend_votes_open_then_close():
    # Бар 20 пробивает верхнюю полосу — голос open_long; бар 21 задевает
    # трейлинг-стоп — голос close_long (SELL)
    candles = make_candles(
        [100.0] * 20 + [101.0, 100.0],
        high=lambda i: 100.0 if i < 20 else (101.0 if i == 20 else 100.5),
        low=lambda i: 100.0 if i < 20 else (100.5 if i == 20 else 100.0),
    )
    strat = OseEnvelopTrendStrategy()
    sigs = drive(strat, candles)
    assert [(i, s.side, s.reason) for i, s in sigs] == [
        (21, Side.BUY, "ose_vote BUY (envelop_trend: open_long)"),
        (22, Side.SELL, "ose_vote SELL (envelop_trend: close_long)"),
    ]


def test_ose_bollinger_contrarian_votes_and_reversal():
    # Всплеск 130 → close>BB: open_short (SELL); обвал 60 → close<BB:
    # close+open_long (BUY); откат 100 → long закрыт по SMA (SELL)
    candles = make_candles([100.0] * 30 + [130.0, 60.0, 100.0])
    strat = build_strategy("ose_bollinger", None)
    sigs = drive(strat, candles)
    assert [(i, s.side, s.reason) for i, s in sigs] == [
        (31, Side.SELL, "ose_vote SELL (bollinger: open_short)"),
        (32, Side.BUY, "ose_vote BUY (bollinger: open_long)"),
        (33, Side.SELL, "ose_vote SELL (bollinger: close_long)"),
    ]


def test_ose_sma_stoch_adaptive_step_enables_entry():
    # Дефолт step_pct=1%: всплеск 100→300 даёт пробой SMA+step и крест K
    strat = OseSmaStochStrategy()
    sigs = drive(strat, make_candles([100.0] * 25 + [300.0]))
    assert [s.side for _, s in sigs] == [Side.BUY]
    assert sigs[0][1].reason == "ose_vote BUY (sma_stoch: open_long)"


def test_ose_sma_stoch_huge_step_blocks_entry():
    candles = make_candles([100.0] * 25 + [300.0])
    strat = OseSmaStochStrategy(OseSmaStochParams(sma_stoch_step_pct=1000.0))
    assert drive(strat, candles) == []


# --- композит ose_all --------------------------------------------------------


def test_ose_all_one_vote_quorum_counts_members():
    # Бар 46 (110, high 112): BUY у price_channel, sma_stoch, envelop_trend;
    # SELL у bollinger; RSI не голосует (нет SMA 50). Кворум 1 → BUY.
    strat = OseAllStrategy()
    sigs = drive(strat, breakout_candles())
    assert len(sigs) == 1
    i, sig = sigs[0]
    assert i == 46
    assert sig.strategy_id == "ose_all"
    assert sig.side is Side.BUY
    assert sig.features["buy_votes"] == 3
    assert sig.features["sell_votes"] == 1
    assert strat._last_votes["buy_members"] == [
        "price_channel", "sma_stoch", "envelop_trend",
    ]
    assert strat._last_votes["sell_members"] == ["bollinger"]


def test_ose_all_no_repeat_while_positions_held():
    candles = breakout_candles() + make_candles([110.0] * 5,
                                                start=_T0 + timedelta(minutes=46))
    strat = OseAllStrategy()
    sigs = drive(strat, candles)
    assert [i for i, _ in sigs] == [46]


def test_ose_all_quorum_four_blocks_three_votes():
    strat = OseAllStrategy(OseAllParams(quorum=4))
    sigs = drive(strat, breakout_candles())
    assert sigs == []
    assert strat._last_skip is not None and "vote_skip" in strat._last_skip


def test_decide_quorum_rules():
    assert _decide(1, 0, 1) is Side.BUY
    assert _decide(0, 1, 1) is Side.SELL
    assert _decide(1, 1, 1) is None
    assert _decide(1, 0, 2) is None
    assert _decide(2, 1, 2) is Side.BUY
    assert _decide(3, 1, 3) is Side.BUY
    assert _decide(3, 3, 1) is None


# --- регистрация и каталог ---------------------------------------------------

OSE_IDS = (
    "ose_all",
    "ose_price_channel",
    "ose_sma_stoch",
    "ose_envelop_trend",
    "ose_rsi_contrtrend",
    "ose_bollinger",
)


def test_ose_strategies_registered_and_catalogued():
    for sid in OSE_IDS:
        assert sid in STRATEGY_REGISTRY
        assert sid in _PARAMS_BY_STRATEGY
        card = STRATEGY_CATALOG.get(sid)
        assert card is not None
        assert card.family == "ose" and card.wave == 5
        strat = build_strategy(sid, None)
        assert strat.strategy_id == sid
        assert strat.warmup_bars() > 0


def test_ose_all_params_defaults_and_validation():
    strat = build_strategy("ose_all", None)
    assert strat.p.quorum == 1
    assert strat.p.sma_stoch_step_pct == 1.0
    strat = build_strategy("ose_all", {"quorum": 3})
    assert strat.p.quorum == 3
    with pytest.raises(ParamValidationError):
        build_strategy("ose_all", {"quorum": 6})
    with pytest.raises(ParamValidationError):
        build_strategy("ose_all", {"unknown_param": 1})


def test_ose_sma_stoch_params_built_from_catalog():
    strat = build_strategy("ose_sma_stoch", {"sma_stoch_step_pct": 2.5})
    assert strat.p.sma_stoch_step_pct == 2.5
    assert strat.p is not None


# --- стык с движком ----------------------------------------------------------


def test_ose_all_through_engine_runner():
    # Движок опрашивает стратегию префиксами CandleWindow с бара warmup;
    # пробой на баре 52 даёт сигнал ose_all, вход — по open бара 53.
    from app.engine.exits import FixedSlTpPolicy
    from app.engine.runner import EngineConfig, EngineRunner

    candles = make_candles(
        [100.0] * 52 + [110.0] + [110.0] * 3,
        high=lambda i: 100.0 if i < 52 else (112.0 if i == 52 else 110.0),
        low=lambda i: 100.0 if i < 52 else (109.0 if i == 52 else 110.0),
    )
    runner = EngineRunner(
        strategy=OseAllStrategy(),
        exit_policy=FixedSlTpPolicy(stop_pct=0.05, target_pct=0.10),
        config=EngineConfig(figi="TEST"),
    )
    ledger = runner.run(candles)
    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.side == "LONG"
    assert trade.entry_index == 53
    assert trade.exit_reason == "end_of_data"
