"""Parity-тесты слоя Universe → Screener.

Задача — доказать, что разбиение монолитного app/bot/universe.py на слои
не изменило поведение. Эталон (oracle) зафиксирован в этом же файле
дословной копией прежней реализации; тесты гоняют эталон и новый код
на одних и тех же данных и сравнивают результат.

Живая БД не используется: сессия подменена FakeSession, записи — синтетика.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.universe import (
    ELIGIBLE,
    TRADEABLE,
    VOLATILE,
    InstrumentRef,
    ScreenItem,
    ScreenReason,
    discovery,
    resample_1m_to_5m,
    screen,
    screen_all,
    select_eligible_universe,
)
from app.bot.universe.bars import load_bars
from app.bot.universe.features import atr_pct, average_turnover
from app.bot.universe.selection import top_n
from app.engine.indicators import atr as atr_series
from app.engine.models import Candle as EC

BASE_TS = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)


def make_bars(figi: str, n: int, interval: int = 1, start: datetime = BASE_TS):
    """Детерминированный ценовой ряд: плавный рост с периодическими откатами."""
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.5 if (i // 7) % 2 == 0 else -0.3
        rows.append(
            SimpleNamespace(
                figi=figi,
                interval=interval,
                ts=start + timedelta(minutes=i * interval),
                open=price,
                high=price + 0.8,
                low=price - 0.8,
                close=price + 0.1,
                volume=1000.0 + (i % 11) * 50,
            )
        )
    return rows


def candles_by_figi(spec: dict) -> dict:
    return {figi: {1: rows, 5: rows} for figi, rows in spec.items()}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Отвечает ровно на те запросы, которые делает слой Universe."""

    def __init__(self, *, eligible_rows=(), instrument_rows=(), candles=None):
        self.eligible_rows = list(eligible_rows)
        self.instrument_rows = list(instrument_rows)
        self.candles = candles or {}
        self.queries = []

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM universe" in sql:
            self.queries.append("universe")
            return _Result(self.eligible_rows)
        if "FROM instrument_info" in sql:
            self.queries.append("instrument_info")
            return _Result(self.instrument_rows)

        compiled = stmt.compile()
        sql = str(compiled)
        params = compiled.params

        if "GROUP BY" in sql:
            interval = params.get("interval_1")
            min_bars = params.get("count_1", 1)
            wanted = set(params.get("figi_1") or [])
            self.queries.append(f"availability:{interval}:{min_bars}")
            out = [
                figi
                for figi in sorted(wanted)
                if len(self.candles.get(figi, {}).get(interval, [])) >= min_bars
            ]
            return _Result(out)

        if "FROM candles" in sql and "ORDER BY" in sql:
            figi = params["figi_1"]
            interval = params["interval_1"]
            limit = params.get("param_1")
            self.queries.append(f"bars:{figi}:{interval}:{limit}")
            rows = sorted(
                self.candles.get(figi, {}).get(interval, []),
                key=lambda r: r.ts,
                reverse=True,
            )
            if limit:
                rows = rows[:limit]
            return _Result(rows)

        if "FROM instruments" in sql:
            self.queries.append("instruments")
            return _Result(self.instrument_rows)

        raise AssertionError(f"FakeSession не умеет отвечать на: {sql}")


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Эталон: прежняя select_eligible_universe, дословно.
# --------------------------------------------------------------------------

async def legacy_select_eligible_universe(db, top_n: int = 20) -> list[dict]:
    from sqlalchemy import func, select, text

    from app.models.candle import Candle

    rows = (
        await db.execute(
            text(
                "SELECT figi, ticker, lot_size, avg_price, avg_daily_turnover, sector "
                "FROM universe WHERE eligible_tier = 'eligible' ORDER BY ticker"
            )
        )
    ).all()
    if not rows:
        return []

    figi_list = [r[0] for r in rows]
    recent = (
        await db.execute(
            select(Candle.figi)
            .where(Candle.figi.in_(figi_list), Candle.interval == 1)
            .group_by(Candle.figi)
            .having(func.count(Candle.ts) >= 1)
        )
    ).scalars().all()
    has_data = set(recent)

    result = []
    for figi, ticker, lot, price, turnover, sector in rows:
        if figi not in has_data:
            continue

        bars = (
            await db.execute(
                select(Candle)
                .where(Candle.figi == figi, Candle.interval == 1)
                .order_by(Candle.ts.desc())
                .limit(1000)
            )
        ).scalars().all()
        if len(bars) < 5:
            continue

        _by5 = {}
        for b in reversed(bars):
            key = int(b.ts.timestamp() // 300)
            if key not in _by5:
                _by5[key] = {
                    "ts": datetime.fromtimestamp(key * 300, tz=timezone.utc),
                    "open": float(b.open), "high": float(b.high),
                    "low": float(b.low), "close": float(b.close),
                    "volume": float(b.volume),
                }
            else:
                g = _by5[key]
                g["high"] = max(g["high"], float(b.high))
                g["low"] = min(g["low"], float(b.low))
                g["close"] = float(b.close)
                g["volume"] += float(b.volume)
        ec = [
            EC(ts=g["ts"], open=g["open"], high=g["high"], low=g["low"],
               close=g["close"], volume=g["volume"])
            for g in _by5.values()
        ]
        if len(ec) < 15:
            continue
        values = atr_series(ec[-44:], 14)
        last_atr = next((v for v in reversed(values) if v is not None), None)

        result.append({
            "figi": figi,
            "ticker": ticker,
            "lot": int(lot) if lot else 10,
            "name": ticker,
            "atr_pct": round(last_atr / ec[-1].close * 100, 3) if last_atr and ec[-1].close else 0,
            "avg_price": float(price) if price else 0,
            "avg_turnover": float(turnover) if turnover else 0,
            "sector": sector or "",
        })

    result.sort(key=lambda x: x["atr_pct"], reverse=True)
    return result[:top_n]


def eligible_row(figi, ticker, lot=10, price=100.0, turnover=5e8, sector="energy"):
    return (figi, ticker, lot, price, turnover, sector)


# --------------------------------------------------------------------------
# 1. Ресемпл: канонический Resampler против прежнего инлайн-алгоритма.
# --------------------------------------------------------------------------

def legacy_resample_5m(bars_desc) -> list[EC]:
    _by5 = {}
    for b in reversed(bars_desc):
        key = int(b.ts.timestamp() // 300)
        if key not in _by5:
            _by5[key] = {
                "ts": datetime.fromtimestamp(key * 300, tz=timezone.utc),
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume),
            }
        else:
            g = _by5[key]
            g["high"] = max(g["high"], float(b.high))
            g["low"] = min(g["low"], float(b.low))
            g["close"] = float(b.close)
            g["volume"] += float(b.volume)
    return [
        EC(ts=g["ts"], open=g["open"], high=g["high"], low=g["low"],
           close=g["close"], volume=g["volume"])
        for g in _by5.values()
    ]


def test_resampler_matches_legacy_inmemory_bucketing():
    for n in (5, 14, 15, 75, 1000):
        bars_asc = make_bars("BBG1", n)
        got = resample_1m_to_5m(bars_asc, "BBG1")
        want = legacy_resample_5m(list(reversed(bars_asc)))
        assert len(got) == len(want), f"n={n}: {len(got)} != {len(want)}"
        for a, b in zip(got, want):
            assert a.ts == b.ts
            assert (a.open, a.high, a.low, a.close, a.volume) == (
                b.open, b.high, b.low, b.close, b.volume
            ), f"n={n} mismatch at {a.ts}"


def test_resampler_keeps_partial_last_bucket():
    bars = make_bars("BBG1", 7)
    got = resample_1m_to_5m(bars, "BBG1")
    assert len(got) == 2
    assert got[0].ts == BASE_TS
    assert got[1].ts == BASE_TS + timedelta(minutes=5)


# --------------------------------------------------------------------------
# 2. Симметрия профилей: eligible оставляет инструмент без ATR, остальные режут.
# --------------------------------------------------------------------------

def test_eligible_profile_keeps_instrument_without_atr():
    item = ScreenItem(ref=InstrumentRef("SBER", "BBG1"), source_bars=100, resampled_bars=20)
    assert screen(item, ELIGIBLE).eligible is True


def test_tradeable_profile_drops_instrument_without_atr():
    item = ScreenItem(
        ref=InstrumentRef("SBER", "BBG1"), source_bars=100, features_valid=False
    )
    result = screen(item, TRADEABLE)
    assert result.eligible is False
    assert result.reason is ScreenReason.INVALID_MARKET_DATA


def test_volatile_profile_drops_instrument_without_atr():
    item = ScreenItem(
        ref=InstrumentRef("SBER", "BBG1"), source_bars=50, features_valid=False
    )
    assert screen(item, VOLATILE).eligible is False


def test_screener_reports_threshold_reasons():
    no_data = ScreenItem(ref=InstrumentRef("A", "f1"), source_bars=0)
    assert screen(no_data, ELIGIBLE).reason is ScreenReason.NO_DATA

    few = ScreenItem(ref=InstrumentRef("B", "f2"), source_bars=4, resampled_bars=1)
    assert screen(few, ELIGIBLE).reason is ScreenReason.INSUFFICIENT_BARS

    short = ScreenItem(ref=InstrumentRef("C", "f3"), source_bars=80, resampled_bars=14)
    result = screen(short, ELIGIBLE)
    assert result.eligible is False
    assert result.reason is ScreenReason.INSUFFICIENT_BARS
    assert "5m" in result.detail


def test_screener_does_not_mutate_input():
    item = ScreenItem(ref=InstrumentRef("A", "f1"), source_bars=3)
    snapshot = (item.ref, item.source_bars, item.features_valid)
    screen(item, ELIGIBLE)
    assert (item.ref, item.source_bars, item.features_valid) == snapshot


def test_screen_all_preserves_candidate_order():
    items = [
        ScreenItem(ref=InstrumentRef("AAA", "f1"), source_bars=50, resampled_bars=20),
        ScreenItem(ref=InstrumentRef("BBB", "f2"), source_bars=1, resampled_bars=0),
        ScreenItem(ref=InstrumentRef("CCC", "f3"), source_bars=50, resampled_bars=20),
    ]
    passed = screen_all(items, ELIGIBLE)
    assert [p.instrument.ticker for p in passed] == ["AAA", "CCC"]


# --------------------------------------------------------------------------
# 3. Признаки: atr_pct и оборот против прежней формулы.
# --------------------------------------------------------------------------

def legacy_atr_pct(bars: list[EC], window: int = 44) -> float:
    values = atr_series(bars[-window:], 14)
    last_atr = next((v for v in reversed(values) if v is not None), None)
    return round(last_atr / bars[-1].close * 100, 3) if last_atr and bars[-1].close else 0


def test_atr_pct_matches_legacy_formula():
    bars = [EC(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
               volume=b.volume) for b in make_bars("BBG1", 200)]
    for window in (44, 34, 20):
        got, valid = atr_pct(bars, window)
        assert got == legacy_atr_pct(bars, window), f"window={window}"
        assert valid is True


def test_atr_pct_invalid_when_close_is_zero():
    bars = [EC(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
               volume=b.volume) for b in make_bars("BBG1", 60)]
    bars[-1] = EC(ts=bars[-1].ts, open=1.0, high=1.0, low=1.0, close=0.0, volume=10.0)
    got, valid = atr_pct(bars, 44)
    assert got == 0
    assert valid is False


def test_average_turnover_divides_by_window_not_by_count():
    bars = [EC(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
               volume=b.volume) for b in make_bars("BBG1", 30)]
    want = sum(float(b.close) * float(b.volume) for b in bars[-10:]) / 10
    assert average_turnover(bars, 10) == want


# --------------------------------------------------------------------------
# 4. Отбор: top_n совпадает со стабильным list.sort(reverse=True).
# --------------------------------------------------------------------------

def test_top_n_matches_stable_sort():
    items = [{"k": 1.0, "i": "a"}, {"k": 3.0, "i": "b"}, {"k": 1.0, "i": "c"}]
    got = top_n(items, key=lambda r: r["k"], limit=10)
    assert [r["i"] for r in got] == ["b", "a", "c"]


def test_top_n_respects_limit():
    items = [{"k": float(i)} for i in range(10)]
    assert len(top_n(items, key=lambda r: r["k"], limit=3)) == 3


# --------------------------------------------------------------------------
# 5. Сквозной паритет select_eligible_universe: эталон против нового кода.
# --------------------------------------------------------------------------

PARITY_CANDLES = {
    "figi_good": make_bars("figi_good", 400),
    "figi_15bars": make_bars("figi_15bars", 15),
    "figi_14bars": make_bars("figi_14bars", 14),
    "figi_thin": make_bars("figi_thin", 3),
    "figi_nodata": [],
}

PARITY_ROWS = [
    eligible_row("figi_good", "ALRS", lot=10, price=118.5, turnover=7.7e8, sector="metals"),
    eligible_row("figi_15bars", "GAZP", lot=None, price=None, turnover=None, sector=None),
    eligible_row("figi_14bars", "LKOH", lot=1, price=6400.0, turnover=2.2e9, sector="oil"),
    eligible_row("figi_thin", "MTSS", lot=1, price=560.0, turnover=3.3e8, sector="telecom"),
    eligible_row("figi_nodata", "PHOR", lot=1, price=6000.0, turnover=1.1e9, sector="chem"),
]


def test_select_eligible_universe_matches_legacy_end_to_end():
    for top in (1, 3, 20):
        legacy_db = FakeSession(
            eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES)
        )
        new_db = FakeSession(
            eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES)
        )
        want = run(legacy_select_eligible_universe(legacy_db, top))
        got = run(select_eligible_universe(new_db, top))
        assert got == want, f"top_n={top}\ngot  ={got}\nwant ={want}"


def test_select_eligible_universe_skips_availability_prefilter():
    """Пре-фильтр GROUP BY/HAVING убран: он стоил 86% цикла и ничего не менял.

    FIGI без свечей уже покрыт end-to-end тестом выше — там он отсекается
    по source_bars. Здесь фиксируем инвариант плана запросов и паритет выхода.
    """
    legacy_db = FakeSession(
        eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES)
    )
    new_db = FakeSession(
        eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES)
    )
    want = run(legacy_select_eligible_universe(legacy_db, 20))
    got = run(select_eligible_universe(new_db, 20))

    assert got == want
    assert any(q.startswith("availability") for q in legacy_db.queries), \
        "эталон обязан делать пре-фильтр — иначе тест не проверяет ничего"
    assert not any(q.startswith("availability") for q in new_db.queries), \
        "новый код не должен делать пре-фильтр"


def test_select_eligible_universe_is_deterministic():
    db_a = FakeSession(eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES))
    db_b = FakeSession(eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES))
    assert run(select_eligible_universe(db_a)) == run(select_eligible_universe(db_b))


def test_select_eligible_universe_empty_table():
    db = FakeSession(eligible_rows=[], candles={})
    assert run(select_eligible_universe(db)) == []


def test_select_eligible_universe_defaults_are_preserved():
    db = FakeSession(eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES))
    got = run(select_eligible_universe(db))
    legacy = run(legacy_select_eligible_universe(
        FakeSession(eligible_rows=PARITY_ROWS, candles=candles_by_figi(PARITY_CANDLES)), 20
    ))
    assert got == legacy
    for row in got:
        assert set(row) == {"figi", "ticker", "lot", "name", "atr_pct",
                            "avg_price", "avg_turnover", "sector"}


# --------------------------------------------------------------------------
# 6. Universe-слой: порядок кандидатов и детерминизм снимка.
# --------------------------------------------------------------------------

def test_liquid_universe_follows_static_ticker_order():
    figi_by_ticker = {"SBER": "f1", "GAZP": "f2", "LKOH": "f3"}
    snapshot = discovery.discover_liquid_universe(figi_by_ticker)
    assert [e.ref.ticker for e in snapshot.entries] == ["SBER", "GAZP", "LKOH"]


def test_liquid_universe_skips_tickers_without_figi():
    snapshot = discovery.discover_liquid_universe({"SBER": "f1", "GAZP": None})
    assert [e.ref.ticker for e in snapshot.entries] == ["SBER"]


def test_snapshot_instruments_mirrors_entries():
    snapshot = discovery.discover_liquid_universe({"SBER": "f1"})
    assert snapshot.instruments == tuple(e.ref for e in snapshot.entries)


def test_count_bars_by_figi_respects_threshold():
    db = FakeSession(candles=candles_by_figi({
        "a": make_bars("a", 40),
        "b": make_bars("b", 10),
    }))
    got = run(discovery.count_bars_by_figi(db, ["a", "b"], interval=1, min_bars=30))
    assert got == {"a"}


def test_count_bars_by_figi_empty_input_makes_no_query():
    db = FakeSession()
    assert run(discovery.count_bars_by_figi(db, [], interval=1)) == set()
    assert db.queries == []


def test_load_bars_returns_ascending_and_respects_limit():
    db = FakeSession(candles=candles_by_figi({"a": make_bars("a", 50)}))
    bars = run(load_bars(db, "a", interval=1, limit=10))
    assert len(bars) == 10
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)
