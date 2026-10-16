#!/usr/bin/env python3
"""B27: оптимизированный генератор C1/C2 (O(M)) — решение = принятый A18.

Причина: наивный A18 `CandidateSignalGenerator.inspect()` на каждой минуте
пересобирает `build_ticker_data(все накопленные минуты)` — O(M) на feed, т.е.
O(M^2) суммарно; на масштабе A12-окна (~44k минут/тикер) это часы (см. bounded
synthetic perf_bench). Здесь сохраняется ТОТ ЖЕ контракт закрытой минуты и ТО ЖЕ
САМОЕ решение: переиспользуется `_evaluate_day` принятого A18 (не изменяя A18),
а дорогая пересборка заменена инкрементальным построением `bar_by_day_label` и
`atr_seq` тем же правилом, что в `build_ticker_data`:
  - session-фильтр минут; выходные бакеты исключаются;
  - полный бар = ровно 10 различных минут (уникальность гарантирует валидатор);
  - bar_by_day_label только для session-bar меток;
  - ATR14 (WARMUP_TR) строится по полным сессионным барам d>=warmup_start в
    хронологическом порядке (первый бар инициализирует previous close).
Ничего в A7/A18 не переопределяется и не меняется. Модуль — НОВЫЙ hash и требует
приёмки архитектора до market use (B27 BRANCH DISPATCH); parity доказывается в
parity_check_fast.py.
"""
from __future__ import annotations

import math
from datetime import timedelta

from day_strategies_core import (
    WARMUP_TR,
    Bar10,
    TickerData,
    _bucket_label,
    is_session_bar_label,
    is_session_minute,
    msk_date,
    msk_mod,
)
from day_candidate_signals import SPEC_HASH, _evaluate_day


class FastCandidateSignalGenerator:
    """Публичный контракт CandidateSignalGenerator: feed/evaluate/inspect/reset/events."""

    def __init__(self, candidate_id: str, ticker: str, warmup_start=None, spec_hash: str = SPEC_HASH):
        self.candidate_id = candidate_id
        self.ticker = ticker
        self.warmup_start = warmup_start
        self.spec_hash = spec_hash
        self.reset()

    def reset(self) -> None:
        self._events: list = []
        self._done_days: set = set()
        self._first_ts = None
        self._last_ts = None
        self._minutes_by_day: dict = {}
        self._minute_by_ts: dict = {}
        self._bucket_minutes: dict = {}
        self._buckets: dict = {}
        self._session_dates: list = []
        self._full_session_bars: list = []
        self._bar_by_day_label: dict = {}
        self._atr_seq: list = []
        self._atr_prev_close = None
        self._atr_trs: list = []
        self._atr = None

    def _effective_warmup_start(self):
        return self.warmup_start or msk_date(self._first_ts)

    def _validate_bar(self, bar) -> None:
        ts = bar.ts
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("feed_ts_naive")
        if ts.second != 0 or ts.microsecond != 0:
            raise ValueError("feed_ts_not_minute_aligned")
        if self._last_ts is not None and ts <= self._last_ts:
            raise ValueError("feed_ts_not_strictly_increasing")
        o, h, l, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            raise ValueError("feed_ohlc_invalid")
        if not (h >= l and h >= max(o, c) and l <= min(o, c)):
            raise ValueError("feed_ohlc_invalid")
        if not (math.isfinite(v) and v >= 0):
            raise ValueError("feed_volume_invalid")

    def _append_atr(self, bar: Bar10) -> None:
        if self._atr_prev_close is None:
            self._atr_seq.append((bar.ts, None, 0))
            self._atr_prev_close = bar.close
            return
        tr = max(bar.high - bar.low, abs(bar.high - self._atr_prev_close),
                 abs(bar.low - self._atr_prev_close))
        self._atr_trs.append(tr)
        n = len(self._atr_trs)
        if n == WARMUP_TR:
            self._atr = sum(self._atr_trs) / WARMUP_TR
        elif n > WARMUP_TR:
            self._atr = (13.0 * self._atr + tr) / WARMUP_TR
        self._atr_seq.append((bar.ts, self._atr, n))
        self._atr_prev_close = bar.close

    def _complete_bucket(self, label, mins: list) -> None:
        bar = Bar10(label, mins, mins[0].open, max(m.high for m in mins),
                    min(m.low for m in mins), mins[-1].close,
                    sum(m.volume for m in mins))
        d = msk_date(bar.ts)
        if d.weekday() >= 5:
            return
        self._buckets[bar.ts] = bar
        if not is_session_bar_label(bar.ts):
            return
        self._bar_by_day_label[(d, msk_mod(bar.ts))] = bar
        if d >= self._effective_warmup_start():
            self._full_session_bars.append(bar)
            self._append_atr(bar)

    def feed(self, bar):
        self._validate_bar(bar)
        if self._first_ts is None:
            self._first_ts = bar.ts
        self._last_ts = bar.ts
        if is_session_minute(bar.ts):
            d = msk_date(bar.ts)
            self._minutes_by_day.setdefault(d, []).append(bar)
            self._minute_by_ts[bar.ts] = bar
            if d.weekday() < 5 and (not self._session_dates or self._session_dates[-1] != d):
                self._session_dates.append(d)
            label = _bucket_label(bar.ts)
            mins = self._bucket_minutes.setdefault(label, [])
            mins.append(bar)
            if len(mins) == 10:
                self._complete_bucket(label, mins)
        return self.evaluate()

    def _ticker_data(self) -> TickerData:
        return TickerData(self.ticker, self._minutes_by_day, self._minute_by_ts,
                          self._buckets, self._session_dates, self._full_session_bars,
                          self._bar_by_day_label, self._atr_seq,
                          self._effective_warmup_start())

    def inspect(self):
        if self._last_ts is None:
            return None
        day = msk_date(self._last_ts)
        if day in self._done_days:
            return None
        observed_until = self._last_ts + timedelta(minutes=1)
        return _evaluate_day(self._ticker_data(), day, observed_until, self._last_ts,
                             self.candidate_id, self.ticker, self.spec_hash)

    def evaluate(self):
        event = self.inspect()
        if event is not None and event["type"] in ("signal", "skip"):
            self._done_days.add(msk_date(self._last_ts))
            self._events.append(event)
            return event
        return None

    @property
    def events(self) -> list:
        return list(self._events)
