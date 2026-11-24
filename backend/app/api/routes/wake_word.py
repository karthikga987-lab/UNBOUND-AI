from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from backend.app.wake_word.service import WakeWordUnavailable

router = APIRouter(prefix="/wake-word", tags=["wake-word"])


@router.get("/status")
def status(request: Request) -> dict[str, object]:
    return request.app.state.runtime.wake_word_service.status()


@router.post("/validate")
def validate(request: Request) -> dict[str, object]:
    """Validate key/model/runtime without creating an audio recorder."""
    try:
        return request.app.state.runtime.wake_word_service.validate()
    except WakeWordUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "wake_word_unavailable", "message": str(exc)}) from exc


@router.post("/start")
def start(request: Request) -> dict[str, object]:
    """Explicit user action starts local microphone capture."""
    try:
        return request.app.state.runtime.wake_word_service.start()
    except WakeWordUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "wake_word_unavailable", "message": str(exc)}) from exc


@router.post("/stop")
def stop(request: Request) -> dict[str, object]:
    return request.app.state.runtime.wake_word_service.stop()


@router.get("/events")
def event(request: Request, after_event_id: int = Query(default=0, ge=0)) -> dict[str, object]:
    service = request.app.state.runtime.wake_word_service
    return {"event": service.event_after(after_event_id), "status": service.status()}
