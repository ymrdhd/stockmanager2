"""ASGI composition root for database-backed and liveness-only development modes."""

import asyncio
import logging
from dataclasses import asdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel

from backend.config import AppConfig
from backend.api.trading import router
from backend.api.portfolio import router as portfolio_router
from backend.api.market import router as market_router
from backend.api.finance import router as finance_router
from backend.application.market import MarketService, QuoteProvider
from backend.infrastructure.database import DatabaseSettings, open_database


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


def create_app(config: AppConfig | None = None, *, database: DatabaseSettings | None = None,
               market_provider: QuoteProvider | None = None) -> FastAPI:
    settings = config if config is not None else AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s event=%(message)s",
        )
        logger = logging.getLogger("stock_manager")
        # Without database settings this factory is a liveness-only development app.
        with open_database(database) if database is not None else nullcontext() as db:
            app.state.database = db
            market = MarketService(db, market_provider) if db is not None else None
            app.state.market = market
            stop = asyncio.Event()
            worker = (asyncio.create_task(market.run(stop, settings.market_refresh_seconds))
                      if market is not None and database is not None and database.mode == 'server'
                      and settings.market_auto_refresh else None)
            logger.info("application_started")
            try:
                yield
            finally:
                stop.set()
                if market is not None:
                    market.stop()
                if worker is not None:
                    await worker
                app.state.market = None
                app.state.database = None
                logger.info("application_stopped")

    app = FastAPI(title=settings.name, version=settings.version, lifespan=lifespan)
    app.include_router(router)
    app.include_router(portfolio_router)
    app.include_router(market_router)
    app.include_router(finance_router)

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def get_health() -> HealthResponse:
        """Report only ASGI application liveness, not business readiness."""
        return HealthResponse(status="ok", version=settings.version)

    @app.get('/api/v1/system/status', tags=['system'])
    def system_status(request: Request):
        db = getattr(request.app.state, 'database', None)
        if db is None:
            return {'mode':'development','writable':False,'identity':None,'version':settings.version}
        # Report identity only after checking the current binding in a read transaction.
        with db.read():
            return {'mode':('server' if db.settings.mode=='server' else
                            'sandbox' if db.settings.writable else 'readonly'),
                    'writable':db.settings.writable,'identity':asdict(db.identity),'version':settings.version}

    return app
