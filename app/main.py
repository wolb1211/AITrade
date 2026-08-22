from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import (
    create_admin_ai_router,
    create_auth_router,
    create_api_router,
    create_mt5_executions_router,
    create_mt5_router,
)
from app.config import Settings
from app.services.ai_service import AiDecisionClient
from app.services.auth_service import UserAuthService
from app.services.decision_service import DecisionService
from app.services.email_service import EmailService
from app.store import MySQLStore, SqliteStore
from app.strategies.pa_agent_lite import PaAgentLiteStrategy
from app.strategies.pa_mock import PaMockStrategy


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    resolved.database_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    mt5_log = logging.getLogger("gainlab.mt5.validation")
    if not mt5_log.handlers:
        file_handler = logging.FileHandler(
            resolved.database_path.parent / "mt5_validation_errors.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        mt5_log.addHandler(file_handler)
        mt5_log.setLevel(logging.INFO)
    if resolved.database_type == "mysql":
        store = MySQLStore(
            host=resolved.mysql_host,
            port=resolved.mysql_port,
            database=resolved.mysql_database,
            user=resolved.mysql_user,
            password=resolved.mysql_password,
        )
    else:
        store = SqliteStore(resolved.database_path)
    ai_client = AiDecisionClient(store, timeout=resolved.ai_timeout)
    strategies = [
        PaMockStrategy(),
        PaAgentLiteStrategy(ai_client),
    ]
    service = DecisionService(store, {strategy.code: strategy for strategy in strategies})
    auth_service = UserAuthService(store, resolved, EmailService(resolved))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        store.cleanup_expired_ai_usage_details()
        if resolved.environment != "production":
            store.ensure_demo_deployment(resolved.demo_deployment_key)

        async def cleanup_usage_details_daily() -> None:
            while True:
                await asyncio.sleep(24 * 60 * 60)
                await asyncio.to_thread(store.cleanup_expired_ai_usage_details)

        cleanup_task = asyncio.create_task(cleanup_usage_details_daily())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    application = FastAPI(
        title="GainLab AI Trading API",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5180",
            "http://localhost:5180",
            "https://gainlab.ai",
            "https://www.gainlab.ai",
            "https://aitrader.gainlab.ai",
        ],
        allow_origin_regex=r"https://.*\.gainlab\.ai",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "gainlab-ai-trading-api",
            "version": "0.1.0",
        }

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        raw_body = (await request.body()).decode("utf-8", errors="replace")
        message = {
            "path": str(request.url.path),
            "method": request.method,
            "errors": exc.errors(),
            "body": raw_body,
        }
        mt5_log.warning("request validation failed: %s", json.dumps(message, ensure_ascii=False))
        return JSONResponse(
            status_code=422,
            content={
                "detail": exc.errors(),
                "body": raw_body,
            },
        )

    application.include_router(create_api_router(store, service))
    application.include_router(create_mt5_router(store, service))
    application.include_router(create_mt5_executions_router(store, service))
    application.include_router(create_admin_ai_router(
        store,
        admin_jwt_secret=resolved.admin_jwt_secret,
        require_admin_auth=resolved.environment == "production" or bool(resolved.admin_jwt_secret),
    ))
    application.include_router(create_auth_router(auth_service))
    application.state.settings = resolved
    application.state.store = store
    return application


app = create_app()
