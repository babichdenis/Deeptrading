"""Тесты портфельного риска (app/bot/portfolio.py)."""
from __future__ import annotations

from app.bot.portfolio import PortfolioLimits, check_order, snapshot

META = {
    "SBER": {"sector": "financial", "beta": 0.8},
    "GAZP": {"sector": "energy", "beta": 1.0},
    "LKOH": {"sector": "energy", "beta": 1.1},
    "NLMK": {"sector": "materials", "beta": 1.0},
}


def _pos(ticker, side, qty, entry):
    return {"ticker": ticker, "side": side, "qty": qty, "entry": entry}


def test_snapshot_exposures_and_sectors():
    snap = snapshot(10000.0, [
        _pos("SBER", "SHORT", 10, 300),   # 3000 short
        _pos("GAZP", "SHORT", 20, 100),   # 2000 short
        _pos("NLMK", "LONG", 10, 100),    # 1000 long
    ], META, {"liquid": 10000, "starting_margin": 2000})
    assert snap["long_notional"] == 1000
    assert snap["short_notional"] == 5000
    assert snap["net_notional"] == -4000
    assert abs(snap["net_exposure_pct"] + 0.4) < 1e-9
    assert snap["sector_pct"]["energy"] == 0.2
    assert snap["sector_pct"]["financial"] == 0.3
    assert snap["margin_use_pct"] == 0.2
    assert snap["margin_free"] == 8000


def test_stress_shorts_lose_on_index_up():
    snap = snapshot(10000.0, [_pos("GAZP", "SHORT", 20, 100)], META, {})
    # short 2000, beta 1.0 → +5% IMOEX = -100₽ = -1%
    assert abs(snap["stress_pct"]["imoex_+5%"] + 0.01) < 1e-9
    assert abs(snap["stress_pct"]["imoex_-5%"] - 0.01) < 1e-9
    assert snap["stress_worst"][0] == "imoex_+10%"


def test_check_order_net_exposure_limit():
    snap = snapshot(10000.0, [_pos("GAZP", "SHORT", 40, 100)], META, {})  # net -4000 = -40%
    lim = PortfolioLimits(max_net_exposure_pct=0.5, max_sector_pct=0, max_margin_use_pct=0, max_stress_loss_pct=0)
    ok, why = check_order(snap, "SELL", 1500, "SBER", META, lim)   # станет -55%
    assert not ok and "чистая экспозиция" in why
    ok2, _ = check_order(snap, "BUY", 1500, "SBER", META, lim)     # станет -25%
    assert ok2


def test_check_order_sector_limit():
    snap = snapshot(10000.0, [_pos("GAZP", "SHORT", 30, 100)], META, {})  # energy 3000 = 30%
    lim = PortfolioLimits(max_net_exposure_pct=0, max_sector_pct=0.35, max_margin_use_pct=0, max_stress_loss_pct=0)
    ok, why = check_order(snap, "SELL", 600, "LKOH", META, lim)  # energy 3600 = 36%
    assert not ok and "сектор" in why


def test_check_order_margin_buffer():
    snap = snapshot(10000.0, [], META, {"liquid": 10000, "starting_margin": 7500})
    lim = PortfolioLimits(max_net_exposure_pct=0, max_sector_pct=0, max_margin_use_pct=0.8, max_stress_loss_pct=0)
    ok, why = check_order(snap, "BUY", 1000, "SBER", META, lim)
    assert not ok and "маржа" in why


def test_check_order_stress_limit():
    snap = snapshot(10000.0, [_pos("GAZP", "SHORT", 300, 100)], META, {})  # -30000 = -300% net
    lim = PortfolioLimits(max_net_exposure_pct=0, max_sector_pct=0, max_margin_use_pct=0, max_stress_loss_pct=0.10)
    ok, why = check_order(snap, "SELL", 2000, "NLMK", META, lim)
    assert not ok and "стресс" in why


def test_check_order_ok():
    snap = snapshot(10000.0, [_pos("SBER", "LONG", 10, 100)], META, {"liquid": 10000, "starting_margin": 1000})
    lim = PortfolioLimits()
    ok, why = check_order(snap, "BUY", 500, "GAZP", META, lim)
    assert ok and why == "ok"


def test_regime_limits_bear_expands_net():
    from app.bot.portfolio import regime_limits
    base = PortfolioLimits()
    lim = regime_limits(base, {"state": "bear"})
    assert lim.max_net_exposure_pct == 1.0
    assert lim.max_sector_pct == 0.40
    assert lim.max_margin_use_pct == 0.75
    assert lim.max_stress_loss_pct == 0.08
    neutral = regime_limits(base, {"state": "neutral"})
    assert neutral.max_net_exposure_pct == 0.5
    rev = regime_limits(base, {"state": "reversal"})
    assert rev.max_net_exposure_pct == 0.35 and rev.max_stress_loss_pct == 0.06


def test_strength_score_short_prefers_weak_vs_market():
    from app.bot.portfolio import strength_score
    weak = strength_score(ret_ticker=-0.01, ret_index=0.0, beta=1.0, side="SELL")
    strong = strength_score(ret_ticker=+0.01, ret_index=0.0, beta=1.0, side="SELL")
    assert weak["score"] > 50 > strong["score"]
    assert weak["rs"] < 0 < strong["rs"]
    assert weak["score"] - strong["score"] > 5.0   # rs доминирует


def test_strength_score_bonuses():
    from app.bot.portfolio import strength_score
    plain = strength_score(ret_ticker=-0.005, ret_index=0.0, side="SELL")
    boosted = strength_score(ret_ticker=-0.005, ret_index=0.0, side="SELL",
                             vol_ratio=1.8, breadth_up_pct=30)
    assert boosted["score"] > plain["score"]
    assert boosted["vol_bonus"] == 4.8 and boosted["breadth_bonus"] == 2.0


def test_drawdown_action_levels():
    from app.bot.portfolio import drawdown_action
    assert drawdown_action(10000, 10000)["level"] == 0
    a = drawdown_action(9400, 10000)   # -6%
    assert a["level"] == 1 and a["close_pct"] == 0.5
    b = drawdown_action(8800, 10000)   # -12%
    assert b["level"] == 2 and b["close_pct"] == 0.8
    assert drawdown_action(0, 10000)["level"] == 0


def test_candidate_score_history_matters():
    from app.bot.portfolio import candidate_score
    good = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL",
                           hist={"n": 30, "wr": 0.55, "wr5": 0.6, "net": 120.0},
                           turnover=50e6, votes=3, total_members=5, vol_ratio=1.6)
    bad = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL",
                          hist={"n": 30, "wr": 0.15, "wr5": 0.0, "net": -90.0},
                          turnover=50e6, votes=3, total_members=5, vol_ratio=1.6)
    assert good["score"] > bad["score"] + 15
    assert good["factors"]["hist"] > 0.7 > bad["factors"]["hist"]
    assert bad["veto"] == ["bad_history"]
    assert good["veto"] == []


def test_candidate_score_vetoes_illiquid():
    from app.bot.portfolio import candidate_score
    r = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL", turnover=0.5e6)
    assert "illiquid" in r["veto"]
    r2 = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL", turnover=20e6)
    assert r2["veto"] == []


def test_candidate_score_regime_and_fit():
    from app.bot.portfolio import candidate_score
    bear_short = candidate_score(ret_ticker=-0.01, ret_index=-0.005, side="SELL", regime="bear")
    bull_short = candidate_score(ret_ticker=-0.01, ret_index=-0.005, side="SELL", regime="bull")
    assert bear_short["score"] > bull_short["score"]
    low_fit = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL", fit=0.0)
    high_fit = candidate_score(ret_ticker=-0.01, ret_index=0.0, side="SELL", fit=1.0)
    assert high_fit["score"] > low_fit["score"]
