"""Explicit manual backup/restore for the dedicated SQLite task database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.orchestrator.task_manager import default_task_database_path
from backend.app.storage.s3_service import S3DocumentService, S3StorageError
from backend.app.storage.sqlite_backup import SQLiteBackupService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--upload", action="store_true", help="create and upload a consistent task database snapshot")
    actions.add_argument("--restore-key", help="restore an existing backup key to a new local file")
    parser.add_argument("--database", type=Path, default=default_task_database_path())
    parser.add_argument("--destination", type=Path, help="new restore destination; must not exist")
    parser.add_argument("--active-database", type=Path, default=default_task_database_path())
    parser.add_argument("--confirm-upload", action="store_true", help="authorize uploading task history to the private S3 prefix")
    parser.add_argument("--confirm-restore", action="store_true", help="authorize restoring to a separate local file")
    args = parser.parse_args()

    try:
        storage = S3DocumentService()
        backups = SQLiteBackupService(storage)
        if args.upload:
            if not args.confirm_upload:
                parser.error("--upload requires --confirm-upload")
            key = f"task-database-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4()}.zip"
            receipt = backups.upload_database(args.database, key, explicitly_approved=True)
            print("snapshot_uploaded=true")
            print(f"object_key={receipt.key}")
            print(f"snapshot_bytes={receipt.database_size_bytes}")
            print(f"snapshot_sha256={receipt.database_sha256}")
            print(f"object_sha256={receipt.object_sha256}")
            print(f"verified={str(receipt.verified).lower()}")
            return 0
        if not args.confirm_restore:
            parser.error("--restore-key requires --confirm-restore")
        if args.destination is None:
            parser.error("--restore-key requires --destination pointing to a new file")
        result = backups.restore_to_new_path(
            args.restore_key,
            args.destination,
            active_database_path=args.active_database,
            explicitly_approved=True,
        )
        print("restore_verified=true")
        print(f"destination={result['destination']}")
        print(f"database_sha256={result['database_sha256']}")
        print(f"integrity_check={result['integrity_check']}")
        return 0
    except S3StorageError as exc:
        print(f"s3_error={exc.code}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
