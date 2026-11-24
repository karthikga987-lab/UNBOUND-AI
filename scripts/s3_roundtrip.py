r"""Opt-in AWS smoke test using only one synthetic object under unbound/.

Example:
  .\.venv-wake\Scripts\python.exe scripts\s3_roundtrip.py --confirm-upload --cleanup-approved

The cleanup flag deletes only the unique test key created by this invocation.
Without it, the synthetic object is deliberately left in the bucket.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryFile
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.storage.s3_service import DEFAULT_BUCKET, DEFAULT_PROFILE, DEFAULT_REGION, S3DocumentService


def run_roundtrip(*, cleanup_approved: bool) -> None:
    session = boto3.Session(profile_name=DEFAULT_PROFILE, region_name=DEFAULT_REGION)
    client = session.client("s3", region_name=DEFAULT_REGION)
    service = S3DocumentService(client=client)
    key = f"demo/smoke-{uuid4()}.txt"
    content = b"UNBOUND S3 smoke test. Synthetic content only. No personal data.\n"
    digest = sha256(content).hexdigest()
    receipt = service.upload_bytes(key, content, explicitly_approved=True, content_type="text/plain; charset=utf-8")
    with TemporaryFile(mode="w+b") as downloaded:
        info = service.download_to_fileobj(receipt.key, downloaded)
        downloaded.seek(0)
        downloaded_digest = sha256()
        while chunk := downloaded.read(64 * 1024):
            downloaded_digest.update(chunk)
    if downloaded_digest.hexdigest() != digest or info.size_bytes != len(content):
        raise RuntimeError("Downloaded bytes did not match the synthetic upload SHA-256")
    print("upload_verified=true")
    print("download_sha256_match=true")
    print(f"object_size_bytes={receipt.size_bytes}")
    print("object_key_under_unbound_prefix=true")
    if cleanup_approved:
        # This is intentionally not exposed by S3DocumentService as a general delete API.
        client.delete_object(Bucket=DEFAULT_BUCKET, Key=receipt.key)
        try:
            client.head_object(Bucket=DEFAULT_BUCKET, Key=receipt.key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise RuntimeError("Could not verify removal of the synthetic object") from None
            print("synthetic_object_cleanup=deleted")
        else:
            raise RuntimeError("The unique synthetic object still exists after requested cleanup")
    else:
        print("synthetic_object_cleanup=not_requested")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-upload", action="store_true", help="explicitly authorize this synthetic upload")
    parser.add_argument("--cleanup-approved", action="store_true", help="authorize deletion of only this run's unique synthetic object")
    args = parser.parse_args()
    if not args.confirm_upload:
        parser.error("pass --confirm-upload to authorize the synthetic S3 write")
    try:
        run_roundtrip(cleanup_approved=args.cleanup_approved)
        return 0
    except S3StorageError as exc:
        print(f"s3_error={exc.code}: {exc}", file=sys.stderr)
        return 1
    except ClientError as exc:
        safe = S3DocumentService._safe_aws_error(exc, "aws_operation_failed", "AWS S3 operation failed")
        print(f"s3_error={safe.code}: {safe}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
