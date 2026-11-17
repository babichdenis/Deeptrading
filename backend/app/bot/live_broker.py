from __future__ import annotations
from app.bot.close_submission import CloseSubmission

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.engine.costs import CostModel
from app.models.paper import PaperAccount, PaperTrade

DEFAULT_ACCOUNT = "default"


@dataclass
class LivePosition:
    figi: str
    ticker: str
    side: str
    qty: int
    entry_price: float
    entry_time: datetime
    stop_loss: float | None
    take_profit: float | None
    strategy_id: str = "v4_enhanced"


@dataclass
class LiveFill:
    figi: str
    side: str
    qty: int
    price: float


@dataclass
class LiveExecution:
    """Структурированный результат брокерского исполнения (ARCH160/ARCH176).

    Единица — ЛОТЫ (как их принимает/возвращает SDK). Штуки получаются
    умножением на instrument.lot РОВНО один раз — при записи сделки.
    """

    order_id: str | None
    status: str  # FILLED | PARTIAL | REJECTED | UNKNOWN
    requested_lots: int
    executed_lots: int | None
    remaining_lots: int | None
    fill_price: float | None = None
    commission: float | None = None
    error: str | None = None


class EntrySubmissionUnknown(Exception):
    pass


class LiveBroker:
    """Брокер поверх T-Invest (sandbox или live по config.settings().bot_mode).

    Переключение профилей — через переменные в .env (BOT_MODE + соответствующие
    токен/аккаунт), без правки кода.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], cost_model: CostModel | None = None, config=None, mode: str | None = None):
        self.config = config
        self.sessions = session_factory
        self.costs = cost_model or CostModel()
        if self.config is None:
            from app.bot.runtime import BotConfig
            self.config = BotConfig()
        _cfg_mode = getattr(self.config, "mode", None)
        _s = get_settings()
        self.mode = mode or (_cfg_mode if _cfg_mode in ("sandbox", "live") else _s.bot_mode)
        if self.mode not in ("sandbox", "live"):
            self.mode = "sandbox"
        self._token = _s.get_token(self.mode)
        self._target = _s.get_target(self.mode)
        self._account = _s.get_account(self.mode)
        self._services = None
        self._client = None
        self._portfolio_snapshot = None
        self._portfolio_ts = 0.0
        self._portfolio_ttl = 15.0
        self._portfolio_retries = 6
        self._portfolio_lock = threading.Lock()
        self._logger = logging.getLogger("app.bot.live_broker")
        self.last_execution: LiveExecution | None = None

    def _log(self, msg: str, level: str = "info") -> None:
        try:
            getattr(self._logger, level, self._logger.info)(msg)
        except Exception:
            pass

    def _get_services(self):
        if self._services is None:
            from t_tech.invest import Client

            client = Client(self._token, target=self._target)
            self._services = client.__enter__()
            self._client = client
        return self._services

    def close(self) -> None:
        """Закрыть gRPC-канал T-Invest.

        Синхронный Client держит non-daemon потоки — без явного close процесс
        uvicorn после shutdown остаётся висеть (и его приходится убивать kill -9).
        """
        try:
            if self._client is not None:
                self._client.__exit__(None, None, None)
        except Exception:
            pass
        self._client = None
        self._services = None

    # --- API-ветвление sandbox/live (одни и те же схемы, разные сервисы) ---
    def _api_post_order(self, *, expected_scope=None, **kwargs):
        """Выставить ордер: live → orders.post_order (+confirm_margin), sandbox → sandbox.post_sandbox_order.

        ARCH174: confirm_margin_trade отражает явную политику маржи (config.use_margin),
        а не факт выбора live-контура. Выбор live сам по себе НЕ даёт согласие на маржу.
        """
        mode = self.mode
        if expected_scope is not None and ((mode, self._account) != expected_scope
                or kwargs.get('account_id') != expected_scope[1]):
            raise ValueError('broker_scope_changed_before_submission')
        services = self._get_services()
        if expected_scope is not None and (self.mode, self._account) != expected_scope:
            raise ValueError('broker_scope_changed_during_service_lookup')
        try:
            if mode == "live":
                consent = bool(getattr(self.config, "use_margin", False))
                return services.orders.post_order(confirm_margin_trade=consent, **kwargs)
            return services.sandbox.post_sandbox_order(**kwargs)
        finally:
            self._flush_portfolio()

    def _api_get_max_lots(self, request):
        """Макс. лотов: live → orders.get_max_lots, sandbox → sandbox.get_sandbox_max_lots."""
        services = self._get_services()
        if self.mode == "live":
            return services.orders.get_max_lots(request=request)
        return services.sandbox.get_sandbox_max_lots(request=request)

    async def request_cancel_order(self, client_order_id: str, *, expected_scope: tuple[str, str]):
        import asyncio
        from t_tech.invest.schemas import OrderIdType

        if (not isinstance(expected_scope, tuple) or len(expected_scope) != 2
                or expected_scope[0] not in ('live', 'sandbox')
                or not isinstance(expected_scope[1], str) or not expected_scope[1].strip()
                or not isinstance(client_order_id, str) or not client_order_id.strip()
                or len(client_order_id) > 64):
            raise ValueError('Explicit cancellation identity required')

        def cancel():
            if (self.mode, self._account) != expected_scope:
                raise ValueError('cancel_scope_changed_before_dispatch')
            services = self._get_services()
            if (self.mode, self._account) != expected_scope:
                raise ValueError('cancel_scope_changed_during_service_lookup')
            method = (services.orders.cancel_order if expected_scope[0] == 'live'
                      else services.sandbox.cancel_sandbox_order)
            try:
                return method(account_id=expected_scope[1], order_id=client_order_id,
                              order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)
            finally:
                self._flush_portfolio()

        await asyncio.to_thread(cancel)
        if (self.mode, self._account) != expected_scope:
            raise ValueError('cancel_scope_changed_after_dispatch')

    async def order_state(self, client_order_id: str, figi: str, side: str, requested_lots: int):
        import asyncio
        from t_tech.invest.schemas import OrderDirection, OrderIdType

        if not self._account or self.mode not in ("live", "sandbox"):
            raise ValueError("broker_scope_missing")
        if not client_order_id or not figi or side not in ("BUY", "SELL"):
            raise ValueError("order_identity_missing")
        if type(requested_lots) is not int or requested_lots <= 0:
            raise ValueError("order_quantity_invalid")
        original_scope = (self.mode, self._account)
        services = self._get_services()
        method = (services.orders.get_order_state if self.mode == "live"
                  else services.sandbox.get_sandbox_order_state)
        response = await asyncio.to_thread(
            method, account_id=self._account, order_id=client_order_id,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
        )
        if (self.mode, self._account) != original_scope:
            raise ValueError('broker_scope_changed_during_order_state')
        direction = (OrderDirection.ORDER_DIRECTION_BUY if side == "BUY"
                     else OrderDirection.ORDER_DIRECTION_SELL)
        if (getattr(response, "order_request_id", None) != client_order_id
                or getattr(response, "figi", None) != figi
                or getattr(response, "direction", None) != direction
                or type(getattr(response, "lots_requested", None)) is not int
                or response.lots_requested != requested_lots
                or not getattr(response, "order_id", None)):
            raise ValueError("broker_order_identity_mismatch")
        if self.mode == "sandbox" and response.execution_report_status == 1 and not response.stages:
            from app.services.sandbox_order_evidence import parse_sandbox_order_aggregate, SandboxOrderState
            lot = await self._get_lot(figi)
            if (self.mode, self._account) != original_scope:
                raise ValueError("broker_scope_changed_during_order_lot_lookup")
            aggregate = parse_sandbox_order_aggregate(response, mode=self.mode,
                broker_account_id=self._account, client_order_id=client_order_id,
                figi=figi, side=side, requested_lots=requested_lots,
                lot_size=lot, as_of=datetime.now(UTC))
            return SandboxOrderState(response, aggregate)
        executed = getattr(response, "lots_executed", None)
        if type(executed) is not int or not 0 <= executed <= requested_lots:
            raise ValueError("broker_order_quantity_inconsistent")
        return response

    async def operations(self, start: datetime, end: datetime):
        import asyncio

        if not self._account:
            raise ValueError("broker_account_missing")
        if self.mode not in ("live", "sandbox"):
            raise ValueError("broker_mode_invalid")
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("operations_window_invalid")
        services = self._get_services()
        method = (services.operations.get_operations if self.mode == "live"
                  else services.sandbox.get_sandbox_operations)
        return await asyncio.to_thread(method, account_id=self._account,
                                       from_=start, to=end)

    async def ensure_account(self, initial_cash: float = 10_000.0) -> PaperAccount | None:
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc is None:
                acc = PaperAccount(name=DEFAULT_ACCOUNT, initial_cash=initial_cash, cash=initial_cash)
                db.add(acc)
                await db.commit()
                await db.refresh(acc)
            return acc

    async def reset(self, initial_cash: float = 10_000.0) -> None:
        # live счёт не сбрасывается — только историю paper-учёта
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc:
                await db.delete(acc)
                await db.commit()
        await self.ensure_account(initial_cash)

    def _q(self, v):
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if hasattr(v, "units") and hasattr(v, "nano"):
            return float(v.units + v.nano / 1e9)
        return float(v)

    def _get_portfolio_sync(self):
        return self._get_services().operations.get_portfolio(account_id=self._account)

    def _is_rate_exhausted(self, exc) -> bool:
        from grpc import StatusCode
        try:
            if getattr(exc, "code", None) == StatusCode.RESOURCE_EXHAUSTED:
                return True
        except Exception:
            pass
        return "RESOURCE_EXHAUSTED" in str(exc)

    def _rate_sleep_seconds(self, exc) -> float:
        try:
            meta = getattr(exc, "metadata", None)
            reset = float(meta.ratelimit_reset) if meta is not None else None
        except (TypeError, ValueError, AttributeError):
            reset = None
        return min(reset if reset and reset > 0 else 10.0, 30.0)

    def _get_portfolio_with_retry(self):
        last = None
        for _ in range(self._portfolio_retries):
            try:
                return self._get_portfolio_sync()
            except Exception as e:
                last = e
                if not self._is_rate_exhausted(e):
                    raise
                time.sleep(self._rate_sleep_seconds(e))
        if self._portfolio_snapshot is not None:
            return self._portfolio_snapshot
        raise last

    def _get_portfolio_safe(self):
        now = time.monotonic()
        with self._portfolio_lock:
            if self._portfolio_snapshot is None or now - self._portfolio_ts >= self._portfolio_ttl:
                self._portfolio_snapshot = self._get_portfolio_with_retry()
                self._portfolio_ts = time.monotonic()
            return self._portfolio_snapshot

    def _flush_portfolio(self):
        """Принудительно сбросить кэш портфеля — следующий вызов problems() перечитает данные."""
        with self._portfolio_lock:
            self._portfolio_snapshot = None
            self._portfolio_ts = 0.0

    async def flush_portfolio(self):
        """Async-обёртка над _flush_portfolio() для вызова из runtime."""
        import asyncio
        await asyncio.to_thread(self._flush_portfolio)

    async def cash(self) -> float:
        """Свободные денежные средства на счёте (руб)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            return self._q(p.total_amount_currencies)

        return await asyncio.to_thread(_f)

    async def equity(self) -> float:
        """Текущий капитал = деньги + стоимость позиций (для аллокации 20%)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            cur = self._q(p.total_amount_currencies)
            shares = self._q(p.total_amount_shares) if hasattr(p, "total_amount_shares") else 0.0
            return cur + shares

        return await asyncio.to_thread(_f)

    async def market_value(self) -> float:
        """Стоимость открытых позиций (сумма |qty × current| по non-currency)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            total = 0.0
            for pos in p.positions:
                if pos.instrument_type == "currency":
                    continue
                qty = pos.quantity.units + pos.quantity.nano / 1e9
                px = self._q(pos.current_price)
                total += abs(qty) * px
            return total

        return await asyncio.to_thread(_f)

    async def free_cash(self) -> float:
        """Свободные собственные деньги = equity − стоимость позиций (для гейта входа)."""
        eq = await self.equity()
        mv = await self.market_value()
        return max(0.0, eq - mv)

    def _mark_degraded(self, component: str, exc: Exception | None = None) -> None:
        """P1.3: фиксация деградации фолбэка (margin/prices/status).

        Раньше API-ошибки тихо превращались в {} / "" / False и runtime
        продолжал работать на неполных данных. Теперь факт деградации
        записывается в self.degraded и виден в /bot/status.
        """
        d = getattr(self, "degraded", None)
        if d is None:
            d = self.degraded = {}
        d[component] = {
            "ts": time.time(),
            "error": f"{type(exc).__name__}: {exc}" if exc else "",
        }

    def _mark_ok(self, component: str) -> None:
        """RECHECK P1: сброс деградации после успешного вызова — иначе
        историческая ошибка выглядит как текущее состояние в /bot/status."""
        d = getattr(self, "degraded", None)
        if d is not None:
            d.pop(component, None)

    async def margin_attributes(self) -> dict:
        """Маржинальные атрибуты счёта: liquid_portfolio, starting_margin, minimal_margin.

        Это «правда» брокера: сколько заморожено под позиции (starting_margin) и
        сколько всего ликвидно (liquid_portfolio) — используется капом экспозиции.
        """
        import asyncio

        def _f():
            services = self._get_services()
            ma = services.users.get_margin_attributes(account_id=self._account)
            return {
                "liquid": self._q(ma.liquid_portfolio),
                "starting_margin": self._q(ma.starting_margin),
                "corrected_margin": self._q(ma.corrected_margin) if getattr(ma, "corrected_margin", None) is not None else None,
                "minimal_margin": self._q(getattr(ma, "minimal_margin", None)),
            }

        try:
            out = await asyncio.to_thread(_f)
            self._mark_ok("margin_attributes")
            return out
        except Exception as e:
            self._mark_degraded("margin_attributes", e)
            return {}

    async def free_funds(self) -> float:
        """Реально свободные средства для сделок = ликвидный портфель − начальная маржа.
        (Свободные деньги на счёте искажены шортами; начальная маржа — замороженное обеспечение.)"""
        import asyncio

        def _f():
            try:
                services = self._get_services()
                ma = services.users.get_margin_attributes(account_id=self._account)
                liquid = self._q(ma.liquid_portfolio)
                start = self._q(ma.starting_margin)
                return max(0.0, liquid - start)
            except Exception:
                # fallback: equity − стоимость позиций
                p = self._get_portfolio_safe()
                eq = self._q(p.total_amount_currencies) + (self._q(p.total_amount_shares) if hasattr(p, "total_amount_shares") else 0.0)
                mv = 0.0
                for pos in p.positions:
                    if pos.instrument_type == "currency":
                        continue
                    q = pos.quantity.units + pos.quantity.nano / 1e9
                    mv += abs(q) * self._q(pos.current_price)
                return max(0.0, eq - mv)

        return await asyncio.to_thread(_f)

    async def verify_position(self, figi: str) -> LivePosition | None:
        """Фактическая позиция в T-Invest (для сверки входа/выхода). None — позиции нет."""
        return await self.get_position(figi)

    async def positions(self) -> list[LivePosition]:
        import asyncio

        def _fetch():
            p = self._get_portfolio_safe()
            out = []
            for pos in p.positions:
                if pos.instrument_type == "currency":
                    continue
                qty = pos.quantity.units + pos.quantity.nano / 1e9
                if abs(qty) < 1:
                    continue
                out.append(LivePosition(
                    figi=pos.figi,
                    ticker=pos.figi[:8],
                    side="LONG" if qty > 0 else "SHORT",
                    qty=abs(int(qty)),
                    entry_price=self._q(pos.average_position_price),
                    entry_time=datetime.now(UTC),
                    stop_loss=None,
                    take_profit=None,
                ))
            return out

        return await asyncio.to_thread(_fetch)

    async def _get_lot(self, figi: str) -> int:
        try:
            from sqlalchemy import text
            async with self.sessions() as db:
                r = await db.execute(text("SELECT lot FROM instruments WHERE figi = :f"), {"f": figi})
                row = r.first()
                if row:
                    return int(row[0])
        except Exception:
            pass
        return 0

    async def get_position(self, figi: str) -> LivePosition | None:
        for p in await self.positions():
            if p.figi == figi:
                return p
        return None

    async def get_max_lots(self, figi: str):
        import asyncio
        from dataclasses import dataclass as _dc

        @_dc
        class _ML:
            buy_cash: int = 0
            buy_margin: int = 0
            sell_cash: int = 0
            sell_margin: int = 0
            buy_money: float = 0.0
            buy_margin_money: float = 0.0
            leverage: float = 1.0

        def _fetch():
            from t_tech.invest.schemas import GetMaxLotsRequest
            req = GetMaxLotsRequest(account_id=self._account, instrument_id=figi)
            ml = self._api_get_max_lots(req)
            bc = ml.buy_limits
            bm = ml.buy_margin_limits
            sc = ml.sell_limits
            sm = ml.sell_margin_limits
            bmu = bc.buy_money_amount.units + bc.buy_money_amount.nano / 1e9
            bmmu = bm.buy_money_amount.units + bm.buy_money_amount.nano / 1e9
            lev = round(bm.buy_max_lots / bc.buy_max_lots, 2) if bc.buy_max_lots > 0 and bm.buy_max_lots > 0 else 1.0
            return _ML(
                buy_cash=bc.buy_max_lots,
                buy_margin=bm.buy_max_lots,
                sell_cash=sc.sell_max_lots,
                sell_margin=sm.sell_max_lots,
                buy_money=bmu,
                buy_margin_money=bmmu,
                leverage=lev,
            )
        return await asyncio.to_thread(_fetch)

    async def execution_references(self, figis: list[str]):
        import asyncio
        from app.bot.execution_reference import ExecutionReference

        requested = set(figis)

        def fetch():
            response = self._get_services().market_data.get_last_prices(figi=sorted(requested))
            received_at = datetime.now(UTC)
            references = {}
            duplicates = set()
            for item in response.last_prices:
                if item.figi not in requested:
                    continue
                if item.figi in references:
                    duplicates.add(item.figi)
                    continue
                references[item.figi] = ExecutionReference(
                    figi=item.figi,
                    price=float(item.price.units) + float(item.price.nano) / 1e9,
                    market_at=item.time,
                    received_at=received_at)
            for figi in duplicates:
                references.pop(figi, None)
            return references

        if not requested:
            return {}
        try:
            result = await asyncio.to_thread(fetch)
            self._mark_ok("execution_references")
            return result
        except Exception as error:
            self._mark_degraded("execution_references", error)
            return {}

    async def last_prices(self, figis: list[str]) -> dict[str, float]:
        """Последние цены по списку figi (для intrabar-проверки SL/TP)."""
        import asyncio

        def _fetch():
            services = self._get_services()
            resp = services.market_data.get_last_prices(figi=list(figis))
            out: dict[str, float] = {}
            for lp in (getattr(resp, "last_prices", None) or []):
                try:
                    out[lp.figi] = float(lp.price.units) + float(lp.price.nano) / 1e9
                except Exception:
                    continue
            return out

        try:
            out = await asyncio.to_thread(_fetch)
            self._mark_ok("last_prices")
            return out
        except Exception as e:
            self._mark_degraded("last_prices", e)
            return {}

    async def get_trading_status(self, figi: str) -> str:
        """Текущий статус торгов инструмента из API (SecurityTradingStatus enum name).
        Возвращает имя константы, например SECURITY_TRADING_STATUS_NORMAL_TRADING."""
        import asyncio

        def _fetch():
            services = self._get_services()
            return services.market_data.get_trading_status(instrument_id=figi)

        try:
            resp = await asyncio.to_thread(_fetch)
            self._mark_ok("trading_status")
            return str(resp.trading_status)
        except Exception as e:
            self._mark_degraded("trading_status", e)
            return ""

    async def main_session_active(self) -> bool:
        """Основная торговая сессия идёт (по живым статусам API, без привязки к акции).
        Истина, когда большинство инструментов в NORMAL_TRADING — это и есть основная
        сессия Мосбиржи. Расписания не используем — позари на статусы в моменте."""
        import asyncio

        def _fetch():
            services = self._get_services()
            return services.market_data.get_trading_statuses()

        try:
            resp = await asyncio.to_thread(_fetch)
            self._mark_ok("main_session_active")
            total = len(resp.trading_statuses)
            if total == 0:
                return False
            n_trading = sum(
                1 for x in resp.trading_statuses
                if int(x.trading_status) == 5  # SECURITY_TRADING_STATUS_NORMAL_TRADING
            )
            return n_trading / total > 0.5
        except Exception as e:
            self._mark_degraded("main_session_active", e)
            return False

    def _execution_from_response(self, resp, order_id: str, requested_qty: int) -> LiveExecution:
        """Собрать структурированный результат из PostOrderResponse (ARCH160/ARCH176).

        Единицы — лоты: requested/executed/remaining. fill_price — фактическая цена.
        Не умножаем на лот здесь — перевод в штуки делается один раз при записи сделки.

        Подтверждённые количества: requested > 0; 0 <= executed <= requested;
        remaining = requested - executed. Противоречивые количества дают UNKNOWN/None;
        fill_price либо конечное положительное число, либо None (неизвестно).
        Комиссия: явный ноль брокера (0.0) отличим от «поля нет» (None).
        """
        import math

        fill = None
        try:
            eop = resp.executed_order_price
            if eop is not None and hasattr(eop, "units") and hasattr(eop, "nano"):
                _fu = int(getattr(eop, "units", 0) or 0)
                _fn = int(getattr(eop, "nano", 0) or 0)
                _f = float(_fu) + float(_fn) / 1e9
                if math.isfinite(_f) and _f > 0:
                    fill = _f
        except Exception:
            fill = None
        requested = requested_qty
        reported_requested = getattr(resp, "lots_requested", None)
        executed = getattr(resp, "lots_executed", None)
        valid_quantity = (
            type(requested) is int and requested > 0
            and type(reported_requested) is int and reported_requested == requested
            and type(executed) is int and 0 <= executed <= requested
        )
        if not valid_quantity:
            return LiveExecution(
                order_id=order_id, status="UNKNOWN", requested_lots=requested,
                executed_lots=None, remaining_lots=None,
                error="inconsistent_broker_quantity",
            )
        remaining = requested - executed
        if executed >= requested and fill is not None:
            status = "FILLED"
        elif executed > 0 and fill is not None:
            status = "PARTIAL"
        else:
            status = "UNKNOWN"
        commission = None
        try:
            _c = getattr(resp, "executed_commission", None)
            if _c is not None and hasattr(_c, "units") and hasattr(_c, "nano"):
                _cu = int(getattr(_c, "units", 0) or 0)
                _cn = int(getattr(_c, "nano", 0) or 0)
                commission = float(_cu) + float(_cn) / 1e9
        except Exception:
            commission = None
        return LiveExecution(
            order_id=order_id,
            status=status,
            requested_lots=requested,
            executed_lots=executed,
            remaining_lots=remaining,
            fill_price=fill,
            commission=commission,
            error=None,
        )

    async def open_position(
        self, figi: str, ticker: str, side: str, qty: int, price: float,
        stop_loss: float | None, take_profit: float | None, strategy_id: str,
        plan_meta: dict | None = None, price_is_filled: bool = False,
        client_order_id: str | None = None,
    ):
        import asyncio
        from t_tech.invest import OrderDirection, OrderType
        from app.bot.open_submission import OpenSubmission
        from app.services.broker_order_journal import BrokerOrderJournal
        from app.services.open_order_intent import OpenIntent
        from app.services.exit_policy_snapshot import ExitPolicySnapshot

        scope = (getattr(self, 'mode', None), getattr(self, '_account', None))
        if scope[0] not in ('live', 'sandbox') or not isinstance(scope[1], str) or not scope[1].strip():
            raise ValueError('entry_broker_scope_missing')
        if side not in ('BUY', 'SELL') or type(qty) is not int or qty <= 0:
            raise ValueError('entry_direction_or_quantity_invalid')
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError('original_entry_request_id_required')
        from app.bot.broker_request_identity import validate_broker_request_id
        validate_broker_request_id(client_order_id)
        plan_meta = plan_meta or {}
        if (type(plan_meta.get('leverage', 1)) not in (int, float) or plan_meta.get('leverage', 1) != 1
                or bool(getattr(getattr(self, 'config', None), 'use_margin', False))):
            raise ValueError('durable_entry_margin_not_supported')
        policy = ExitPolicySnapshot.from_payload(plan_meta.get('exit_policy'))
        self.last_execution = None
        lot = await self._get_lot(figi)
        if (self.mode, self._account) != scope or type(lot) is not int or lot <= 0:
            raise ValueError('entry_lot_or_scope_unknown')
        if plan_meta.get('lot_size') != lot:
            raise ValueError('entry_lot_changed_since_sizing')
        await self.flush_portfolio()
        if (self.mode, self._account) != scope:
            raise ValueError('entry_scope_changed_in_preflight')
        existing = await self.get_position(figi)
        if (self.mode, self._account) != scope or existing is not None:
            raise ValueError('entry_requires_no_existing_position')
        intent = OpenIntent(mode=scope[0], broker_account_id=scope[1], client_order_id=client_order_id,
            figi=figi, side=side, requested_units=qty * lot, lot_size=lot, created_at=datetime.now(UTC),
            ticker=ticker, strategy_id=strategy_id,
            stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
            take_profit=Decimal(str(take_profit)) if take_profit is not None else None, exit_policy=policy)
        intent.validate()
        journal = BrokerOrderJournal(self.sessions)
        if not await journal.reserve(intent):
            if await journal.load(intent) is not None:
                raise EntrySubmissionUnknown('entry_request_already_reserved')
            raise ValueError('entry_instrument_already_reserved')
        direction = OrderDirection.ORDER_DIRECTION_BUY if side == 'BUY' else OrderDirection.ORDER_DIRECTION_SELL
        try:
            if (self.mode, self._account) != scope:
                raise ValueError('entry_scope_changed_after_reservation')
            response = await asyncio.to_thread(self._api_post_order, expected_scope=scope,
                instrument_id=figi, quantity=qty, direction=direction, account_id=scope[1],
                order_type=OrderType.ORDER_TYPE_MARKET, order_id=client_order_id)
            if (self.mode, self._account) != scope:
                raise ValueError('entry_scope_changed_after_submission')
            if not self._close_response_identity(response, request_id=client_order_id, figi=figi,
                                                 direction=direction, requested_lots=qty):
                raise ValueError('entry_response_identity_mismatch')
            executed = getattr(response, 'lots_executed', None)
            if type(executed) is not int or not 0 <= executed <= qty:
                raise ValueError('entry_response_quantity_mismatch')
            status = self._close_evidence_status(response, executed * lot, qty * lot)
            order_id = getattr(response, 'order_id', None)
            if not await journal.observe(intent, broker_order_id=order_id, broker_status=status, executed_units=executed * lot):
                raise ValueError('entry_observation_not_saved')
            if (self.mode, self._account) != scope:
                raise ValueError('entry_scope_changed_after_observation')
        except Exception as error:
            raise EntrySubmissionUnknown('entry_submission_requires_reconciliation') from error
        return OpenSubmission(*scope, client_order_id, order_id, figi, side, qty * lot, executed * lot, lot, status)

    async def close_position(self, figi: str, price: float, reason: str, *, client_order_id: str | None = None, expected_units: int | None = None, entry_time: datetime | None = None, entry_time_source: str = "unknown", local_position_id: int | None = None, entry_client_order_id: str | None = None) -> CloseSubmission | None:
        import asyncio
        from uuid import uuid4

        from t_tech.invest import OrderDirection, OrderType

        scope = (getattr(self, 'mode', None), getattr(self, '_account', None))
        mode, account = scope
        if mode not in ('live', 'sandbox') or not isinstance(account, str) or not account.strip():
            return None
        def scope_matches():
            return (self.mode, self._account) == scope

        pos = await self.get_position(figi)
        if not scope_matches() or pos is None:
            return None
        if pos.side not in ('LONG', 'SHORT'):
            return None
        direction = OrderDirection.ORDER_DIRECTION_SELL if pos.side == "LONG" else OrderDirection.ORDER_DIRECTION_BUY
        if expected_units is not None and pos.qty != expected_units:
            self._log(f"CLOSE POSITION_CHANGED {figi[-6:]}: reconcile before submitting")
            return None

        lot = await self._get_lot(figi)
        if not scope_matches() or type(lot) is not int or lot <= 0:
            self._log(f"CLOSE LOT_UNKNOWN {figi[-6:]} {getattr(pos, 'ticker', figi[-6:])}: close blocked")
            return None
        # ARCH233: количество к закрытию — положительное целое, кратное подтверждённому
        # лоту. Раньше `max(pos.qty // lot, 1)` на остатке 0/5 при лоте 10 отправлял
        # заявку на 1 лот (больше остатка), а на 15 — молча закрывал 10 из 15 как
        # «полное закрытие». Некратный/неизвестный остаток не угадываем: оставляем
        # позицию неразрешённой до сверки с брокером.
        _held_shares = getattr(pos, "qty", None)
        if type(_held_shares) is not int or _held_shares <= 0:
            self._log(f"CLOSE NOTHING {figi[-6:]} {getattr(pos, 'ticker', figi[-6:])}: "
                      f"у брокера нет остатка (qty={getattr(pos, 'qty', None)}); заявка не отправлена")
            return None
        if _held_shares % lot != 0:
            self._log(f"CLOSE LOT_MISMATCH {figi[-6:]} {getattr(pos, 'ticker', figi[-6:])}: "
                      f"остаток {_held_shares} акций не кратен лоту {lot}; "
                      f"неполное закрытие и floor не отправляем — нужна сверка")
            return None
        order_qty = _held_shares // lot

        # Internal accounting identity must be known before anything durable is written:
        # an invented fallback account would book an exit on a stranger's ledger.
        try:
            async with self.sessions() as db:
                acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
                account_id = getattr(acc, "id", None)
        except Exception as error:
            self._log(f"CLOSE STORAGE_UNAVAILABLE {figi[-6:]} {getattr(pos, 'ticker', figi[-6:])}: "
                      f"{type(error).__name__}: {str(error)[:90]}")
            return None
        if not scope_matches() or type(account_id) is not int or account_id <= 0:
            self._log(f"CLOSE ACCOUNT_UNKNOWN {figi[-6:]} {getattr(pos, 'ticker', figi[-6:])}: "
                      f"no accounting identity, close not submitted")
            return None

        # ARCH379: freeze scope once. Reading mode/account/enums back off a mutable object
        # after an await lets a scope switch send with one identity and record with another.
        request_id = client_order_id or str(uuid4())
        from app.bot.broker_request_identity import validate_broker_request_id
        validate_broker_request_id(request_id)
        requested_lots = order_qty
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        pos_side = pos.side
        pos_ticker = str(getattr(pos, "ticker", "") or "")
        pos_entry_price = pos.entry_price
        pos_strategy = str(getattr(pos, "strategy_id", "") or "")

        from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent
        from app.services.close_position_snapshot import ClosePositionSnapshot

        # The broker position carries snapshot time, not proven entry time: never relabel it.
        # Provenance may be passed in, or already declared by the position object itself.
        _src = entry_time_source
        _ts = entry_time
        if _src not in ("broker_history", "execution_ledger", "sandbox_order_timestamp"):
            _src = getattr(pos, "entry_time_source", "unknown")
            if _ts is None:
                _ts = getattr(pos, "entry_time_confirmed", None)
        _entry = _ts if _src in ("broker_history", "execution_ledger", "sandbox_order_timestamp") else None
        # ARCH393: the durable local position link travels with the snapshot only as a pair,
        # and only when both identities are usable; an unlinked close stays unlinked.
        if local_position_id is not None or entry_client_order_id is not None:
            if (type(local_position_id) is not int or local_position_id <= 0
                    or not isinstance(entry_client_order_id, str)
                    or not entry_client_order_id.strip() or len(entry_client_order_id) > 64):
                self._log(f"CLOSE LOCAL_IDENTITY_INVALID {figi[-6:]}: keep request unlinked")
                _link = (None, None)
            else:
                _link = (local_position_id, entry_client_order_id.strip())
        else:
            _link = (None, None)
        _rate = getattr(getattr(self, "config", None), "commission_rate", None)
        snapshot = ClosePositionSnapshot(
            ticker=pos_ticker,
            position_side=pos_side,
            position_units=_held_shares,
            entry_price=Decimal(str(pos_entry_price)),
            observed_at=datetime.now(UTC),
            entry_time=_entry,
            entry_time_source=_src if _entry is not None else "unknown",
            strategy_id=pos_strategy,
            exit_reason=str(reason)[:32],
            internal_account_id=account_id,
            commission_rate=Decimal(str(_rate)) if _rate is not None else None,
            cost_basis="configured_estimate" if _rate is not None else "unknown",
            local_position_id=_link[0],
            entry_client_order_id=_link[1],
        )
        journal = BrokerOrderJournal(self.sessions)
        intent = CloseIntent(
            mode=mode,
            broker_account_id=account,
            client_order_id=request_id,
            figi=figi,
            side=close_side,
            requested_units=_held_shares,
            lot_size=lot,
            created_at=datetime.now(UTC),
            position_snapshot=snapshot,
        )
        try:
            reserved = await journal.reserve(intent)
        except Exception as error:
            # Fail closed: no durable reservation means no submission, never an unjournaled send.
            self._log(f"CLOSE JOURNAL_UNAVAILABLE {figi[-6:]} "
                      f"{pos_ticker or figi[-6:]}: {type(error).__name__}: {str(error)[:90]}")
            return None
        if not reserved:
            self._log(f"CLOSE DUPLICATE_INTENT {figi[-6:]} "
                      f"{pos_ticker or figi[-6:]}: unresolved request already exists, not resent")
            return None

        if not scope_matches():
            return None

        def _place():
            if not scope_matches():
                return None
            resp = self._api_post_order(
                expected_scope=scope,
                instrument_id=figi,
                quantity=requested_lots,
                direction=direction,
                account_id=account,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=request_id,
            )
            return resp

        resp = await asyncio.to_thread(_place)
        if not scope_matches():
            return None
        if not self._close_response_identity(resp, request_id=request_id, figi=figi,
                                            direction=direction, requested_lots=requested_lots):
            return None
        executed_lots = getattr(resp, 'lots_executed', None)
        if type(executed_lots) is not int or not 0 <= executed_lots <= requested_lots:
            self._log(f'CLOSE QUANTITY_UNCONFIRMED {figi[-6:]}')
            return None
        executed_units = executed_lots * lot
        broker_order_id = await self._journal_observe(journal, intent, resp, executed_units)
        if not broker_order_id or not scope_matches():
            return None
        return CloseSubmission(mode, account, request_id, broker_order_id, figi, close_side,
                               _held_shares, executed_units, lot,
                               self._close_evidence_status(resp, executed_units, _held_shares))

    def _close_response_identity(self, resp, *, request_id: str, figi: str,
                                 direction, requested_lots: int) -> bool:
        """The response must be provably this request before any evidence is stored."""
        checks = (("order_request_id", request_id, False), ("figi", figi, False),
                  ("direction", direction, False), ("lots_requested", requested_lots, True))
        for name, expected, strict_int in checks:
            value = getattr(resp, name, None)
            if strict_int and type(value) is not int:
                self._log(f"CLOSE RESPONSE_MISMATCH {figi[-6:]} {name}={value!r} not an integer")
                return False
            if value != expected:
                self._log(f"CLOSE RESPONSE_MISMATCH {figi[-6:]} {name}={value!r} expected={expected!r}")
                return False
        return True

    @staticmethod
    def _close_evidence_status(resp, executed_units: int, requested_units: int) -> str:
        from t_tech.invest.schemas import OrderExecutionReportStatus as Status
        raw = getattr(resp, "execution_report_status", None)
        statuses = {
            Status.EXECUTION_REPORT_STATUS_FILL: "FILLED",
            Status.EXECUTION_REPORT_STATUS_REJECTED: "REJECTED",
            Status.EXECUTION_REPORT_STATUS_CANCELLED: "CANCELLED",
            Status.EXECUTION_REPORT_STATUS_NEW: "NEW",
            Status.EXECUTION_REPORT_STATUS_PARTIALLYFILL: "PARTIAL_FILLED",
        }
        if (not isinstance(raw, int) or isinstance(raw, bool) or raw not in statuses
                or type(requested_units) is not int or requested_units <= 0
                or type(executed_units) is not int or not 0 <= executed_units <= requested_units):
            raise ValueError("invalid_broker_close_status_or_quantity")
        status = statuses[raw]
        if (status == "FILLED" and executed_units != requested_units
                or status == "PARTIAL_FILLED" and not 0 < executed_units < requested_units
                or status in ("NEW", "REJECTED") and executed_units != 0):
            raise ValueError("broker_close_status_quantity_conflict")
        return status

    async def _journal_observe(self, journal, intent, resp, executed_units: int):
        """Store validated cumulative broker evidence; returns the broker order id or None."""
        broker_order_id = getattr(resp, "order_id", None)
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            self._log(f"CLOSE NO_BROKER_ID {intent.figi[-6:]}: evidence not stored, needs reconciliation")
            return None
        status = "UNKNOWN"
        try:
            status = self._close_evidence_status(resp, executed_units, intent.requested_units)
            stored = await journal.observe(intent, broker_order_id=broker_order_id,
                                           broker_status=status, executed_units=executed_units)
        except Exception as error:
            self._log(f"CLOSE OBSERVE_FAILED {intent.figi[-6:]} {status} "
                      f"units={executed_units}: {type(error).__name__}: {str(error)[:90]}")
            return None
        if not stored:
            self._log(f"CLOSE OBSERVE_MISMATCH {intent.figi[-6:]} {status} units={executed_units}: "
                      "evidence rejected, needs reconciliation")
            return None
        return broker_order_id

    async def update_protective_levels(self, figi: str, stop: float | None, target: float | None) -> None:
        pass  # live stop-заявки не ставим — выход по сигналу

    async def trades_history(self, limit: int = 100) -> list[PaperTrade]:
        from sqlalchemy import desc

        async with self.sessions() as db:
            res = await db.execute(
                select(PaperTrade).order_by(desc(PaperTrade.exit_time)).limit(limit)
            )
            return list(res.scalars().all())
