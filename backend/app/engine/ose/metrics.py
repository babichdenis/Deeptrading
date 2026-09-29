"""Метрики прогона OsEngine-портов роботов (app/engine/ose/metrics.py).

Извлечение метрик из прогонного скрипта backtest_ose.py (живёт на .8) в
тестируемый модуль. Семантика сохранена 1:1:

- единицы — пункты (разности цен), 1 контракт, без комиссий;
- сделки — FIFO: каждый fill ("close", price) относится к старейшей
  незакрытой позиции tab.positions[closed]; корректно для всех портов
  Wave A/B — они входят только при отсутствии открытых позиций;
- эквити — mark-to-market на закрытии бара: realized + unrealized
  (нереализованное считается по close последнего бара);
- max_dd — максимум (peak - equity) по закрытиям баров; интрабарные
  хвосты не учитываются (так вёл счёт backtest_ose.py); первый бар
  задаёт пик (peak = equity первого бара), а не 0;
- in_market_pct — доля баров, на закрытии которых есть открытая позиция;
  выход внутри бара (стоп-слот) делает бар «вне рынка»;
- buy_hold — last_close - first_close: что дал бы buy&hold на серии.

Использование (порядок событий — как у тестера OsEngine):
    tracker = MetricsTracker(tab)
    for c in candles:
        tab.process_intrabar(c)            # стопы/слоты внутри бара
        robot.on_candle_finished(prefix)   # событие закрытия бара
        tracker.on_bar(c)                  # метрики после событий
    report = tracker.report()

Или целиком: report = run_robot_with_metrics(robot, candles).

Как тестируется (tests/test_ose_metrics.py): юнит-сценарии с ручным
вождением TesterTab и числами, посчитанными руками; инварианты (pnl =
realized + unrealized, max_dd воспроизводится пересчётом по
equity_curve); интеграция — реальный порт на серии с уже проверенными
в test_ose_robots.py fill'ами.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from app.engine.models import Candle

from .robots import Side, TesterTab

__all__ = ["MetricsTracker", "run_robot_with_metrics", "synthetic_series"]


@dataclass
class MetricsTracker:
    """Накопитель метрик поверх TesterTab (семантика — в докстринге
    модуля). on_bar вызывается после process_intrabar и
    on_candle_finished бара: сначала досъедаются новые fill'ы (close →
    реализация по FIFO), затем mark-to-market, peak/dd и счётчики."""

    tab: TesterTab
    fill_ptr: int = 0
    closed: int = 0
    realized: float = 0.0
    unrealized: float = 0.0
    equity: float = 0.0
    peak: float | None = None
    max_dd: float = 0.0
    bars: int = 0
    bars_in_market: int = 0
    first_close: float | None = None
    last_close: float | None = None
    trades: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def on_bar(self, candle: Candle) -> None:
        # 1) новые fill'ы: close реализуется по FIFO-префиксу позиций
        for action, price in self.tab.fills[self.fill_ptr:]:
            if action != "close":
                continue
            pos = self.tab.positions[self.closed]
            side = 1.0 if pos.side is Side.BUY else -1.0
            pnl = (price - pos.entry_price) * side
            self.realized += pnl
            self.trades.append(pnl)
            self.closed += 1
        self.fill_ptr = len(self.tab.fills)

        # 2) mark-to-market на close бара (открытые позиции)
        self.unrealized = 0.0
        for pos in self.tab.positions_open_all:
            side = 1.0 if pos.side is Side.BUY else -1.0
            self.unrealized += (candle.close - pos.entry_price) * side
        self.equity = self.realized + self.unrealized
        self.equity_curve.append(self.equity)

        # 3) peak/dd: первый бар задаёт пик (как в backtest_ose.py)
        if self.peak is None or self.equity > self.peak:
            self.peak = self.equity
        if self.peak - self.equity > self.max_dd:
            self.max_dd = self.peak - self.equity

        # 4) счётчики баров
        self.bars += 1
        if self.tab.positions_open_all:
            self.bars_in_market += 1
        if self.first_close is None:
            self.first_close = candle.close
        self.last_close = candle.close

    def report(self) -> dict:
        """Итог прогона. profit_factor: gross_win/gross_loss; при
        gross_loss == 0 — inf (есть прибыли) или 0.0 (прибылей нет)."""
        wins = [p for p in self.trades if p > 0]
        losses = [p for p in self.trades if p <= 0]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        if gross_loss > 0:
            pf: float = gross_win / gross_loss
        else:
            pf = float("inf") if gross_win > 0 else 0.0
        return {
            "bars": self.bars,
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_pct": (len(wins) / len(self.trades) * 100.0) if self.trades else 0.0,
            "profit_factor": pf,
            "realized": self.realized,
            "unrealized": self.unrealized,
            "pnl": self.equity,
            "avg_trade": (self.realized / len(self.trades)) if self.trades else 0.0,
            "max_dd": self.max_dd,
            "in_market_pct": (self.bars_in_market / self.bars * 100.0) if self.bars else 0.0,
            "buy_hold": (self.last_close - self.first_close) if self.first_close is not None else 0.0,
            "equity_curve": list(self.equity_curve),
        }


def run_robot_with_metrics(robot, candles: Sequence[Candle]) -> dict:
    """Прогон робота по серии с метриками. Порядок событий — как у
    тестера OsEngine: process_intrabar → CandleFinishedEvent. События
    роботу уходят с префиксов ≥ 2 свечей (некоторым индикаторам нужна
    пара баров — так же в feed() тестов); трекер при этом видит каждый
    бар серии."""
    tab = robot.tab
    tracker = MetricsTracker(tab)
    n = len(candles)
    for size in range(1, n + 1):
        c = candles[size - 1]
        if size >= 2:
            tab.process_intrabar(c)
            robot.on_candle_finished(candles[:size])
        tracker.on_bar(c)
    return tracker.report()


def synthetic_series(n: int = 400, seed: int = 42) -> list[Candle]:
    """Детерминированная серия для приёмочных гейтов и замеров роботов:
    синус-дрейф (период ~75 баров — тренды и откаты) + равномерный шум.
    random.Random(seed) кроссплатформен — числа совпадают на Mac и .8,
    что и требуется для сравнения замеров двух машин."""
    rng = random.Random(seed)
    t0 = datetime(2026, 1, 5, 10, 0)
    out: list[Candle] = []
    price = 100.0
    for i in range(n):
        close = price + math.sin(i / 12.0) * 0.8 + rng.uniform(-1.0, 1.0)
        high = max(price, close) + rng.uniform(0.0, 0.6)
        low = min(price, close) - rng.uniform(0.0, 0.6)
        out.append(Candle(ts=t0 + timedelta(minutes=i), open=price,
                          high=high, low=low, close=close, volume=100.0))
        price = close
    return out
