# Optional private S3 storage

The optional `S3DocumentService` reads AWS credentials through the standard
AWS shared profile/configuration chain. It does not expose credentials, create
public URLs, set an object ACL, or provide a general delete operation.

Defaults are bucket `unbound-ai-documents-2026-karthik`, region `ap-south-1`,
profile `unbound-ai-local`, and the `unbound/` object prefix. Uploads require an
explicit `explicitly_approved=True` call, are limited to 20 MiB by default, and
are checked with `head_object` for exact size and SHA-256 metadata. Downloads
stream to a caller-owned file object and enforce the same size limit. Listing
is restricted to `unbound/`.

Install the optional dependency only when using S3:

```powershell
.\.venv-wake\Scripts\python.exe -m pip install -r backend\requirements-s3.txt
```

Run the synthetic live round trip from the repository root:

```powershell
.\.venv-wake\Scripts\python.exe scripts\s3_roundtrip.py --confirm-upload --cleanup-approved
```

The first flag authorizes one generated synthetic upload. The second authorizes
deletion of only that run's unique test object after it is downloaded and
hash-checked. Without `--cleanup-approved`, the object remains in the bucket.
This smoke script is not exposed to the model or frontend. Document S3 APIs are
not wired into the assistant/task ToolRegistry.

## Manual task database backup

Task history uses a separate SQLite database at
`%LOCALAPPDATA%\UNBOUND_AI\tasks.sqlite3` by default. Backups are a manual
operation; this is not background or bidirectional synchronization. The
snapshot uses SQLite's online backup API, so a running backend may continue to
write in WAL mode. The compressed archive is bounded to the service's 20 MiB
limit and contains only the task database and a versioned integrity manifest.
It does not include conversations, documents, reminders, browser profiles, or
the browser replay encryption key.

Upload requires an explicit flag and creates a new, non-overwriting object
under `unbound/backups/`. The request asks S3 for SSE-S3 encryption and HEAD
verification checks size and SHA-256 metadata. Restore verifies the manifest,
database checksum, and SQLite integrity and only creates a new destination;
it refuses to target the active database or overwrite an existing path.

```powershell
# Upload a consistent snapshot; review the disclosure before confirming.
.\.venv-wake\Scripts\python.exe scripts\s3_task_backup.py --upload --confirm-upload

# Restore an existing object into a separate, previously unused path.
.\.venv-wake\Scripts\python.exe scripts\s3_task_backup.py --restore-key unbound/backups/OBJECT.zip --destination "$env:TEMP\unbound-task-recovery.sqlite3" --confirm-restore
```

The tool uses the configured `unbound-ai-local` profile, bucket, region, and
existing bounded `unbound/` service. A live synthetic SQLite upload, HEAD
verification, download, restore to a temporary separate path, checksum
comparison, and SQLite integrity check succeeded. The S3 object reported
`AES256` server-side encryption. The object key is
`unbound/backups/synthetic-verification-47caea65-1f7b-40d8-be7e-9e270a64de76.zip`
and was deliberately retained; no remote deletion was authorized. Prefix
listing succeeded. `s3:GetBucketLocation` and `s3:GetBucketVersioning` were
denied, so bucket-region metadata and versioning configuration were not
independently queried (uploads/downloads through the configured ap-south-1
client did succeed). No bucket is made public. There is no S3 dashboard,
background retry loop, or automatic sync.
