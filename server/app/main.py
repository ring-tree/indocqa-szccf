"""FastAPI 入口：路由装配、请求上下文（request_id）中间件、统一异常处理。"""

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.kb import router as kb_router
from app.api.models import router as models_router
from app.api.sessions import router as sessions_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.indexing.lock import acquire_server_lock, release_server_lock
from app.session import store as session_store

logger = logging.getLogger("indocqa")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _session_gc(settings) -> None:
    """服务长期运行时每小时清理一次超过 TTL 的会话。"""
    while True:
        await asyncio.sleep(3600)
        await asyncio.to_thread(
            session_store.cleanup, settings.session_db, settings.session_ttl_days
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    acquire_server_lock(settings)
    removed = session_store.cleanup(settings.session_db, settings.session_ttl_days)
    if removed:
        logger.info("会话清理：删除 %s 个超过 %s 天的会话", removed, settings.session_ttl_days)
    task = asyncio.create_task(_session_gc(settings))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # 进程退出时再清一次并压缩文件，避免 WAL/空洞长期堆积
        session_store.cleanup(settings.session_db, settings.session_ttl_days)
        try:
            session_store.compact(settings.session_db)
        except sqlite3.Error as e:
            logger.warning("会话库压缩失败：%s", e)
        release_server_lock()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(chat_router)
    app.include_router(kb_router)
    app.include_router(sessions_router)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "%s %s -> %s (%.0f ms) [%s]",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - start) * 1000,
            request_id,
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """pydantic 校验失败也走统一错误包，避免 422 默认结构泄漏到前端。"""
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": 40001,
                    "message": "参数校验失败",
                    "detail": jsonable_encoder(exc.errors()),
                }
            },
        )

    return app


app = create_app()
