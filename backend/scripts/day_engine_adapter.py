#!/usr/bin/env python3
"""A9: причинный адаптер дневных стратегий S1/S2 для общего EngineRunner.

Модуль импортируется без side effects: нет сети, БД, print, argparse, запуска
app.main/runtime. Использует ТОЛЬКО точки расширения движка — Strategy protocol
и ExitPolicy — и чистые константы/хелперы принятого ядра A7
(scripts/day_strategies_core.py, read-only).

Адаптер получает от движка закрытый 1m-префикс (CandleWindow) и формирует
полные 10min-бары и ATR причинно, из уже увиденных минут: никакой полный будущий
ряд и никакой заранее рассчитанный список сделок стратегии не передаются.
reset() возвращает стратегию в исходное состояние, повторный прогон даёт тот же
результат.

Схема таймингов (совпадает с каноном A7):
  - полный 10min-бар завершается на своей последней минуте (метка+9);
  - сигнал входа эмитится на закрытии последней минуты решающего бара; движок
    открывает позицию по open СЛЕДУЮЩЕГО бара (exact next_open A7);
  - ATR14 Wilder считается по полным сессионным барам строго до решающего;
  - SL/TP считаются от базы входа (open минуты входа): exit-политика
    восстанавливает базу из фактической цены исполнения по модели издержек;
  - time-exit эмитится на закрытии 18:29, движок закрывает по open 18:30.

Известные ограничения существующего API (подробно в отчёте A9):
  - движок исполняет pending на СЛЕДУЮЩЕМ баре без сверки ожидаемого ts, поэтому
    отсутствие ровно нужной минуты (entry_label / 18:30) адаптер проверить
    причинно не может — это BLOCKED-кейс с минимальным предложением изменения API
    (поле ожидаемого времени исполнения у Signal);
  - движок не умеет возвращать INCOMPLETE (путь неизвестен): открытая позиция
    всегда закрывается числом (SL/TP/end_of_data), тогда как A7 помечает день
    INCOMPLETE. Это определение, а не ошибка адаптера.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
for _p in (_BACKEND, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import day_strategies_core as a7  # noqa: E402

from app.engine.costs import CostModel  # noqa: E402
from app.engine.exits import ExitPolicy  # noqa: E402
from app.engine.models import Candle, ExitPlan, Side, Signal  # noqa: E402
from app.engine.runner import EngineConfig, EngineRunner  # noqa: E402

ONE_MIN = timedelta(minutes=1)
SIDE_BY_INT = {1: Side.BUY, -1: Side.SELL}
SIDE_INT_BY_SIDE = {Side.BUY: 1, Side.SELL: -1}


@dataclass(frozen=True)
class FullBar:
    label: datetime
    day: date
    mod: int
    open: float
    high: float
    low: float
    close: float


class _PlanHub:
    """Одноразовая передача параметров входа стратегия -> exit-политика."""

    def __init__(self) -> None:
        self._plan = None

    def put(self, atr: float, strategy: str) -> None:
        self._plan = (atr, strategy)

    def take(self):
        plan = self._plan
        self._plan = None
        return plan

    def clear(self) -> None:
        self._plan = None


class DayExitPolicy(ExitPolicy):
    """SL/TP A7: уровни от базы входа, восстановленной из accepted-fill.

    A7 считает SL/TP от open минуты входа (entry_base), а движок передаёт
    политике фактическую исполненную цену (fill, уже со slippage). При
    tick_size=0 fill = base*(1 + slope*slippage_bps/1e4), поэтому база
    восстанавливается точно; уровни совпадают с эталоном при любых издержках.
    """

    policy_id = "day_engine_adapter"
    version = "1.0.0"

    def __init__(self, hub: _PlanHub, slippage_bps: float) -> None:
        self._hub = hub
        self._slippage_bps = slippage_bps

    def plan_entry(self, side: Side, entry_price: float, bars) -> ExitPlan:
        plan = self._hub.take()
        if plan is None:
            raise RuntimeError(
                "day_engine_adapter: plan_entry без ожидаемого плана — вход движка "
                "не сопровождался сигналом стратегии"
            )
        atr, strategy = plan
        sign = 1.0 if side is Side.BUY else -1.0
        base = entry_price / (1.0 + self._slippage_bps / 1e4 * sign)
        stop = base - sign * a7.SL_ATR_MULT * atr
        target = base + sign * a7.TP_ATR_MULT * atr if strategy == "S2" else None
        return ExitPlan(stop_loss=stop, take_profit=target)


class DayEngineStrategy:
    """Причинная S1/S2-стратегия движка для одного фиксированного конфига A7.

    Внутреннее состояние потоковое: строится из минут, пришедших в on_bar. Один
    экземпляр соответствует одной конфигурации. reset() полностью обнуляет
    состояние и хаб плана.
    """

    strategy_id = "day_engine_adapter"
    version = "1.0.0"

    def __init__(
        self,
        config: a7.StrategyConfig,
        warmup_start: date,
        eval_start: date,
        eval_end: date,
        hub: _PlanHub,
    ) -> None:
        self.config = config
        self.warmup_start = warmup_start
        self.eval_start = eval_start
        self.eval_end = eval_end
        self.hub = hub
        self._decision_mod = a7.label_mod(config.decision_label)
        self.reset()

    def reset(self) -> None:
        self._bucket_minutes: dict = {}
        self._bucket_seen: set = set()
        self._bar_by_day_mod: dict = {}
        self._day_open: dict = {}
        self._close_1830: dict = {}
        self._session_dates: list = []
        self._session_dates_set: set = set()
        self._seen_minutes: set = set()
        self._atr_seq: list = []
        self._trs: list = []
        self._prev_close = None
        self._atr = None
        self._n_tr = 0
        self._fired: set = set()
        self.entry_info: dict = {}
        self.signals: list = []
        self._last_ts = None
        self.hub.clear()

    def warmup_bars(self) -> int:
        return 0

    def on_bar(self, window):
        bar = window[-1]
        ts = bar.ts
        if not a7.is_session_minute(ts):
            return None
        if ts == self._last_ts:
            return None
        self._last_ts = ts
        day = a7.msk_date(ts)
        mod = a7.msk_mod(ts)
        self._observe(day, mod, bar)
        if mod == a7.TIME_EXIT_MOD - 1:
            sig = self._maybe_time_exit(day, mod, ts)
            if sig is not None:
                return sig
        if mod == self._decision_mod + a7.BAR_LABEL_STEP - 1:
            return self._maybe_entry(day, ts)
        return None

    def _observe(self, day: date, mod: int, bar) -> None:
        ts = bar.ts
        self._seen_minutes.add(ts)
        if day.weekday() < 5 and day not in self._session_dates_set:
            self._session_dates_set.add(day)
            self._session_dates.append(day)
        if ts in self._bucket_seen:
            return
        label = a7._bucket_label(ts)
        self._bucket_seen.add(ts)
        self._bucket_minutes.setdefault(label, []).append(bar)
        if mod % a7.BAR_LABEL_STEP != a7.BAR_LABEL_STEP - 1:
            return
        mins = self._bucket_minutes[label]
        if len(mins) != 10 or not a7.is_session_bar_label(label) or day.weekday() >= 5:
            return
        bar10 = FullBar(
            label=label,
            day=day,
            mod=a7.msk_mod(label),
            open=mins[0].open,
            high=max(m.high for m in mins),
            low=min(m.low for m in mins),
            close=mins[-1].close,
        )
        self._bar_by_day_mod[(day, bar10.mod)] = bar10
        if bar10.mod == a7.label_mod("10:00"):
            self._day_open[day] = bar10.open
        if bar10.mod == a7.label_mod("18:30"):
            self._close_1830[day] = bar10.close
        if day >= self.warmup_start:
            self._ingest_atr(bar10)

    def _ingest_atr(self, bar10: FullBar) -> None:
        if self._prev_close is None:
            self._atr_seq.append((bar10.label, None, 0))
            self._prev_close = bar10.close
            return
        tr = max(
            bar10.high - bar10.low,
            abs(bar10.high - self._prev_close),
            abs(bar10.low - self._prev_close),
        )
        self._trs.append(tr)
        self._n_tr = len(self._trs)
        if self._n_tr == a7.WARMUP_TR:
            self._atr = sum(self._trs) / a7.WARMUP_TR
        elif self._n_tr > a7.WARMUP_TR:
            self._atr = (13.0 * self._atr + tr) / a7.WARMUP_TR
        self._atr_seq.append((bar10.label, self._atr, self._n_tr))
        self._prev_close = bar10.close

    def _atr_before_decision(self):
        if len(self._atr_seq) < 2:
            return None, 0
        _, atr_v, n_tr = self._atr_seq[-2]
        return atr_v, n_tr

    def _prev_session_date(self, day: date):
        lo = day - timedelta(days=a7.PREV_DATE_MAX_GAP_DAYS)
        best = None
        for d in self._session_dates:
            if lo <= d < day and (best is None or d > best):
                best = d
        return best

    def _continuity(self, day: date, from_ts: datetime, to_ts: datetime) -> bool:
        t = from_ts
        while t <= to_ts:
            if t not in self._seen_minutes:
                return False
            t += ONE_MIN
        return True

    def _emit(self, sig: Signal) -> Signal:
        self.signals.append(sig)
        return sig

    def _maybe_entry(self, day: date, ts: datetime):
        if day in self._fired:
            return None
        if not (self.eval_start <= day <= self.eval_end):
            return None
        cfg = self.config
        day_open = self._day_open.get(day)
        if day_open is None:
            return None
        dec = self._bar_by_day_mod.get((day, self._decision_mod))
        if dec is None:
            return None
        if cfg.strategy == "S1":
            half_ret = dec.close / day_open - 1.0
            if not (abs(half_ret) > cfg.theta + a7.FLOAT_EPS):
                return None
            side = 1 if half_ret > 0 else -1
        else:
            prev = self._prev_session_date(day)
            if prev is None:
                return None
            prev_close = self._close_1830.get(prev)
            if prev_close is None:
                return None
            gap = day_open / prev_close - 1.0
            if not (abs(gap) >= cfg.g_min - a7.FLOAT_EPS):
                return None
            cont = dec.close / day_open - 1.0
            frac_gap = a7.S2_CONT_FRAC * abs(gap)
            fade = (cont > 0) != (gap > 0) or abs(cont) < frac_gap - a7.FLOAT_EPS
            if not fade:
                return None
            side = -1 if gap > 0 else 1
        atr_v, n_tr = self._atr_before_decision()
        if n_tr < a7.WARMUP_TR:
            return None
        if atr_v is None or not (atr_v > 0):
            return None
        entry_ts = a7.label_ts(day, cfg.entry_label)
        self._fired.add(day)
        self.entry_info[day] = {
            "side": side,
            "entry_ts": entry_ts,
            "atr": atr_v,
            "signal_time": dec.label + timedelta(minutes=a7.BAR_LABEL_STEP),
            "day_open": day_open,
            "decision_close": dec.close,
        }
        self.hub.put(atr_v, cfg.strategy)
        return self._emit(
            Signal(
                strategy_id=self.strategy_id,
                side=SIDE_BY_INT[side],
                time=ts,
                reason=f"a7_{cfg.strategy}",
                features={"atr": atr_v},
                kind="entry",
            )
        )

    def _maybe_time_exit(self, day: date, mod: int, ts: datetime):
        info = self.entry_info.get(day)
        if info is None:
            return None
        if (day, a7.label_mod("18:20")) not in self._bar_by_day_mod:
            return None
        if not self._continuity(day, info["entry_ts"], ts):
            return None
        return self._emit(
            Signal(
                strategy_id=self.strategy_id,
                side=SIDE_BY_INT[-info["side"]],
                time=ts,
                reason="a7_time_exit_18_30",
                features={},
                kind="exit",
            )
        )


def build_engine_config(figi: str, costs: a7.CostParams) -> EngineConfig:
    """Конфиг движка, эквивалентный A7: qty=1, без сессии/трейлинга/BE/частичных."""
    return EngineConfig(
        figi=figi,
        qty=1,
        mode="both",
        allow_short=True,
        cost_model=CostModel(
            commission_rate=costs.commission_bps / 1e4,
            slippage_bps=costs.slippage_bps,
            tick_size=0.0,
        ),
        wick_tol=0.0,
        be_trigger_r=0.0,
        be_offset_pct=0.0,
        abort_r=0.0,
        abort_max_bars=0,
        partial_r=0.0,
        partial_fraction=0.5,
        partial_to_be=True,
    )


def minutes_to_candles(minutes: list) -> list:
    """Сессионные минуты -> строго возрастающий ряд Candle для движка."""
    rows = [b for b in minutes if a7.is_session_minute(b.ts)]
    rows.sort(key=lambda b: b.ts)
    return [
        Candle(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
        for b in rows
    ]


def run_engine(
    ticker: str,
    candles: list,
    config: a7.StrategyConfig,
    warmup_start: date,
    eval_start: date,
    eval_end: date,
    costs: a7.CostParams,
    runner=None,
):
    """Прогон одной конфигурации через EngineRunner. -> (strategy, ledger)."""
    hub = _PlanHub()
    strategy = DayEngineStrategy(config, warmup_start, eval_start, eval_end, hub)
    if runner is None:
        runner = EngineRunner(
            strategy, DayExitPolicy(hub, costs.slippage_bps),
            build_engine_config(ticker, costs),
        )
    ledger = runner.run(candles)
    return strategy, ledger
