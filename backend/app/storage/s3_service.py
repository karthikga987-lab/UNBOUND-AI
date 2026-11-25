"""Small private S3 object service scoped to UNBOUND's configured prefix.

This module is optional and is not exposed as an LLM tool or unauthenticated
upload route. Callers must explicitly confirm uploads. Credentials are loaded
by boto3's normal profile/configuration chain and are never returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, BinaryIO


DEFAULT_BUCKET = "unbound-ai-documents-2026-karthik"
DEFAULT_REGION = "ap-southeast-2"
DEFAULT_PROFILE = "unbound-ai-local"
ALLOWED_PREFIX = "unbound/"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_KEY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class S3StorageError(RuntimeError):
    """Sanitized storage error safe for callers and UI messages."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class S3ObjectInfo:
    key: str
    size_bytes: int
    last_modified: Any | None = None


@dataclass(frozen=True)
class S3UploadReceipt:
    key: str
    size_bytes: int
    sha256: str
    verified: bool


class S3DocumentService:
    """Private object operations restricted to one bucket prefix and size cap."""

    def __init__(
        self,
        *,
        bucket: str = DEFAULT_BUCKET,
        region: str = DEFAULT_REGION,
        profile: str = DEFAULT_PROFILE,
        max_bytes: int = DEFAULT_MAX_BYTES,
        client: Any | None = None,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("bucket is required")
        self.bucket = bucket
        self.region = region
        self.profile = profile
        self.max_bytes = max_bytes
        if client is None:
            try:
                import boto3
                from botocore.config import Config
                from botocore.exceptions import BotoCoreError, ProfileNotFound

                try:
                    session = boto3.Session(profile_name=profile, region_name=region)
                    client = session.client(
                        "s3",
                        region_name=region,
                        config=Config(
                            connect_timeout=5,
                            read_timeout=30,
                            retries={"total_max_attempts": 3, "mode": "standard"},
                        ),
                    )
                except (BotoCoreError, ProfileNotFound) as exc:
                    raise S3StorageError("credentials_unavailable", "AWS profile credentials are unavailable") from exc
            except ImportError as exc:
                raise S3StorageError("dependency_unavailable", "Optional S3 support requires boto3") from exc
        self._client = client

    @staticmethod
    def object_key(name: str) -> str:
        """Return a normalized key under unbound/; reject traversal and unsafe names."""
        if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
            raise S3StorageError("invalid_object_key", "Object name is invalid")
        candidate = name[len(ALLOWED_PREFIX):] if name.startswith(ALLOWED_PREFIX) else name
        parts = candidate.split("/")
        if not parts or any(part in {"", ".", ".."} or not _KEY_PART.fullmatch(part) for part in parts):
            raise S3StorageError("invalid_object_key", "Object name must use safe path segments")
        return ALLOWED_PREFIX + "/".join(parts)

    def upload_bytes(
        self,
        name: str,
        content: bytes,
        *,
        explicitly_approved: bool = False,
        content_type: str = "application/octet-stream",
    ) -> S3UploadReceipt:
        """Upload one explicitly approved byte payload and verify it with HEAD.

        No ACL is set; objects remain private under the bucket's existing policy.
        """
        if explicitly_approved is not True:
            raise S3StorageError("approval_required", "Explicit user confirmation is required before upload")
        if not isinstance(content, bytes):
            raise S3StorageError("invalid_upload", "Upload content must be bytes")
        if len(content) == 0:
            raise S3StorageError("empty_upload", "Empty files cannot be uploaded")
        if len(content) > self.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "File exceeds the configured S3 upload limit")
        if not isinstance(content_type, str) or len(content_type) > 200 or "\r" in content_type or "\n" in content_type:
            raise S3StorageError("invalid_content_type", "Content type is invalid")
        key = self.object_key(name)
        digest = sha256(content).hexdigest()
        try:
            # The service caps objects at 20 MiB, so a single conditional PutObject
            # is sufficient and prevents overwriting an existing key.
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                Metadata={"sha256": digest},
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._safe_aws_error(exc, "upload_failed", "S3 upload or verification failed") from exc
        metadata = head.get("Metadata") or {}
        if head.get("ContentLength") != len(content) or metadata.get("sha256") != digest:
            raise S3StorageError("upload_verification_failed", "S3 object metadata did not match the uploaded content")
        return S3UploadReceipt(key=key, size_bytes=len(content), sha256=digest, verified=True)

    def list_objects(self, *, limit: int = 100) -> list[S3ObjectInfo]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 1000):
            raise S3StorageError("invalid_limit", "List limit must be between 1 and 1000")
        try:
            response = self._client.list_objects_v2(Bucket=self.bucket, Prefix=ALLOWED_PREFIX, MaxKeys=limit)
        except Exception as exc:
            raise self._safe_aws_error(exc, "list_failed", "S3 object listing failed") from exc
        objects = []
        for item in response.get("Contents", []):
            key = item.get("Key")
            if isinstance(key, str) and key.startswith(ALLOWED_PREFIX):
                objects.append(S3ObjectInfo(key=key, size_bytes=int(item.get("Size", 0)), last_modified=item.get("LastModified")))
        return objects[:limit]

    def verify_existing_object(self, name: str, *, size_bytes: int, sha256_digest: str) -> bool:
        """Verify an already-present content-addressed object for idempotent sync."""
        key = self.object_key(name)
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._safe_aws_error(exc, "verification_failed", "Existing S3 object could not be verified") from exc
        metadata = head.get("Metadata") or {}
        if head.get("ContentLength") != size_bytes or metadata.get("sha256") != sha256_digest:
            raise S3StorageError("object_integrity_mismatch", "Existing S3 object did not match the expected checksum")
        return True

    def download_to_fileobj(self, name: str, destination: BinaryIO) -> S3ObjectInfo:
        """Stream one bounded object to a caller-owned destination file object."""
        if not callable(getattr(destination, "write", None)):
            raise S3StorageError("invalid_download_destination", "Download destination must be a writable file object")
        key = self.object_key(name)
        body = None
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
            expected_size = int(head.get("ContentLength", -1))
            if expected_size < 0 or expected_size > self.max_bytes:
                raise S3StorageError("file_size_limit_exceeded", "S3 object exceeds the configured download limit")
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            total = 0
            while True:
                chunk = body.read(min(64 * 1024, self.max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise S3StorageError("file_size_limit_exceeded", "S3 object exceeds the configured download limit")
                destination.write(chunk)
            if total != expected_size:
                raise S3StorageError("download_incomplete", "Downloaded S3 object size did not match its metadata")
            return S3ObjectInfo(key=key, size_bytes=total, last_modified=head.get("LastModified"))
        except S3StorageError:
            raise
        except Exception as exc:
            raise self._safe_aws_error(exc, "download_failed", "S3 object download failed") from exc
        finally:
            if body is not None:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

    @staticmethod
    def _safe_aws_error(exc: Exception, fallback_code: str, fallback_message: str) -> S3StorageError:
        """Map SDK errors to sanitized categories; never echo SDK messages/credentials."""
        code = ""
        try:
            code = str(exc.response.get("Error", {}).get("Code", ""))
        except Exception:
            pass
        known = {
            "AccessDenied": ("permission_denied", "AWS denied this S3 operation"),
            "AllAccessDisabled": ("permission_denied", "AWS denied this S3 operation"),
            "NoSuchBucket": ("bucket_unavailable", "Configured S3 bucket is unavailable"),
            "NoSuchKey": ("object_not_found", "S3 object was not found"),
            "InvalidAccessKeyId": ("credentials_unavailable", "AWS profile credentials are invalid"),
            "ExpiredToken": ("credentials_expired", "AWS profile credentials have expired"),
            "PreconditionFailed": ("object_exists", "An object already exists at this key"),
        }
        safe_code, safe_message = known.get(code, (fallback_code, fallback_message))
        return S3StorageError(safe_code, safe_message)
