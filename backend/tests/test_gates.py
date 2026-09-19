"""Тесты вынесенных гейтов входа (app/bot/gates.py) — чистые функции, без рантайма."""
from __future__ import annotations

from types import SimpleNamespace

from app.bot.gates import (
    GateResult, MarketContext, TimeContext, TIME_GATES, MARKET_GATES,
    gate_already_held, gate_direction, gate_entries_paused, gate_last_hour,
    gate_liquidity, gate_loss_streak, gate_orderbook, gate_risk_day, gate_session,
    gate_volatility, run_gate_chain,
)


def _cfg(**kw):
    base = dict(sessions=["morning", "day"], long_allowed=True, short_allowed=True,
                entry_last_hour_block=True, entry_ob_imbalance_max=0.3,
                entry_ob_spread_max=25.0, entry_min_turnover=0.0,
                entry_volatility_max_mult=3.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_time_gates_pass():
    ctx = TimeContext(cfg=_cfg(), side="BUY")
    assert run_gate_chain(TIME_GATES, ctx).passed


def test_entries_paused():
    r = gate_entries_paused(TimeContext(cfg=_cfg(), side="BUY", entries_paused=True))
    assert not r.passed and r.key == "entries_paused"


def test_session_filter():
    r = gate_session(TimeContext(cfg=_cfg(), side="BUY", sessions_allowed=False))
    assert not r.passed and r.key == "session_filter"


def test_last_hour_only_when_enabled():
    ctx = TimeContext(cfg=_cfg(entry_last_hour_block=False), side="BUY", is_last_hour=True)
    assert gate_last_hour(ctx).passed
    ctx2 = TimeContext(cfg=_cfg(entry_last_hour_block=True), side="BUY", is_last_hour=True)
    r = gate_last_hour(ctx2)
    assert not r.passed and r.key == "last_hour" and "day" in r.detail


def test_last_hour_names_last_session():
    ctx = TimeContext(cfg=_cfg(sessions=["morning", "day", "evening"]), side="BUY",
                      is_last_hour=True)
    assert "evening" in gate_last_hour(ctx).detail


def test_direction_disabled():
    assert gate_direction(TimeContext(cfg=_cfg(long_allowed=False), side="BUY")).key == "long_disabled"
    assert gate_direction(TimeContext(cfg=_cfg(short_allowed=False), side="SELL")).key == "short_disabled"
    assert gate_direction(TimeContext(cfg=_cfg(long_allowed=False), side="SELL")).passed


def test_risk_and_loss_streak_and_held():
    assert not gate_risk_day(TimeContext(cfg=_cfg(), side="BUY", risk_allowed=False,
                                         risk_state="LOSS_LIMIT", daily_pnl=-1200)).passed
    assert not gate_loss_streak(TimeContext(cfg=_cfg(), side="BUY", loss_hold=True,
                                            loss_why="2 убытка")).passed
    assert not gate_already_held(TimeContext(cfg=_cfg(), side="BUY", already_held=True)).passed


def test_liquidity():
    cfg = _cfg(entry_min_turnover=1_000_000)
    assert not gate_liquidity(MarketContext(cfg=cfg, side="BUY", turnover=500_000)).passed
    assert gate_liquidity(MarketContext(cfg=cfg, side="BUY", turnover=2_000_000)).passed
    # 0 = выкл
    assert gate_liquidity(MarketContext(cfg=_cfg(), side="BUY", turnover=1)).passed


def test_volatility():
    cfg = _cfg(entry_volatility_max_mult=3.0)
    assert not gate_volatility(MarketContext(cfg=cfg, side="BUY", atr_pct=9.0,
                                             atr_pct_median=2.0)).passed
    assert gate_volatility(MarketContext(cfg=cfg, side="BUY", atr_pct=3.0,
                                         atr_pct_median=2.0)).passed
    assert gate_volatility(MarketContext(cfg=_cfg(entry_volatility_max_mult=0.0),
                                         side="BUY", atr_pct=99.0, atr_pct_median=1.0)).passed


def test_orderbook_imbalance_and_spread():
    cfg = _cfg()
    ob_bad = {"imbalance": -0.6, "spread_bps": 5.0}
    r = gate_orderbook(MarketContext(cfg=cfg, side="BUY", orderbook=ob_bad))
    assert not r.passed and r.key == "orderbook"
    ob_spread = {"imbalance": 0.0, "spread_bps": 40.0}
    r2 = gate_orderbook(MarketContext(cfg=cfg, side="SELL", orderbook=ob_spread))
    assert not r2.passed and "спред" in r2.detail
    ob_ok = {"imbalance": 0.1, "spread_bps": 5.0}
    assert gate_orderbook(MarketContext(cfg=cfg, side="BUY", orderbook=ob_ok)).passed
    # SELL при перевесе покупок > 0.3 — блок
    assert not gate_orderbook(MarketContext(cfg=cfg, side="SELL",
                                            orderbook={"imbalance": 0.5, "spread_bps": 5.0})).passed


def test_orderbook_error_when_missing():
    r = gate_orderbook(MarketContext(cfg=_cfg(), side="BUY", orderbook=None))
    assert not r.passed and r.key == "orderbook_error"
    # лимиты выключены — стакан не нужен
    r2 = gate_orderbook(MarketContext(cfg=_cfg(entry_ob_imbalance_max=0, entry_ob_spread_max=0),
                                      side="BUY", orderbook=None))
    assert r2.passed


def test_chain_short_circuit():
    ctx = TimeContext(cfg=_cfg(long_allowed=False), side="BUY", entries_paused=True)
    r = run_gate_chain(TIME_GATES, ctx)
    assert not r.passed and r.key == "entries_paused"  # первый по рангу
