"""Слой Universe: отбор акций для торговли.

Разложен на независимые слои вместо трёх функций в одном файле:

    discovery.py  — кто попадает в рассмотрение (таблица universe,
                    instrument_info, список ликвидных тикеров)
    bars.py       — доступность данных и ресемпл 1m -> 5m
    features.py   — признаки (ATR, ATR%) и их FeatureSet-контракт
    screener.py   — чистые правила допуска, без I/O
    selection.py  — ранжирование, percentile и Top-N
    allocation.py — детерминированное превращение SelectionResult в целевой
                    размер позиции (TargetPortfolio)
    rebalance.py  — разница между текущим и желаемым состоянием (RebalancePlan)
    policy.py     — превращение плана в целевые намерения исполнения (OrderIntent)
    compat.py     — прежний публичный API, собранный из слоёв выше

Конвейер Phase 1-8 (контракты в domain.py):

    UniverseSnapshot → ScreenedInstrument → FeatureSet[] → SelectionResult
        → EqualWeightAllocation → TargetPortfolio → DirectTargetRebalance
        → RebalancePlan → DirectRebalancePolicy → OrderIntent[+rejected]

Контракт прежнего API сохранён: `from app.bot.universe import
select_eligible_universe, select_volatile_universe` продолжает работать
без изменений на стороне вызова (app/bot/runtime.py).

ВАЖНО: live-рантайм использует ТОЛЬКО compat-путь (`select_eligible_universe`).
Selection/Allocation/Rebalance/Policy — исследовательский API: он не подключён
к торговому решению и не порождает ордеров. TargetPortfolio — ЖЕЛАЕМОЕ
состояние; RebalancePlan — разница состояний; OrderIntent по документам Phase 8
даже не исполняется — это желание, а не сделка.
"""
from __future__ import annotations

from .bars import load_all_bars, load_bars, resample_1m_to_5m
from .allocation import (
    AllocationPolicy,
    EqualWeightAllocation,
)
from .rebalance import (
    DirectTargetRebalance,
    RebalancePlanner,
    current_portfolio_from_dicts,
    plan_target_positions_are_unique,
)
from .policy import (
    DirectRebalancePolicy,
    RebalancePolicy,
    order_intent_to_legacy,
)
from .compat import (
    align_step,
    count_day_candles,
    select_all_tradeable,
    select_eligible_universe,
    select_volatile_universe,
    session_date,
    utc_now,
)
from .discovery import (
    LIQUID_TICKERS,
    count_bars_by_figi,
    discover_eligible_universe,
    discover_liquid_universe,
    discover_tradeable_universe,
)
from .domain import (
    AllocationInput,
    CurrentPortfolio,
    CurrentPosition,
    FeatureSet,
    InstrumentRef,
    RankingMethod,
    OrderIntent,
    PolicyResult,
    RebalanceAction,
    RebalanceActionType,
    RebalanceContext,
    RebalancePlan,
    RejectedAction,
    RejectionReason,
    ScreenItem,
    ScreenReason,
    ScreenedInstrument,
    ScreenResult,
    SelectionItem,
    SelectionResult,
    TargetPortfolio,
    TargetPosition,
    UniverseEntry,
    UniverseSnapshot,
    UniverseSource,
    MarketFeatures,
    SectorMembership,
    StrategyFamily,
    TrendDirection,
    TrendFeatures,
    VolatilityFeatures,
)
from .features import (
    ATR_PERIOD,
    FEATURE_WINDOW,
    average_turnover,
    atr_pct,
    compute_feature_set,
    compute_feature_sets,
    compute_market_features,
    compute_market_features_many,
)
from .eligibility import (
    EligibilityResult,
    eligibility_screen,
    eligible_instruments,
)
from .sectors import (
    sector_memberships,
    get_sector,
    group_by_sector,
    get_sector_members,
    sectors_known,
)
from .volatility import (
    compute_volatility_features,
    compute_volatility_features_many,
)
from .trend import (
    compute_trend_features,
    compute_trend_features_many,
)
from .screener import (
    ELIGIBLE,
    PROFILES,
    TRADEABLE,
    VOLATILE,
    ScreenProfile,
    screen,
    screen_all,
    screen_by_strategy,
    screen_reasons,
    StrategyScreener,
    StrategyScreenResult,
    TrendStrengthScreener,
    MeanReversionScreener,
)
from .selection import SelectionStrategy, rank_features, select, top_n, rank_candidates, select_top_n, RankedCandidate

__all__ = [
    "select_eligible_universe",
    "select_all_tradeable",
    "select_volatile_universe",
    "count_day_candles",
    "utc_now",
    "session_date",
    "align_step",
    "LIQUID_TICKERS",
    "discover_eligible_universe",
    "discover_tradeable_universe",
    "discover_liquid_universe",
    "count_bars_by_figi",
    "load_bars",
    "load_all_bars",
    "resample_1m_to_5m",
    "atr_pct",
    "average_turnover",
    "compute_feature_set",
    "compute_feature_sets",
    "ATR_PERIOD",
    "FEATURE_WINDOW",
    "screen",
    "screen_all",
    "screen_reasons",
    "ScreenProfile",
    "ELIGIBLE",
    "TRADEABLE",
    "VOLATILE",
    "PROFILES",
    "top_n",
    "rank_features",
    "select",
    "SelectionStrategy",
    "AllocationPolicy",
    "EqualWeightAllocation",
    "AllocationInput",
    "TargetPosition",
    "TargetPortfolio",
    "CurrentPosition",
    "CurrentPortfolio",
    "RebalancePlanner",
    "DirectTargetRebalance",
    "RebalanceAction",
    "RebalanceActionType",
    "RebalancePlan",
    "RebalancePolicy",
    "DirectRebalancePolicy",
    "RebalanceContext",
    "PolicyResult",
    "OrderIntent",
    "RejectedAction",
    "RejectionReason",
    "order_intent_to_legacy",
    "current_portfolio_from_dicts",
    "plan_target_positions_are_unique",
    "FeatureSet",
    "RankingMethod",
    "SelectionItem",
    "SelectionResult",
    "InstrumentRef",
    "ScreenItem",
    "ScreenReason",
    "ScreenResult",
    "ScreenedInstrument",
    "UniverseEntry",
    "UniverseSnapshot",
    "UniverseSource",
    "screen_by_strategy",
    "StrategyScreener",
    "StrategyScreenResult",
    "TrendStrengthScreener",
    "MeanReversionScreener",
    "rank_candidates",
    "select_top_n",
    "RankedCandidate",
    "MarketFeatures",
    "SectorMembership",
    "StrategyFamily",
    "TrendDirection",
    "TrendFeatures",
    "VolatilityFeatures",
    "compute_market_features",
    "compute_market_features_many",
    "EligibilityResult",
    "eligibility_screen",
    "eligible_instruments",
    "sector_memberships",
    "get_sector",
    "group_by_sector",
    "get_sector_members",
    "sectors_known",
    "compute_volatility_features",
    "compute_volatility_features_many",
    "compute_trend_features",
    "compute_trend_features_many",
]
