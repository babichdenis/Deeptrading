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


# --- trend / portfolio (STAGE B/C) ---------------------------------------------------

from app.bot.gates import (  # noqa: E402
    PortfolioContext, TrendContext, PORTFOLIO_GATES, TREND_GATES,
    gate_daily_bias, gate_h1_align, gate_ls_balance, gate_max_positions,
    gate_mtf_h1, gate_mtf_m5, gate_rank, gate_require_member, gate_sector_cluster,
    gate_tf_conflict,
)


def _tcfg(**kw):
    base = dict(daily_bias=True, daily_bias_mode="veto", entry_h1_align=True,
                entry_tf_conflict=True, mtf_align=False, mtf_trigger=False,
                ensemble_require_member="", ensemble_quorum=2,
                max_positions=5, max_sector_positions=2, balance_min_positions=3,
                max_short_share=0.7)
    base.update(kw)
    return SimpleNamespace(**base)


def test_daily_bias_veto_and_info():
    ctx = TrendContext(cfg=_tcfg(), side="SELL", daily_bias="up", daily_hist=0.4)
    assert not gate_daily_bias(ctx).passed
    ctx_info = TrendContext(cfg=_tcfg(daily_bias_mode="info"), side="SELL", daily_bias="up")
    assert gate_daily_bias(ctx_info).passed
    ctx_ok = TrendContext(cfg=_tcfg(), side="BUY", daily_bias="up")
    assert gate_daily_bias(ctx_ok).passed
    ctx_off = TrendContext(cfg=_tcfg(daily_bias=False), side="SELL", daily_bias="up")
    assert gate_daily_bias(ctx_off).passed


def test_h1_align_and_tf_conflict():
    assert not gate_h1_align(TrendContext(cfg=_tcfg(), side="SELL", h1_ok=True,
                                          h1_side="BUY", h1_hist=0.2)).passed
    assert gate_h1_align(TrendContext(cfg=_tcfg(entry_h1_align=False), side="SELL",
                                      h1_ok=True, h1_side="BUY")).passed
    # daily up vs H1 SELL — конфликт
    assert not gate_tf_conflict(TrendContext(cfg=_tcfg(), side="BUY", daily_bias="up",
                                             h1_ok=True, h1_side="SELL")).passed
    # daily up и H1 BUY — ок
    assert gate_tf_conflict(TrendContext(cfg=_tcfg(), side="BUY", daily_bias="up",
                                         h1_ok=True, h1_side="BUY")).passed


def test_legacy_mtf_off_by_default():
    ctx = TrendContext(cfg=_tcfg(), side="SELL", h1_ok=True, h1_side="BUY")
    assert gate_mtf_h1(ctx).passed
    assert gate_mtf_m5(TrendContext(cfg=_tcfg(), side="SELL", m5_ok=True,
                                    m5_trend="rising")).passed
    # включили legacy — блокирует
    assert not gate_mtf_h1(TrendContext(cfg=_tcfg(mtf_align=True), side="SELL",
                                        h1_ok=True, h1_side="BUY")).passed
    assert not gate_mtf_m5(TrendContext(cfg=_tcfg(mtf_trigger=True), side="SELL",
                                        m5_ok=True, m5_trend="rising")).passed


def test_require_member_and_rank():
    ctx = TrendContext(cfg=_tcfg(ensemble_require_member="macd_cross"), side="BUY",
                       require_member="macd_cross",
                       members_for=("rsi_reversal",), votes=2, quorum=2)
    assert not gate_require_member(ctx).passed
    ctx_ok = TrendContext(cfg=_tcfg(ensemble_require_member="macd_cross"), side="BUY",
                          require_member="macd_cross",
                          members_for=("macd_cross", "rsi_reversal"), votes=2, quorum=2)
    assert gate_require_member(ctx_ok).passed
    assert not gate_rank(TrendContext(cfg=_tcfg(), side="BUY", rank_why="net -120₽ n=6")).passed
    assert gate_rank(TrendContext(cfg=_tcfg(), side="BUY")).passed


def test_portfolio_gates():
    assert not gate_max_positions(PortfolioContext(cfg=_tcfg(max_positions=3), side="BUY",
                                                   held_count=3)).passed
    assert gate_max_positions(PortfolioContext(cfg=_tcfg(max_positions=3), side="BUY",
                                               held_count=2)).passed
    assert not gate_sector_cluster(PortfolioContext(cfg=_tcfg(max_sector_positions=2),
                                                    side="BUY", sector="oil",
                                                    sector_count=2)).passed
    assert gate_sector_cluster(PortfolioContext(cfg=_tcfg(max_sector_positions=2),
                                                side="BUY", sector="other",
                                                sector_count=5)).passed
    # шорт-перекос: 3 шорта из 3 при лимите 70%
    assert not gate_ls_balance(PortfolioContext(cfg=_tcfg(max_short_share=0.7), side="SELL",
                                                held_count=3, short_count=3)).passed
    # меньше min_total — баланс не ограничиваем
    assert gate_ls_balance(PortfolioContext(cfg=_tcfg(max_short_share=0.7), side="SELL",
                                            held_count=1, short_count=1)).passed
    # BUY не ограничивается балансом
    assert gate_ls_balance(PortfolioContext(cfg=_tcfg(max_short_share=0.5), side="BUY",
                                            held_count=4, short_count=4)).passed


def test_trend_portfolio_chains():
    assert run_gate_chain(TREND_GATES, TrendContext(cfg=_tcfg(), side="BUY")).passed
    r = run_gate_chain(PORTFOLIO_GATES,
                       PortfolioContext(cfg=_tcfg(max_positions=1), side="BUY", held_count=1))
    assert not r.passed and r.key == "max_positions"
