"""Тесты HOLD после серии убытков (loss streak hold).

Правило: после N убытков подряд по тикеру (или глобально) новые входы
запрещены на hold_min минут от последнего убытка. Чистая функция _loss_hold_left.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.runtime import _loss_hold_left

T0 = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)


def test_no_hold_below_threshold():
    assert _loss_hold_left(T0, count=1, last_ts=T0, n=2, hold_min=60) == 0.0


def test_hold_active_right_after_loss():
    left = _loss_hold_left(T0 + timedelta(minutes=1), count=2, last_ts=T0, n=2, hold_min=60)
    assert 58.9 <= left <= 59.1


def test_hold_expires_after_window():
    assert _loss_hold_left(T0 + timedelta(minutes=61), count=2, last_ts=T0, n=2, hold_min=60) == 0.0


def test_hold_exact_boundary():
    assert _loss_hold_left(T0 + timedelta(minutes=60), count=2, last_ts=T0, n=2, hold_min=60) == 0.0


def test_hold_more_losses_still_active():
    left = _loss_hold_left(T0 + timedelta(minutes=30), count=5, last_ts=T0, n=2, hold_min=60)
    assert 29.9 <= left <= 30.1


def test_hold_disabled_by_zero_minutes():
    assert _loss_hold_left(T0 + timedelta(minutes=1), count=2, last_ts=T0, n=2, hold_min=0) == 0.0


def test_hold_no_last_ts():
    assert _loss_hold_left(T0, count=2, last_ts=None, n=2, hold_min=60) == 0.0
