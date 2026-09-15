"""Tests for IMOEX guard (app/bot/imoex_guard.py).

Сценарий-якорь: 14.09.2026, IMOEX 2321.6 (17:50 МСК) -> 2345.6 (18:10), +24п (+1.03%)
за 20 минут. Guard должен активировать UP-всплеск и запретить SELL-входы,
а после затухания хода ниже порога — снять запрет (release).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.imoex_guard import ImoexGuardState, block_for, move_at, step

T0 = datetime(2026, 9, 14, 14, 50, tzinfo=timezone.utc)  # 17:50 МСК


def _buf(prices: list[float], step_min: int = 1):
    return [(T0 + timedelta(minutes=step_min * i), float(p)) for i, p in enumerate(prices)]


# --- move_at ---

def test_move_at_basic_up():
    buf = _buf([100.0] * 20 + [101.0])  # +1.0 за 20 минут
    mv = move_at(buf, T0 + timedelta(minutes=20), 20)
    assert mv is not None
    pts, pct, _, _ = mv
    assert abs(pts - 1.0) < 1e-9
    assert abs(pct - 1.0) < 1e-9


def test_move_at_insufficient_history():
    buf = _buf([100.0] * 5)
    assert move_at(buf, T0 + timedelta(minutes=4), 20) is None


def test_move_at_uses_last_bar_before_now():
    buf = _buf([100.0] * 21 + [999.0])
    # now = 20 минут -> бар 999.0 (ts=21м) ещё не должен попасть
    mv = move_at(buf, T0 + timedelta(minutes=20), 20)
    pts, _, _, _ = mv
    assert abs(pts) < 1e-9


def test_move_at_empty():
    assert move_at([], T0, 20) is None


# --- step: активация ---

def test_activate_up_on_pct():
    st = ImoexGuardState()
    ev = step(st, 24.0, 1.03, T0, on_pct=0.8, min_block_min=5)
    assert ev == "activate_up"
    assert st.active == 1
    assert st.activations == 1


def test_activate_down_on_points():
    st = ImoexGuardState()
    ev = step(st, -25.0, -0.3, T0, on_pct=0.8, on_points=20.0, min_block_min=0)
    assert ev == "activate_down"
    assert st.active == -1


def test_no_activation_below_threshold():
    st = ImoexGuardState()
    ev = step(st, 10.0, 0.4, T0, on_pct=0.8, on_points=20.0, min_block_min=0)
    assert ev is None
    assert st.active == 0


# --- step: гистерезис/стабилизация ---

def test_release_after_calm_and_min_block():
    st = ImoexGuardState()
    step(st, 24.0, 1.03, T0, on_pct=0.8, min_block_min=5)
    # Через 3 минуты ход уже спокоен, но min_block не прошёл — блока нет release
    assert step(st, 3.0, 0.13, T0 + timedelta(minutes=3), on_pct=0.8, min_block_min=5) is None
    assert st.active == 1
    # Через 6 минут — release
    assert step(st, 2.0, 0.09, T0 + timedelta(minutes=6), on_pct=0.8, min_block_min=5) == "release"
    assert st.active == 0
    assert st.releases == 1


def test_no_release_while_still_hot():
    st = ImoexGuardState()
    step(st, 24.0, 1.03, T0, on_pct=0.8, min_block_min=5)
    ev = step(st, 20.0, 0.86, T0 + timedelta(minutes=10), on_pct=0.8, min_block_min=5)
    assert ev is None
    assert st.active == 1


def test_release_threshold_is_half_of_on():
    st = ImoexGuardState()
    step(st, 24.0, 1.03, T0, on_pct=0.8, min_block_min=0)
    # 0.5% > off (0.4%) — ещё держим
    assert step(st, 11.0, 0.5, T0 + timedelta(minutes=1), on_pct=0.8, min_block_min=0) is None
    assert st.active == 1
    # 0.2% < off — release
    assert step(st, 4.0, 0.2, T0 + timedelta(minutes=2), on_pct=0.8, min_block_min=0) == "release"


def test_direction_flip_reactivates_immediately():
    st = ImoexGuardState()
    step(st, 24.0, 1.03, T0, on_pct=0.8, min_block_min=30)
    ev = step(st, -25.0, -1.05, T0 + timedelta(minutes=1), on_pct=0.8, min_block_min=30)
    assert ev == "activate_down"
    assert st.active == -1
    assert st.activations == 2


# --- сценарий 14.09.2026 ---

def test_case_14_09_spike():
    """+1.03% за 20 минут -> UP-всплеск (SELL-входы запрещены), стабилизация через ~30 мин."""
    prices = [2321.6] * 20 + [2345.6] + [2345.6] * 20
    buf = _buf(prices)
    now = T0 + timedelta(minutes=20)
    pts, pct, _, _ = move_at(buf, now, 20)
    st = ImoexGuardState()
    assert step(st, pts, pct, now, on_pct=0.8, on_points=20.0, min_block_min=5) == "activate_up"
    # Через 20 минут после активации ход 0% — release
    now2 = T0 + timedelta(minutes=40)
    pts2, pct2, _, _ = move_at(buf, now2, 20)
    assert step(st, pts2, pct2, now2, on_pct=0.8, on_points=20.0, min_block_min=5) == "release"
    assert st.active == 0


# --- block_for: контр-входы, per-ticker beta, второй уровень (chase) ---

def test_block_for_counter_sides():
    st = ImoexGuardState(active=1, pct=1.03, move=24.0)
    assert block_for(st, "SELL") is not None
    assert block_for(st, "BUY") is None
    st = ImoexGuardState(active=-1, pct=-1.03, move=-24.0)
    assert block_for(st, "BUY") is not None
    assert block_for(st, "SELL") is None


def test_block_for_inactive():
    st = ImoexGuardState(active=0)
    assert block_for(st, "SELL") is None
    assert block_for(None, "BUY") is None


def test_block_for_min_beta_filters():
    st = ImoexGuardState(active=1, pct=1.03, move=24.0)
    # beta бумаги 0.7 < порога 1.0 -> контр-вход разрешён
    assert block_for(st, "SELL", beta=0.7, min_beta=1.0) is None
    # beta 1.3 >= 1.0 -> блок
    assert block_for(st, "SELL", beta=1.3, min_beta=1.0) is not None
    # нет данных beta -> консервативно без блока при min_beta>0
    assert block_for(st, "SELL", beta=None, min_beta=1.0) is None


def test_block_for_chase_second_level():
    st = ImoexGuardState(active=1, pct=1.8, move=42.0)
    # обычно BUY при UP-всплеске разрешён
    assert block_for(st, "BUY") is None
    # но при |ходе| >= 1.5% "догон" блокируется
    assert block_for(st, "BUY", chase_pct=1.5) is not None
    st = ImoexGuardState(active=-1, pct=-1.8, move=-42.0)
    assert block_for(st, "SELL") is None
    assert block_for(st, "SELL", chase_pct=1.5) is not None
    # при умеренном ходе chase не активен
    st = ImoexGuardState(active=1, pct=1.0, move=23.0)
    assert block_for(st, "BUY", chase_pct=1.5) is None
