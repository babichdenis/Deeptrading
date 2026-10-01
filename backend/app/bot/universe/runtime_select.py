"""Мост runtime → Universe 2.0 (research/test): StrategyScreener → Top-N.

Подключается ТОЛЬКО в тест-контуре при settings.universe_mode ∈ UNIVERSE_MODES
(.env UNIVERSE_MODE). Live/sandbox-путь не затрагивает: там остаётся compat
(select_eligible_universe / select_volatile_universe).

Конвейер (см. app/bot/universe/__init__.py):
    discover_liquid_universe | discover_tradeable_universe → features(as_of)
        → TrendStrengthScreener | MeanReversionScreener → rank_candidates → Top-N

Режимы:
    v2_trend / v2_meanrev        — источник LIQUID_TICKERS (15 ликвидных)
    v2_trend_all / v2_meanrev_all — источник instrument_info (api_trade_available),
                                    полный рынок; предфильтр по наличию 5m-баров

Пороговые константы — research-дефолты (как в tests/test_universe_v2_e2e.py),
менять только осознанно вместе с документацией.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .bars import load_bars_bulk_bounded
from .discovery import (
    DEFAULT_LOT,
    discover_liquid_universe,
    discover_tradeable_universe,
    fetch_lot_by_figi,
)
from .domain import InstrumentRef
from .features import FEATURE_WINDOW, compute_market_features_many
from .screener import MeanReversionScreener, TrendStrengthScreener, screen_by_strategy
from .selection import rank_candidates

TREND_MIN_STRENGTH = 0.05      # TrendStrengthScreener: минимум |normalized_slope|
MEANREV_MAX_STRENGTH = 0.01    # MeanReversionScreener: максимум силы тренда

UNIVERSE_MODES = (
    "v2_trend", "v2_meanrev",
    "v2_trend_all", "v2_meanrev_all",
)

_SOURCE_INTERVAL_TRADEABLE = 5  # как в compat: фичи считаются по 5m барам
_MIN_BARS = 30                  # как TRADEABLE_MIN_BARS в compat


def _is_all_market(mode: str) -> bool:
    return mode.endswith("_all")


def _strategy_mode(mode: str) -> str:
    return mode[:-4] if _is_all_market(mode) else mode


async def _resolve_lots(
    db: AsyncSession, figis: list[str], known: dict[str, int]
) -> dict[str, int]:
    """Добирает лот для FIGI, которых нет в known, одним запросом.

    known приходит из источника Universe (instrument_info уже отдан в снимке).
    Остаток — хвост рынка, которого в снимке нет; его лот честно добирается из
    той же таблицы, а не подставляется константой.
    """
    missing = [f for f in figis if f not in known]
    resolved = dict(known)
    if missing:
        resolved.update(await fetch_lot_by_figi(db, missing))
    return resolved


async def _load_snapshot_bars(
    db: AsyncSession, figi_by_ticker: dict[str, str], mode: str, as_of: datetime,
) -> tuple[list, dict[str, list], dict[str, int]]:
    """(список ref'ов, bars_by_figi, lot_by_figi) для выбранного источника.

    Бары грузятся одним bounded-запросом: последние FEATURE_WINDOW баров с
    ts <= as_of на FIGI (AUDIT P1.1). Меры читают только хвост окна, поэтому
    результат совпадает с прежней загрузкой всей истории побитово, но вместо
    ~3000 строк на инструмент едет ~44. Эквивалентность зафиксирована тестом
    test_universe_v2_bulk_load.py.
    """
    if _is_all_market(mode):
        from sqlalchemy import text as _text

        snapshot = await discover_tradeable_universe(db)
        # instrument_info.figi может быть TCS-фигой, а свечи в БД — по BBG (как
        # в runtime): резолвим ticker → BBG через figi_by_ticker, fallback — своя фига.
        entries_by_figi: dict[str, tuple[str, int]] = {}
        for entry in snapshot.entries:
            bb = figi_by_ticker.get((entry.ref.ticker or "").upper()) or entry.ref.figi
            entries_by_figi.setdefault(bb, (entry.ref.ticker or "", int(entry.lot or DEFAULT_LOT)))
        ticker_by_figi = {v: k for k, v in figi_by_ticker.items()}

        # Полный рынок «по данным»: instrument_info.api_trade_available неполный
        # (127 из 502, без SBER) — берём все фиги с достаточной 5m-историей в candles;
        # тикер/лот — из tradeable-снимка, иначе из instruments (fallback хвост фиги, лот 10).
        # Предфильтр тоже ограничен as_of: иначе инструмент с историей ТОЛЬКО после
        # старта реплея проходил бы отбор и молча уходил в невалидные признаки.
        figis = (await db.execute(
            _text(
                "SELECT figi FROM candles WHERE interval=:i AND ts <= :a "
                "GROUP BY figi HAVING count(*) >= :m"
            ),
            {"i": _SOURCE_INTERVAL_TRADEABLE, "a": as_of, "m": _MIN_BARS},
        )).scalars().all()
        bars_by = await load_bars_bulk_bounded(
            db, figis, interval=_SOURCE_INTERVAL_TRADEABLE,
            window=FEATURE_WINDOW, as_of=as_of,
        )
        refs = [
            InstrumentRef(
                ticker=entries_by_figi.get(figi, (ticker_by_figi.get(figi, figi[-6:]), 0))[0],
                figi=figi,
            )
            for figi in figis
        ]
        known_lots = {figi: lot for figi, (_t, lot) in entries_by_figi.items()}
        return refs, bars_by, await _resolve_lots(db, list(figis), known_lots)

    snapshot = discover_liquid_universe(figi_by_ticker)
    figis = [e.ref.figi for e in snapshot.entries]
    bars_by = await load_bars_bulk_bounded(
        db, figis, interval=_SOURCE_INTERVAL_TRADEABLE,
        window=FEATURE_WINDOW, as_of=as_of,
    )
    return [e.ref for e in snapshot.entries], bars_by, await _resolve_lots(db, figis, {})


async def select_screened_universe(
    db: AsyncSession,
    figi_by_ticker: dict[str, str],
    *,
    mode: str,
    top_n: int,
    as_of: datetime,
) -> list[dict]:
    """Скрининг универса стратегией и Top-N по score (research/test).

    Возвращает legacy-совместимые dict'ы (figi/ticker/atr_pct/lot_size) —
    их напрямую принимает runtime._startup. as_of обязателен: фичи видят
    только bars.time <= as_of (look-ahead дисциплина).

    lot_size — реальный лот инструмента (AUDIT P1.2): он приходит из
    instrument_info, а DEFAULT_LOT подставляется только когда лот неизвестен
    (инструмента нет в instrument_info) — это видно по отсутствию ключа.
    """
    if mode not in UNIVERSE_MODES:
        raise ValueError(f"unknown universe mode: {mode!r} (ожидается {UNIVERSE_MODES})")

    refs, bars_by, lot_by_figi = await _load_snapshot_bars(db, figi_by_ticker, mode, as_of)
    features = compute_market_features_many(refs, bars_by, as_of=as_of)

    if _strategy_mode(mode) == "v2_trend":
        screener = TrendStrengthScreener(min_strength=TREND_MIN_STRENGTH)
    else:
        screener = MeanReversionScreener(max_strength=MEANREV_MAX_STRENGTH)

    results = screen_by_strategy(features, screener)
    score_by_figi = {f.instrument.figi: float(r.score or 0.0) for f, r in zip(features, results)}
    accepted = [f.instrument for f, r in zip(features, results) if r.accepted]
    ranked = rank_candidates(accepted, lambda ref: score_by_figi[ref.figi])[: max(top_n, 0)]

    features_by_figi = {f.instrument.figi: f for f in features}
    out: list[dict] = []
    for candidate in ranked:
        feat = features_by_figi[candidate.instrument.figi]
        vol = feat.volatility
        out.append({
            "figi": candidate.instrument.figi,
            "ticker": candidate.instrument.ticker,
            "atr_pct": float(getattr(vol, "atr_pct", 0.0) or 0.0),
            "lot_size": lot_by_figi.get(candidate.instrument.figi, DEFAULT_LOT),
            "screen": screener.name,
            "screen_score": candidate.score,
        })
    return out
