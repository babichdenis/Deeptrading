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
