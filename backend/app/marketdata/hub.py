"""CandleHub — центральный объект управления рыночными данными.

CandleHub не знает, какая стратегия торгует. Он только:
- normalize → deduplicate → ordering → closed/forming → gap detection
- генерирует события (CANDLE_FORMING, CANDLE_CLOSED, CANDLE_GAP и т.д.)
- управляет подписками (hot add/remove)
- управляет warmup (загрузка истории → инициализация индикаторов → LIVE)
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.marketdata.events import EventBus
from app.marketdata.indicators import IndicatorHub
from app.marketdata.models import (
    Candle,
    CandleEvent,
    CandleState,
    FeedStatus,
    IndicatorKey,
    MarketDataEvent,
    Subscription,
    SubscriptionState,
    Symbol,
)
from app.marketdata.source import MarketDataSource
from app.marketdata.store import CandleStore

logger = logging.getLogger("candlehub")


class CandleHub:
    """Единый источник истины для market data.

    Подключение:
        hub = CandleHub(market_data=TinvestMarketDataSource(token))
        await hub.add_symbol("BBG004730N88", "SBER")
        await hub.subscribe("BBG004730N88", "1m")
        await hub.subscribe("BBG004730N88", "5m", indicators=["ema:20", "rsi:14"])

    Подписка на события:
        hub.on(CandleState.CLOSED, strategy.on_candle)
        hub.on(CandleState.FORMING, ui.update_forming)
    """

    def __init__(
        self,
        market_data: MarketDataSource | None = None,
        indicator_hub: IndicatorHub | None = None,
        event_bus: EventBus | None = None,
        maxlen: int = 5000,
    ):
        self._market_data = market_data
        self._indicators = indicator_hub or IndicatorHub()
        self._events = event_bus or EventBus()
        self._maxlen = maxlen

        self._symbols: dict[str, Symbol] = {}
        self._subscriptions: dict[tuple[str, str], SubscriptionState] = {}
        self._stores: dict[tuple[str, str], CandleStore] = {}
        self._sequences: dict[tuple[str, str], int] = defaultdict(int)
        self._reasons: dict[str, int] = {
            "received": 0,
            "duplicate": 0,
            "out_of_order": 0,
            "gap": 0,
            "corrected": 0,
            "closed": 0,
        }

    # --- управление символами -------------------------------------------

    async def add_symbol(self, figi: str, symbol: str = "", name: str = "", lot: int = 1) -> Symbol:
        """Добавить инструмент (hot add)."""
        logger.info("[CandleHub] SYMBOL_ADD_REQUEST figi=%s symbol=%s", figi, symbol)
        sym = Symbol(figi=figi, symbol=symbol, name=name, lot=lot)
        self._symbols[figi] = sym
        logger.info("[CandleHub] SYMBOL_ADDED figi=%s symbol=%s", figi, symbol)
        return sym

    async def remove_symbol(self, figi: str) -> None:
        """Удалить инструмент (hot remove)."""
        logger.info("[CandleHub] remove_symbol figi=%s", figi)
        keys_to_remove = [k for k in self._subscriptions if k[0] == figi]
        for key in keys_to_remove:
            del self._subscriptions[key]
            if key in self._stores:
                del self._stores[key]
        if figi in self._symbols:
            del self._symbols[figi]
        logger.info("[CandleHub] SYMBOL_REMOVED figi=%s", figi)

    # --- управление подписками -------------------------------------------

    async def subscribe(
        self,
        figi: str,
        timeframe: str,
        *,
        closed_only: bool = True,
        indicators: list[str] | None = None,
    ) -> None:
        """Подписаться на данные по инструменту и таймфрейму.

        После подписки автоматически выполняется warmup:
        1. Загрузка истории
        2. Инициализация индикаторов
        3. Переход в состояние LIVE
        """
        logger.info("[CandleHub] SUBSCRIPTION_REQUEST figi=%s tf=%s", figi, timeframe)
        sub = Subscription(
            figi=figi,
            timeframe=timeframe,
            closed_only=closed_only,
            indicators=tuple(indicators or ()),
        )
        key = (figi, timeframe)
        state = SubscriptionState(subscription=sub, status=FeedStatus.SUBSCRIBING)
        self._subscriptions[key] = state
        self._stores[key] = CandleStore(maxlen=self._maxlen)

        if self._market_data:
            await self._market_data.subscribe(figi, timeframe)

        state.status = FeedStatus.SUBSCRIBED
        logger.info("[CandleHub] SUBSCRIPTION_CONFIRMED figi=%s tf=%s", figi, timeframe)

        # Warmup
        await self._warmup(key)

    async def unsubscribe(self, figi: str, timeframe: str) -> None:
        """Отписаться от данных."""
        key = (figi, timeframe)
        if key in self._subscriptions:
            self._subscriptions[key].status = FeedStatus.REMOVING
            if self._market_data:
                await self._market_data.unsubscribe(figi, timeframe)
            del self._subscriptions[key]
            if key in self._stores:
                del self._stores[key]
            logger.info("[CandleHub] SUBSCRIPTION_REMOVED figi=%s tf=%s", figi, timeframe)

    @staticmethod
    def _warmup_bars_for(specs: tuple[str, ...]) -> int:
        """Сколько баров нужно прогреть для индикаторов подписки.

        EMA(n) требует n баров, RSI(n) — n+1 (нужен предыдущий close),
        ATR(n) — n+1. Берём максимум по всем индикаторам.
        """
        if not specs:
            return 0
        warmup = 0
        for spec in specs:
            name, _, params = spec.partition(":")
            period = 0
            if params:
                try:
                    period = int(float(params.split(",")[0]))
                except ValueError:
                    period = 0
            if name.lower() in ("ema", "sma"):
                need = period
            elif name.lower() in ("rsi", "atr"):
                need = period + 1
            else:
                need = period
            warmup = max(warmup, need)
        return warmup

    async def _warmup(self, key: tuple[str, str]) -> None:
        """Warmup: загрузка истории → инициализация индикаторов → LIVE."""
        figi, timeframe = key
        state = self._subscriptions[key]
        state.status = FeedStatus.WARMING_UP
        logger.info("[CandleHub] HISTORY_WARMUP_START figi=%s tf=%s", figi, timeframe)

        state.warmup_bars = self._warmup_bars_for(state.subscription.indicators)
        if state.warmup_bars:
            logger.info("[CandleHub] warmup requires %d bars (indicators=%s)",
                        state.warmup_bars, state.subscription.indicators)

        t0 = time.monotonic()
        if self._market_data:
            try:
                history = await self._market_data.fetch_history(
                    figi, timeframe,
                    datetime.now(timezone.utc) - timedelta(days=120),
                    datetime.now(timezone.utc),
                )
                for candle in history:
                    self._process_candle(candle, state)
            except Exception as e:
                logger.error("[CandleHub] warmup failed for %s %s: %s", figi, timeframe, e)
                state.status = FeedStatus.DEGRADED
                state.error = str(e)
                return

        elapsed = time.monotonic() - t0
        state.status = FeedStatus.LIVE
        logger.info("[CandleHub] HISTORY_WARMUP_DONE figi=%s tf=%s in %.2fs", figi, timeframe, elapsed)
        logger.info("[CandleHub] LIVE_READY figi=%s tf=%s", figi, timeframe)

    # --- обработка свечей ------------------------------------------------

    def _emit_candle(
        self,
        key: tuple[str, str],
        candle: Candle,
        state: str,
        *,
        is_new: bool = True,
        is_correction: bool = False,
    ) -> None:
        """Собрать и отправить CandleEvent."""
        figi, timeframe = key
        sym = self._symbols.get(figi)
        self._events.emit_candle(
            CandleEvent(
                event_id=str(uuid.uuid4()),
                symbol=sym.symbol if sym else "",
                figi=figi,
                timeframe=timeframe,
                ts=candle.ts,
                candle=candle,
                state=state,
                source=candle.source,
                sequence=candle.sequence,
                is_new=is_new,
                is_correction=is_correction,
            )
        )

    def _process_candle(self, candle: Candle, state: SubscriptionState) -> None:
        """Обработать свечу: normalize → deduplicate → ordering → closed/forming."""
        key = (candle.figi, candle.timeframe)
        store = self._stores.get(key)
        if store is None:
            return

        self._reasons["received"] += 1
        forming = store.forming()

        # Duplicate: этот ts уже лежит в закрытой истории (даже если он
        # старее текущего forming — повтор закрытого бара, не новая дыра).
        if any(closed.ts == candle.ts for closed in store.get()):
            self._reasons["duplicate"] += 1
            logger.debug("[CandleHub] CANDLE_DUPLICATE figi=%s tf=%s ts=%s",
                         candle.figi, candle.timeframe, candle.ts)
            return

        # Out of order: свеча старее текущей forming и в истории её нет.
        if forming is not None and candle.ts < forming.ts:
            self._reasons["out_of_order"] += 1
            logger.debug("[CandleHub] CANDLE_OUT_OF_ORDER figi=%s tf=%s ts=%s forming_ts=%s",
                         candle.figi, candle.timeframe, candle.ts, forming.ts)
            return

        # Дыра позади закрытой границы (forming уже сброшен, ts в store нет).
        if store.last_ts is not None and candle.ts <= store.last_ts:
            self._reasons["out_of_order"] += 1
            logger.debug("[CandleHub] CANDLE_OUT_OF_ORDER figi=%s tf=%s ts=%s last_ts=%s",
                         candle.figi, candle.timeframe, candle.ts, store.last_ts)
            return

        # Обновление forming-бара: тот же ts, новые OHLCV
        if forming is not None and candle.ts == forming.ts:
            updated = Candle(
                ts=candle.ts,
                open=candle.open,
                high=max(candle.high, forming.high),
                low=min(candle.low, forming.low),
                close=candle.close,
                volume=max(candle.volume, forming.volume),
                figi=candle.figi,
                timeframe=candle.timeframe,
                source=candle.source,
                sequence=forming.sequence,
                state=CandleState.FORMING,
            )
            is_correction = updated.close != forming.close
            if is_correction:
                self._reasons["corrected"] += 1
            store.update_forming(updated)
            state.last_ts = updated.ts
            self._preview_indicators(key, updated)
            self._emit_candle(key, updated, "FORMING", is_new=False,
                              is_correction=is_correction)
            return

        # Gap: от последней ПРИНЯТОЙ свечи (forming тоже считается).
        # store.last_ts — это последний CLOSED и отстаёт на один бар,
        # из-за этого обычный следующий бар выглядел как дыра.
        if state.last_ts is not None:
            expected = self._next_ts(state.last_ts, candle.timeframe)
            if candle.ts > expected:
                missing = int((candle.ts - expected).total_seconds() //
                              self._tf_seconds(candle.timeframe))
                logger.warning("[CandleHub] CANDLE_GAP figi=%s tf=%s expected=%s received=%s missing=%d",
                               candle.figi, candle.timeframe, expected, candle.ts, missing)
                self._reasons["gap"] += 1
                self._events.emit_market(MarketDataEvent(
                    event_type="CANDLE_GAP",
                    figi=candle.figi,
                    timeframe=candle.timeframe,
                    ts=candle.ts,
                    details={
                        "expected": expected.isoformat(),
                        "received": candle.ts.isoformat(),
                        "missing": missing,
                    },
                ))

        # Новая свеча
        state.sequence += 1
        new_candle = Candle(
            ts=candle.ts,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            figi=candle.figi,
            timeframe=candle.timeframe,
            source=candle.source,
            sequence=state.sequence,
            state=CandleState.FORMING,
        )

        # Закрыть предыдущую forming перед началом новой
        if forming is not None:
            closed_candle = store.close(forming)
            self._reasons["closed"] += 1
            self._commit_indicators(key, closed_candle)
            self._emit_candle(key, closed_candle, "CLOSED")

        state.last_ts = new_candle.ts

        # For history/replay: закрывать сразу, без forming
        if new_candle.source in ("history", "replay"):
            store.update_forming(new_candle)
            closed_candle = store.close(new_candle)
            self._reasons["closed"] += 1
            self._commit_indicators(key, closed_candle)
            self._emit_candle(key, closed_candle, "CLOSED")
            return

        # Update forming
        store.update_forming(new_candle)
        self._preview_indicators(key, new_candle)
        self._emit_candle(key, new_candle, "FORMING")

    @staticmethod
    def _tf_seconds(timeframe: str) -> int:
        """Длительность таймфрейма в секундах."""
        return {
            "1min": 60, "5min": 300, "10min": 600, "15min": 900,
            "30min": 1800, "hour": 3600, "2h": 7200, "4h": 14400,
            "day": 86400,
        }.get(timeframe, 60)

    def _close_forming_if_needed(self, store: CandleStore, state: SubscriptionState) -> None:
        """Закрыть forming-бар, если пришла новая свеча (другой bucket).

        В реальной реализации это определяется границей бакета.
        Здесь — упрощённо: если forming.ts < last_closed.ts, закрыть forming.
        """
        forming = store.forming()
        if forming is None:
            return

        last_closed = store.last()
        if last_closed is not None and forming.ts < last_closed.ts:
            # Close the forming candle
            closed_candle = store.close(forming)
            state.sequence += 1

            # Emit CLOSED event
            event = CandleEvent(
                event_id=str(uuid.uuid4()),
                symbol=self._symbols.get(forming.figi, Symbol(figi=forming.figi)).symbol,
                figi=forming.figi,
                timeframe=forming.timeframe,
                ts=forming.ts,
                candle=closed_candle,
                state="CLOSED",
                source=forming.source,
                sequence=state.sequence,
            )
            self._events.emit_candle(event)

    @staticmethod
    def _next_ts(ts: datetime, timeframe: str) -> datetime:
        """Следующий ожидаемый ts для таймфрейма."""
        mapping = {
            "1min": timedelta(minutes=1),
            "5min": timedelta(minutes=5),
            "10min": timedelta(minutes=10),
            "15min": timedelta(minutes=15),
            "30min": timedelta(minutes=30),
            "hour": timedelta(hours=1),
            "2h": timedelta(hours=2),
            "4h": timedelta(hours=4),
            "day": timedelta(days=1),
        }
        return ts + mapping.get(timeframe, timedelta(minutes=1))

    # --- API для чтения ---------------------------------------------------

    def get_store(self, figi: str, timeframe: str) -> CandleStore | None:
        """Получить хранилище свечей для подписки."""
        return self._stores.get((figi, timeframe))

    def get_subscription(self, figi: str, timeframe: str) -> SubscriptionState | None:
        """Получить состояние подписки."""
        return self._subscriptions.get((figi, timeframe))

    def close_expired_forming(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Закрыть forming-бары, у которых истёк bucket.

        Логика как в OsEngine CandleSeries.SetNewTime: свеча считается
        закрытой, когда время бакета прошло, даже если следующая свеча
        ещё не пришла (тиков нет, перерыв, дельта стрима).

        Возвращает список закрытых ключей (figi, timeframe).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        closed: list[tuple[str, str]] = []

        for key, state in list(self._subscriptions.items()):
            store = self._stores.get(key)
            if store is None or state.status not in (FeedStatus.LIVE, FeedStatus.DEGRADED):
                continue
            forming = store.forming()
            if forming is None:
                continue

            bucket_end = forming.ts + timedelta(seconds=self._tf_seconds(forming.timeframe))
            if now > bucket_end:
                closed_candle = store.close(forming)
                state.last_ts = closed_candle.ts
                self._reasons["closed"] += 1
                self._commit_indicators(key, closed_candle)
                self._emit_candle(key, closed_candle, "CLOSED")
                closed.append(key)

        return closed

    def _indicator_keys(self, key: tuple[str, str]) -> list[IndicatorKey]:
        """IndicatorKey'ы, объявленные в подписке (figi, timeframe)."""
        figi, timeframe = key
        state = self._subscriptions.get(key)
        if state is None:
            return []
        keys: list[IndicatorKey] = []
        for spec in state.subscription.indicators:
            name, _, params = spec.partition(":")
            parsed: tuple[int | float, ...] = ()
            if params:
                for chunk in params.split(","):
                    try:
                        parsed += (float(chunk) if "." in chunk else int(chunk),)
                    except ValueError:
                        logger.warning("[CandleHub] bad indicator spec %r (figi=%s tf=%s)",
                                       spec, figi, timeframe)
            keys.append(IndicatorKey(figi=figi, timeframe=timeframe,
                                     indicator=name, params=parsed))
        return keys

    def _preview_indicators(self, key: tuple[str, str], candle: Candle) -> None:
        """Preview-фаза индикаторов на forming-свече (состояние не меняется)."""
        for ind_key in self._indicator_keys(key):
            try:
                self._indicators.preview(ind_key, candle)
            except Exception as e:
                logger.warning("[CandleHub] INDICATOR_ERROR preview %s: %s", ind_key, e)

    def _commit_indicators(self, key: tuple[str, str], candle: Candle) -> None:
        """Commit-фаза индикаторов на закрытой свече (состояние фиксируется)."""
        for ind_key in self._indicator_keys(key):
            try:
                self._indicators.commit(ind_key, candle)
            except Exception as e:
                logger.warning("[CandleHub] INDICATOR_ERROR commit %s: %s", ind_key, e)

    def get_indicator(self, figi: str, timeframe: str, indicator: str, params: tuple[int | float, ...] = ()):
        """Получить индикатор из кэша."""
        key = IndicatorKey(figi=figi, timeframe=timeframe, indicator=indicator, params=params)
        return self._indicators.get(key)

    # --- события ----------------------------------------------------------

    def on(self, event_type: CandleState | str, handler: Callable) -> None:
        """Подписаться на событие."""
        self._events.on_candle(event_type.value if isinstance(event_type, CandleState) else event_type, handler)

    def off(self, event_type: CandleState | str, handler: Callable) -> None:
        """Отписаться от события."""
        self._events.off_candle(event_type.value if isinstance(event_type, CandleState) else event_type, handler)

    # --- диагностика ------------------------------------------------------

    @property
    def stats(self) -> dict:
        """Статистика всех подписок."""
        return {
            "symbols": len(self._symbols),
            "subscriptions": {
                f"{k[0]}:{k[1]}": {
                    "status": v.status.value,
                    "sequence": v.sequence,
                    "last_ts": v.last_ts.isoformat() if v.last_ts else None,
                    "store_size": len(self._stores.get(k, CandleStore())),
                }
                for k, v in self._subscriptions.items()
            },
            "indicators": len(self._indicators._indicators),
            "reasons": dict(self._reasons),
        }
