import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as SATimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router
from app.core import metrics
from app.core.config import settings
from app.core.errors import ApiError, envelope
from app.core.logging import correlation_id_var, log, setup_logging, user_id_var
from app.db.session import engine

setup_logging(settings.log_level)
logger = logging.getLogger("app.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Seat Reservation", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RequestContextMiddleware:
    """Pure ASGI middleware: correlation id, request log line, HTTP metrics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        cid = (headers.get(b"x-request-id") or b"").decode()[:64] or uuid.uuid4().hex
        correlation_id_var.set(cid)
        user_id_var.set("-")
        start = time.perf_counter()
        status = [500]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", cid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.HTTP.labels(scope["method"], str(status[0])).inc()
            if status[0] >= 500:
                metrics.HTTP_5XX.inc()
            path = scope["path"]
            if path not in ("/metrics", "/health/live"):
                log(logger, "request", action="http", method=scope["method"], path=path,
                    status=status[0], latency_ms=round((time.perf_counter() - start) * 1000, 1))


app.add_middleware(RequestContextMiddleware)


def _json(status: int, err: ApiError) -> JSONResponse:
    return JSONResponse(envelope(err, correlation_id_var.get()), status_code=status)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return _json(exc.status, exc)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    details = {".".join(str(p) for p in e["loc"][1:]) or "body": e["msg"] for e in exc.errors()}
    return _json(400, ApiError(400, "VALIDATION_ERROR", "Request validation failed", details))


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
    return _json(exc.status_code, ApiError(exc.status_code, code, str(exc.detail)))


@app.exception_handler(DBAPIError)
@app.exception_handler(OperationalError)
@app.exception_handler(SATimeoutError)
@app.exception_handler(OSError)  # connection refused / reset while the DB is down or restarting
@app.exception_handler(TimeoutError)
async def db_unavailable_handler(request: Request, exc: Exception):
    # Fail closed: dependency trouble is a 503 (never a half-applied success, never a generic 500).
    log(logger, "database_error", level=logging.ERROR, error=type(exc).__name__)
    return JSONResponse(
        envelope(ApiError(503, "SERVICE_UNAVAILABLE", "Service temporarily unavailable"), correlation_id_var.get()),
        status_code=503,
        headers={"Retry-After": "2"},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("unhandled_error")
    return _json(500, ApiError(500, "INTERNAL_ERROR", "Internal server error"))


app.include_router(router)
