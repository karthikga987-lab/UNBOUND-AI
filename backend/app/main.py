"""FastAPI entry point for the UNBOUND AI backend."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.api.routes.health import router as health_router
from backend.app.api.routes.tasks import router as tasks_router
from backend.app.api.runtime import TaskRuntime

logger = logging.getLogger(__name__)


def create_app(runtime: TaskRuntime | None = None) -> FastAPI:
    application = FastAPI(title="UNBOUND AI API", version="0.1.0")
    application.state.runtime = runtime or TaskRuntime()
    application.include_router(health_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request", "message": "Request data is invalid"}})

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": {"code": "internal_error", "message": "An unexpected internal error occurred"}})

    return application


app = create_app()
