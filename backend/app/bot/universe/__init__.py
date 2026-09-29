"""Слой Universe: отбор акций для торговли.

Разложен на независимые слои вместо трёх функций в одном файле:

    discovery.py  — кто попадает в рассмотрение (таблица universe,
                    instrument_info, список ликвидных тикеров)
    bars.py       — доступность данных и ресемпл 1m -> 5m
    features.py   — признаки (ATR, ATR%, оборот)
    screener.py   — чистые правила допуска, без I/O
    selection.py  — ранжирование и Top-N
    compat.py     — прежний публичный API, собранный из слоёв выше

Контракт прежнего API сохранён: `from app.bot.universe import
select_eligible_universe, select_volatile_universe` продолжает работать
без изменений на стороне вызова (app/bot/runtime.py).
"""
from __future__ import annotations

from .bars import load_all_bars, load_bars, resample_1m_to_5m
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
    InstrumentRef,
    ScreenItem,
    ScreenReason,
    ScreenedInstrument,
    ScreenResult,
    UniverseEntry,
    UniverseSnapshot,
    UniverseSource,
)
from .features import average_turnover, atr_pct
from .screener import (
    ELIGIBLE,
    PROFILES,
    TRADEABLE,
    VOLATILE,
    ScreenProfile,
    screen,
    screen_all,
    screen_reasons,
)
from .selection import top_n

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
    "screen",
    "screen_all",
    "screen_reasons",
    "ScreenProfile",
    "ELIGIBLE",
    "TRADEABLE",
    "VOLATILE",
    "PROFILES",
    "top_n",
    "InstrumentRef",
    "ScreenItem",
    "ScreenReason",
    "ScreenResult",
    "ScreenedInstrument",
    "UniverseEntry",
    "UniverseSnapshot",
    "UniverseSource",
]
