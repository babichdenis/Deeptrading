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

from .bars import load_all_bars
from .discovery import (
    discover_liquid_universe,
    discover_tradeable_universe,
)
from .domain import InstrumentRef
from .features import compute_market_features_many
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


async def _load_snapshot_bars(
    db: AsyncSession, figi_by_ticker: dict[str, str], mode: str,
) -> tuple[list, dict[str, list]]:
    """(список ref'ов, bars_by_figi) для выбранного источника универса.

    Для полного рынка — предфильтр count_bars_by_figi(min_bars), затем полная
    история (as_of-отсечение делает features); для ликвидного списка — вся
    доступная история (как compat).
    """
    if _is_all_market(mode):
        from sqlalchemy import text as _text

        snapshot = await discover_tradeable_universe(db)
        # instrument_info.figi может быть TCS-фигой, а свечи в БД — по BBG (как
        # в runtime): резолвим ticker → BBG через figi_by_ticker, fallback — своя фига.
        entries_by_figi: dict[str, tuple[str, int]] = {}
        for entry in snapshot.entries:
            bb = figi_by_ticker.get((entry.ref.ticker or "").upper()) or entry.ref.figi
            entries_by_figi.setdefault(bb, (entry.ref.ticker or "", int(entry.lot or 10)))
        ticker_by_figi = {v: k for k, v in figi_by_ticker.items()}

        # Полный рынок «по данным»: instrument_info.api_trade_available неполный
        # (127 из 502, без SBER) — берём все фиги с достаточной 5m-историей в candles;
        # тикер/лот — из tradeable-снимка, иначе из instruments (fallback хвост фиги, лот 10).
        figis = (await db.execute(
            _text("SELECT figi FROM candles WHERE interval=:i GROUP BY figi HAVING count(*) >= :m"),
            {"i": _SOURCE_INTERVAL_TRADEABLE, "m": _MIN_BARS},
        )).scalars().all()
        bars_by: dict[str, list] = {}
        refs = []
        for figi in figis:
            ticker, _lot = entries_by_figi.get(figi, (ticker_by_figi.get(figi, figi[-6:]), 10))
            # Вся история: фичи сами отсекают бары позже as_of (limit «последних N
            # от сейчас» брал бы бары ПОСЛЕ старта реплея и ломал as_of-дисциплину).
            bars_by[figi] = await load_all_bars(db, figi, interval=_SOURCE_INTERVAL_TRADEABLE)
            refs.append(InstrumentRef(ticker=ticker, figi=figi))
        return refs, bars_by

    snapshot = discover_liquid_universe(figi_by_ticker)
    bars_by = {}
    for entry in snapshot.entries:
        bars_by[entry.ref.figi] = await load_all_bars(
            db, entry.ref.figi, interval=_SOURCE_INTERVAL_TRADEABLE
        )
    return [e.ref for e in snapshot.entries], bars_by


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
    """
    if mode not in UNIVERSE_MODES:
        raise ValueError(f"unknown universe mode: {mode!r} (ожидается {UNIVERSE_MODES})")

    refs, bars_by = await _load_snapshot_bars(db, figi_by_ticker, mode)
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
            "lot_size": 10,
            "screen": screener.name,
            "screen_score": candidate.score,
        })
    return out
