"""ASGI composition root: one process serves the built browser app and the API."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.api.auth import router as auth_router
from backend.api.finance import router as finance_router
from backend.api.maintenance import router as maintenance_router
from backend.api.market import router as market_router
from backend.api.portfolio import router as portfolio_router
from backend.api.requests import router as request_router
from backend.api.strategy import router as strategy_router
from backend.api.trading import router
from backend.application.market import MarketService, QuoteProvider
from backend.application.strategy import StrategyService
from backend.config import AppConfig
from backend.infrastructure.database import DatabaseSettings, open_database
from backend.security.middleware import BrowserSecurity, Security

INDEX_FILE = 'index.html'
ASSET_DIR = 'assets'


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


def web_root(static_dir: Path | None) -> Path | None:
    """Return the directory holding a complete production build, or None."""
    if static_dir is None:
        return None
    root = static_dir.resolve()
    if not root.is_dir() or not (root / INDEX_FILE).is_file():
        return None
    return root


def create_app(config: AppConfig | None = None, *, database: DatabaseSettings | None = None,
               market_provider: QuoteProvider | None = None) -> FastAPI:
    settings = config if config is not None else AppConfig()
    security = (Security.build(settings.auth, require_identity=settings.require_write_identity)
                if settings.auth is not None else None)

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
            app.state.strategy = StrategyService(db, near_stop_bps=settings.near_stop_bps) if db is not None else None
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
                app.state.strategy = None
                app.state.database = None
                logger.info("application_stopped")

    app = FastAPI(title=settings.name, version=settings.version, lifespan=lifespan)
    app.state.security = security
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(portfolio_router)
    app.include_router(market_router)
    app.include_router(strategy_router)
    app.include_router(finance_router)
    app.include_router(maintenance_router)
    app.include_router(request_router)
    if security is not None:
        app.add_middleware(BrowserSecurity, security=security)

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def get_health() -> HealthResponse:
        """Report only ASGI application liveness, not business readiness."""
        return HealthResponse(status="ok", version=settings.version)

    @app.get('/api/v1/system/status', tags=['system'])
    def system_status(request: Request):
        db = getattr(request.app.state, 'database', None)
        root = web_root(settings.static_dir)
        web = {'built': root is not None, 'path': str(root) if root is not None else None}
        if db is None:
            return {'mode': 'development', 'writable': False, 'identity': None,
                    'version': settings.version, 'web': web, 'listen_port': (request.scope.get('server') or (None,None))[1],
                    'auth_required': security is not None}
        # Report identity only after checking the current binding in a read transaction.
        with db.read():
            return {'mode': ('server' if db.settings.mode == 'server' else
                             'sandbox' if db.settings.writable else 'readonly'),
                    'writable': db.settings.writable, 'identity': asdict(db.identity),
                    'version': settings.version, 'web': web, 'listen_port': (request.scope.get('server') or (None,None))[1],
                    'auth_required': security is not None}

    root = web_root(settings.static_dir)
    if root is None:
        @app.get('/', include_in_schema=False)
        def missing_web_build() -> JSONResponse:
            """Refuse to pretend the browser application exists when it was never built."""
            return JSONResponse(status_code=503, content={
                'detail': {
                    'code': 'web_build_missing',
                    'message': 'frontend build is missing; run npm run build in frontend/',
                }})
    else:
        assets = root / ASSET_DIR
        if assets.is_dir():
            app.mount('/' + ASSET_DIR, StaticFiles(directory=assets), name='assets')
        # The shell is served for the single-page routes the browser app owns.
        app.mount('/', StaticFiles(directory=root, html=True), name='web')

    return app
