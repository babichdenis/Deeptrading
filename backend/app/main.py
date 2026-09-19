import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models  # noqa: F401
from app.api.routes import analysis, bot as bot_routes, catalog, candles, instruments, lab, ml as ml_routes, orchestrator_route, quorum, research, sandbox, screener, signals, test as test_routes, warehouse, ws
from app.config import get_settings
from app.database import Base, engine
from app.logging_setup import setup_logging

_log = logging.getLogger("uvicorn")

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Догоняющие ALTER'ы (idempotent). Основная схема-эволюция — в alembic/
        # (включить: RUN_MIGRATIONS_ON_START=1), см. alembic/versions/0001_*.
        from sqlalchemy import text as _text
        try:
            await conn.execute(_text(
                "ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS mode VARCHAR(8) DEFAULT 'sandbox'"))
        except Exception:
            pass
        try:
            await conn.execute(_text(
                "ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS test_name VARCHAR(64)"))
            await conn.execute(_text(
                "CREATE INDEX IF NOT EXISTS ix_sandbox_trades_test_name ON sandbox_trades (test_name)"))
        except Exception:
            pass
    try:
        if get_settings().run_migrations_on_start:
            from alembic import command as _alc
            from alembic.config import Config as _AlcCfg
            from pathlib import Path as _Path
            _cfg = _AlcCfg(str(_Path(__file__).resolve().parents[1] / "alembic.ini"))
            _alc.upgrade(_cfg, "head")
            _log.info("alembic upgrade head: ok")
    except Exception as e:  # noqa: BLE001
        _log.warning("alembic upgrade head failed: %s", e)
    from app.services.test_queue import queue_dispatcher
    from app.services.ensemble_queue import queue_dispatcher as ens_dispatcher

    queue_dispatcher.start()
    ens_dispatcher.start()

    # Auto-start paper bot on backend startup
    try:
        from app.bot.runtime import runtime, BotConfig
        from app.config import get_settings
        _s = get_settings()
        _mode = _s.bot_mode if _s.bot_mode in ("sandbox", "live", "test") else "sandbox"
        if not runtime.running:
            cfg = BotConfig(
                strategy_id="ensemble_v4",
                interval_name="1min",
                top_n=40,
                use_ensemble=True,
                mode=_mode,
                sessions=["morning", "day", "evening"],
                long_allowed=True,
                short_allowed=True,
                ensemble_session="all",
                atr_period=14,
                atr_multiplier=4.0,
                atr_risk_reward=4.0,
                leverage=1.0,
                commission_rate=0.0005,
                slippage_bps=2.0,
                confirm_flip=2,
                reentry_cooldown_bars=15,
                overnight=(_mode != "live"),
            )
            # Сохранённые настройки пользователя (PATCH/UI) важнее хардкода:
            # иначе рестарт сбрасывает pos_pct/margin_sizing/лимиты/rank/очередь.
            try:
                from app.bot.runtime import BOT_PERSIST_FIELDS, load_bot_settings
                _saved = await load_bot_settings()
                for _f in BOT_PERSIST_FIELDS:
                    if _f in _saved:
                        try:
                            setattr(cfg, _f, _saved[_f])
                        except Exception:
                            pass
            except Exception:
                pass
            # Режим/фид/реплей — всегда из .env, не из сохранёнок.
            cfg.mode = _mode
            cfg.feed = "replay" if _mode == "test" else "stream"
            if _mode == "test":
                try:
                    from app.bot.runtime import apply_test_overrides
                    apply_test_overrides(cfg)
                except Exception:
                    pass
                cfg.mode = "test"
                cfg.feed = "replay"
                cfg.test_name = _s.bot_test_name or os.environ.get("BOT_TEST_NAME", "")
                cfg.replay_start = _s.bot_test_start or os.environ.get("BOT_TEST_START", "")
                cfg.replay_end = _s.bot_test_end or os.environ.get("BOT_TEST_END", "")
                cfg.replay_pace = _s.bot_test_pace or os.environ.get("BOT_TEST_PACE", "fast")
            asyncio.create_task(runtime.start(cfg))
    except Exception as e:
        import logging
        logging.getLogger("uvicorn").warning("Auto-start bot failed: %s", e)

    # IMOEX keepalive: постоянная запись 1м свечей индекса MOEX в БД (даже без бота).
    # Нужна IMOEX-guard'у бота (запрет входов против всплеска индекса) и аналитике.
    async def _imoex_keepalive() -> None:
        import logging as _logging
        _log = _logging.getLogger("uvicorn")
        await asyncio.sleep(3.0)
        while True:
            try:
                from app.bot.moex import sync_imoex_recent
                _n = await asyncio.to_thread(sync_imoex_recent, 180)
                if _n:
                    _log.info("IMOEX keepalive: +%s свечей", _n)
            except Exception as e:
                _log.warning("IMOEX keepalive: %s", str(e)[:120])
            await asyncio.sleep(300.0)

    _imoex_task = asyncio.create_task(_imoex_keepalive())

    # Daily bars keepalive: дневные бары (interval=24) для bias/аналитики.
    # Проверяем каждые 30 мин; синк — раз в сутки после вечерней сессии (00:00–01:00 МСК)
    # или при старте, если бары устарели (>2 дней).
    async def _daily_bars_keepalive() -> None:
        import logging as _logging
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from zoneinfo import ZoneInfo as _ZI
        _log = _logging.getLogger("uvicorn")
        _msk = _ZI("Europe/Moscow")
        _last_run = None
        await asyncio.sleep(20.0)
        while True:
            try:
                from app.bot.moex import sync_moex_daily
                from app.database import SessionLocal as _SL
                from sqlalchemy import text as _text
                now = _dt.now(_tz.utc)
                msk = now.astimezone(_msk)
                # устарели ли дневные бары (последний бар старше 2 суток)?
                _stale = True
                try:
                    async with _SL() as db:
                        _mx = (await db.execute(_text(
                            "SELECT max(ts) FROM candles WHERE interval = 24"))).scalar()
                    _stale = (_mx is None) or ((now - _mx) > _td(days=2))
                except Exception:
                    _stale = False
                _window = (msk.hour == 0) or (msk.hour == 23 and msk.minute >= 55)
                _due = _last_run is None or (now - _last_run) > _td(hours=20)
                if _stale or (_window and _due):
                    _last_run = now
                    async with _SL() as db:
                        rows = (await db.execute(_text(
                            "SELECT figi, ticker FROM universe"))).all()
                    _n = _ok = 0
                    for _f, _t in rows:
                        try:
                            _k = await asyncio.to_thread(sync_moex_daily, str(_f), str(_t), 400)
                            _n += _k
                            _ok += 1 if _k else 0
                        except Exception:
                            pass
                        await asyncio.sleep(0.25)
                    _log.info("daily bars keepalive: %s тикеров, %s баров", _ok, _n)
            except Exception as e:
                _log.warning("daily bars keepalive: %s", str(e)[:120])
            await asyncio.sleep(1800.0)

    _daily_task = asyncio.create_task(_daily_bars_keepalive())

    try:
        yield
    finally:
        # Гасим бота и его фоновые циклы ДО dispose — иначе uvicorn висит на
        # shutdown (pending tasks), и процесс приходится убивать kill -9.
        try:
            from app.bot.runtime import runtime as _rt
            await _rt.stop()
        except Exception:
            pass
        _imoex_task.cancel()
        _daily_task.cancel()
        for _t in (_imoex_task, _daily_task):
            try:
                await _t
            except (asyncio.CancelledError, Exception):
                pass
        queue_dispatcher.stop()
        ens_dispatcher.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Анализ рынка акций и торговый робот (Т-Инвестиции)",
        lifespan=lifespan,
    )
    _cors_origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins or ["*"],
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    if not _cors_origins:
        logging.getLogger("uvicorn").warning(
            "CORS_ORIGINS пуст — CORS открыт на все домены (*). "
            "Для строгого режима задайте CORS_ORIGINS=http://localhost:5173 в .env."
        )

    # Bearer-токен на мутирующие методы (POST/PATCH/PUT/DELETE) под /api.
    # Пока API_TOKEN пуст — отключено (обратная совместимость).
    _write_token = settings.api_token

    @app.middleware("http")
    async def _write_auth(request: Request, call_next):
        if (_write_token and request.method in ("POST", "PATCH", "PUT", "DELETE")
                and request.url.path.startswith("/api")):
            if request.headers.get("authorization") != f"Bearer {_write_token}":
                return JSONResponse(status_code=401,
                                    content={"detail": "unauthorized"})
        return await call_next(request)
    app.include_router(instruments.router)
    app.include_router(candles.router)
    app.include_router(analysis.router)
    app.include_router(catalog.router)
    app.include_router(research.router)
    app.include_router(orchestrator_route.router)
    app.include_router(signals.router)
    app.include_router(quorum.router)
    app.include_router(lab.router)
    app.include_router(bot_routes.router)
    app.include_router(sandbox.router)
    app.include_router(test_routes.router)
    app.include_router(ml_routes.router)
    app.include_router(warehouse.router)
    app.include_router(ws.router)
    app.include_router(screener.router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
