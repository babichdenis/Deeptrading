"""Изоляция прогонов: повторный start() в одном процессе — чистое состояние.

Реплей запускается без перезапуска процесса (API отдаёт restarted=false),
поэтому per-run состояние прошлого прогона не должно влиять на следующий.
Ключевой симптом утечки: _loss_streak/_last_loss_ts из прошлого прогона
блокируют входы в новом (last_loss_ts лежит в будущем относительно виртуальных
часов нового окна), плюс остатки _last_exit_bar/_held/_exit_plans.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.runtime import PaperBotRuntime

T0 = datetime(2026, 10, 8, 18, 10, tzinfo=timezone.utc)


def _pollute(rt: PaperBotRuntime) -> None:
    rt._loss_streak["FIGI1"] = 3
    rt._last_loss_ts["FIGI1"] = T0
    rt._global_loss_streak = 3
    rt._global_last_loss_ts = T0
    rt._last_exit_bar["FIGI1"] = 99
    rt._bar_counter = 999
    rt._held.add("FIGI1")
    rt._held_since["FIGI1"] = 1.0
    rt._exit_plans["FIGI1"] = object()
    rt._trail_stop["FIGI1"] = 1.0
    rt._trail_active["FIGI1"] = True
    rt._exit_target["FIGI1"] = 1.0
    rt._entry_bar_index["FIGI1"] = 5
    rt._peak_pnl["FIGI1"] = {"pnl": 1.0}
    rt._gb_peak["FIGI1"] = 7.5
    rt._prev_close["FIGI1"] = 100.0
    rt._day_jumps["FIGI1"] = {"2026-10-08": 2}
    rt._bad_day["FIGI1"] = {"2026-10-08": True}
    rt._move_selected = {"FIGI1"}
    rt._move_leaving = {"FIGI2"}
    rt._cand_queue["FIGI1"] = {"score": 1.0}
    rt._candles_rejected = 5
    rt._imoex_buf.append((T0, 100.0))
    rt._imoex_state = object()
    rt._turnover_cache["FIGI1"] = 1.0
    rt._daily_pnl_cache = (T0, -50.0)
    rt._equity_peak = 123.0
    rt._dd_level_done = 2
    rt._overnight_cache = {"FIGI1": True}
    rt._regimes["FIGI1"] = {"state": "TREND_UP"}
    rt._5m_cache["FIGI1"] = [1, 2]
    rt._5m_last_close["FIGI1"] = 7
    rt._persist_queue.append((1,))
    rt._persist_queue_5m.append((1,))
    rt._log_persist_queue.append(("i", "s", "m", "t", "r"))


def test_reset_run_state_clears_all_leaks():
    rt = PaperBotRuntime()
    _pollute(rt)
    rt._reset_run_state()
    assert rt._loss_streak == {}
    assert rt._last_loss_ts == {}
    assert rt._global_loss_streak == 0
    assert rt._global_last_loss_ts is None
    assert rt._last_exit_bar == {}
    assert rt._bar_counter == 0
    assert rt._held == set()
    assert rt._held_since == {}
    assert rt._exit_plans == {}
    assert rt._trail_stop == {}
    assert rt._trail_active == {}
    assert rt._exit_target == {}
    assert rt._entry_bar_index == {}
    assert rt._peak_pnl == {}
    assert rt._gb_peak == {}
    assert rt._prev_close == {}
    assert rt._day_jumps == {}
    assert rt._bad_day == {}
    assert rt._move_selected is None
    assert rt._move_leaving == set()
    assert rt._cand_queue == {}
    assert rt._candles_rejected == 0
    assert rt._imoex_buf == []
    assert rt._imoex_state is None
    assert rt._turnover_cache == {}
    assert rt._daily_pnl_cache is None
    assert rt._equity_peak == 0.0
    assert rt._dd_level_done == 0
    assert rt._overnight_cache is None
    assert rt._regimes == {}
    assert rt._5m_cache == {}
    assert rt._5m_last_close == {}
    assert len(rt._persist_queue) == 0
    assert len(rt._persist_queue_5m) == 0
    assert len(rt._log_persist_queue) == 0


def test_reset_run_state_unblocks_loss_streak_hold():
    rt = PaperBotRuntime()
    _pollute(rt)
    rt._replay_from = datetime(2025, 12, 1, tzinfo=timezone.utc)
    rt._replay_cur = datetime(2025, 12, 5, 6, 0, tzinfo=timezone.utc)
    blocked, why = rt._loss_streak_block("FIGI1")
    assert blocked is True
    assert "убытков подряд" in why
    rt._reset_run_state()
    rt._replay_from = datetime(2025, 12, 1, tzinfo=timezone.utc)
    rt._replay_cur = datetime(2025, 12, 5, 6, 0, tzinfo=timezone.utc)
    blocked, why = rt._loss_streak_block("FIGI1")
    assert blocked is False
    assert why == ""


def test_reset_run_state_keeps_config_and_events():
    rt = PaperBotRuntime()
    rt.config.max_positions = 5
    events = rt.events
    rt._reset_run_state()
    assert rt.config.max_positions == 5
    assert rt.events is events


def test_loss_hold_left_uses_virtual_clock():
    left = PaperBotRuntime()._loss_streak_block("NOPE")
    assert left == (False, "")


def test_momentum_hold_boundary():
    from app.bot.runtime import _loss_hold_left
    now = T0 + timedelta(minutes=30)
    assert 29.9 <= _loss_hold_left(now, 2, T0, 2, 60.0) <= 30.1
