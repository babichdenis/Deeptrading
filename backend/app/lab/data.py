"""Данные Signal Lab: синхронный коннект, 1m-загрузка, канонический реземпл.

Синхронный SQLAlchemy (+psycopg2) — как в scripts/ose_exit_matrix.py: защита от
вечных сетевых ожиданий (connect_timeout, keepalives, statement_timeout).
Реземпл — только канонический Resampler (метка = начало UTC-бакета).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import get_settings
from app.marketdata.resampler import Resampler
from app.engine.models import Candle as EngineCandle


@dataclass
class LabBar:
    """Бар с figi — совместим с Resampler (duck-typing) и EngineCandle."""
    figi: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def sync_url(url: str | None = None) -> str:
    if url:
        return url
    # Удалённые воркеры (машина .8) берут DSN из окружения — секрет не в коде.
    env = (os.environ.get("LAB_DB_URL") or "").strip()
    if env:
        return env
    return get_settings().database_url.replace("+asyncpg", "")


def engine_sync(url: str | None = None):
    return create_engine(
        sync_url(url),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "options": "-c statement_timeout=600000",
        },
    )


def resolve_universe(eng, tickers: list[str]) -> list[dict]:
    """Тикеры → [{ticker, figi, lot}] в порядке конфига (детерминизм)."""
    if not tickers:
        return []
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT figi, ticker, lot FROM instruments WHERE upper(ticker) = ANY(:t)"
        ), {"t": [t.upper() for t in tickers]}).fetchall()
    got = {str(r[1]).upper(): {"figi": r[0], "ticker": str(r[1]), "lot": int(r[2] or 1)}
           for r in rows}
    out = []
    for t in tickers:
        item = got.get(t.upper())
        if item is None:
            continue
        out.append(item)
    return out


def load_1m(eng, figi: str, ts_from: str, ts_to: str) -> list[LabBar]:
    """Минутные бары из candles (interval=1), канонический источник."""
    with eng.connect() as c:
        rows = c.execute(text(
            'SELECT ts, open, high, low, close, volume FROM candles '
            'WHERE figi = :f AND "interval" = 1 AND ts >= :a AND ts <= :b ORDER BY ts'
        ), {"f": figi, "a": ts_from, "b": ts_to}).fetchall()
    return [LabBar(figi=figi, ts=r[0], open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5] or 0.0))
            for r in rows]


def build_tf(bars_1m: list[LabBar], tf: str) -> list[LabBar]:
    """1m → tf каноническим Resampler'ом (закрытые бакеты + flush)."""
    if tf == "1min":
        return list(bars_1m)
    rs = Resampler(tf)
    out: list[LabBar] = []
    for bar in bars_1m:
        closed = rs.feed(bar)
        if closed is not None:
            out.append(closed)
    out.extend(rs.flush())
    return out


def to_engine_candles(bars: list[LabBar]) -> list[EngineCandle]:
    return [EngineCandle(ts=b.ts, open=b.open, high=b.high, low=b.low,
                         close=b.close, volume=b.volume) for b in bars]


def atr_series(bars: list[LabBar], period: int = 14) -> list[float | None]:
    """Канонический ATR (Wilder, SMA-seed) — app/engine/indicatorhub._atr.

    Возвращает ряд той же длины; None до прогрева (индекс < period).
    """
    from app.engine.indicatorhub import _atr
    candles = to_engine_candles(bars)
    vals = _atr(candles, period)
    return list(vals) if vals is not None else [None] * len(bars)


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()
