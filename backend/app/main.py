import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models  # noqa: F401
from app.api.routes import analysis, analysis_reports, analysis_replays, bot as bot_routes, catalog, candles, candlehub, instruments, lab, ml as ml_routes, news as news_routes, orchestrator_route, quorum, research, sandbox, screener, signals, test as test_routes, warehouse, ws
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
        # NB: get_settings НЕ импортируем локально — модульный импорт сверху
        # уже есть; локальный import затенял его до присваивания и ломал
        # get_settings() в блоке RUN_MIGRATIONS_ON_START выше (UnboundLocalError):
        # «alembic upgrade head failed: cannot access local variable...».
        _s = get_settings()
        # Контур и параметры прогона — из data/run_state.json (единственный
        # источник, пишет POST /api/v1/bot/mode). .env — легаси-фолбэк, и мы
        # честно пишем в лог, откуда взялись: раньше TEST_ENGINE жил только в
        # процессе, и после рестарта контур молча падал в ensemble_v4 вместо
        # одиночного движка (реплей на порядок медленнее).
        from app.services.run_state import resolve_boot_state, sync_env
        _state, _src = resolve_boot_state(_s)
        _mode = _state.get("mode") if _state.get("mode") in ("sandbox", "live", "test") else "sandbox"
        if _mode == "test":
            # process-env — производная от файла, не самостоятельный источник
            sync_env(_state)
        import logging as _logging
        _logging.getLogger("uvicorn").info(
            "Auto-start contour=%s from %s (engine=%s tf=%s test=%s)",
            _mode, _src, _state.get("test_engine") or "ensemble_v4",
            _state.get("test_interval") or "1min", _state.get("test_name") or "-")

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
            # Контур/фид/реплей — из run_state.json (не из сохранёнок настроек).
            cfg.mode = _mode
            cfg.feed = "replay" if _mode == "test" else "stream"
            if _mode == "test":
                # Тест-оверрайды (вариант + секция "bot" вариант-файла) применяет
                # runtime.start() — единственное место, ПОСЛЕДНИМИ по приоритету,
                # чтобы bot_config.json/gates_config.json их не затирали.
                cfg.mode = "test"
                cfg.feed = "replay"
                cfg.test_name = _state.get("test_name", "")
                cfg.replay_start = _state.get("replay_start", "")
                cfg.replay_end = _state.get("replay_end", "")
                cfg.replay_pace = _state.get("replay_pace") or "fast"
                cfg.test_variant = _s.bot_test_variant or os.environ.get("TEST_VARIANT", "")
                cfg.replay_log_persist = bool(_state.get("replay_log_persist"))
                # Оверрайды движка/TF/параметров/пресета — через process-env,
                # который мы только что пересобрали из run_state.json (sync_env).

            # Сильная ссылка на задачу старта: create_task без ссылки может
            # быть собран GC до завершения старта (рвутся asyncpg-коннекты).
            _bot_start_task = asyncio.create_task(runtime.start(cfg))
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

    # bot_logs retention (P2.2): раз в 6 часов удаляем строки старше
    # BOT_LOGS_RETENTION_DAYS (0 = выключено). Индекс по ts уже есть в модели.
    async def _botlogs_retention() -> None:
        import logging as _logging
        from sqlalchemy import text as _text
        _log = _logging.getLogger("uvicorn")
        await asyncio.sleep(30.0)
        while True:
            try:
                from app.config import get_settings
                _days = get_settings().bot_logs_retention_days
                if _days > 0:
                    async with engine.begin() as conn:
                        _res = await conn.execute(
                            _text("DELETE FROM bot_logs WHERE ts < now() - make_interval(days => :d)"),
                            {"d": _days},
                        )
                        if _res.rowcount:
                            _log.info("bot_logs retention: удалено %s строк старше %s дн.", _res.rowcount, _days)
            except Exception as e:
                _log.warning("bot_logs retention: %s", str(e)[:120])
            await asyncio.sleep(6 * 3600.0)

    _retention_task = asyncio.create_task(_botlogs_retention())

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
        _retention_task.cancel()
        for _t in (_imoex_task, _daily_task, _retention_task):
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

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        import uuid as _uuid
        from app.bot.runtime import _request_id_ctx

        rid = request.headers.get("x-request-id") or _uuid.uuid4().hex[:12]
        token = _request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = rid
            return response
        finally:
            _request_id_ctx.reset(token)
    app.include_router(instruments.router)
    app.include_router(candles.router)
    app.include_router(candlehub.router)
    app.include_router(analysis.router)
    app.include_router(analysis_reports.router)
    app.include_router(analysis_replays.router)
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
    app.include_router(news_routes.router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        from app.bot.runtime import runtime

        task_errors = getattr(runtime, "_task_errors", {})
        critical_dead = any(
            err.get("count", 0) > 0
            for name, err in task_errors.items()
            if name in ("persist", "reconcile", "main")
        )

        return {
            "status": "degraded" if critical_dead else "ok",
            "running": bool(getattr(runtime, "running", False)),
            "starting": bool(getattr(runtime, "starting", False)),
            "tasks": {
                "main": bool(getattr(getattr(runtime, "task", None), "done", lambda: True)() is False),
                "startup": bool(getattr(getattr(runtime, "startup_task", None), "done", lambda: True)() is False),
                "persist": bool(getattr(getattr(runtime, "_persist_task", None), "done", lambda: True)() is False),
                "reconcile": bool(getattr(getattr(runtime, "_reconcile_task", None), "done", lambda: True)() is False),
            },
            "task_errors": {k: {"count": v.get("count", 0), "last_error": v.get("error", "")[:100]} for k, v in task_errors.items()},
        }

    @app.get("/api/live", tags=["system"])
    async def live() -> dict:
        return {"status": "ok", "alive": True}

    @app.get("/api/ready", tags=["system"])
    async def ready() -> dict:
        from app.bot.runtime import runtime
        from app.database import engine
        from sqlalchemy import text as _text

        task_errors = getattr(runtime, "_task_errors", {})
        critical_dead = any(
            err.get("count", 0) > 0
            for name, err in task_errors.items()
            if name in ("persist", "reconcile", "main")
        )

        db_ok = False
        try:
            async with engine.connect() as conn:
                await conn.execute(_text("SELECT 1"))
            db_ok = True
        except Exception:
            pass

        if critical_dead or not db_ok:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "db_ok": db_ok,
                    "critical_tasks_dead": critical_dead,
                    "task_errors": {k: {"count": v.get("count", 0)} for k, v in task_errors.items()},
                },
            )

        return {
            "status": "ready",
            "db_ok": db_ok,
            "running": bool(getattr(runtime, "running", False)),
        }

    return app


app = create_app()
