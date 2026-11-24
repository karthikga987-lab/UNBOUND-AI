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
This smoke script is not exposed to the model or frontend. S3 upload, listing,
and download are not yet wired into the React UI or assistant/task ToolRegistry.
