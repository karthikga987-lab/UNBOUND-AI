"""FastAPI entry point for the UNBOUND AI backend."""
from __future__ import annotations

import logging
import ipaddress
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.routes.health import router as health_router
from backend.app.api.routes.conversation import router as conversation_router, sessions_router
from backend.app.api.documents import build_document_router
from backend.app.api.routes.assistant import router as assistant_router
from backend.app.api.routes.memories import router as memories_router
from backend.app.api.routes.reminders import router as reminders_router
from backend.app.api.routes.tasks import router as tasks_router
from backend.app.api.routes.wake_word import router as wake_word_router
from backend.app.api.runtime import TaskRuntime

logger = logging.getLogger(__name__)
ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
ALLOWED_BROWSER_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:5175",
}
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_DOCUMENT_REQUEST_BODY_BYTES = 21 * 1024 * 1024


class LocalPrototypeSecurityMiddleware:
    """Keep this unauthenticated prototype bound to loopback/local browser origins."""

    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    async def _reject(send, status_code: int, code: str, message: str) -> None:
        import json

        body = json.dumps({"detail": {"code": code, "message": message}}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _host_is_allowed(host_header: bytes | None) -> bool:
        if not host_header:
            return False
        host = host_header.decode("latin-1").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host in ALLOWED_LOCAL_HOSTS

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        body_limit = (
            MAX_DOCUMENT_REQUEST_BODY_BYTES
            if scope.get("path") == "/api/documents/upload"
            else self.max_body_bytes
        )
        client = scope.get("client")
        remote_host = client[0] if client else None
        if remote_host is None:
            await self._reject(send, 403, "local_only", "This local prototype accepts loopback connections only")
            return
        try:
            if not ipaddress.ip_address(remote_host).is_loopback:
                await self._reject(send, 403, "local_only", "This local prototype accepts loopback connections only")
                return
        except ValueError:
            # Starlette's in-process test clients use these symbolic peers; real
            # network servers must provide a parseable loopback IP address.
            if remote_host not in {"testclient", "testserver"}:
                await self._reject(send, 403, "local_only", "This local prototype accepts loopback connections only")
                return

        if not self._host_is_allowed(headers.get(b"host")):
            await self._reject(send, 400, "invalid_host", "Host header is not allowed for this local service")
            return

        origin = headers.get(b"origin")
        if scope.get("method", "GET").upper() in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            if origin.decode("latin-1") not in ALLOWED_BROWSER_ORIGINS:
                await self._reject(send, 403, "origin_not_allowed", "Browser origin is not allowed")
                return

        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > body_limit:
                    await self._reject(
                        send, 413, "request_too_large",
                        f"Request body exceeds the configured {body_limit // (1024 * 1024)} MiB limit",
                    )
                    return
            except ValueError:
                await self._reject(send, 400, "invalid_content_length", "Content-Length header is invalid")
                return

        # Buffer a bounded body so chunked requests cannot bypass the content-length check.
        chunks: list[bytes] = []
        body_size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            body_size += len(chunk)
            if body_size > body_limit:
                await self._reject(
                    send, 413, "request_too_large",
                    f"Request body exceeds the configured {body_limit // (1024 * 1024)} MiB limit",
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay_body():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_body, send)


def create_app(runtime: TaskRuntime | None = None) -> FastAPI:
    selected_runtime = runtime or TaskRuntime()
    if runtime is None:
        selected_runtime.register_document_tools()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            selected_runtime.startup()
        except Exception:
            selected_runtime.shutdown()
            raise
        try:
            yield
        finally:
            selected_runtime.shutdown()

    application = FastAPI(title="UNBOUND AI API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_BROWSER_ORIGINS),
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    application.add_middleware(LocalPrototypeSecurityMiddleware)
    application.state.runtime = selected_runtime
    application.state.document_store = selected_runtime.document_store
    application.state.document_searcher = selected_runtime.document_searcher
    application.state.context_intelligence = selected_runtime.context_intelligence
    application.include_router(health_router, prefix="/api")
    application.include_router(conversation_router, prefix="/api")
    application.include_router(sessions_router, prefix="/api")
    application.include_router(assistant_router, prefix="/api")
    application.include_router(memories_router, prefix="/api")
    application.include_router(reminders_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    application.include_router(wake_word_router, prefix="/api")
    application.include_router(
        build_document_router(
            selected_runtime.document_store,
            selected_runtime.document_searcher,
            selected_runtime.context_intelligence,
        )
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request", "message": "Request data is invalid"}})

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Exception messages/tracebacks can contain prompts, tool output, or paths.
        logger.error("Unhandled API error: exception_class=%s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": {"code": "internal_error", "message": "An unexpected internal error occurred"}})

    return application


app = create_app()
