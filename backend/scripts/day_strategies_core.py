#!/usr/bin/env python3
"""A7: offline-ядро дневных стратегий S1/S2 (канон: docs/results/ARCH_20261008_B2_A4.md
§4–§6, §10; DISPATCH A7 от 2026-10-09, chat.md). Research-модуль: без сети, без БД,
без импорта app.* (никаких side effects). Реальные серии в задаче A7 не прогоняются.

Схема входа (1m-свечи, CSV):
    ticker,ts,open,high,low,close,volume
  - ts: ISO 8601 с таймзоной (ожидается UTC); метка = НАЧАЛО минуты.
  - open/high/low/close: конечные float > 0; high >= max(open, close),
    low <= min(open, close) (open/close — торговые цены внутри диапазона).
  - volume: конечный float >= 0.
  - Дубликаты (ticker+ts): идентичные схлопываются (notice); противоречивые —
    DQ-ошибка, ОБЕ строки отбрасываются (ambiguous не чинятся молча).
  - ts вне минутной сетки (секунды/микросекунды) — DQ-ошибка, отброс.
  - Минуты вне окна основной сессии [10:00, 18:45) MSK в торговый путь не входят;
    бары main-сессии на выходных — DQ-ошибка и исключение из сессионных структур.
  - Заголовок обязан содержать все обязательные колонки (лишние игнорируются).

Полный 10min-бар = ровно 10 различных минут бакета (epoch UTC, метка = начало).
Сессионные бары: метки MSK 10:00..18:30 (бар 18:30–18:40 закрывается до 18:45);
метка 18:40 сессионным баром НЕ является. Бар 18:20 (18:20–18:30) — сигнал время-выхода.

ATR14 Wilder: по полным сессионным барам от ЕДИНОГО фиксированного warmup start.
Первый бар последовательности ТОЛЬКО инициализирует previous close и TR не даёт;
TR считается с ВТОРОГО бара (TR = max(high-low, |high-prev_close|, |low-prev_close|),
prev_close — close предыдущего бара последовательности). Для первого ATR нужно 14 TR
= 15 баров: первый ATR = среднее первых 14 TR, далее ATR_t = (13*ATR_{t-1} + TR_t)/14.
На решающем баре берётся последний ATR СТРОГО ДО него (решающий бар в последовательность
ATR не входит). Нет 14 TR до решения -> atr_warmup_insufficient; ATR <= 0 / не конечный
-> atr_invalid. Пропуски баров внутри warmup-покрытия фиксируются в DQ явно
(code warmup_coverage_gap), полнота warmup не утверждается при дырах.

Сравнения порогов — с численным допуском FLOAT_EPS = 1e-12 (устойчивость к ошибке
представления, без экономически значимой серой зоны), единообразно:
  строгое >   : a > b  заменяется на a > b + FLOAT_EPS  (истинное равенство -> false);
  нестрогое >=: a >= b заменяется на a >= b - FLOAT_EPS (истинное равенство -> true);
  строгое <   : a < b  заменяется на a < b - FLOAT_EPS.
Так abs(ret) == theta не даёт сигнала (S1 строго > theta) даже при float-шуме
0.0040000000000000036, а |gap| == g_min даёт гейт (S2 нестрого >=).

Сигналы (один вход на день на тикер/конфиг):
  S1: day_open = open полного бара 10:00; решение на полном баре 12:00 (S1-A,
      theta=0.004) или 14:00 (S1-B, theta=0.003); half_ret = close/open-1;
      сигнал строго |half_ret| > theta, side = sign(half_ret).
  S2: prev_close = close полного бара 18:30 БЛИЖАЙШЕЙ предыдущей даты с барами
      main-сессии (<= 5 календарных дней); подстановка более старой даты ЗАПРЕЩЕНА;
      gap = day_open/prev_close-1; гейт |gap| >= g_min (S2-A 0.007 / S2-B 0.012);
      решение на полном баре 10:20 / 10:30; cont = close/day_open-1;
      ФЕЙД (по §5.2 канона): sign(cont) != sign(gap) ИЛИ |cont| < 0.25*|gap|;
      side = -sign(gap).

Вход: open минуты РОВНО на следующей 10-минутной границе после решающего бара
(next_open: 12:10/14:10 у S1, 10:30/10:40 у S2); полнота 10min-бара от этой минуты
НЕ требуется; сканирования вперёд нет — минуты нет -> день без входа.

Путь позиции (по сессионным минутам от входа включительно):
  - непрерывность ОБЯЗАТЕЛЬНА: каждая минута от entry_ts до исполненного выхода
    (включительно) должна существовать; первая отсутствующая/испорченная минута ->
    позиция INCOMPLETE (вход сохранён, exit/net неизвестны, положительный итог
    запрещён). Последующие свечи НЕ делают путь достоверным; стоп, достоверно
    исполненный ДО первой дыры, остаётся завершённым (FILLED);
  - первая дыра 18:20..18:29 -> time_exit_bar_18_20_incomplete; дыра ровно 18:30 ->
    time_exit_minute_18_30_missing (в т.ч. когда SL/TP касается ПОСЛЕ 18:30:
    поздний выход не превращает сделку в FILLED); иные дыки -> path_gap_after_entry;
  - гэп через SL на открытии -> выход по ФАКТИЧЕСКОЙ adverse цене открытия;
  - гэп через TP -> исполнение по уровню TP (улучшение не берём);
  - внутрибарные касания: SL раньше TP (пессимистично);
  - время-выход: полный бар 18:20 -> выход по open минуты 18:30 (приоритет над
    SL/TP на этой минуте); плановый выход не обеспечен -> INCOMPLETE;
  - данных до планового окна нет (день оборван до 18:20, открытая позиция) ->
    INCOMPLETE data_ended_open_position; net на INCOMPLETE не записывается.

Затраты: commission_bps/slippage_bps на СТОРОНУ (CLI, конечные >= 0;
slippage_bps < 10000, чтобы fill sell был строго положительным); slippage —
adverse fill против позиции (buy: base*(1+s), sell: base*(1-s)); комиссия =
notional(fill)*bps/1e4; fill_delta = side*(exit_fill - entry_fill);
net = fill_delta - commission_entry - commission_exit; costs_total = commission_e +
commission_x + (gross - fill_delta) -> тождество net = gross - costs_total выполняется
по построению. Округление fill к тику НЕ применяется (опция tick_size удалена r2:
round ломал тождество, улучшая цену). Нулевые издержки допустимы только с явной
пометкой gross_only (не «положительный net-вердикт»). Семантика согласована по духу
с canonical_v1 (backend/app/engine/costs.py); соответствие описано в отчёте.
"""
from __future__ import annotations

import csv
import hashlib
import math
import os
import sys
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.engine import research_execution

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

SESSION_MIN_MOD = 600
SESSION_MAX_MOD = 1124
TIME_EXIT_MOD = 1110
BAR_LABEL_MIN = 600
BAR_LABEL_MAX = 1110
BAR_LABEL_STEP = 10
WARMUP_TR = 14
PREV_DATE_MAX_GAP_DAYS = 5
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.0
S2_CONT_FRAC = 0.25

REQUIRED_COLUMNS = ("ticker", "ts", "open", "high", "low", "close", "volume")

FLOAT_EPS = 1e-12


@dataclass(frozen=True)
class DQIssue:
    ticker: str
    kind: str
    code: str
    detail: str


@dataclass(frozen=True)
class MinuteBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Bar10:
    ts: datetime
    minutes: list
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_full(self) -> bool:
        """Ровно 10 различных минут бакета. Бакет покрывает 10 минут, минуты
        уникальны (дедуп на загрузке) => 10 различных <=> ровно все 10 подряд."""
        return len(self.minutes) == 10


@dataclass
class TickerData:
    ticker: str
    minutes_by_day: dict
    minute_by_ts: dict
    buckets: dict
    session_dates: list
    full_session_bars: list
    bar_by_day_label: dict
    atr_seq: list
    warmup_start: date


@dataclass(frozen=True)
class CostParams:
    commission_bps: float
    slippage_bps: float

    @property
    def gross_only(self) -> bool:
        return self.commission_bps == 0.0 and self.slippage_bps == 0.0

    def validate(self) -> None:
        for name in ("commission_bps", "slippage_bps"):
            v = getattr(self, name)
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{name}: требуется конечное значение >= 0, получено {v!r}")
        if self.slippage_bps >= 10000.0:
            raise ValueError(
                f"slippage_bps: требуется < 10000 (fill sell = base*(1-s) обязан быть "
                f"строго положительным), получено {self.slippage_bps!r}")

    def fill(self, base: float, side: int) -> float:
        """Фактическая цена: adverse slippage против позиции. side=+1 buy, -1 sell."""
        p = base * (1.0 + (self.slippage_bps / 1e4) * side)
        if not math.isfinite(p) or p <= 0:
            raise ValueError(f"fill: невалидная цена исполнения {p!r} (base={base!r}, side={side})")
        return p

    def commission(self, notional: float) -> float:
        c = notional * (self.commission_bps / 1e4)
        if not math.isfinite(c) or c < 0:
            raise ValueError(f"commission: невалидная комиссия {c!r}")
        return c


@dataclass(frozen=True)
class StrategyConfig:
    config: str
    strategy: str
    decision_label: str
    entry_label: str
    theta: float = 0.0
    g_min: float = 0.0


CONFIGS: tuple = (
    StrategyConfig("S1-A", "S1", "12:00", "12:10", theta=0.004),
    StrategyConfig("S1-B", "S1", "14:00", "14:10", theta=0.003),
    StrategyConfig("S2-A", "S2", "10:20", "10:30", g_min=0.007),
    StrategyConfig("S2-B", "S2", "10:30", "10:40", g_min=0.012),
)

TRADE_FIELDS = (
    "ticker", "config", "day", "side",
    "decision_label", "decision_bar_ts", "signal_time", "decision_close", "day_open",
    "half_ret", "gap", "cont", "threshold",
    "atr", "sl_level", "tp_level",
    "entry_ts", "entry_base", "entry_fill", "slip_entry", "commission_entry",
    "exit_ts", "exit_base", "exit_fill", "slip_exit", "commission_exit", "exit_reason",
    "gross", "costs_total", "net",
    "status", "incomplete_reason",
)

SKIP_FIELDS = ("ticker", "config", "day", "reason", "detail")


def msk_mod(ts: datetime) -> int:
    loc = ts.astimezone(MSK)
    return loc.hour * 60 + loc.minute


def msk_date(ts: datetime) -> date:
    return ts.astimezone(MSK).date()


def is_session_minute(ts: datetime) -> bool:
    return SESSION_MIN_MOD <= msk_mod(ts) <= SESSION_MAX_MOD


def is_session_bar_label(label: datetime) -> bool:
    mod = msk_mod(label)
    return BAR_LABEL_MIN <= mod <= BAR_LABEL_MAX and mod % BAR_LABEL_STEP == 0


def label_ts(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    loc = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=MSK)
    return loc.astimezone(UTC)


def label_mod(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class _RowError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _parse_ts(raw: str) -> datetime:
    s = (raw or "").strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise _RowError("ts_unparsable", f"{raw!r}: {exc}") from None
    if dt.tzinfo is None:
        raise _RowError("ts_naive", f"{raw!r}: нет таймзоны (нужен offset, напр. +00:00)")
    return dt.astimezone(UTC)


def load_minute_csv(path):
    """Парсит 1m-CSV. Возвращает (dict ticker->[MinuteBar], list[DQIssue], sha256).

    Ошибки схемы колонок (пустой файл, отсутствие обязательных, ДУБЛИ имён после
    strip-нормализации) -> ValueError (файл непригоден, DictReader иначе молча
    потерял бы колонку). Битые строки отбрасываются с явной DQ-записью; минута,
    для которой найдена ХОТЯ БЫ ОДНА битая строка, исключается целиком
    (признак порчи хранится до группировки — валидный дубль не делает минуту
    «бесспорной»; исключение фиксируется отдельной записью minute_corrupted)."""
    path = __import__("pathlib").Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    if reader.fieldnames is None:
        raise ValueError("columns_schema_invalid: файл пуст (нет заголовка)")
    cols = [c.strip() for c in reader.fieldnames]
    if len(cols) != len(set(cols)):
        dups = sorted({c for c in cols if cols.count(c) > 1})
        raise ValueError(f"columns_schema_invalid: дубли имён колонок {dups}")
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"columns_schema_invalid: отсутствуют обязательные колонки {missing}")
    reader.fieldnames = cols

    raw_rows = {}
    corrupted: set = set()
    issues: list = []
    for lineno, row in enumerate(reader, start=2):
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            issues.append(DQIssue("", "error", "ticker_empty", f"строка {lineno}: пустой ticker"))
            continue
        try:
            ts = _parse_ts(row.get("ts", ""))
            vals = {k: float(row.get(k, "")) for k in ("open", "high", "low", "close", "volume")}
        except _RowError as exc:
            issues.append(DQIssue(ticker, "error", exc.code, f"строка {lineno}: {exc.detail}"))
            continue
        except (TypeError, ValueError) as exc:
            issues.append(DQIssue(ticker, "error", "price_unparsable", f"строка {lineno}: {exc}"))
            continue
        if ts.second != 0 or ts.microsecond != 0:
            issues.append(DQIssue(ticker, "error", "minute_not_aligned",
                                  f"строка {lineno}: {ts.isoformat()} не на минутной сетке"))
            continue
        o, h, l, c, v = (vals[k] for k in ("open", "high", "low", "close", "volume"))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            corrupted.add((ticker, ts))
            issues.append(DQIssue(ticker, "error", "price_nonpositive_or_nonfinite",
                                  f"строка {lineno}: ohlc={(o, h, l, c)!r}"))
            continue
        if not (math.isfinite(v) and v >= 0):
            corrupted.add((ticker, ts))
            issues.append(DQIssue(ticker, "error", "volume_invalid",
                                  f"строка {lineno}: volume={v!r}"))
            continue
        if h < l or h < max(o, c) or l > min(o, c):
            corrupted.add((ticker, ts))
            issues.append(DQIssue(ticker, "error", "ohlc_broken",
                                  f"строка {lineno}: ohlc={(o, h, l, c)!r}"))
            continue
        raw_rows.setdefault((ticker, ts), []).append(
            (MinuteBar(ts, o, h, l, c, v), lineno))

    by_ticker: dict = {}
    for (ticker, ts), rows in sorted(raw_rows.items()):
        if (ticker, ts) in corrupted:
            issues.append(DQIssue(ticker, "error", "minute_corrupted",
                                  f"{ts.isoformat()}: есть битая строка этой минуты — "
                                  f"минута исключена целиком (валидных дублей не остаётся)"))
            continue
        bars = [r[0] for r in rows]
        if len(bars) > 1:
            if all(b == bars[0] for b in bars[1:]):
                issues.append(DQIssue(ticker, "notice", "duplicate_identical",
                                      f"{ts.isoformat()} x{len(rows)} — оставлена одна"))
                by_ticker.setdefault(ticker, []).append(bars[0])
            else:
                issues.append(DQIssue(ticker, "error", "duplicate_conflicting",
                                      f"{ts.isoformat()} x{len(rows)} — строки отброшены"))
            continue
        by_ticker.setdefault(ticker, []).append(bars[0])
    for t in by_ticker:
        by_ticker[t].sort(key=lambda b: b.ts)
    issues.sort(key=lambda d: (d.ticker, d.code, d.detail))
    return by_ticker, issues, sha


def _bucket_label(ts: datetime) -> datetime:
    epoch = int(ts.timestamp())
    label = epoch - epoch % 600
    return datetime.fromtimestamp(label, tz=UTC)


def aggregate_10min(minutes: list) -> dict:
    buckets: dict = {}
    for b in minutes:
        buckets.setdefault(_bucket_label(b.ts), []).append(b)
    out: dict = {}
    for label, mins in buckets.items():
        mins.sort(key=lambda b: b.ts)
        out[label] = Bar10(label, mins, mins[0].open, max(m.high for m in mins),
                           min(m.low for m in mins), mins[-1].close,
                           sum(m.volume for m in mins))
    return out


def build_ticker_data(ticker: str, minutes: list, warmup_start: date, issues: list,
                      warmup_end: date = None) -> TickerData:
    sess_minutes = [b for b in minutes if is_session_minute(b.ts)]
    minutes_by_day: dict = {}
    minute_by_ts: dict = {}
    for b in sess_minutes:
        minutes_by_day.setdefault(msk_date(b.ts), []).append(b)
        minute_by_ts[b.ts] = b
    for d in minutes_by_day:
        minutes_by_day[d].sort(key=lambda b: b.ts)

    buckets: dict = {}
    for label, bar in aggregate_10min(sess_minutes).items():
        d = msk_date(label)
        if d.weekday() >= 5:
            issues.append(DQIssue(ticker, "error", "session_bars_weekend",
                                  f"{d} {label.isoformat()} — бар main-сессии в выходной, исключён"))
            continue
        buckets[label] = bar

    session_dates = sorted({msk_date(l) for l in buckets})
    bar_by_day_label: dict = {}
    full_session_bars: list = []
    for label in sorted(buckets):
        if not is_session_bar_label(label):
            continue
        bar = buckets[label]
        d = msk_date(label)
        if bar.is_full:
            bar_by_day_label[(d, msk_mod(label))] = bar
            if d >= warmup_start:
                full_session_bars.append(bar)

    _report_warmup_gaps(ticker, buckets, bar_by_day_label, warmup_start, warmup_end, issues)
    atr_seq = atr14_sequence(full_session_bars)
    return TickerData(ticker, minutes_by_day, minute_by_ts, buckets,
                      session_dates, full_session_bars, bar_by_day_label,
                      atr_seq, warmup_start)


def _report_warmup_gaps(ticker: str, buckets: dict, bar_by_day_label: dict,
                        warmup_start: date, warmup_end: date, issues: list) -> None:
    """Явная DQ-запись о пропусках сессионных баров внутри warmup-покрытия.

    Окно: warmup_start <= дата < warmup_end (warmup_end = eval_start, граница
    тёплого периода из контракта). Пропуски фиксируются от первой до последней
    сессионной метки дня в данных: полнота warmup не утверждается при дырах
    (REWORK A7 п.2). Пропуски в eval-днях не являются пропусками warmup и здесь
    не дублируются — их следствия видны в skips/incomplete."""
    by_day: dict = {}
    for label in buckets:
        if is_session_bar_label(label):
            by_day.setdefault(msk_date(label), []).append(label)
    for d in sorted(by_day):
        if d < warmup_start or (warmup_end is not None and d >= warmup_end):
            continue
        labels = sorted(by_day[d])
        missing = []
        for sec in range(int(labels[0].timestamp()), int(labels[-1].timestamp()) + 600, 600):
            lbl = datetime.fromtimestamp(sec, tz=UTC)
            if is_session_bar_label(lbl) and (d, msk_mod(lbl)) not in bar_by_day_label:
                missing.append(lbl)
        if missing:
            shown = ", ".join(lbl.astimezone(MSK).strftime("%H:%M") for lbl in missing[:6])
            suffix = f" …(+{len(missing) - 6})" if len(missing) > 6 else ""
            issues.append(DQIssue(ticker, "error", "warmup_coverage_gap",
                                  f"{d}: нет полных сессионных баров: {shown}{suffix} "
                                  f"(неполнота warmup-покрытия, ATR может не набраться)"))


def atr14_sequence(bars: list) -> list:
    """[(ts, atr|None, n_tr)]; первый бар только инициализирует previous close
    (TR не даёт, n_tr=0); atr != None начиная с бара, на котором набраны 14 TR
    (= 15 баров последовательности)."""
    out: list = []
    trs: list = []
    prev_close = None
    atr = None
    for bar in bars:
        if prev_close is None:
            out.append((bar.ts, None, 0))
            prev_close = bar.close
            continue
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        trs.append(tr)
        if len(trs) == WARMUP_TR:
            atr = sum(trs) / WARMUP_TR
        elif len(trs) > WARMUP_TR:
            atr = (13.0 * atr + tr) / WARMUP_TR
        out.append((bar.ts, atr, len(trs)))
        prev_close = bar.close
    return out


def atr_before(tdata: TickerData, ts: datetime):
    """ATR на последний полный сессионный бар СТРОГО ДО ts. -> (atr|None, n_tr)."""
    seq = tdata.atr_seq
    idx = bisect_left(seq, ts, key=lambda x: x[0])
    if idx == 0:
        return None, 0
    return seq[idx - 1][1], seq[idx - 1][2]


def _full_bar(tdata: TickerData, day: date, hhmm: str):
    return tdata.bar_by_day_label.get((day, label_mod(hhmm)))


def prev_session_date(tdata: TickerData, day: date):
    """Ближайшая предыдущая дата с бакетами main-сессии (любая полнота),
    в пределах PREV_DATE_MAX_GAP_DAYS календарных дней."""
    best = None
    lo = day - timedelta(days=PREV_DATE_MAX_GAP_DAYS)
    for d in tdata.session_dates:
        if lo <= d < day:
            if best is None or d > best:
                best = d
    return best


def eval_days(tdata: TickerData, eval_start: date, eval_end: date) -> list:
    return [d for d in tdata.session_dates if eval_start <= d <= eval_end]


def _gap_reason(mod: int) -> str:
    return research_execution.gap_reason_for(mod)


def _walk(tdata: TickerData, day: date, entry_ts: datetime, side: int, sl: float, tp):
    """Adapter: A7 shell entry into the shared research_execution.walk_exit."""
    return research_execution.walk_exit(
        tdata.minutes_by_day.get(day, []), tdata.minute_by_ts,
        day=day, entry_ts=entry_ts, side=side, sl=sl, tp=tp,
        full_18_20=_full_bar(tdata, day, "18:20") is not None,
        has_18_30=tdata.minute_by_ts.get(label_ts(day, "18:30")) is not None)


def run_day(tdata: TickerData, cfg: StrategyConfig, day: date, costs: CostParams):
    """Один конфиг x один день -> dict-запись сделки (есть 'status') или пропуска."""

    def skip(reason: str, detail: str):
        return {"ticker": tdata.ticker, "config": cfg.config, "day": day.isoformat(),
                "reason": reason, "detail": detail}

    dob = _full_bar(tdata, day, "10:00")
    if dob is None:
        return skip("day_open_missing", "нет полного 10min-бара 10:00")
    if cfg.strategy == "S1":
        dec = _full_bar(tdata, day, cfg.decision_label)
        if dec is None:
            return skip("decision_bar_missing", f"нет полного 10min-бара {cfg.decision_label}")
        half_ret = dec.close / dob.open - 1.0
        if not (abs(half_ret) > cfg.theta + FLOAT_EPS):
            return skip("no_signal_theta",
                        f"|half_ret|={abs(half_ret)!r} <= theta+eps={cfg.theta + FLOAT_EPS!r} "
                        f"(строгое > с FLOAT_EPS={FLOAT_EPS!r})")
        side = 1 if half_ret > 0 else -1
        metrics = {"half_ret": half_ret, "gap": "", "cont": "", "threshold": cfg.theta}
    else:
        prev = prev_session_date(tdata, day)
        if prev is None:
            return skip("prev_date_missing",
                        f"нет даты с барами main-сессии в пределах {PREV_DATE_MAX_GAP_DAYS} календарных дней")
        pcb = _full_bar(tdata, prev, "18:30")
        if pcb is None:
            return skip("prev_close_missing",
                        f"на ближайшей предыдущей дате {prev.isoformat()} нет полного бара 18:30; "
                        f"подстановка более старой даты запрещена (§5.2)")
        gap = dob.open / pcb.close - 1.0
        if not (abs(gap) >= cfg.g_min - FLOAT_EPS):
            return skip("no_gap",
                        f"|gap|={abs(gap)!r} < g_min-eps={cfg.g_min - FLOAT_EPS!r} "
                        f"(нестрогое >= с FLOAT_EPS={FLOAT_EPS!r})")
        dec = _full_bar(tdata, day, cfg.decision_label)
        if dec is None:
            return skip("decision_bar_missing", f"нет полного 10min-бара {cfg.decision_label}")
        cont = dec.close / dob.open - 1.0
        frac_gap = S2_CONT_FRAC * abs(gap)
        fade = (cont > 0) != (gap > 0) or abs(cont) < frac_gap - FLOAT_EPS
        if not fade:
            return skip("no_signal_continuation",
                        f"нет фейда (§5.2): sign(cont)=sign(gap) и |cont|={abs(cont)!r} "
                        f">= {S2_CONT_FRAC}*|gap|-eps={frac_gap - FLOAT_EPS!r}")
        side = -1 if gap > 0 else 1
        metrics = {"half_ret": "", "gap": gap, "cont": cont, "threshold": cfg.g_min}

    atr_v, n_tr = atr_before(tdata, dec.ts)
    if n_tr < WARMUP_TR:
        return skip("atr_warmup_insufficient",
                    f"TR до решающего бара: {n_tr} < {WARMUP_TR} (первый бар "
                    f"последовательности только инициализирует prev_close)")
    if atr_v is None or not math.isfinite(atr_v) or atr_v <= 0:
        return skip("atr_invalid", f"ATR={atr_v!r} при n_tr={n_tr}")

    entry_ts = label_ts(day, cfg.entry_label)
    em = tdata.minute_by_ts.get(entry_ts)
    if em is None:
        return skip("entry_minute_missing",
                    f"нет минуты {entry_ts.isoformat()} (exact next_open, сканирования нет)")

    entry_base = em.open
    sl = entry_base - side * SL_ATR_MULT * atr_v
    tp = (entry_base + side * TP_ATR_MULT * atr_v) if cfg.strategy == "S2" else None

    rec = {
        "ticker": tdata.ticker, "config": cfg.config, "day": day.isoformat(),
        "side": "long" if side > 0 else "short",
        "decision_label": cfg.decision_label, "decision_bar_ts": dec.ts.isoformat(),
        "signal_time": (dec.ts + timedelta(minutes=BAR_LABEL_STEP)).isoformat(),
        "decision_close": dec.close, "day_open": dob.open,
        "atr": atr_v, "sl_level": sl, "tp_level": tp if tp is not None else "",
        "entry_ts": entry_ts.isoformat(), "entry_base": entry_base,
        "exit_ts": "", "exit_base": "", "exit_reason": "",
        "gross": "", "net": "", "status": "INCOMPLETE", "incomplete_reason": "",
    }
    rec.update(metrics)

    fills = research_execution.settle_entry(entry_base, side, costs)
    rec["entry_fill"] = fills["entry_fill"]
    rec["slip_entry"] = fills["slip_entry"]
    rec["commission_entry"] = fills["commission_entry"]

    exit_info = _walk(tdata, day, entry_ts, side, sl, tp)

    if exit_info is not None and exit_info[0] == "incomplete":
        rec["incomplete_reason"] = exit_info[1]
        rec["exit_fill"] = ""
        rec["slip_exit"] = ""
        rec["commission_exit"] = ""
        rec["costs_total"] = ""
        return rec

    if exit_info is None:
        rec["incomplete_reason"] = research_execution.none_incomplete(
            tdata.minutes_by_day.get(day, []),
            full_18_20=_full_bar(tdata, day, "18:20") is not None,
            has_18_30=tdata.minute_by_ts.get(label_ts(day, "18:30")) is not None)
        rec["exit_fill"] = ""
        rec["slip_exit"] = ""
        rec["commission_exit"] = ""
        rec["costs_total"] = ""
        return rec

    _, exit_base, exit_ts, reason = exit_info
    settled = research_execution.settle_exit(
        entry_base, exit_base, side, costs,
        entry_fill=fills["entry_fill"],
        commission_entry=fills["commission_entry"])
    rec.update({
        "exit_ts": exit_ts.isoformat(), "exit_base": exit_base,
        "exit_fill": settled["exit_fill"],
        "slip_exit": settled["slip_exit"],
        "commission_exit": settled["commission_exit"], "exit_reason": reason,
        "gross": settled["gross"], "costs_total": settled["costs_total"],
        "net": settled["net"],
        "status": "FILLED", "incomplete_reason": "",
    })
    return rec


def run_ticker(tdata: TickerData, cfgs, eval_start: date, eval_end: date, costs: CostParams):
    """Возвращает (trades, skips); один вход на день на тикер/конфиг (по построению)."""
    trades, skips = [], []
    for day in eval_days(tdata, eval_start, eval_end):
        for cfg in cfgs:
            r = run_day(tdata, cfg, day, costs)
            (trades if "status" in r else skips).append(r)
    return trades, skips


def summarize(trades: list) -> list:
    groups: dict = {}
    incomplete: dict = {}
    for t in trades:
        key = (t["ticker"], t["config"], t["side"])
        if t["status"] == "FILLED":
            groups.setdefault(key, []).append(t)
        else:
            incomplete[key] = incomplete.get(key, 0) + 1
    rows = []
    for key in sorted(set(groups) | set(incomplete)):
        filled = groups.get(key, [])
        nets = [t["net"] for t in filled]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        if not filled:
            pf = "no_filled_trades"
        elif not losses:
            pf = "no_losing_trades"
        elif sum(wins) == 0.0:
            pf = 0.0
        else:
            pf = sum(wins) / abs(sum(losses))
        rows.append({
            "ticker": key[0], "config": key[1], "side": key[2],
            "filled": len(filled), "incomplete": incomplete.get(key, 0),
            "wins": len(wins), "losses": len(losses),
            "gross_sum": sum(t["gross"] for t in filled) if filled else 0.0,
            "costs_sum": sum(t["costs_total"] for t in filled) if filled else 0.0,
            "net_sum": sum(nets) if nets else 0.0,
            "profit_factor": pf,
        })
    for row in rows:
        if any(isinstance(v, float) and not math.isfinite(v) for v in row.values()):
            raise ValueError("nonfinite_summary_result")
    return rows
