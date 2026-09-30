"""Domain-контракты конвейера Universe → Screener → Features → Selection.

Отсюда вниз по конвейеру:
    UniverseSnapshot → ScreenResult/ScreenedInstrument → FeatureSet → SelectionResult

В этом модуле ТОЛЬКО контракты (frozen dataclasses и enum'ы), никакой логики:
Universe/Screener/Features/Selection считают в своих модулях
(discovery.py, screener.py, features.py, selection.py).

Слои разделены по ответственности, и это проверяется тестами:
    Screener  — только «можно ли передать дальше»;
    Features  — только считает признаки, не сортирует;
    Selection — только ранжирует и режет Top-N, не знает про веса;
    веса/деньги/ордера/позиции появляются на Phase 6-7 (Allocation, Rebalance).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from app.engine.models import Side


class UniverseSource(str, Enum):
    """Откуда Universe берёт кандидатов. Соответствует трём legacy-функциям."""

    ELIGIBLE_TABLE = "eligible_table"
    INSTRUMENT_INFO = "instrument_info"
    LIQUID_TICKERS = "liquid_tickers"


class ScreenReason(str, Enum):
    """Причины отсева. Список взят из реальных проверок app/bot/universe.py.

    Отдельные значения для сырых и ресемпленных баров нужны потому, что
    legacy-код отсеивал их двумя разными проверками на разных шагах.
    """

    NO_DATA = "no_data"
    INSUFFICIENT_BARS = "insufficient_bars"
    INSUFFICIENT_RESAMPLED_BARS = "insufficient_resampled_bars"
    INVALID_MARKET_DATA = "invalid_market_data"
    NOT_TRADEABLE = "not_tradeable"
    BLACKLISTED = "blacklisted"


@dataclass(frozen=True, order=True)
class InstrumentRef:
    """Однозначная идентичность инструмента.

    Порядок полей даёт стабильный tie-break (ticker ASC) для детерминизма
    snapshot'а и ранжирования.
    """

    ticker: str
    figi: str = ""
    exchange: str = ""


@dataclass(frozen=True)
class ScreenResult:
    """Вердикт допуска для одного инструмента."""

    eligible: bool
    reason: ScreenReason | None = None
    detail: str = ""


@dataclass(frozen=True)
class ScreenItem:
    """Вход Screener'а: кандидат плюс факты о его доступности.

    features_valid — вердикт Feature-слоя (ATR посчитан и close > 0).
    Сам ATR здесь не вычисляется и не хранится.
    """

    ref: InstrumentRef
    source_bars: int
    resampled_bars: int = 0
    features_valid: bool = True
    tradeable: bool = True
    blacklisted: bool = False

    @classmethod
    def for_ref(cls, ref: InstrumentRef, *, bars: int) -> "ScreenItem":
        return cls(ref=ref, source_bars=bars)


@dataclass(frozen=True)
class ScreenedInstrument:
    """Прошедший screening инструмент вместе с метаданными доступности."""

    instrument: InstrumentRef
    result: ScreenResult
    source_bars: int = 0
    resampled_bars: int = 0


@dataclass(frozen=True)
class UniverseEntry:
    """Кандидат Universe: идентичность, статика из БД и доступность данных."""

    ref: InstrumentRef
    lot: int = 10
    name: str = ""
    avg_price: float = 0.0
    avg_turnover: float = 0.0
    sector: str = ""
    bars: int = 0


@dataclass(frozen=True)
class UniverseSnapshot:
    """Детерминированный снимок кандидатов на момент as_of."""

    as_of: datetime
    source: UniverseSource
    entries: tuple[UniverseEntry, ...] = ()

    @property
    def instruments(self) -> tuple[InstrumentRef, ...]:
        return tuple(e.ref for e in self.entries)

    def with_bars(self, bars_by_figi: dict[str, int]) -> "UniverseSnapshot":
        """Возвращает копию snapshot'а с подставленной доступностью по figi."""
        return UniverseSnapshot(
            as_of=self.as_of,
            source=self.source,
            entries=tuple(
                UniverseEntry(
                    ref=e.ref,
                    lot=e.lot,
                    name=e.name,
                    avg_price=e.avg_price,
                    avg_turnover=e.avg_turnover,
                    sector=e.sector,
                    bars=bars_by_figi.get(e.ref.figi, 0),
                )
                for e in self.entries
            ),
        )


class RankingMethod(str, Enum):
    """Способ ранжирования Selection. Расширяется на Phase 5+."""

    ALL_ELIGIBLE = "all_eligible"
    TOP_N_ATR = "top_n_atr"


@dataclass(frozen=True)
class FeatureSet:
    """Признаки одного инструмента, посчитанные по барам до as_of включительно.

    close — последний видимый закрытый бар; именно он делитель в atr_pct.
    avg_price из UniverseEntry сюда НЕ входит: это статичная колонка таблицы
    universe, а не признак, посчитанный по рыночным данным.

    valid=False означает «признаки не получились» (нет баров, нулевой close или
    не хватило истории для ATR). Решение, что делать с невалидным инструментом,
    принимает Screener/Selection, а не этот слой.

    Никаких весов, рангов и целевых долей здесь нет — это Phase 6+.
    """

    instrument: InstrumentRef
    as_of: datetime
    atr: float | None = None
    atr_pct: float | None = None
    close: float = 0.0
    bars_used: int = 0
    valid: bool = False


@dataclass(frozen=True)
class SelectionItem:
    """Инструмент в ранкинге Selection: score, позиция и признаки.

    rank — 1-based, после детерминированного tie-break (score DESC, ticker ASC).
    atr_pct сохранён как есть: percentile его дополняет, но не заменяет, и live
    продолжает читать абсолютное значение.

    Здесь намеренно НЕТ target_weight/notional/quantity/lots: это ответственность
    Allocation, а Selection отвечает только на вопрос «кто выше в ранкинге».
    """

    instrument: InstrumentRef
    score: float
    rank: int
    atr: float | None = None
    atr_pct: float | None = None
    atr_pct_percentile: float | None = None


@dataclass(frozen=True)
class AllocationInput:
    """Всё, что нужно детерминированной аллокации. Broker сюда не передаётся.

    prices/lot_sizes индексированы по figi (canonical identity), с фолбэком на
    ticker: у InstrumentRef figi может быть пустой. Пропуск инструмента в мапе
    трактуется как невалидная цена/лот — позиция не создаётся.
    """

    selection: SelectionResult
    equity: float
    prices: Mapping[str, float]
    lot_sizes: Mapping[str, int]
    as_of: datetime | None = None


@dataclass(frozen=True)
class TargetPosition:
    """Целевая позиция: желаемый размер. Не список сделок.

    target_weight — теоретический вес policy (1/N). actual_weight — реальный вес
    после округления до лота (actual_notional / equity): он может лежать ниже
    target_weight, и это нормальный результат lot-aware sizing.

    Здесь НЕТ order_id/side/execution status/fill/slippage/commission — это
    слои Execution. Selection может «выпасть» из Top-N, но продажа решится
    на Phase 7 (RebalancePlan), не здесь.
    """

    instrument: InstrumentRef
    target_weight: float
    actual_weight: float
    target_qty: int
    actual_qty: int
    target_notional: float
    actual_notional: float
    price: float
    lot_size: int


@dataclass(frozen=True)
class TargetPortfolio:
    """Желаемое состояние портфеля на as_of. НЕ текущий портфель и НЕ ордера.

    positions — детерминированный кортеж в порядке SelectionResult.selected.
    cash_target (резерв) и unallocated_cash (остаток после округления до лотов)
    разделены намеренно: первый задаёт policy, второй — результат floor-rounding.
    """

    as_of: datetime | None
    equity: float
    positions: tuple[TargetPosition, ...]
    cash_target: float
    unallocated_cash: float
    allocation_method: str
    reserve_cash_pct: float = 0.0

    def validate(self) -> tuple[str, ...]:
        """Инварианты TargetPortfolio: пустой кортеж означает «всё ок».

        Проверяет веса, кратность лоту, номинал и ограничение по капиталу.
        Возвращает кортеж нарушений вместо исключения: на Phase 7 RebalancePlan
        сможет решать, что с ними делать.
        """
        violations: list[str] = []
        total = 0.0
        for pos in self.positions:
            if pos.target_weight < 0:
                violations.append(f"{pos.instrument.ticker}: target_weight < 0")
            if pos.target_qty < 0 or pos.actual_qty < 0:
                violations.append(f"{pos.instrument.ticker}: target_qty < 0")
            if pos.lot_size > 0 and pos.target_qty % pos.lot_size != 0:
                violations.append(f"{pos.instrument.ticker}: target_qty не кратен лоту")
            if pos.target_notional < 0 or pos.actual_notional < 0:
                violations.append(f"{pos.instrument.ticker}: negative notional")
            total += pos.actual_notional
        invested_limit = self.equity - self.cash_target
        if self.equity > 0 and total > invested_limit + 1e-9:
            violations.append(f"notional {total:.2f} > investable {invested_limit:.2f}")
        if self.cash_target < 0 or self.unallocated_cash < 0:
            violations.append("cash < 0")
        return tuple(violations)


@dataclass(frozen=True)
class SelectionResult:
    """Итог Selection: полный ранкинг плюс срезанный Top-N.

    items — ВСЕ проранжированные инструменты (для UI и исследований);
    selected — срез top_n. ranking_method фиксирует, чем считался score, чтобы
    результат был самодостаточным и воспроизводимым.

    Смена ранкинга НЕ означает сигнал на выход: из selected бумага может
    исчезнуть, но SELL/BUY/CLOSE Selection не порождает — это Phase 7
    (TargetPortfolio → RebalancePlan).
    """

    as_of: datetime | None
    items: tuple[SelectionItem, ...]
    selected: tuple[SelectionItem, ...]
    ranking_method: str
    top_n: int


@dataclass(frozen=True)
class CurrentPosition:
    """Текущая позиция в текущем портфеле. Read-only снимок состояния.

    current_notional = current_qty * price. Никаких fills/slippage/commission:
    это уже прошлая история, а не желаемое состояние.
    """

    instrument: InstrumentRef
    current_qty: int
    price: float = 0.0
    current_notional: float = 0.0


@dataclass(frozen=True)
class CurrentPortfolio:
    """Текущее состояние портфеля на as_of. НЕ target и НЕ ордера.

    Минимальный snapshot для сравнения с TargetPortfolio. Если в проекте
    появится живой CurrentPortfolio — этот контракт служит его read-only видом;
    runtime-реализация портфеля при этом не меняется.
    """

    as_of: datetime | None
    equity: float
    positions: tuple[CurrentPosition, ...] = ()
    cash: float = 0.0


class RebalanceActionType(str, Enum):
    """Действие плана — ЕДИНСТВЕННЫЙ контракт набора значений. Не строки в разнобой."""

    NOOP = "NOOP"
    BUY = "BUY"
    SELL = "SELL"
    REMOVE = "REMOVE"


@dataclass(frozen=True)
class RebalanceAction:
    """Описание изменения состояния одной позиции. НЕ Order и НЕ Broker-call.

    delta_qty = target_qty - current_qty. LOT-AWARE округление уже сделано
    Allocation, Rebalance не пересчитывает quantity.
    """

    instrument: InstrumentRef
    action: RebalanceActionType
    current_qty: int
    target_qty: int
    delta_qty: int
    current_notional: float
    target_notional: float
    delta_notional: float
    price: float


@dataclass(frozen=True)
class RebalancePlan:
    """Разница между желаемым (Target) и текущим (Current) состоянием портфеля.

    actions содержит ТОЛЬКО реальные изменения (BUY/SELL/REMOVE); отсутствие
    action = отсутствие изменения (вариант B из спецификации Phase 7, §11).
    Отсутствие инструмента в Target НЕ означает немедленную продажу: это план,
    а разрешит/исполнит его следующий слой.

    НЕ execution model: здесь нет ордеров, брокеров и цен исполнения.
    """

    as_of: datetime | None
    current_equity: float
    target_equity: float
    current_cash: float
    target_cash: float
    cash_delta: float
    actions: tuple[RebalanceAction, ...]
    planner: str = "direct_target"

    def validate(self) -> tuple[str, ...]:
        """Инварианты плана: пустой кортеж = ок. Проверяет количества,
        дельту и согласованность action; НЕ трогает risk/margin/часы — это
        не responsibility RebalancePlan.
        """
        violations: list[str] = []
        for act in self.actions:
            tag = act.instrument.ticker
            if act.current_qty < 0 or act.target_qty < 0:
                violations.append(f"{tag}: qty < 0")
            if act.delta_qty != act.target_qty - act.current_qty:
                violations.append(f"{tag}: delta != target - current")
            if act.action is RebalanceActionType.BUY and act.delta_qty <= 0:
                violations.append(f"{tag}: BUY требует delta > 0")
            if act.action is RebalanceActionType.SELL and act.delta_qty >= 0:
                violations.append(f"{tag}: SELL требует delta < 0")
            if act.action is RebalanceActionType.REMOVE and not (
                act.target_qty == 0 and act.current_qty > 0
            ):
                violations.append(f"{tag}: REMOVE требует target=0 и current>0")
            if act.action is RebalanceActionType.NOOP and act.delta_qty != 0:
                violations.append(f"{tag}: NOOP требует delta == 0")
        if self.current_cash < 0 or self.target_cash < 0:
            violations.append("cash < 0")
        return tuple(violations)


# ── Phase 8: RebalancePolicy → OrderIntent ──────────────────────────────────


class RejectionReason(str, Enum):
    """Единый Enum причин отклонения action. Без произвольных строк по проекту."""

    BELOW_MIN_TRADE_VALUE = "below_min_trade_value"
    MAX_TURNOVER_EXCEEDED = "max_turnover_exceeded"
    COOLDOWN = "cooldown"
    MAX_COST_EXCEEDED = "max_cost_exceeded"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    ZERO_QUANTITY = "zero_quantity"
    INSUFFICIENT_CASH = "insufficient_cash"


@dataclass(frozen=True)
class RebalanceContext:
    """Всё, что policy нужно сверх плана. Explicit и детерминированный —
    никаких DB/Broker/wall-clock внутри: as_of приходит снаружи.

    prices/last_rebalance_at/lot_sizes индексированы по figi (canonical),
    с фолбэком на ticker, как в Allocation.
    """

    equity: float
    prices: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    as_of: datetime | None = None
    available_cash: float | None = None
    last_rebalance_at: Mapping[str, datetime] | None = None
    lot_sizes: Mapping[str, int] | None = None


@dataclass(frozen=True)
class OrderIntent:
    """Намерение execution: что execution может попытаться исполнить.

    НЕ исполненный ордер: здесь НЕТ order_id/broker_order_id/fill_price/
    filled_qty/commission/execution_status/reject_reason. quantity — всегда
    положительное (направление задаёт side); notional = quantity * price.
    action — трассировка policy-источника (BUY/SELL/REMOVE), не runtime-уровень.
    """

    instrument: InstrumentRef
    side: Side
    quantity: int
    price: float
    notional: float
    source: str = "rebalance"
    action: RebalanceActionType | None = None


@dataclass(frozen=True)
class RejectedAction:
    """Action, который policy не пропустила. Причина обязательна (Enum)."""

    instrument: InstrumentRef
    action: RebalanceActionType
    reason: RejectionReason
    delta_qty: int
    delta_notional: float


@dataclass(frozen=True)
class PolicyResult:
    """Результат policy: принятые OrderIntent'ы + отклонённые с причинами.

    accepted/rejected — детерминированные кортежи в порядке plan.actions.
    Policy может целиком отклонить план (MAX_TURNOVER_EXCEEDED, INSUFFICIENT_CASH)
    — тогда accepted пуст, а rejected повторяет все действия плана.
    """

    as_of: datetime | None
    accepted: tuple[OrderIntent, ...]
    rejected: tuple[RejectedAction, ...]
    planner: str = "direct_rebalance"

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)


# ── Параллельный слой Universe 2.0 (M2) — новый домен, legacy не трогаем ─────
# Contract'ы ниже принадлежат новому слою `universe/` (volatility.py, trend.py,
# sectors.py, screener.py StrategyScreener, selection.py). Они аддитивны: ничего
# из контрактов выше не изменено и не удалено.


class TrendDirection(str, Enum):
    """Направление направленного движения из TrendMeasure.

    Это ИЗМЕРЕНИЕ, а не решение и не сигнал: UP/DOWN/FLAT не говорит «покупай»
    и не добавляет инструмент в какой-то «Trend Universe». Порог FLAT — решение
    конкретной стратегии, а не этот enum.
    """

    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class StrategyFamily(str, Enum):
    """Лёгкая metadata-классификация стратегии.

    Metadata для registry и тестов разделения; НЕ классификация рынка и НЕ
    управляет Universe. В первой версии достаточно enum'а + теста, полный
    registry роботов не строится. Примеры: PriceChannelTrade → TREND_FOLLOWING,
    RsiContrtrend → MEAN_REVERSION, PinBarTrade → PATTERN, PumpDetector → EVENT,
    SectorsSetBollingerMomentum → SECTOR_ROTATION, DividendCapture → CALENDAR,
    RebalancerByMomentum → PORTFOLIO, MarketDepthScreener → MICROSTRUCTURE.
    """

    TREND_FOLLOWING = "trend_following"
    MEAN_REVERSION = "mean_reversion"
    PATTERN = "pattern"
    EVENT = "event"
    SECTOR_ROTATION = "sector_rotation"
    CALENDAR = "calendar"
    PORTFOLIO = "portfolio"
    MICROSTRUCTURE = "microstructure"


@dataclass(frozen=True)
class SectorMembership:
    """Принадлежность инструмента сектору/группе. Отдельное измерение.

    НЕ числовой признак MarketFeatures и НЕ фильтр Universe: отсутствие сектора
    не удаляет инструмент. valid_from/valid_to — опциональный период действия
    (пустые, если данные не позволяют его знать). Потребители: Strategy Selection
    и Portfolio/Allocation/Risk.
    """

    instrument: InstrumentRef
    sector: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True)
class VolatilityFeatures:
    """Результат VolatilityMeasure: насколько инструмент волатилен.

    НЕ ранк и НЕ решение: кто и как использует atr/atr_pct — выбор стратегии.
    ATR — КАНОНИЧЕСКИЙ (app.engine.indicatorhub._atr), второго atr() в новом
    слое НЕТ. valid=False — данных не хватило/плохие бары; причина в reason.
    Никаких Top-N и ranking здесь нет.
    """

    instrument: InstrumentRef
    as_of: datetime
    atr: float | None = None
    atr_pct: float | None = None
    realized_volatility: float | None = None
    range_pct: float | None = None
    bars_used: int = 0
    valid: bool = False
    reason: str = ""


@dataclass(frozen=True)
class TrendFeatures:
    """Результат TrendMeasure: есть ли и насколько сильно направленное движение.

    direction/strength/slope — ИЗМЕРЕНИЕ, а не сигнал (direction=UP не значит
    BUY). normalized_slope = regression_slope / ATR — масштабно-независим.
    strength — детерминированная нормализация |normalized_slope| в [0, 1]
    (min(|ns|, 1.0)); 0 на строго плоском ряду. valid=False — недостаточная
    история/невалидные бары/нет ATR для нормировки; причина в reason.
    """

    instrument: InstrumentRef
    as_of: datetime
    direction: TrendDirection = TrendDirection.FLAT
    strength: float = 0.0
    slope: float = 0.0
    normalized_slope: float = 0.0
    bars_used: int = 0
    valid: bool = False
    reason: str = ""


@dataclass(frozen=True)
class MarketFeatures:
    """Композиция измерений для одного инструмента на as_of.

    volatility + trend = market features. Sector сюда НЕ входит как число:
    принадлежность — metadata, берётся из Universe/SectorMembership.
    Каждый из подъектов может быть невалиден по отдельности, это отражено в них.
    """

    instrument: InstrumentRef
    as_of: datetime
    volatility: VolatilityFeatures | None = None
    trend: TrendFeatures | None = None

    @property
    def valid(self) -> bool:
        return bool(
            self.volatility is not None
            and self.volatility.valid
            and self.trend is not None
            and self.trend.valid
        )
