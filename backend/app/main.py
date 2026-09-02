from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401
from app.api.routes import analysis, bot as bot_routes, catalog, candles, instruments, lab, ml as ml_routes, orchestrator_route, quorum, research, sandbox, signals, test as test_routes, warehouse, ws
from app.config import get_settings
from app.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.services.test_queue import queue_dispatcher
    from app.services.ensemble_queue import queue_dispatcher as ens_dispatcher

    queue_dispatcher.start()
    ens_dispatcher.start()
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
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
