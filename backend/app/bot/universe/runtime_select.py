"""Мост runtime → Universe 2.0 (research/test): StrategyScreener → Top-N.

Подключается ТОЛЬКО в тест-контуре при settings.universe_mode ∈ {v2_trend, v2_meanrev}
(.env UNIVERSE_MODE). Live/sandbox-путь не затрагивает: там остаётся compat
(select_eligible_universe / select_volatile_universe).

Конвейер (см. app/bot/universe/__init__.py):
    discover_liquid_universe → compute_market_features_many(as_of)
        → TrendStrengthScreener | MeanReversionScreener → rank_candidates → Top-N

Пороговые константы — research-дефолты (как в tests/test_universe_v2_e2e.py),
менять только осознанно вместе с документацией.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .bars import load_all_bars
from .discovery import discover_liquid_universe
from .features import compute_market_features_many
from .screener import MeanReversionScreener, TrendStrengthScreener, screen_by_strategy
from .selection import rank_candidates

TREND_MIN_STRENGTH = 0.05      # TrendStrengthScreener: минимум |normalized_slope|
MEANREV_MAX_STRENGTH = 0.01    # MeanReversionScreener: максимум силы тренда
UNIVERSE_MODES = ("v2_trend", "v2_meanrev")

_SOURCE_INTERVAL_TRADEABLE = 5  # как в compat: фичи считаются по 5m барам


async def select_screened_universe(
    db: AsyncSession,
    figi_by_ticker: dict[str, str],
    *,
    mode: str,
    top_n: int,
    as_of: datetime,
) -> list[dict]:
    """Скрининг ликвидного универса стратегией и Top-N по score (research/test).

    Возвращает legacy-совместимые dict'ы (figi/ticker/atr_pct/lot_size) —
    их напрямую принимает runtime._startup. as_of обязателен: фичи видят
    только bars.time <= as_of (look-ahead дисциплина).
    """
    if mode not in UNIVERSE_MODES:
        raise ValueError(f"unknown universe mode: {mode!r} (ожидается {UNIVERSE_MODES})")

    snapshot = discover_liquid_universe(figi_by_ticker)
    bars_by: dict[str, list] = {}
    for entry in snapshot.entries:
        bars_by[entry.ref.figi] = await load_all_bars(
            db, entry.ref.figi, interval=_SOURCE_INTERVAL_TRADEABLE
        )

    features = compute_market_features_many(
        [entry.ref for entry in snapshot.entries], bars_by, as_of=as_of
    )
    if mode == "v2_trend":
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
