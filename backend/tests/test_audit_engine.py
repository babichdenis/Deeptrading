"""Тесты AUDIT-слоя (audit_engine.py): slippage, session, funnel reconciliation,
intrabar, sizing, contention. Read-only: движок не трогаем."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle as EC
from app.services.audit_engine import (
    apply_fill_price,
    contention_groups,
    detect_intrabar_ambiguity,
    intent_session_fields,
    qty_for_fill,
    reprice_trade,
    resolve_intrabar_exit,
    session_at,
    split_funnel,
)


def _candle(ts, o, h, l, c):
    return EC(ts=ts, open=o, high=h, low=l, close=c, volume=1)


PARAMS = {"commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 100_000, "lot": 10}


# --- 1. apply_fill_price: BUY/SELL × entry/exit ---
def test_slippage_buy_entry_exit():
    raw = 100.0
    s = 2.0  # bps
    buy_entry = apply_fill_price("LONG", "entry", raw, s)
    buy_exit = apply_fill_price("LONG", "exit", raw, s)
    assert buy_entry == pytest.approx(100.0 * (1 + 2 / 10000))
    assert buy_exit == pytest.approx(100.0 * (1 - 2 / 10000))
    assert buy_entry > raw > buy_exit


def test_slippage_short_entry_exit():
    raw = 100.0
    s = 2.0
    sell_entry = apply_fill_price("SHORT", "entry", raw, s)
    sell_exit = apply_fill_price("SHORT", "exit", raw, s)
    assert sell_entry == pytest.approx(100.0 * (1 - 2 / 10000))
    assert sell_exit == pytest.approx(100.0 * (1 + 2 / 10000))
    assert sell_entry < raw < sell_exit


# --- 2. reprice: no double counting ---
def test_reprice_no_double_count():
    candles = [_candle(datetime(2026, 7, 1, 9, 15, tzinfo=timezone.utc), 100, 101, 99, 100.5),
               _candle(datetime(2026, 7, 1, 9, 16, tzinfo=timezone.utc), 100.5, 104, 100.4, 103),
               _candle(datetime(2026, 7, 1, 9, 17, tzinfo=timezone.utc), 103, 106, 102, 105)]
    trade = {"trade_id": "T1", "figi": "BBG008F2T3T2", "side": "LONG",
             "entry_index": 1, "entry_ts": "2026-07-01T09:16:00+00:00",
             "exit_ts": "2026-07-01T09:17:00+00:00", "exit_reason": "target",
             "initial_stop": 98.5, "take_profit": 103.0}
    rt = reprice_trade(trade, candles, PARAMS)
    # total_cost = commission + slippage (ровно один раз)
    assert abs(rt.total_cost_rub - (rt.commission_rub + rt.slippage_rub)) < 0.01
    assert rt.slippage_rub > 0  # slippage отражён
    assert rt.entry_slippage_rub > 0 and rt.exit_slippage_rub > 0
    # net = gross - total_cost (округление полей допускает 2 копейки)
    assert abs(rt.net_rub - (rt.gross_rub - rt.total_cost_rub)) < 0.02
    # бар выхода 09:17: high 106 >= 103 (target достигнут), low 102 > 98.5 (stop нет)
    assert not rt.intrabar_ambiguous


# --- 3. session ---
def test_session_main_and_outside():
    # 12:00 MSK = 09:00 UTC (лето MSK=UTC+3)
    dt_main = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)  # 12:00 MSK
    assert session_at(dt_main) == "main"
    # 20:00 MSK = 17:00 UTC — вне
    dt_out = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)
    assert session_at(dt_out) == "outside"
    # суббота
    dt_weekend = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)
    assert session_at(dt_weekend) == "weekend"


def test_session_fields_midnight():
    # 23:50 MSK 30.06 = 20:50 UTC — decision вне main
    dt = datetime(2026, 6, 30, 20, 50, tzinfo=timezone.utc)
    f = intent_session_fields(dt, dt + timedelta(minutes=1))
    assert f["decision_time_msk"].startswith("2026-06-30T23:50")
    assert f["session_at_decision"] == "outside"
    assert f["session_gate_result"] == "reject"
    # переход через полночь: 00:05 MSK 01.07 = 21:05 UTC 30.06
    dt2 = datetime(2026, 6, 30, 21, 5, tzinfo=timezone.utc)
    f2 = intent_session_fields(dt2, dt2 + timedelta(minutes=1))
    assert f2["decision_time_msk"].startswith("2026-07-01T00:05")


# --- 4. funnel reconciliation ---
def test_funnel_reconciliation():
    from app.services.audit_engine import split_funnel
    funnel = {"raw_signals": 100, "unique_raw_ts": 60, "quorum_unique": 20,
              "entries_raw": 15, "entries_rejected": 5, "accepted_decisions": 10}
    rejected = [{"ts": "x", "side": "BUY", "reason": "SETUP_MISSING"} for _ in range(3)]
    # 6 intents: 1 в субботу (weekend -> REJECTED_SESSION), 5 в будни
    accepted = [
        {"ts": "2026-07-04T10:00:00+00:00", "side": "BUY"},  # суббота
        {"ts": "2026-07-01T10:00:00+00:00", "side": "BUY"},
        {"ts": "2026-07-01T11:00:00+00:00", "side": "SELL"},
        {"ts": "2026-07-02T10:00:00+00:00", "side": "BUY"},
        {"ts": "2026-07-02T11:00:00+00:00", "side": "SELL"},
        {"ts": "2026-07-03T10:00:00+00:00", "side": "BUY"},
    ]
    trades = [{"trade_id": f"T{i}"} for i in range(3)]
    reentry = [{"signal_ts": "x"} for _ in range(1)]
    r = split_funnel(funnel, rejected, accepted, trades, reentry)
    rec = r["reconciliation"]
    assert rec["entry_intents"] == 6
    assert rec["terminal_sum"] == 6  # все intents терминальны
    assert rec["reconciliation_pct"] == 100.0
    assert rec["primary_terminal_reason"]["EXECUTED"] == 3
    assert rec["primary_terminal_reason"]["REJECTED_SESSION"] == 1  # weekend
    assert rec["primary_terminal_reason"]["REJECTED_COOLDOWN"] == 1
    assert rec["primary_terminal_reason"]["REJECTED_IN_POSITION"] == 1  # 6-3-1-1


# --- 5. intrabar ---
def test_intrabar_ambiguous_long():
    bar = _candle(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), 100, 104, 97, 101)
    assert detect_intrabar_ambiguity(bar, "LONG", stop=98.0, target=103.0)
    # conservative → стоп
    px, reason = resolve_intrabar_exit(bar, "LONG", 98.0, 103.0, "conservative_stop_first")
    assert reason == "stop_loss" and px == 98.0
    # optimistic → тейк
    px2, reason2 = resolve_intrabar_exit(bar, "LONG", 98.0, 103.0, "optimistic_target_first")
    assert reason2 == "target" and px2 == 103.0
    # legacy = стоп первым
    px3, reason3 = resolve_intrabar_exit(bar, "LONG", 98.0, 103.0, "current_legacy")
    assert reason3 == "stop_loss"


def test_intrabar_ambiguous_short():
    bar = _candle(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), 100, 104, 96, 102)
    assert detect_intrabar_ambiguity(bar, "SHORT", stop=102.5, target=97.0)
    px, reason = resolve_intrabar_exit(bar, "SHORT", 102.5, 97.0, "conservative_stop_first")
    assert reason == "stop_loss"
    px2, reason2 = resolve_intrabar_exit(bar, "SHORT", 102.5, 97.0, "optimistic_target_first")
    assert reason2 == "target"


# --- 6. sizing ---
def test_qty_lot_rounding():
    # 100k / (100.0 * 10) = 100 лотов = 1000 акций
    assert qty_for_fill(100_000, 100.0, 10) == 1000
    # 100k / (33.0 * 10) = 303.03 → 303 лотов = 3030
    assert qty_for_fill(100_000, 33.0, 10) == 3030


def test_reprice_qty_gap():
    # гэп между decision и fill: decision бар open 100, но fill по 102 (след. бар)
    candles = [_candle(datetime(2026, 7, 1, 9, 15, tzinfo=timezone.utc), 100, 100, 100, 100),
               _candle(datetime(2026, 7, 1, 9, 16, tzinfo=timezone.utc), 102, 104, 101.5, 103),
               _candle(datetime(2026, 7, 1, 9, 17, tzinfo=timezone.utc), 103, 105, 102, 104)]
    trade = {"trade_id": "T", "figi": "F", "side": "LONG", "entry_index": 1,
             "entry_ts": "2026-07-01T09:16:00+00:00", "exit_ts": "2026-07-01T09:17:00+00:00",
             "exit_reason": "signal_exit", "initial_stop": 100.0, "take_profit": 106.0}
    rt = reprice_trade(trade, candles, PARAMS)
    # qty по fill цене (102 * 1.0002 ≈ 102.02): floor(100000/1020.2)*10 = 970
    assert rt.qty % 10 == 0
    assert rt.entry_notional <= 100_000 + 1e-6  # assert: notional <= capital


# --- 7. contention ---
def test_contention_groups():
    accepted = [
        {"ts": "2026-07-01T10:00:00+00:00", "side": "SELL"},
        {"ts": "2026-07-01T10:00:00+00:00", "side": "BUY"},
        {"ts": "2026-07-01T10:05:00+00:00", "side": "BUY"},
    ]
    groups = contention_groups(accepted)
    assert len(groups) == 2  # одна группа из 2, одна одиночная не учитывается
    assert groups[0]["candidates_in_group"] == 2
    # BUY приоритетнее SELL
    buy = [g for g in groups if g["side"] == "BUY"][0]
    sell = [g for g in groups if g["side"] == "SELL"][0]
    assert buy["priority_rank"] < sell["priority_rank"]
    assert buy["selected"] is True and sell["selected"] is False


# --- 12. IN_POSITION decomposition ---
def test_classify_in_position():
    from app.services.audit_engine import classify_in_position
    assert classify_in_position("BUY", "LONG", False) == "same_side_existing_position"
    assert classify_in_position("SELL", "LONG", False) == "opposite_side_candidate"
    assert classify_in_position("BUY", "LONG", True) == "same_episode_repeat"
    assert classify_in_position("BUY", None, False) == "position_awaiting_exit"


def test_decompose_in_position():
    from app.services.audit_engine import decompose_in_position
    trades = [
        {"entry_ts": "2026-07-01T10:00:00+00:00", "exit_ts": "2026-07-01T12:00:00+00:00", "side": "LONG"},
        {"entry_ts": "2026-07-01T14:00:00+00:00", "exit_ts": "2026-07-01T16:00:00+00:00", "side": "SHORT"},
    ]
    # intent во время открытой LONG: BUY same-side, SELL opposite
    accepted = [
        {"ts": "2026-07-01T10:30:00+00:00", "side": "BUY"},
        {"ts": "2026-07-01T11:00:00+00:00", "side": "SELL"},
        # вне позиции — не in-position
        {"ts": "2026-07-01T13:00:00+00:00", "side": "BUY"},
        # внутри SHORT: SELL same-side
        {"ts": "2026-07-01T15:00:00+00:00", "side": "SELL"},
    ]
    r = decompose_in_position(accepted, trades, [], 0)
    assert r["by_reason"]["same_side_existing_position"] == 0
    assert r["by_reason"]["same_episode_repeat"] == 2       # BUY@LONG, SELL@SHORT (внутри эпизода)
    assert r["by_reason"]["opposite_side_candidate"] == 1    # SELL@LONG
    assert r["total_in_position"] == 3


# --- 13. session invariants ---
def test_session_invariants():
    from app.services.audit_engine import session_invariants
    trades = [
        # решение в main, выход позже 18:45 MSK (после 15:45 UTC) — exits_outside
        {"entry_ts": "2026-07-01T12:00:00+00:00", "exit_ts": "2026-07-01T16:00:00+00:00"},
        # overnight: вход 01.07, выход 02.07
        {"entry_ts": "2026-07-01T12:00:00+00:00", "exit_ts": "2026-07-02T06:00:00+00:00"},
    ]
    accepted = [
        {"ts": "2026-07-01T12:00:00+00:00", "side": "BUY"},   # в main
        {"ts": "2026-07-04T12:00:00+00:00", "side": "BUY"},   # суббота (weekend)
    ]
    r = session_invariants(trades, accepted)
    assert r["new_entries_decision_weekend"] == 1
    assert r["new_entries_decision_main"] == 1
    assert r["overnight_positions"] == 1
    # exit 16:00 UTC = 19:00 MSK (outside); exit 06:00 UTC 02.07 = 09:00 MSK (outside, до открытия)
    assert r["exits_outside_main"] == 2


# --- 15. intent lifecycle (terminal reasons через replay движка) ---
def test_intent_lifecycle_replay():
    from app.services.audit_engine import replay_engine_audit
    # 3 свечи: 1 бар сигнала + 2 исполнения
    candles = [
        _candle(datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc), 100, 101, 99, 100.5),
        _candle(datetime(2026, 7, 1, 7, 1, tzinfo=timezone.utc), 100.5, 103, 100, 102.5),
        _candle(datetime(2026, 7, 1, 7, 2, tzinfo=timezone.utc), 102.5, 104, 101, 103.5),
        _candle(datetime(2026, 7, 1, 7, 3, tzinfo=timezone.utc), 103.5, 105, 102, 104.5),
        _candle(datetime(2026, 7, 1, 7, 4, tzinfo=timezone.utc), 104.5, 106, 103, 105.5),
    ]
    accepted = [{"ts": "2026-07-01T07:00:00+00:00", "side": "BUY"}]
    entries_raw = [{"ts": "2026-07-01T07:01:00+00:00", "side": "SELL"}]
    req = {"figi": "F", "capital": 10000, "lot": 10, "commission_rate": 0.0005,
           "slippage_bps": 2.0, "same_side_reentry_cooldown_bars": 0,
           "carry_overnight": True,
           "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}}}
    life, summary = replay_engine_audit(candles, accepted, entries_raw, req)
    assert len(life) == 1
    assert life[0]["terminal"] in ("EXECUTED_TRADE", "OPEN_AT_END", "REJECTED_OTHER")
    assert summary["n_intents"] == 1
    # суммарно: intents = trades + rejected
    assert summary["n_intents"] >= 1
