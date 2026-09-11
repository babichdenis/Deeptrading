import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401
from app.api.routes import analysis, bot as bot_routes, catalog, candles, instruments, lab, ml as ml_routes, orchestrator_route, quorum, research, sandbox, screener, signals, test as test_routes, warehouse, ws
from app.config import get_settings
from app.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Миграция: признак контура сделки (sandbox|live) в реестре сделок.
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
            if _mode == "test":
                cfg.mode = "test"
                cfg.feed = "replay"
                cfg.test_name = os.environ.get("BOT_TEST_NAME", "")
                cfg.replay_start = os.environ.get("BOT_TEST_START", "")
                cfg.replay_end = os.environ.get("BOT_TEST_END", "")
                cfg.replay_pace = "fast"
            asyncio.create_task(runtime.start(cfg))
    except Exception as e:
        import logging
        logging.getLogger("uvicorn").warning("Auto-start bot failed: %s", e)

    try:
        yield
    finally:
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
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
