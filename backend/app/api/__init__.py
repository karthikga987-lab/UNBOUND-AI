"""FastAPI application entry point for UNBOUND AI backend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.context import ContextIntelligence
from backend.app.api.documents import build_document_router

STORAGE_DIR = Path("./document_storage")


def create_app(storage_dir: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="UNBOUND AI Backend",
        description="Document intelligence and context retrieval API",
        version="0.1.0",
    )

    resolved_storage = (storage_dir or STORAGE_DIR).resolve()
    store = DocumentStore(resolved_storage)
    searcher = DocumentSearcher(store)
    intelligence = ContextIntelligence(store, searcher)

    # Attach to app state for dependency injection
    app.state.document_store = store
    app.state.document_searcher = searcher
    app.state.context_intelligence = intelligence

    app.include_router(build_document_router(store, searcher, intelligence))

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
