"""Edge cases for compute_ensemble and engine.

Goal: verify no crashes on boundary conditions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle, Side
from app.engine.runner import EngineRunner, EngineConfig
from app.engine.strategies import build_strategy
from app.engine.exits import AtrStopPolicy
from app.engine.policies import SignalPolicy, SignalPolicyConfig
from app.engine.sessions import SessionPolicy, SessionPolicyConfig
from app.engine.costs import CostModel
from app.engine.metrics import summarize, max_drawdown_pct, equity_curve
from app.engine.quorum import merge_quorum
from app.services.ensemble import compute_ensemble, _validate_candles


T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _make_candles(prices: list[float], start: datetime | None = None) -> list[Candle]:
    base = start or T0
    return [
        Candle(ts=base + timedelta(minutes=i),
               open=p * 0.999, high=p * 1.005,
               low=p * 0.995, close=p, volume=1000)
        for i, p in enumerate(prices)
    ]


def _run_engine(candles, strategy_id="rsi_reversal", mode="long", allow_short=False):
    strategy = build_strategy(strategy_id, {"period": 14, "oversold": 30, "overbought": 80})
    exit_policy = AtrStopPolicy(period=14, multiplier=2.0, risk_reward=4.0)
    cfg = EngineConfig(
        figi="TEST", qty=1, mode=mode, allow_short=allow_short,
        cost_model=CostModel(commission_rate=0.003, slippage_bps=2.0),
    )
    runner = EngineRunner(strategy=strategy, exit_policy=exit_policy, config=cfg)
    return runner.run(candles)


class TestEdgeCandles:
    def test_zero_candles_ensemble(self):
        result = compute_ensemble([], {"figi": "X", "setups": [], "quorum": 2,
                                        "from_ts": T0.isoformat(),
                                        "to_ts": T0.isoformat()})
        assert isinstance(result, dict)
        assert "error" in result or len(result.get("trades", [])) == 0

    def test_single_candle_ensemble(self):
        c = [Candle(ts=T0, open=100, high=101, low=99, close=100.5, volume=1000)]
        result = compute_ensemble(c, {"figi": "X", "setups": [], "quorum": 2,
                                       "from_ts": T0.isoformat(),
                                       "to_ts": T0.isoformat()})
        assert isinstance(result, dict)

    def test_broken_candles_filtered(self):
        good = Candle(ts=T0, open=100, high=101, low=99, close=100.5, volume=1000)
        broken = [
            Candle(ts=T0 + timedelta(minutes=1), open=0, high=101, low=99, close=100, volume=1000),
            Candle(ts=T0 + timedelta(minutes=2), open=100, high=90, low=110, close=100, volume=1000),
            Candle(ts=T0 + timedelta(minutes=3), open=100, high=101, low=99, close=100, volume=-1),
        ]
        result, skipped = _validate_candles([good] + broken)
        assert skipped == 3
        assert len(result) == 1

    def test_all_broken_candles(self):
        broken = [
            Candle(ts=T0 + timedelta(minutes=i), open=0, high=0, low=0, close=0, volume=0)
            for i in range(200)
        ]
        result, skipped = _validate_candles(broken)
        assert skipped == 200
        assert len(result) == 0


class TestEdgeEngine:
    def test_no_signal_0_trades(self):
        flat = [Candle(ts=T0 + timedelta(minutes=i),
                        open=100, high=100.1, low=99.9, close=100, volume=100)
                for i in range(200)]
        ledger = _run_engine(flat)
        assert len(ledger.trades) == 0

    def test_consecutive_losses_no_crash(self):
        prices = [100.0] * 300
        for i in range(0, 300, 30):
            for j in range(15):
                prices[i + j] = 100 + j * 0.3
            for j in range(15):
                prices[i + 15 + j] = 100 - j * 0.8
        candles = _make_candles(prices)
        ledger = _run_engine(candles)
        metrics = summarize(ledger.trades)
        dd = max_drawdown_pct(equity_curve(ledger.trades))
        assert isinstance(dd, float)
        assert metrics["trades"] >= 0


class TestEdgeQuorum:
    def test_empty_member_runs(self):
        sigs, funnel = merge_quorum([], 2)
        assert sigs == []

    def test_single_vote_below_quorum(self):
        runs = [("rsi", [{"ts": T0, "side": "BUY"}])]
        sigs, funnel = merge_quorum(runs, 2)
        assert len(sigs) == 0

    def test_equal_buy_sell_no_signal(self):
        runs = [
            ("rsi", [{"ts": T0, "side": "BUY"}]),
            ("macd", [{"ts": T0, "side": "SELL"}]),
        ]
        sigs, funnel = merge_quorum(runs, 1)
        assert len(sigs) == 0

    def test_all_same_side(self):
        runs = [
            ("rsi", [{"ts": T0, "side": "BUY"}]),
            ("macd", [{"ts": T0, "side": "BUY"}]),
            ("donchian", [{"ts": T0, "side": "BUY"}]),
        ]
        sigs, funnel = merge_quorum(runs, 2)
        assert len(sigs) == 1
        assert sigs[0]["side"] == "BUY"


class TestEdgeMetrics:
    def test_empty_trades(self):
        m = summarize([])
        assert m["trades"] == 0
        assert m["net"] == 0

    def test_single_winning_trade(self):
        from app.engine.models import Trade
        t = Trade(trade_id="T1", figi="X", side="LONG", qty=1,
                  entry_time=T0, entry_index=0, entry_price=100,
                  exit_time=T0 + timedelta(minutes=5), exit_index=5,
                  exit_price=101, initial_stop=98, take_profit=104,
                  gross_pnl=1.0, commission=0.3, slippage=0.02,
                  net_pnl=0.68, exit_reason="target", bars_held=5)
        m = summarize([t])
        assert m["trades"] == 1
        assert m["wins"] == 1
        assert m["net"] > 0

    def test_single_losing_trade(self):
        from app.engine.models import Trade
        t = Trade(trade_id="T1", figi="X", side="LONG", qty=1,
                  entry_time=T0, entry_index=0, entry_price=100,
                  exit_time=T0 + timedelta(minutes=5), exit_index=5,
                  exit_price=97, initial_stop=98, take_profit=104,
                  gross_pnl=-3.0, commission=0.3, slippage=0.02,
                  net_pnl=-3.32, exit_reason="stop_loss", bars_held=5)
        m = summarize([t])
        assert m["trades"] == 1
        assert m["losses"] == 1
        assert m["net"] < 0
