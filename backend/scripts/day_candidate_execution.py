#!/usr/bin/env python3
"""A24: изолированный adapter исполнения кандидатов C1/C2/C3 (только synthetic).

Принимает ПРИНЯТЫЙ A18 signal event, валидированный TickerData (A7) и CostParams.
Не генерирует сигнал повторно и не изменяет его на основании будущего пути.
Путь позиции — ТОЛЬКО существующий A7 `_walk` (gap SL по open, gap TP по raw
уровню, SL раньше TP внутри бара, time-exit 18:30 при полном 18:20); никакого
копирования/нового движка. Accounting — те же формулы A7 (CostParams.fill /
commission; gross = side*(exit_base-entry_base); fill_delta = side*(exit_fill-
entry_fill); net = fill_delta - ce - cx; costs_total = ce + cx + (gross -
fill_delta) — тождество net = gross - costs_total по построению).

Статусы ledger (семантика A7):
  FILLED     — достоверный выход; все fills/fees/net известны;
  INCOMPLETE — вход известен (entry/fills входа сохранены), exit/net неизвестны:
               первая отсутствующая минута от входа, необеспеченный плановый
               выход, обрыв данных (при _walk None — ровно политика A7: НЕ
               закрывать последним close);
  SKIP       — вход отсутствует: open РОВНО intended_entry_time нет -> SKIP
               missing_entry, без переноса/повторного входа (следующая минута
               вход не заменяет);
  REJECT     — невалидный принимаемый signal или невалидные стоимости
               (явный код и detail, без мутации; machine-status, не исключение).

Вход: цена = open минуты РОВНО intended_entry_time. SL = raw entry - side*2*ATR,
TP = raw entry + side*3*ATR для ОБОИХ C1/C2 (множители A7; ATR — из принятого
сигнала, не пересчитывается). Одна позиция на candidate/ticker/day (по построению
принимается ровно один signal на день). Стоимости: CostParams.validate().
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

import day_strategies_core as a7
import intraday_trend_candidate

CANDIDATE_SPEC_HASH = "2a1ecab0ce437f643bae2a541a7abc306728c2aa1347f78e6a7ed27ae876f9e7"
TREND_SPEC_HASH = intraday_trend_candidate.TREND_SPEC_HASH
CANDIDATE_SPEC_HASHES = {
    "C1": CANDIDATE_SPEC_HASH,
    "C2": CANDIDATE_SPEC_HASH,
    intraday_trend_candidate.CANDIDATE_ID: TREND_SPEC_HASH,
}
CANDIDATES = tuple(sorted(CANDIDATE_SPEC_HASHES))


def register_specs(mapping: dict) -> None:
    """Совместимо регистрирует spec hashes новых кандидатов (BATCH-15: B01..B15).

    Не меняет существующие C1/C2/C3; отклоняет попытку переписать чужой hash.
    Регистрация не зависит от порядка imports — вызывается раннером после загрузки
    нужной группы.
    """
    for cid, h in mapping.items():
        if cid in CANDIDATE_SPEC_HASHES and CANDIDATE_SPEC_HASHES[cid] != h:
            raise ValueError(
                f"spec_hash conflict: {cid} уже зарегистрирован как "
                f"{CANDIDATE_SPEC_HASHES[cid]!r}, отклонена попытка "
                f"перезаписи на {h!r}")
        CANDIDATE_SPEC_HASHES[cid] = h
    global CANDIDATES
    CANDIDATES = tuple(sorted(CANDIDATE_SPEC_HASHES))
SL_ATR_MULT = a7.SL_ATR_MULT
TP_ATR_MULT = a7.TP_ATR_MULT
SIDE_INT = {"long": 1, "short": -1}

REQUIRED_SIGNAL_FIELDS = (
    "type", "candidate_id", "ticker", "day", "spec_hash",
    "side", "atr", "decision_label", "decision_close_time",
    "intended_entry_time", "observed_until",
)


def _parse_ts(raw: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("ts_not_string")
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("ts_naive")
    return dt.astimezone(timezone.utc)


def _signal_hash(signal) -> str:
    if not isinstance(signal, dict):
        return ""
    payload = json.dumps(signal, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _blank() -> dict:
    return {
        "ticker": "", "candidate_id": "", "day": "", "side": "",
        "spec_hash": "", "signal_hash": "", "decision_label": "",
        "decision_close_time": "", "observed_until": "", "intended_entry_time": "",
        "atr": "", "sl_level": "", "tp_level": "",
        "entry_ts": "", "entry_base": "", "entry_fill": "", "slip_entry": "",
        "commission_entry": "", "exit_ts": "", "exit_base": "", "exit_fill": "",
        "slip_exit": "", "commission_exit": "", "exit_reason": "",
        "gross": "", "costs_total": "", "net": "",
        "status": "", "incomplete_reason": "", "detail": "",
    }


def _reject(signal, ticker: str, reason: str, detail: str) -> dict:
    rec = _blank()
    if isinstance(signal, dict):
        rec.update({
            "ticker": signal.get("ticker", ticker),
            "candidate_id": signal.get("candidate_id", ""),
            "day": signal.get("day", ""),
            "side": signal.get("side", ""),
            "spec_hash": signal.get("spec_hash", ""),
            "signal_hash": _signal_hash(signal),
            "decision_label": signal.get("decision_label", ""),
        })
    else:
        rec["ticker"] = ticker
    rec["status"] = "REJECT"
    rec["incomplete_reason"] = reason
    rec["detail"] = detail
    return rec


def execute_candidate_signal(signal, tdata: a7.TickerData, costs: a7.CostParams,
                             *, expected_spec_hash: str = None) -> dict:
    """Исполняет один принятый A18 signal event на валидированном TickerData.

    Возвращает ledger-запись A24 (поля A7-семантики + signal identity/hash).
    REJECT — без изменения состояния; SKIP — вход отсутствует.

    spec_hash принимается ТОЛЬКО равный зарегистрированному для candidate_id
    (C1/C2 — канон A17; C3 — TREND_SPEC_HASH из intraday_trend_candidate).
    expected_spec_hash (необязательный) — дополнительный строгий пин; если задан,
    обязан совпасть с регистрацией. Произвольный неизвестный candidate_id
    отклоняется (unknown_candidate).
    """
    ticker = tdata.ticker
    try:
        costs.validate()
    except ValueError as exc:
        return _reject(signal, ticker, "invalid_costs", str(exc))

    if not isinstance(signal, dict) or signal.get("type") != "signal":
        return _reject(signal, ticker, "not_a_signal",
                       f"принимается только A18 signal event, получено {signal!r:.200}")
    missing = [f for f in REQUIRED_SIGNAL_FIELDS if f not in signal]
    if missing:
        return _reject(signal, ticker, "missing_field",
                       f"отсутствуют поля: {', '.join(missing)}")
    cid = signal["candidate_id"]
    if cid not in CANDIDATE_SPEC_HASHES:
        return _reject(signal, ticker, "unknown_candidate",
                       f"candidate_id={cid!r} не зарегистрирован в {CANDIDATES}")
    registered = CANDIDATE_SPEC_HASHES[cid]
    if signal["ticker"] != ticker:
        return _reject(signal, ticker, "ticker_mismatch",
                       f"signal.ticker={signal['ticker']!r} != tdata.ticker={ticker!r}")
    if expected_spec_hash is not None and expected_spec_hash != registered:
        return _reject(signal, ticker, "spec_hash_mismatch",
                       f"expected_spec_hash={expected_spec_hash!r} != "
                       f"registered {cid}={registered!r}")
    if signal["spec_hash"] != registered:
        return _reject(signal, ticker, "spec_hash_mismatch",
                       f"signal.spec_hash={signal['spec_hash']!r} != {registered!r}")
    side = signal["side"]
    if side not in SIDE_INT:
        return _reject(signal, ticker, "invalid_side", f"side={side!r}")
    s = SIDE_INT[side]
    atr = signal["atr"]
    if not (isinstance(atr, (int, float)) and math.isfinite(atr) and atr > 0):
        return _reject(signal, ticker, "atr_invalid", f"atr={atr!r}")

    try:
        dc = _parse_ts(signal["decision_close_time"])
        ie = _parse_ts(signal["intended_entry_time"])
        ob = _parse_ts(signal["observed_until"])
    except (ValueError, TypeError) as exc:
        return _reject(signal, ticker, "ts_unparsable", f"{exc}")
    if ie != dc:
        return _reject(signal, ticker, "entry_time_mismatch",
                       f"intended_entry={ie.isoformat()} != decision_close={dc.isoformat()}")
    if ob < dc:
        return _reject(signal, ticker, "observed_before_decision_close",
                       f"observed_until={ob.isoformat()} < decision_close={dc.isoformat()}")
    if ie.second != 0 or ie.microsecond != 0:
        return _reject(signal, ticker, "entry_not_minute_aligned",
                       f"intended_entry={ie.isoformat()} вне минутной сетки")
    day = a7.msk_date(ie)
    if signal["day"] != day.isoformat():
        return _reject(signal, ticker, "day_mismatch",
                       f"signal.day={signal['day']} != msk день входа {day.isoformat()}")

    em = tdata.minute_by_ts.get(ie)
    if em is None:
        rec = _blank()
        rec.update({
            "ticker": ticker, "candidate_id": cid, "day": day.isoformat(),
            "side": side, "spec_hash": signal["spec_hash"],
            "signal_hash": _signal_hash(signal),
            "decision_label": signal["decision_label"],
            "decision_close_time": signal["decision_close_time"],
            "observed_until": signal["observed_until"],
            "intended_entry_time": signal["intended_entry_time"],
            "status": "SKIP", "incomplete_reason": "missing_entry",
            "detail": f"нет минуты {ie.isoformat()} (exact intended_entry, сканирования нет)",
        })
        return rec

    entry_base = em.open
    sl = entry_base - s * SL_ATR_MULT * atr
    tp = entry_base + s * TP_ATR_MULT * atr

    fills = a7.research_execution.settle_entry(entry_base, s, costs)
    entry_fill = fills["entry_fill"]
    slip_entry = fills["slip_entry"]
    commission_entry = fills["commission_entry"]

    rec = _blank()
    rec.update({
        "ticker": ticker, "candidate_id": cid, "day": day.isoformat(),
        "side": side, "spec_hash": signal["spec_hash"],
        "signal_hash": _signal_hash(signal),
        "decision_label": signal["decision_label"],
        "decision_close_time": signal["decision_close_time"],
        "observed_until": signal["observed_until"],
        "intended_entry_time": signal["intended_entry_time"],
        "atr": atr, "sl_level": sl, "tp_level": tp,
        "entry_ts": ie.isoformat(), "entry_base": entry_base,
        "entry_fill": entry_fill, "slip_entry": slip_entry,
        "commission_entry": commission_entry,
    })

    exit_info = a7._walk(tdata, day, ie, s, sl, tp)

    if exit_info is not None and exit_info[0] == "incomplete":
        rec["status"] = "INCOMPLETE"
        rec["incomplete_reason"] = exit_info[1]
        rec["detail"] = f"первая отсутствующая/непокрытая минута пути: {exit_info[2].isoformat()}"
        return rec

    if exit_info is None:
        rec["status"] = "INCOMPLETE"
        rec["incomplete_reason"] = a7.research_execution.none_incomplete(
            tdata.minutes_by_day.get(day, []),
            full_18_20=a7._full_bar(tdata, day, "18:20") is not None,
            has_18_30=tdata.minute_by_ts.get(a7.label_ts(day, "18:30")) is not None)
        rec["detail"] = "выхода до конца данных нет; закрывать последним close запрещено (A7 incomplete)"
        return rec

    _, exit_base, exit_ts, reason = exit_info
    settled = a7.research_execution.settle_exit(
        entry_base, exit_base, s, costs,
        entry_fill=entry_fill, commission_entry=commission_entry)
    exit_fill = settled["exit_fill"]
    slip_exit = settled["slip_exit"]
    commission_exit = settled["commission_exit"]
    gross = settled["gross"]
    costs_total = settled["costs_total"]
    net = settled["net"]
    rec.update({
        "exit_ts": exit_ts.isoformat(), "exit_base": exit_base,
        "exit_fill": exit_fill, "slip_exit": slip_exit,
        "commission_exit": commission_exit, "exit_reason": reason,
        "gross": gross, "costs_total": costs_total, "net": net,
        "status": "FILLED", "incomplete_reason": "", "detail": "",
    })
    return rec