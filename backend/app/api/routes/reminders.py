from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import UUID4

from backend.app.reminders.models import ReminderCreateRequest, ReminderRecord, ReminderUpdateRequest
from backend.app.reminders.store import ReminderNotFoundError, ReminderStoreError

router = APIRouter(prefix="/reminders", tags=["reminders"])


def _service(request: Request):
    return request.app.state.runtime.reminder_service


@router.post("", response_model=ReminderRecord, status_code=201)
def create_reminder(body: ReminderCreateRequest, request: Request) -> ReminderRecord:
    try:
        return _service(request).create(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_reminder_time", "message": str(exc)}) from exc
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.get("", response_model=list[ReminderRecord])
def list_reminders(request: Request) -> list[ReminderRecord]:
    try:
        return _service(request).list()
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.get("/{reminder_id}", response_model=ReminderRecord)
def get_reminder(reminder_id: UUID4, request: Request) -> ReminderRecord:
    try:
        return _service(request).get(str(reminder_id))
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "reminder_not_found", "message": "Reminder was not found"}) from exc
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.post("/{reminder_id}/confirm", response_model=ReminderRecord)
def confirm_reminder(reminder_id: UUID4, request: Request) -> ReminderRecord:
    try:
        return _service(request).confirm(str(reminder_id))
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "reminder_not_found", "message": "Reminder was not found"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "reminder_state_conflict", "message": str(exc)}) from exc
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.patch("/{reminder_id}", response_model=ReminderRecord)
def update_reminder(reminder_id: UUID4, body: ReminderUpdateRequest, request: Request) -> ReminderRecord:
    try:
        return _service(request).update(str(reminder_id), body)
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "reminder_not_found", "message": "Reminder was not found"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_reminder_time", "message": str(exc)}) from exc
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.post("/{reminder_id}/complete", response_model=ReminderRecord)
def complete_reminder(reminder_id: UUID4, request: Request) -> ReminderRecord:
    try:
        return _service(request).complete(str(reminder_id))
    except ReminderNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "reminder_not_found", "message": "Reminder was not found"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "reminder_state_conflict", "message": str(exc)}) from exc
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@router.delete("/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: UUID4, request: Request) -> Response:
    try:
        if not _service(request).delete(str(reminder_id)):
            raise HTTPException(status_code=404, detail={"code": "reminder_not_found", "message": "Reminder was not found"})
        return Response(status_code=204)
    except ReminderStoreError as exc:
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc
