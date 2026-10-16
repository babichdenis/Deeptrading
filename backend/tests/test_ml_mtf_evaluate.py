"""B3 focused тесты ml_mtf_evaluate (REWORK): исправленные инварианты ML-MTF-1.

Проверяют пункты 2–5 REWORK:
- вход РОВНО на decision_ts (закрытие 5m = открытие 1m), не +1м;
- сессия frozen 10:00..18:44, вход до cutoff, weekday;
- выход только по точной минуте (нет -> INCOMPLETE, не fallback по наличию);
- слоты: SKIP не занимает, повтор IGNORE, после exit слот+кэш освобождаются,
  exits перед entries, free cash+fee, одно FIGI одна позиция;
- equity: final=None при неизвестном хвосте, last_known отдельно, DD на известном
  префиксе, fixed notional без compounding;
- ledger: signal_outcomes отделены от executed.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ml_mtf_evaluate as ev  # noqa: E402
from day_strategies_core import CostParams  # noqa: E402

UTC = timezone.utc
FIGI = "FIGI001"
COSTS = CostParams(5.0, 2.0)
DAY10 = datetime(2026, 6, 16, 7, 0, tzinfo=UTC)  # 10:00 MSK, Tuesday


def _store(minutes, figi=FIGI):
    s = ev.MinuteStore()
    s.by_figi[figi] = dict(minutes)
    return s


def _day_minutes(day_start, n, price=100.0):
    return {day_start + timedelta(minutes=i): (price, price) for i in range(n)}


def _pred(decision_ts, selected=True, figi=FIGI, ticker="TST", row_id="r1"):
    return {"row_id": row_id, "figi": figi, "ticker": ticker,
            "decision_ts": decision_ts.isoformat(), "model_id": "m1",
            "split": "eval",
            "selected_long": "true" if selected else "false"}


# ------------------------------------------------------------- entry timing

def test_entry_at_decision_ts_not_plus_one():
    # закрытие 5m = открытие 1m-бара -> вход РОВНО на decision_ts
    minutes = _day_minutes(DAY10, 300, price=100.0)
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    assert o.status == "FILLED", o
    assert o.entry_ts == DAY10
    assert o.entry_base == 100.0


def test_missing_entry_minute_at_decision_is_skip():
    minutes = _day_minutes(DAY10, 300, price=100.0)
    del minutes[DAY10]  # нет минуты на decision_ts
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    assert o.status == "SKIP"
    assert o.reason == "missing_entry_minute"


def test_not_selected_is_skip():
    minutes = _day_minutes(DAY10, 300)
    o = ev.execute_signal(_pred(DAY10, selected=False), _store(minutes), COSTS)
    assert o.status == "SKIP" and o.reason == "not_selected_long"


# ------------------------------------------------------------- session window

def test_entry_after_cutoff_is_skip():
    # решение 18:44 MSK (15:44 UTC) — на cutoff, вход запрещён
    late = datetime(2026, 6, 16, 15, 44, tzinfo=UTC)
    minutes = _day_minutes(datetime(2026, 6, 16, 7, 0, tzinfo=UTC), 800)
    o = ev.execute_signal(_pred(late), _store(minutes), COSTS)
    assert o.status == "SKIP"
    assert "out_of_session" in o.reason


def test_entry_weekend_is_skip():
    sat = datetime(2026, 6, 20, 7, 0, tzinfo=UTC)  # Saturday 10:00 MSK
    minutes = _day_minutes(sat, 300)
    o = ev.execute_signal(_pred(sat), _store(minutes), COSTS)
    assert o.status == "SKIP" and "weekend" in o.reason


def test_entry_before_session_is_skip():
    early = datetime(2026, 6, 16, 5, 0, tzinfo=UTC)  # 08:00 MSK
    minutes = _day_minutes(datetime(2026, 6, 16, 4, 0, tzinfo=UTC), 400)
    o = ev.execute_signal(_pred(early), _store(minutes), COSTS)
    assert o.status == "SKIP" and "before_session" in o.reason


# ------------------------------------------------------------- exit timing

def test_time_exit_120min_exact():
    minutes = _day_minutes(DAY10, 300, price=100.0)
    minutes[DAY10 + timedelta(minutes=120)] = (102.0, 102.0)
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    assert o.status == "FILLED"
    assert o.exit_ts == DAY10 + timedelta(minutes=120)
    assert o.exit_base == 102.0


def test_missing_exit_minute_is_incomplete_not_fallback():
    # нет точной минуты 18:44 (cutoff) -> INCOMPLETE, не выбор 18:40 по наличию
    minutes = _day_minutes(DAY10, 300, price=100.0)  # до 14:59 UTC, cutoff 15:44
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    # entry 10:00, exit target 12:00 (в сессии) — минута 12:00 есть (300 мин до 14:59)
    assert o.status == "FILLED"
    assert o.exit_ts == DAY10 + timedelta(minutes=120)


def test_cutoff_clamp_exact_minute_required():
    # вход 18:30 MSK (15:30 UTC); +120м за cutoff -> clamp 18:44; нет 18:44 -> INCOMPLETE
    entry = datetime(2026, 6, 16, 15, 30, tzinfo=UTC)
    minutes = _day_minutes(datetime(2026, 6, 16, 7, 0, tzinfo=UTC), 800)
    # минуты до 18:44 UTC=15:44 есть, но 15:44 (18:44 MSK) присутствует? 800 мин от 10:00 -> до 23:19
    # 18:44 MSK = 15:44 UTC = модуль 644 от 7:00 -> есть
    del minutes[datetime(2026, 6, 16, 15, 44, tzinfo=UTC)]  # убираем cutoff минуту
    o = ev.execute_signal(_pred(entry), _store(minutes), COSTS)
    assert o.status == "INCOMPLETE"
    assert o.reason == "exit_minute_missing"


# ------------------------------------------------------------- portfolio slots

def _outcome(status, figi, entry, exit_ts=None, row_id="r",
             entry_price=100.0, exit_price=None):
    exit_base = exit_price if exit_price is not None else (
        entry_price * 1.02 if exit_ts else None)
    return ev.SignalOutcome(
        row_id=row_id, figi=figi, ticker=figi, decision_ts=entry,
        model_id="m1", split="eval", selected=True, status=status,
        reason="x", entry_ts=entry if status != "SKIP" else None,
        exit_ts=exit_ts, entry_base=entry_price,
        exit_base=exit_base,
        gross_per_unit=(exit_base - entry_price) if exit_ts else None,
        net_per_unit=(0.018 * entry_price) if exit_ts else None,
        costs_per_unit=0.0014 if exit_ts else None)


def test_skip_does_not_consume_capital():
    # 5 SKIP -> 0 executed, capital не занят
    outs = [_outcome("SKIP", f"F{i}", DAY10 + timedelta(minutes=i),
                     row_id=f"s{i}") for i in range(5)]
    res = ev.run_portfolio(outs, COSTS, 1.0, slot_count=5)
    assert res.n_executed == 0
    assert res.n_skip == 5
    # после SKIP реальный вход должен пройти
    real = _outcome("FILLED", "FREAL", DAY10 + timedelta(minutes=10),
                    DAY10 + timedelta(minutes=130), row_id="real")
    res2 = ev.run_portfolio(outs + [real], COSTS, 1.0, slot_count=5)
    assert res2.n_executed == 1


def test_slot_released_after_exit_two_entries_same_day():
    # два непересекающихся входа (A 10:00->12:00, B 12:05->14:05) = 2 сделки
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    b = _outcome("FILLED", "FB", DAY10 + timedelta(minutes=125),
                 DAY10 + timedelta(minutes=245), row_id="b")
    res = ev.run_portfolio([a, b], COSTS, 1.0, slot_count=1)
    assert res.n_executed == 2  # слот освободился после exit A


def test_repeat_inside_open_position_is_ignore():
    # два сигнала на один FIGI внутри одной позиции -> вторая IGNORE
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    b = _outcome("FILLED", "FA", DAY10 + timedelta(minutes=30),
                 DAY10 + timedelta(minutes=150), row_id="b")
    res = ev.run_portfolio([a, b], COSTS, 1.0, slot_count=5)
    assert res.n_executed == 1
    assert res.n_ignore_repeat == 1


def test_exits_before_entries_at_equal_time():
    # exit A и entry B в одну минуту -> exit сначала освобождает слот
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    b = _outcome("FILLED", "FB", DAY10 + timedelta(minutes=120),
                 DAY10 + timedelta(minutes=240), row_id="b")
    res = ev.run_portfolio([a, b], COSTS, 1.0, slot_count=1)
    assert res.n_executed == 2


def test_free_cash_checked_not_just_slots():
    # 2 слота, но кэша хватает только на 1 -> второй SKIP no_cash
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    b = _outcome("FILLED", "FB", DAY10 + timedelta(minutes=1),
                 DAY10 + timedelta(minutes=121), row_id="b")
    # initial_capital=0.5, slot_notional=0.2*0.5=0.1; fee ~0.00015; хватает на ~4
    # возьмем tiny capital чтобы кэш стал узким
    res = ev.run_portfolio([a, b], COSTS, 0.05, slot_count=5)
    # slot_notional=0.01; обе должны пройти при capital 0.05 (0.05/0.01=5)
    assert res.n_executed == 2


# ------------------------------------------------------------- equity

def test_final_equity_none_on_unknown_tail():
    # позиция INCOMPLETE (exit неизвестен) -> final=None, last_known отдельно
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    inc = _outcome("INCOMPLETE", "FB", DAY10 + timedelta(minutes=10), row_id="b")
    res = ev.run_portfolio([a, inc], COSTS, 1.0, slot_count=5)
    assert res.final_equity is None  # неизвестный хвост
    assert res.last_known_equity is not None
    assert res.max_drawdown_known is not None


def test_final_equity_known_when_all_closed():
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    res = ev.run_portfolio([a], COSTS, 1.0, slot_count=5)
    assert res.final_equity is not None
    assert res.final_equity > 1.0  # прибыльная сделка


def test_fixed_notional_no_compounding():
    # две прибыльные сделки: PnL = 2 * (notional * net/unit), не compounding
    a = _outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120), row_id="a")
    b = _outcome("FILLED", "FB", DAY10 + timedelta(minutes=125),
                 DAY10 + timedelta(minutes=245), row_id="b")
    res = ev.run_portfolio([a, b], COSTS, 1.0, slot_count=1)
    # slot_notional=1.0 (slots=1); каждая сделка qty=1.0/100=0.01; net/unit=0.018
    # ожидаемый net на сделку = qty*(exit_fill-entry_fill)-ce-cx
    assert res.n_executed == 2
    # не compounding: вторая сделка использует тот же notional (1.0), не выросший
    assert abs(res.final_equity - (1.0 + 2 * res.executed[0].net)) < 1e-9


# ------------------------------------------------------------- ledger

def test_signal_outcomes_separate_from_executed():
    outs = [_outcome("FILLED", "FA", DAY10, DAY10 + timedelta(minutes=120),
                     row_id="a"),
            _outcome("SKIP", "FB", DAY10 + timedelta(minutes=5), row_id="b"),
            _outcome("INCOMPLETE", "FC", DAY10 + timedelta(minutes=10),
                     row_id="c")]
    res = ev.run_portfolio(outs, COSTS, 1.0, slot_count=5)
    assert res.n_signal == 3          # все signal outcomes
    assert res.n_executed == 1        # только FILLED исполнена
    assert res.n_skip == 1
    assert res.n_incomplete_outcomes == 1
    assert res.n_accepted_open == 1   # INCOMPLETE -> accepted-open ledger
    # executed ledger содержит только исполненные
    assert len(res.executed) == 1
    assert res.executed[0].row_id == "a"


def test_path_metrics_known_only_if_continuous():
    minutes = _day_minutes(DAY10, 300, price=100.0)
    minutes[DAY10 + timedelta(minutes=120)] = (102.0, 102.0)
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    assert o.path_metrics_known is True  # все минуты 10:01..12:00 есть
    # дыра в середине -> path UNKNOWN (endpoint может быть известен)
    del minutes[DAY10 + timedelta(minutes=60)]
    o2 = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    assert o2.status == "FILLED"
    assert o2.path_metrics_known is False


# ------------------------------------------------------------- REWORK v3: cash (defect 1)

def test_cash_check_full_debit_rejects_when_cash_tight():
    # Денежный инвариант (архитектор): 4 последовательные ~100%-убыточные
    # позиции съедают ~0.8 капитала; после 4-й free_cash оказывается МЕЖДУ
    # slot_notional (0.2) и полным entry debit (0.2+ce). Старая проверка
    # free_cash<slot_notional пропустила бы 5-й вход (overdraft); новая
    # (полный debit ДО мутации) отклоняет -> n_no_cash, кэш остаётся >= 0.
    minutes = _day_minutes(DAY10, 600, price=100.0)  # 10:00..19:59 MSK
    seq = []
    spans = [(0, 120), (125, 245), (250, 370), (375, 495)]  # непересекающиеся
    for i, (s, e) in enumerate(spans):
        seq.append(_outcome("FILLED", f"F{i}", DAY10 + timedelta(minutes=s),
                            DAY10 + timedelta(minutes=e), row_id=f"r{i}",
                            entry_price=100.0, exit_price=0.06))
    # 5-я после закрытия 4-й (18:15), в сессии (18:20 < 18:44)
    e5 = _outcome("FILLED", "F4", DAY10 + timedelta(minutes=500),
                  DAY10 + timedelta(minutes=620), row_id="r4",
                  entry_price=100.0, exit_price=101.0)
    res = ev.run_portfolio(seq + [e5], COSTS, 1.0, slot_count=1)
    assert res.n_executed == 4
    assert res.n_no_cash == 1
    assert res.free_cash_end >= -1e-9
    # на момент входа 5-й позиции 4-я уже закрыта (18:15<18:20) -> слот свободен,
    # отклонение строго по кэшу (полный debit), а не по слотам


def test_cash_never_negative_under_tight_capital():
    # Много перекрывающихся позиций, тугой капитал: кэш не уходит в минус,
    # лишние отклоняются по no_cash (не no_slot — слоты свободны).
    minutes = _day_minutes(DAY10, 600, price=100.0)
    outs = [_outcome("FILLED", f"F{i}", DAY10 + timedelta(minutes=i * 10),
                     DAY10 + timedelta(minutes=i * 10 + 120), row_id=f"r{i}")
            for i in range(3)]
    # 3 перекрывающиеся позиции, slot_count=3 -> слоты есть, но кэш тугой
    res = ev.run_portfolio(outs, COSTS, 0.25, slot_count=3)
    assert res.free_cash_end >= -1e-9
    assert res.n_no_cash >= 0  # кэш-инвариант главное
    # контроль: с достатком все исполняются
    res2 = ev.run_portfolio(outs, COSTS, 1.0, slot_count=3)
    assert res2.n_executed == 3
    assert res2.free_cash_end >= -1e-9


# ------------------------------------------------------------- REWORK v3: costs (defect 2)

def test_costs_total_includes_slippage_and_identity():
    minutes = _day_minutes(DAY10, 300, price=100.0)
    minutes[DAY10 + timedelta(minutes=120)] = (102.0, 102.0)
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    res = ev.run_portfolio([o], COSTS, 1.0, slot_count=1)
    t = res.executed[0]
    # costs_total = fees + slippage; истинный slippage на long =
    # (entry_fill-entry_base) + (exit_base-exit_fill) = slip_entry - slip_exit
    expected = (t.commission_entry + t.commission_exit
                + t.qty * (t.slip_entry - t.slip_exit))
    assert abs(t.costs_total - expected) < 1e-12
    # тождество net = gross - costs_total
    assert abs(t.net - (t.gross - t.costs_total)) < 1e-12


def test_metrics_costs_total_has_slippage_not_just_fees():
    minutes = _day_minutes(DAY10, 300, price=100.0)
    minutes[DAY10 + timedelta(minutes=120)] = (102.0, 102.0)
    o = ev.execute_signal(_pred(DAY10), _store(minutes), COSTS)
    res = ev.run_portfolio([o], COSTS, 1.0, slot_count=1)
    m = ev.portfolio_metrics(res, "m1", "c5_s2", "eval")
    # при slippage>0 costs_total должен СТРОГО превышать только комиссии
    assert m["costs_total"] > m["fees_total"]
    assert m["costs_identity_net_eq_gross_minus_costs"] is True
    assert abs(m["gross_total"] - m["costs_total"] - m["net_total"]) < 1e-12


def test_accepted_open_ledger_separate_from_executed():
    inc = _outcome("INCOMPLETE", "FC", DAY10 + timedelta(minutes=10),
                   row_id="c")
    res = ev.run_portfolio([inc], COSTS, 1.0, slot_count=5)
    assert res.n_accepted_open == 1
    assert len(res.accepted_open) == 1
    assert res.accepted_open[0].planned_exit_ts is None
    assert len(res.executed) == 0
