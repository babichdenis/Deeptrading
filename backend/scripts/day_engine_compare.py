#!/usr/bin/env python3
"""A9: CLI сравнения эталона A7 и EngineRunner на локальной синтетике (r2).

Прогоняет оба пути на одних и тех же 1m-данных и одном config/периоде/издержках,
сравнивает ТОЛЬКО те поля, которые реально есть у обеих сторон, и пишет
machine-readable артефакты в НОВЫЙ каталог:

  diff.csv          — по дню: категория (MATCH/EXPECTED_DIFFERENCE/BLOCKED), счётчики,
                      известный префикс, лишние engine-сделки, суммы net парных строк;
  field_diff.csv    — по (день, поле): ref/eng значения, available, match
                      (NOT_AVAILABLE там, где API движка поля не даёт);
  ref_trades.csv    — все записи A7 (FILLED и INCOMPLETE) как есть;
  ref_skips.csv     — все пропуски A7;
  engine_trades.csv — все сделки движка (включая лишние и engine-only выходы);
  signals.csv       — сигналы, реально эмитированные адаптером в движок;
  dq.csv, summary.json, manifest.json.

Это именно сравнение с существующим EngineRunner, а не подстановка A7-результата
под его именем. Никаких «залечиваний» округлением, удалением сделок или будущим
знанием. Время/side/статус сравниваются точно; цены/деньги — isclose(rel=abs=1e-9).

Классификация:
  MATCH                — A7 skip и движок без сделки; либо FILLED и все ДОСТУПНЫЕ поля
                         совпали; либо INCOMPLETE с полностью совпавшим известным входом
                         и без спора по известным полям НЕТ (см. ниже).
  EXPECTED_DIFFERENCE  — заранее описанная разница статуса/выхода при совпавшем известном
                         префиксе: (a) TP-gap, ДОКАЗАННЫЙ численно по свече фактического
                         выхода (A7 фиксирует raw TP, движок берёт open строго за TP;
                         fill/commission/slippage/net согласованы с CostModel и qty=1);
                         (b) A7 INCOMPLETE (путь неизвестен) против числового закрытия
                         движка (SL/TP/end_of_data) при совпавшем входе.
  BLOCKED              — отсутствующий/лишний/неверный вход; поздний выход на недопустимом
                         баре; необъяснённое числовое расхождение; A7 INCOMPLETE без входа
                         движка. Показывается с минимальным воспроизведением.

Примеры:
  python backend/scripts/day_engine_compare.py \\\n      --input backend/reports/.../inputs/EX01_s1_long_timeexit.csv \\\n      --warmup-start 2026-09-07 --eval-start 2026-09-08 --eval-end 2026-09-08 \\\n      --commission-bps 0 --slippage-bps 0 --configs S1-A \\\n      --out backend/reports/architect_20261009/b3/a9/<UTC_RUN_ID>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
for _p in (_BACKEND, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import day_strategies_core as a7  # noqa: E402
import day_engine_adapter as ad  # noqa: E402

ABS_TOL = 1e-9
REL_TOL = 1e-9
NET_VERDICT = (
    "NOT_ASSIGNED: WF/OOS и финальный holdout не выполнялись; это проверка "
    "совместимости путей A7 и EngineRunner на синтетике, не торговое преимущество."
)
DIFF_FIELDS = ("run_id", "ticker", "config", "day", "category", "ref_status",
               "eng_trades", "extra_engine_trades", "known_prefix_match",
               "ref_net", "eng_net", "diffs", "note")
FIELD_FIELDS = ("run_id", "ticker", "config", "day", "field", "ref", "eng",
                "available", "match")
ENGINE_TRADE_FIELDS = ("run_id", "ticker", "config", "trade_id", "side", "entry_time",
                       "entry_price", "initial_stop", "take_profit", "exit_time",
                       "exit_price", "bars_held", "gross_pnl", "commission", "slippage",
                       "net_pnl", "exit_reason")
SIGNAL_FIELDS = ("run_id", "ticker", "config", "day", "kind", "time", "side", "reason",
                 "intended_entry_ts", "atr")
DQ_FIELDS = ("ticker", "kind", "code", "detail")
ENGINE_SOURCE_FILES = ("runner.py", "models.py", "costs.py", "exits.py", "policies.py",
                       "views.py", "sessions.py", "ledger.py")
ENTRY_FIELDS = {"side", "signal_time", "entry_time", "entry_fill", "sl_level", "tp_level"}
ENGINE_EXIT_CATEGORY = {
    "stop_loss": "stop",
    "target": "tp",
    "signal_exit": "time",
    "session_close": "session",
    "end_of_data": "end_of_data",
}


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается дата YYYY-MM-DD: {s!r}") from None


def _approx(a, b) -> bool:
    return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=ABS_TOL)


def _num(v):
    return None if v in ("", None) else float(v)


def _a7_cat(reason: str) -> str:
    if not reason:
        return ""
    if reason.startswith("sl_"):
        return "stop"
    if reason.startswith("tp_"):
        return "tp"
    if reason.startswith("time_exit"):
        return "time"
    return reason


def _eng_cat(reason: str) -> str:
    return ENGINE_EXIT_CATEGORY.get((reason or "").lower(), reason)


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _fmt(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _load_ticker(paths):
    by_ticker: dict = {}
    issues: list = []
    sources: list = []
    for p in paths:
        tick, dq, sha = a7.load_minute_csv(p)
        sources.append({"path": str(p), "sha256": sha})
        issues.extend(dq)
        for t, rows in tick.items():
            if t in by_ticker:
                raise SystemExit(f"ticker {t!r} в нескольких файлах — объединение не поддержано")
            by_ticker[t] = rows
    return by_ticker, issues, sources


def _values_match(field: str, rv, ev) -> bool:
    if field == "side":
        return str(rv).lower() == str(ev).lower()
    if field in ("sl_level", "tp_level"):
        a, b = _num(rv), _num(ev)
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return _approx(a, b)
    if field in ("entry_fill", "exit_fill", "gross_fill_delta", "commission", "slippage", "net"):
        return _approx(rv, ev)
    return rv == ev


def _field_compare(run_id, ticker, cfgname, day, ref, trade, info, mode):
    rows: list = []
    diffs: list = []
    filled = mode == "filled"
    have = trade is not None

    def add(field, rv, ev, available=True, match_override=None):
        if not have:
            available = False
        match = match_override if match_override is not None else (
            _values_match(field, rv, ev) if available else None)
        rows.append({"run_id": run_id, "ticker": ticker, "config": cfgname,
                     "day": day.isoformat(), "field": field, "ref": _fmt(rv),
                     "eng": _fmt(ev), "available": available, "match": match})
        if available and match is False:
            diffs.append((field, rv, ev))

    sig_eng = info["signal_time"].isoformat() if info else "NOT_AVAILABLE"
    add("signal_time", ref.get("signal_time"), sig_eng,
        available=info is not None and have)
    add("side", ref.get("side"), str(trade.side) if have else "NOT_AVAILABLE")
    add("entry_time", ref.get("entry_ts"),
        trade.entry_time.isoformat() if have else "NOT_AVAILABLE")
    add("entry_base", ref.get("entry_base"), "NOT_AVAILABLE", available=False)
    add("entry_fill", ref.get("entry_fill"),
        trade.entry_price if have else "NOT_AVAILABLE")
    add("sl_level", ref.get("sl_level"),
        trade.initial_stop if have else "NOT_AVAILABLE")
    add("tp_level", ref.get("tp_level"),
        trade.take_profit if have else "NOT_AVAILABLE")
    add("exit_time", ref.get("exit_ts"),
        trade.exit_time.isoformat() if have else "NOT_AVAILABLE", available=filled)
    add("exit_base", ref.get("exit_base"), "NOT_AVAILABLE", available=False)
    add("exit_fill", ref.get("exit_fill"),
        trade.exit_price if have else "NOT_AVAILABLE", available=filled)
    if filled:
        add("reason", ref.get("exit_reason"), str(trade.exit_reason), available=True,
            match_override=_a7_cat(ref.get("exit_reason")) == _eng_cat(str(trade.exit_reason)))
    else:
        add("reason", ref.get("exit_reason"),
            str(trade.exit_reason) if have else "NOT_AVAILABLE", available=False)
    add("gross_base", ref.get("gross"), "NOT_AVAILABLE", available=False)
    if filled:
        side = 1 if ref["side"] == "long" else -1
        fd = side * (float(ref["exit_fill"]) - float(ref["entry_fill"]))
        add("gross_fill_delta", fd, trade.gross_pnl)
        add("commission", float(ref["commission_entry"]) + float(ref["commission_exit"]),
            trade.commission)
        add("slippage", abs(float(ref["slip_entry"])) + abs(float(ref["slip_exit"])),
            trade.slippage)
        add("net", float(ref["net"]), trade.net_pnl)
    else:
        add("gross_fill_delta", "NOT_AVAILABLE", "NOT_AVAILABLE", available=False)
        add("commission", "NOT_AVAILABLE", "NOT_AVAILABLE", available=False)
        add("slippage", "NOT_AVAILABLE", "NOT_AVAILABLE", available=False)
        add("net", "NOT_AVAILABLE", "NOT_AVAILABLE", available=False)
    add("status", ref.get("status"), "filled", available=False,
        match_override=(ref.get("status") == "FILLED"))
    return rows, diffs


def _tp_gap_proof(ref, trade, candle_by_ts, costs, diffs):
    """Численное доказательство TP-gap по фактической свече выхода движка.

    Возвращает (proven: bool, detail: str, evidence: dict). Проверяет:
    1. ref exit_reason == "tp_gap_open" (A7 зафиксировал gap через TP).
    2. engine exit_reason == "target" (движок исполнил TP).
    3. Фактический gap: свеча engine exit имеет open строго за raw TP
       в направлении улучшения цены.
    4. Согласование engine fill/commission/slippage/net с CostModel и qty=1.
    5. Разницы commission/slippage/net между A7 и engine объяснены гэпом.
    Если хоть одно условие не выполняется — gap не доказан (BLOCKED).
    Базовая цена движка НЕ берётся из эталона.
    """
    ev: dict = {}
    if ref.get("exit_reason") != "tp_gap_open":
        return False, f"ref exit_reason = {ref.get('exit_reason')!r} != tp_gap_open", ev
    if str(trade.exit_reason).lower() != "target":
        return False, f"engine exit_reason = {str(trade.exit_reason)!r} != target", ev
    tp = _num(ref.get("tp_level"))
    if tp is None:
        return False, "ref tp_level отсутствует", ev
    candle = candle_by_ts.get(trade.exit_time)
    if candle is None:
        return False, "нет свечи выхода в engine input (evidence недоступно)", ev
    open_ = float(candle.open)
    entry_sign = 1 if ref["side"] == "long" else -1
    exit_side = -entry_sign
    if entry_sign == 1 and not open_ > tp:
        return False, f"LONG open={open_} не строго за TP={tp}", ev
    if entry_sign == -1 and not open_ < tp:
        return False, f"SHORT open={open_} не строго за TP={tp}", ev
    ev["exit_open"] = open_
    ev["tp_level"] = tp
    slip_exit = _num(ref.get("slip_exit"))
    if slip_exit is None:
        return False, "ref slip_exit отсутствует", ev
    a7_fill = costs.fill(tp, exit_side)
    if not _approx(ref["exit_fill"], a7_fill):
        return False, f"A7 exit_fill={ref['exit_fill']} != fill(tp_level)={a7_fill}", ev
    if not _approx(slip_exit, float(ref["exit_fill"]) - tp):
        return False, "A7 slip_exit не согласован с tp_level", ev
    eng_fill = costs.fill(open_, exit_side)
    if not _approx(trade.exit_price, eng_fill):
        return False, f"engine exit_price={trade.exit_price} != fill(open)={eng_fill}", ev
    d_exit = float(trade.exit_price) - float(ref["exit_fill"])
    d_gross = entry_sign * d_exit
    d_comm = costs.commission(trade.exit_price) - costs.commission(float(ref["exit_fill"]))
    d_slip = abs(float(trade.exit_price) - open_) - abs(float(ref["exit_fill"]) - tp)
    d_net = d_gross - d_comm
    expected = {"exit_fill": d_exit, "gross_fill_delta": d_gross,
                "commission": d_comm, "slippage": d_slip, "net": d_net}
    ev["expected_deltas"] = {k: v for k, v in expected.items()}
    ref_fd = entry_sign * (float(ref["exit_fill"]) - float(ref["entry_fill"]))
    actual_all = {
        "exit_fill": float(trade.exit_price) - float(ref["exit_fill"]),
        "gross_fill_delta": float(trade.gross_pnl) - ref_fd,
        "commission": float(trade.commission) - (float(ref["commission_entry"]) + float(ref["commission_exit"])),
        "slippage": float(trade.slippage) - (abs(float(ref["slip_entry"])) + abs(float(ref["slip_exit"]))),
        "net": float(trade.net_pnl) - float(ref["net"]),
    }
    ev["actual_deltas"] = actual_all
    for field, actual in actual_all.items():
        if not _approx(actual, expected[field]):
            return False, f"поле {field}: дельта {actual!r} != ожидаемой {expected[field]!r}", ev
    for f, rv, engv in diffs:
        if f not in expected:
            return False, f"различие в неразрешённом поле {f}", ev
        actual = float(engv) - float(rv)
        ev["actual_deltas"][f] = actual
        if not _approx(actual, expected[f]):
            return False, (f"поле {f}: фактическая дельта {actual!r} != ожидаемой "
                           f"{expected[f]!r}"), ev
    detail = (f"TP-gap доказан численно: candle open={open_!r} строго за TP={tp!r}; "
              f"A7 fill=fill(TP)={a7_fill!r}, engine fill=fill(open)={trade.exit_price!r}; "
              f"дельты {sorted(expected)} согласованы с CostModel/qty=1")
    return True, detail, ev


def _tpgap_selftest() -> list:
    """Три ручных случая к TP-gap: zero-cost, 5/2 bps и испорченный net."""
    ts = datetime(2026, 9, 8, 7, 31, tzinfo=timezone.utc)
    candle = ad.Candle(ts=ts, open=96.5, high=97.0, low=96.0, close=96.4, volume=1.0)
    by_ts = {ts: candle}

    def ref_for(costs):
        exit_side = 1
        exit_fill = costs.fill(97.0, exit_side)
        entry_fill = costs.fill(100.0, -1)
        ce = costs.commission(entry_fill)
        cx = costs.commission(exit_fill)
        return {
            "signal_time": ts.isoformat(), "side": "short", "entry_ts": ts.isoformat(),
            "entry_base": 100.0, "entry_fill": entry_fill, "sl_level": 102.0,
            "tp_level": 97.0, "exit_ts": ts.isoformat(), "exit_base": 97.0,
            "exit_fill": exit_fill, "exit_reason": "tp_gap_open", "gross": 3.0,
            "commission_entry": ce, "commission_exit": cx,
            "slip_entry": entry_fill - 100.0, "slip_exit": exit_fill - 97.0,
            "net": -1.0 * (exit_fill - entry_fill) - (ce + cx), "status": "FILLED",
            "incomplete_reason": "",
        }

    def trade_for(costs, net_override=None):
        exit_fill = costs.fill(96.5, 1)
        entry_fill = costs.fill(100.0, -1)
        gross = -1.0 * (exit_fill - entry_fill)
        comm = costs.commission(entry_fill) + costs.commission(exit_fill)
        slip = abs(entry_fill - 100.0) + abs(exit_fill - 96.5)
        net = gross - comm if net_override is None else net_override
        return SimpleNamespace(side="SHORT", entry_time=ts, exit_time=ts,
                               entry_price=entry_fill, initial_stop=102.0,
                               take_profit=97.0, exit_price=exit_fill,
                               exit_reason="target", gross_pnl=gross, commission=comm,
                               slippage=slip, net_pnl=net)

    cases = (
        ("zero_cost_gap", a7.CostParams(0.0, 0.0), None, True),
        ("costs_5_2_gap", a7.CostParams(5.0, 2.0), None, True),
        ("corrupted_net_gap", a7.CostParams(0.0, 0.0), -997.0, False),
    )
    results = []
    for name, costs, net_override, expect in cases:
        ref = ref_for(costs)
        tr = trade_for(costs, net_override)
        _, diffs = _field_compare("selftest", "SYN", "S2-A", a7.msk_date(ts), ref, tr,
                                  None, "filled")
        proven, detail, ev = _tp_gap_proof(ref, tr, by_ts, costs, diffs)
        results.append({"case": name, "expected_proven": expect, "proven": proven,
                        "passed": proven is expect, "detail": detail,
                        "diffs": [f for f, _, _ in diffs], "evidence": ev})
    return results


def _classify(ref, eng, diffs, extra_engine, candle_by_ts, costs) -> tuple:
    no_ref = ref is None or "status" not in ref
    if no_ref:
        if eng:
            reason = ref["reason"] if ref is not None else "no_ref_record"
            note = (f"A7 skip ({reason}); движок вошёл там, где A7 не входит. "
                    "Исполнение pending на следующем баре без сверки entry_ts "
                    "-> нужен Signal.execute_at (см. отчёт).")
            return "BLOCKED", note
        return "MATCH", "оба без сделки (A7 skip)"
    status = ref["status"]
    if status == "FILLED":
        if not eng:
            return "BLOCKED", "эталон FILLED, движок без сделки"
        if len(eng) > 1:
            return "BLOCKED", f"движок дал {len(eng)} сделок вместо 1"
        if not diffs:
            return "MATCH", ""
        proven, detail, _ = _tp_gap_proof(ref, eng[0], candle_by_ts, costs, diffs)
        if proven:
            return "EXPECTED_DIFFERENCE", detail
        fields = ", ".join(f for f, _, _ in diffs)
        return "BLOCKED", (f"числовое расхождение не объяснено ({detail}); "
                           f"поля: {fields}")
    if status == "INCOMPLETE":
        if not eng:
            return "BLOCKED", ("A7 INCOMPLETE (вход зафиксирован), движок без входа/сделки "
                               "— известный вход не воспроизведён")
        if len(eng) > 1:
            return "BLOCKED", f"движок дал {len(eng)} сделок вместо 1"
        entry_diffs = [d for d in diffs if d[0] in ENTRY_FIELDS]
        if entry_diffs:
            fields = ", ".join(f for f, _, _ in entry_diffs)
            return "BLOCKED", f"известный вход не совпал: {fields}"
        if ref.get("incomplete_reason") == "time_exit_minute_18_30_missing" and \
                str(eng[0].exit_reason).lower() in ("signal_exit", "session_close"):
            return "BLOCKED", ("A7 INCOMPLETE time_exit_minute_18_30_missing; движок закрыл "
                               "на недопустимом баре (18:30 отсутствует) -> нужен "
                               "Signal.execute_at; поздний exit оставил бы позицию открытой.")
        return "EXPECTED_DIFFERENCE", (
            f"известный вход совпал; A7 INCOMPLETE ({ref.get('incomplete_reason')}) — "
            f"exit/net неизвестны и не сравниваются; движок закрыл числом "
            f"({eng[0].exit_reason}).")
    return "BLOCKED", f"неизвестный статус A7: {status!r}"


def _run_one(run_id, ticker, rows, cfg, warmup_start, eval_start, eval_end, costs):
    issues: list = []
    tdata = a7.build_ticker_data(ticker, rows, warmup_start, issues, warmup_end=eval_start)
    ref_trades, ref_skips = a7.run_ticker(tdata, [cfg], eval_start, eval_end, costs)
    candles = ad.minutes_to_candles(rows)
    candle_by_ts = {c.ts: c for c in candles}
    strategy, ledger = ad.run_engine(
        ticker, candles, cfg, warmup_start, eval_start, eval_end, costs,
    )
    eng_by_day: dict = {}
    for tr in ledger.trades:
        eng_by_day.setdefault(a7.msk_date(tr.entry_time), []).append(tr)
    ref_by_day = {date.fromisoformat(r["day"]): r for r in ref_trades}
    ref_skip_by_day = {date.fromisoformat(r["day"]): r for r in ref_skips}
    days = sorted(set(ref_by_day) | set(ref_skip_by_day) | set(eng_by_day))

    rows_out: list = []
    fields_out: list = []
    for day in days:
        ref = ref_by_day.get(day) or ref_skip_by_day.get(day)
        eng = eng_by_day.get(day, [])
        info = strategy.entry_info.get(day)
        diffs: list = []
        extra_engine = 0
        if ref is not None and ref.get("status") == "FILLED" and len(eng) == 1:
            fr, diffs = _field_compare(run_id, ticker, cfg.config, day, ref, eng[0], info,
                                       "filled")
            fields_out.extend(fr)
        elif ref is not None and ref.get("status") == "INCOMPLETE" and len(eng) == 1:
            fr, diffs = _field_compare(run_id, ticker, cfg.config, day, ref, eng[0], info,
                                       "incomplete")
            fields_out.extend(fr)
        elif not eng:
            if ref is not None and "status" in ref:
                fr, _ = _field_compare(run_id, ticker, cfg.config, day, ref, None, info,
                                       "missing")
                fields_out.extend(fr)
        else:
            extra_engine = len(eng) - (1 if ref is not None and "status" in ref else 0)
        known_prefix = bool(not any(d[0] in ENTRY_FIELDS for d in diffs))
        category, note = _classify(ref, eng, diffs, extra_engine, candle_by_ts, costs)
        diff_txt = " ; ".join(f"{f}: ref={_fmt(a)} eng={_fmt(b)}" for f, a, b in diffs)
        ref_net = eng_net = ""
        if ref is not None and ref.get("status") == "FILLED" and len(eng) == 1:
            ref_net = ref["net"]
            eng_net = eng[0].net_pnl
        skip_reason = ref.get("reason") if ref is not None else None
        note_suffix = ""
        if category == "BLOCKED" and extra_engine > 0 and ref is not None and \
                ref.get("status") == "INCOMPLETE":
            note_suffix = "; engine-only выход: " + ",".join(
                str(t.exit_reason) for t in eng)
        rows_out.append({
            "run_id": run_id, "ticker": ticker, "config": cfg.config,
            "day": day.isoformat(), "category": category,
            "ref_status": (ref.get("status") or f"skip:{skip_reason}") if ref else "",
            "eng_trades": len(eng), "extra_engine_trades": max(extra_engine, 0),
            "known_prefix_match": known_prefix,
            "ref_net": ref_net, "eng_net": eng_net,
            "diffs": diff_txt,
            "note": note + note_suffix,
        })

    ref_rows = [{"run_id": run_id, **r} for r in ref_trades]
    skip_rows = [{"run_id": run_id, **r} for r in ref_skips]
    eng_rows = [{
        "run_id": run_id, "ticker": ticker, "config": cfg.config, "trade_id": t.trade_id,
        "side": t.side, "entry_time": t.entry_time.isoformat(), "entry_price": t.entry_price,
        "initial_stop": t.initial_stop, "take_profit": t.take_profit,
        "exit_time": t.exit_time.isoformat(), "exit_price": t.exit_price,
        "bars_held": t.bars_held, "gross_pnl": t.gross_pnl, "commission": t.commission,
        "slippage": t.slippage, "net_pnl": t.net_pnl, "exit_reason": t.exit_reason,
    } for t in ledger.trades]
    signal_rows = []
    for s in strategy.signals:
        d = a7.msk_date(s.time)
        info = strategy.entry_info.get(d, {})
        signal_rows.append({
            "run_id": run_id, "ticker": ticker, "config": cfg.config, "day": d.isoformat(),
            "kind": s.kind, "time": s.time.isoformat(), "side": str(s.side),
            "reason": s.reason,
            "intended_entry_ts": info.get("entry_ts").isoformat() if info.get("entry_ts") else "",
            "atr": info.get("atr", ""),
        })
    return rows_out, fields_out, ref_rows, skip_rows, eng_rows, signal_rows, issues


def _causality_check(ticker, rows, cfg, warmup_start, eval_start, eval_end, costs):
    candles = ad.minutes_to_candles(rows)
    hub = ad._PlanHub()
    strategy = ad.DayEngineStrategy(cfg, warmup_start, eval_start, eval_end, hub)
    from app.engine.runner import EngineRunner
    runner = EngineRunner(
        strategy, ad.DayExitPolicy(hub, costs.slippage_bps),
        ad.build_engine_config(ticker, costs),
    )
    led1 = runner.run(candles)
    fp1 = led1.fingerprint()
    led2 = runner.run(candles)
    reset_ok = fp1 == led2.fingerprint()
    full_trades = list(led1.trades)
    full_signals = list(strategy.signals)
    suffix_ok = True
    detail = []
    for k in (5, 20):
        if len(candles) <= k + 2:
            continue
        cut = len(candles) - k
        boundary = candles[cut - 1].ts
        st_cut, led_cut = ad.run_engine(
            ticker, candles[:cut], cfg, warmup_start, eval_start, eval_end, costs,
        )
        ft = [t for t in full_trades if t.exit_time < boundary]
        ct = [t for t in led_cut.trades if t.exit_time < boundary]
        fs = [s for s in full_signals if s.time < boundary]
        cs = [s for s in st_cut.signals if s.time < boundary]
        trades_same = len(ft) == len(ct) and all(
            (a.entry_time, a.exit_time, a.entry_price, a.exit_price, a.exit_reason,
             a.net_pnl) == (b.entry_time, b.exit_time, b.entry_price, b.exit_price,
                            b.exit_reason, b.net_pnl)
            for a, b in zip(ft, ct))
        signals_same = len(fs) == len(cs) and all(
            (a.kind, a.time, str(a.side), a.reason) == (b.kind, b.time, str(b.side), b.reason)
            for a, b in zip(fs, cs))
        same = trades_same and signals_same
        suffix_ok = suffix_ok and same
        detail.append({"dropped_minutes": k, "prefix_trades": len(ft),
                       "prefix_signals": len(fs), "trades_identical": trades_same,
                       "signals_identical": signals_same, "identical": same})
    return {"config": cfg.config, "reset_identical": reset_ok,
            "suffix_invariant": suffix_ok, "suffix": detail}


def _write_csv(path: Path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if isinstance(r, dict):
                row = {k: _fmt(r.get(k, "")) for k in fields}
            else:
                row = {k: _fmt(getattr(r, k, "")) for k in fields}
            w.writerow(row)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A9 r2: сравнение A7 и EngineRunner (offline)")
    ap.add_argument("--a7-run", action="append", type=Path, default=[],
                    help="каталог прогона A7 r2 с manifest.json (можно повторять)")
    ap.add_argument("--input", action="append", default=[])
    ap.add_argument("--warmup-start", type=_parse_date)
    ap.add_argument("--eval-start", type=_parse_date)
    ap.add_argument("--eval-end", type=_parse_date)
    ap.add_argument("--commission-bps", type=float)
    ap.add_argument("--slippage-bps", type=float)
    ap.add_argument("--configs", nargs="+", default=None,
                    choices=[c.config for c in a7.CONFIGS])
    ap.add_argument("--data-note", default="")
    ap.add_argument("--causality-check", action="store_true",
                    help="проверить reset и инвариантность к будущему суффиксу по каждому config")
    ap.add_argument("--out", type=Path, required=True,
                    help="НОВЫЙ каталог результатов (существующий непустой запрещён)")
    args = ap.parse_args(argv)

    jobs = []
    if args.a7_run:
        for d in args.a7_run:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            conf = man["config"]
            inputs = []
            for it in man.get("inputs", []):
                p = Path(it["path"])
                if not p.exists() and (d / "inputs" / Path(it["path"]).name).exists():
                    p = d / "inputs" / Path(it["path"]).name
                inputs.append(p)
            jobs.append({
                "run_id": man.get("run_id", d.name), "inputs": inputs,
                "warmup_start": date.fromisoformat(conf["warmup_start"]),
                "eval_start": date.fromisoformat(conf["eval_start"]),
                "eval_end": date.fromisoformat(conf["eval_end"]),
                "commission_bps": float(conf["commission_bps"]),
                "slippage_bps": float(conf["slippage_bps"]),
                "configs": list(conf.get("configs") or [c.config for c in a7.CONFIGS]),
                "data_note": man.get("data_note", ""),
            })
    else:
        if not args.input:
            ap.error("нужен --a7-run или --input")
        missing = [n for n in ("warmup_start", "eval_start", "eval_end",
                               "commission_bps", "slippage_bps")
                   if getattr(args, n) is None]
        if missing:
            ap.error("обязательны: " + ", ".join("--" + m.replace("_", "-") for m in missing))
        jobs.append({
            "run_id": "cli", "inputs": [Path(p) for p in args.input],
            "warmup_start": args.warmup_start, "eval_start": args.eval_start,
            "eval_end": args.eval_end, "commission_bps": args.commission_bps,
            "slippage_bps": args.slippage_bps,
            "configs": args.configs or [c.config for c in a7.CONFIGS],
            "data_note": args.data_note,
        })
    if args.data_note:
        for j in jobs:
            j["data_note"] = args.data_note

    diff_rows: list = []
    field_rows: list = []
    ref_rows: list = []
    skip_rows: list = []
    eng_rows: list = []
    signal_rows: list = []
    all_issues: list = []
    source_list: list = []
    causalities: dict = {}

    for job in jobs:
        costs = a7.CostParams(job["commission_bps"], job["slippage_bps"])
        costs.validate()
        cfg_set = set(job["configs"])
        cfgs = [c for c in a7.CONFIGS if c.config in cfg_set]
        by_ticker, issues, sources = _load_ticker(job["inputs"])
        all_issues.extend(issues)
        source_list.extend(sources)
        if not by_ticker:
            raise SystemExit("нет валидных строк ни в одном входном файле")
        for ticker in sorted(by_ticker):
            rows = by_ticker[ticker]
            for cfg in cfgs:
                r, fr, rt, rs, et, sg, dq = _run_one(
                    job["run_id"], ticker, rows, cfg, job["warmup_start"],
                    job["eval_start"], job["eval_end"], costs)
                diff_rows.extend(r)
                field_rows.extend(fr)
                ref_rows.extend(rt)
                skip_rows.extend(rs)
                eng_rows.extend(et)
                signal_rows.extend(sg)
                all_issues.extend(dq)
                if args.causality_check:
                    for _ticker in sorted(by_ticker):
                        for _cfg in cfgs:
                            key = f"{job['run_id']}:{_ticker}:{_cfg.config}"
                            causalities[key] = _causality_check(
                                _ticker, by_ticker[_ticker], _cfg,
                                job["warmup_start"], job["eval_start"],
                                job["eval_end"], costs)

    out = args.out
    if out.exists() and any(out.iterdir()):
        ap.error(f"--out {out} существует и не пуст (immutable-каталог)")
    out.mkdir(parents=True, exist_ok=True)

    _write_csv(out / "diff.csv", DIFF_FIELDS,
               sorted(diff_rows, key=lambda x: (x["run_id"], x["ticker"], x["config"], x["day"])))
    _write_csv(out / "field_diff.csv", FIELD_FIELDS, field_rows)
    ref_fields = ("run_id",) + tuple(a7.TRADE_FIELDS)
    _write_csv(out / "ref_trades.csv", ref_fields, ref_rows)
    _write_csv(out / "ref_skips.csv", ("run_id",) + tuple(a7.SKIP_FIELDS), skip_rows)
    _write_csv(out / "engine_trades.csv", ENGINE_TRADE_FIELDS, eng_rows)
    _write_csv(out / "signals.csv", SIGNAL_FIELDS, signal_rows)
    _write_csv(out / "dq.csv", DQ_FIELDS, all_issues)

    selftest = _tpgap_selftest()
    (out / "tpgap_selftest.json").write_text(
        json.dumps(selftest, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")

    totals = {"MATCH": 0, "EXPECTED_DIFFERENCE": 0, "BLOCKED": 0}
    for r in diff_rows:
        totals[r["category"]] += 1
    filled = [r for r in diff_rows if r["ref_status"] == "FILLED"]
    matched_filled = [r for r in filled if r["category"] == "MATCH"]

    def _sum(rows, key):
        return round(sum(float(r[key]) for r in rows if r[key] != ""), 10)

    engine_only = [r for r in diff_rows if r["ref_status"] == "" and r["eng_trades"] > 0]
    summary = {
        "data_note": jobs[0]["data_note"] if jobs else "",
        "runs": [j["run_id"] for j in jobs],
        "tolerance": {"rel": REL_TOL, "abs": ABS_TOL,
                      "time_side_status": "exact"},
        "counts": {"days_compared": len(diff_rows), "filled_ref": len(filled),
                   "filled_match_available_fields": len(matched_filled),
                   "dq_issues": len(all_issues),
                   "engine_only_days": len(engine_only), "totals": totals},
        "parity": {
            "full_field_parity_confirmed": False,
            "match_scope": "MATCH = совпадение ТОЛЬКО доступных полей; у движка raw "
                           "entry_base/exit_base/gross_base NOT_AVAILABLE, поэтому "
                           "полная field-parity не подтверждена",
            "engine_raw_base_available": False,
        },
        "tpgap_selftest": {
            "cases": [{"case": c["case"], "passed": c["passed"]} for c in selftest],
            "all_passed": all(c["passed"] for c in selftest),
        },
        "net": {
            "scope": "сумма ТОЛЬКО по парным строкам ref FILLED (по одной engine-сделке); "
                     "engine-only/EOD-закрытия и INCOMPLETE вне этой суммы",
            "paired_filled_ref_net_sum": _sum(filled, "ref_net"),
            "paired_filled_engine_net_sum": _sum(filled, "eng_net"),
            "matched_pairs_ref_net_sum": _sum(matched_filled, "ref_net"),
        },
        "causality": causalities,
        "acceptance": {
            "filled_parity_all_fields": "NOT_CONFIRMED (сравниваются только доступные поля)",
            "blocked": totals["BLOCKED"],
            "note": "сводный net по сделкам смотрится в diff.csv; пустой набор сделок "
                    "приёмкой не является; execute_at сам по себе полную parity не "
                    "закрывает; TP-gap доказывается численно (tpgap_selftest.json)",
        },
        "net_verdict": NET_VERDICT,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8")

    outputs = {}
    for name in ("diff.csv", "field_diff.csv", "ref_trades.csv", "ref_skips.csv",
                 "engine_trades.csv", "signals.csv", "dq.csv", "tpgap_selftest.json",
                 "summary.json"):
        outputs[name] = _sha256_file(out / name)
    engine_dir = _BACKEND / "app" / "engine"
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "day_engine_compare.py",
        "adapter": "day_engine_adapter.py",
        "python": sys.version.split()[0],
        "data_note": summary["data_note"],
        "params": [{"run_id": j["run_id"],
                    "warmup_start": j["warmup_start"].isoformat(),
                    "eval_start": j["eval_start"].isoformat(),
                    "eval_end": j["eval_end"].isoformat(),
                    "commission_bps": j["commission_bps"],
                    "slippage_bps": j["slippage_bps"],
                    "configs": j["configs"]} for j in jobs],
        "code_sha256": {
            "day_engine_adapter.py": _sha256_file(_HERE / "day_engine_adapter.py"),
            "day_engine_compare.py": _sha256_file(_HERE / "day_engine_compare.py"),
            "day_strategies_core.py": _sha256_file(_HERE / "day_strategies_core.py"),
            "day_strategies_replay.py": _sha256_file(_HERE / "day_strategies_replay.py"),
        },
        "engine_sha256": {name: _sha256_file(engine_dir / name)
                          for name in ENGINE_SOURCE_FILES},
        "inputs": source_list,
        "outputs_sha256": outputs,
        "counts": summary["counts"],
        "parity": summary["parity"],
        "tpgap_selftest": summary["tpgap_selftest"],
        "net": summary["net"],
        "causality": causalities,
        "net_verdict": NET_VERDICT,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8")

    print(f"out={out}")
    print(f"days_compared={len(diff_rows)} filled_ref={len(filled)} "
          f"filled_match_available_fields={len(matched_filled)} engine_only={len(engine_only)}")
    print(f"MATCH={totals['MATCH']} EXPECTED_DIFFERENCE={totals['EXPECTED_DIFFERENCE']} "
          f"BLOCKED={totals['BLOCKED']}")
    st_ok = sum(1 for c in selftest if c["passed"])
    print(f"tpgap_selftest: {st_ok}/{len(selftest)} passed "
          f"({', '.join(c['case'] + '=' + str(c['passed']) for c in selftest)})")
    if causalities:
        n_ok = sum(1 for c in causalities.values()
                   if c["reset_identical"] and c["suffix_invariant"])
        print(f"causality: {n_ok}/{len(causalities)} reset+suffix OK")
    print(NET_VERDICT)
    return 0 if totals["BLOCKED"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
