from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from backend.app.storage.s3_service import S3StorageError
from backend.app.storage.sqlite_backup import SQLiteBackupService


router = APIRouter(prefix="/storage/s3", tags=["storage"])


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_sync: StrictBool


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_key: str
    confirm_restore: StrictBool


def _require_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or origin not in request.app.state.allowed_browser_origins:
        raise HTTPException(status_code=403, detail={"code": "origin_not_allowed", "message": "A trusted local frontend origin is required"})


@router.get("/status")
def get_sync_status(request: Request) -> dict[str, object]:
    return request.app.state.runtime.s3_sync_status()


@router.post("/sync")
def trigger_sync(body: SyncRequest, request: Request) -> dict[str, object]:
    _require_trusted_origin(request)
    if body.confirm_sync is not True:
        raise HTTPException(status_code=400, detail={"code": "confirmation_required", "message": "Confirm this S3 data transfer before syncing"})
    service = request.app.state.runtime.s3_sync_service
    if service is None:
        raise HTTPException(status_code=503, detail={"code": "sync_unavailable", "message": "S3 synchronization is disabled or unavailable"})
    try:
        return service.run_once(force=True)
    except S3StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/backups")
def list_backups(request: Request, limit: int = 100) -> list[dict[str, object]]:
    service = request.app.state.runtime.s3_sync_service
    if service is None:
        return []
    try:
        items = service.storage.list_objects(limit=max(1, min(limit, 1000)))
        sync_snapshot_prefix = f"{service.sync_prefix}/snapshot-"
        return [
            {"key": item.key, "size_bytes": item.size_bytes,
             "last_modified": item.last_modified.isoformat() if item.last_modified else None,
             "kind": "task_database_backup" if item.key.startswith("unbound/backups/") else "application_snapshot"}
            for item in items if item.key.startswith("unbound/backups/") or item.key.startswith(sync_snapshot_prefix)
        ][:limit]
    except S3StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/restore")
def restore_task_backup(body: RestoreRequest, request: Request) -> dict[str, object]:
    _require_trusted_origin(request)
    if body.confirm_restore is not True:
        raise HTTPException(status_code=400, detail={"code": "confirmation_required", "message": "Confirm restore into a new recovery file"})
    runtime = request.app.state.runtime
    service = runtime.s3_sync_service
    if service is None:
        raise HTTPException(status_code=503, detail={"code": "sync_unavailable", "message": "S3 storage is disabled or unavailable"})
    sync_snapshot_prefix = f"{service.sync_prefix}/"
    if not body.object_key.startswith("unbound/backups/") and not re.fullmatch(
        rf"{re.escape(sync_snapshot_prefix)}snapshot-[a-f0-9]{{64}}\.zip", body.object_key
    ):
        raise HTTPException(status_code=400, detail={"code": "invalid_backup_key", "message": "Object is not a supported task backup or application snapshot"})
    root = Path(service.state.path).parent / "recovery"
    destination = root / f"tasks-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}.sqlite3"
    repository_path = getattr(runtime.manager.repository, "database_path", None)
    if repository_path is None:
        raise HTTPException(status_code=503, detail={"code": "restore_unavailable", "message": "Active task database path is unavailable"})
    try:
        if body.object_key.startswith(sync_snapshot_prefix):
            destination = destination.with_suffix("")
            return service.restore_snapshot_to_new_directory(
                body.object_key, destination, explicitly_approved=True,
            )
        return SQLiteBackupService(service.storage).restore_to_new_path(
            body.object_key, destination, active_database_path=repository_path, explicitly_approved=True,
        )
    except S3StorageError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
