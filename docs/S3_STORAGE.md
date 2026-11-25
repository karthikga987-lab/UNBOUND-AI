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
client did succeed). No bucket is made public.

## Opt-in automatic application snapshots

Automatic synchronization is disabled by default. It uploads a content-addressed
snapshot containing the task/evidence database, the document metadata database,
and only the document files and extracted text referenced by records in the
controlled `DocumentStore`. SQLite databases are copied with SQLite's online
backup API; the active database and WAL files are never copied directly. The
document database copy rewrites machine-specific absolute paths to package-local
relative paths. Browser profiles, credentials, conversations, reminders,
temporary files, caches, logs, and arbitrary user files are excluded.

**The snapshot can contain private task requests, evidence excerpts, indexed
documents and extracted text.** Review that scope before enabling automatic
upload. The opt-in uploads an initial snapshot at backend startup and then at
the configured interval. Failed uploads remain pending in a separate local
SQLite sync-state database and retry with capped exponential backoff. Keys are
content-addressed; retries verify an existing object's size and SHA-256 rather
than overwriting it. Remote objects are never automatically deleted. Successful
uploads request SSE-S3 (`AES256`); this is AWS-side encryption, not client-side
encryption. Local task/document SQLite remains unencrypted at rest.

The backend default is a 64 MiB snapshot limit. Configuration:

```powershell
$env:UNBOUND_S3_SYNC_ENABLED = "1" # opt-in to background upload of the data scope above
$env:UNBOUND_S3_BUCKET = "unbound-ai-documents-2026-karthik"
$env:UNBOUND_S3_REGION = "ap-south-1"
$env:UNBOUND_S3_PROFILE = "unbound-ai-local"
$env:UNBOUND_S3_SYNC_INTERVAL_SECONDS = "3600" # allowed: 60..86400
$env:UNBOUND_S3_SYNC_PREFIX = "sync" # S3 key prefix below unbound/; default preserves unbound/sync/
$env:UNBOUND_S3_SYNC_MAX_BYTES = "67108864" # allowed: 1 MiB..512 MiB
$env:UNBOUND_S3_SYNC_RETRY_BASE_SECONDS = "30"
$env:UNBOUND_S3_SYNC_RETRY_MAX_SECONDS = "3600"
# Optional: $env:UNBOUND_S3_SYNC_STATE_DB = "$env:LOCALAPPDATA\UNBOUND_AI\s3_sync.sqlite3"
```

Start FastAPI after setting the variables. `GET /api/storage/s3/status` and
`GET /api/health` report persisted sync status. The React **S3 backup** panel
also provides a manual **Sync now** button with an action-time confirmation.
Mutating endpoints require a loopback request and a trusted configured browser
Origin. The API still has no user authentication: same-machine processes can
spoof Origin, so this remains a single-user local prototype and must not be
exposed to a network. The automatic startup sync is an explicit configuration
opt-in rather than an approval per individual object.

`GET /api/storage/s3/backups` lists task-database backups and application
snapshots. Restore verifies the content-addressed archive, manifest, every
member checksum/size, and both SQLite integrity checks before publishing a new
recovery directory. Older task-database backups restore to a new database file.
Neither workflow replaces active data or an existing destination. The
application snapshot recovery copy is not automatically wired into the live
runtime; it must be reviewed and selectively restored by an operator. No remote
deletes are available through the API.

S3 synchronization is best-effort and local operations continue when AWS is
unavailable. Check `status`, `pending_count`, `retry_count`, `next_retry_at`,
`last_error_code`, `last_success_at`, and the last content-addressed key. Status
contains safe error categories only, not AWS SDK messages or credentials.
