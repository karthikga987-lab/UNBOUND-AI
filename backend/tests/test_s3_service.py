from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pytest

from backend.app.storage.s3_service import S3DocumentService, S3StorageError


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **kwargs):
        data = kwargs["Body"]
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append(("upload", bucket, key, kwargs))
        if (bucket, key) in self.objects:
            class AlreadyExists(Exception):
                response = {"Error": {"Code": "PreconditionFailed"}}

            raise AlreadyExists("key already exists")
        self.objects[(bucket, key)] = (data, kwargs["Metadata"])

    def head_object(self, *, Bucket, Key):
        data, metadata = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "Metadata": metadata}

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
        self.calls.append(("list", Bucket, Prefix, MaxKeys))
        values = [
            {"Key": key, "Size": len(data)}
            for (bucket, key), (data, _metadata) in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": values[:MaxKeys], "KeyCount": min(len(values), MaxKeys)}

    def get_object(self, *, Bucket, Key):
        data, _ = self.objects[(Bucket, Key)]
        return {"Body": BytesIO(data)}


def test_upload_requires_explicit_confirmation_and_verifies_head_metadata():
    client = FakeS3()
    service = S3DocumentService(client=client)
    content = b"Synthetic demo object only."

    with pytest.raises(S3StorageError, match="Explicit user confirmation") as exc:
        service.upload_bytes("demo/sample.txt", content)
    assert exc.value.code == "approval_required"
    assert client.calls == []

    receipt = service.upload_bytes("demo/sample.txt", content, explicitly_approved=True, content_type="text/plain")
    assert receipt.key == "unbound/demo/sample.txt"
    assert receipt.size_bytes == len(content)
    assert receipt.sha256 == sha256(content).hexdigest()
    assert receipt.verified is True
    assert client.calls[0][3]["Metadata"] == {"sha256": receipt.sha256}
    assert client.calls[0][3]["IfNoneMatch"] == "*"
    assert "ACL" not in client.calls[0][3]
    assert client.calls[0][3]["ServerSideEncryption"] == "AES256"


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape", "unbound/../escape", "\\server\\share", "bad\\name", "/absolute", "a//b", "a/./b", "x\nheader"])
def test_object_keys_cannot_escape_or_leave_the_allowed_prefix(name):
    with pytest.raises(S3StorageError) as exc:
        S3DocumentService.object_key(name)
    assert exc.value.code == "invalid_object_key"


def test_upload_size_empty_and_key_validation_fail_before_aws_call():
    client = FakeS3()
    service = S3DocumentService(client=client, max_bytes=4)
    cases = [
        ("x.txt", b"", "empty_upload"),
        ("x.txt", b"12345", "file_size_limit_exceeded"),
        ("../x.txt", b"1", "invalid_object_key"),
    ]
    for name, content, code in cases:
        with pytest.raises(S3StorageError) as exc:
            service.upload_bytes(name, content, explicitly_approved=True)
        assert exc.value.code == code
    assert client.calls == []


def test_list_and_download_are_limited_to_unbound_prefix_and_stream_checked_size():
    client = FakeS3()
    service = S3DocumentService(client=client, max_bytes=64)
    payload = b"synthetic object payload"
    service.upload_bytes("roundtrip.txt", payload, explicitly_approved=True)

    items = service.list_objects(limit=10)
    assert [item.key for item in items] == ["unbound/roundtrip.txt"]
    assert client.calls[-1] == ("list", service.bucket, "unbound/", 10)
    destination = BytesIO()
    info = service.download_to_fileobj("unbound/roundtrip.txt", destination)
    assert destination.getvalue() == payload
    assert info.size_bytes == len(payload)


def test_streaming_download_rejects_unexpected_oversized_response():
    class Oversized(FakeS3):
        def head_object(self, *, Bucket, Key):
            return {"ContentLength": 2, "Metadata": {}}

        def get_object(self, *, Bucket, Key):
            return {"Body": BytesIO(b"123456")}

    service = S3DocumentService(client=Oversized(), max_bytes=4)
    with pytest.raises(S3StorageError) as exc:
        service.download_to_fileobj("x.txt", BytesIO())
    assert exc.value.code == "file_size_limit_exceeded"


def test_head_verification_failure_is_not_reported_as_success():
    class BadHead(FakeS3):
        def head_object(self, *, Bucket, Key):
            return {"ContentLength": 1, "Metadata": {"sha256": "wrong"}}

    with pytest.raises(S3StorageError) as exc:
        S3DocumentService(client=BadHead()).upload_bytes("x.txt", b"synthetic", explicitly_approved=True)
    assert exc.value.code == "upload_verification_failed"


def test_upload_does_not_overwrite_an_existing_object():
    client = FakeS3()
    service = S3DocumentService(client=client)
    service.upload_bytes("same.txt", b"first", explicitly_approved=True)
    with pytest.raises(S3StorageError) as exc:
        service.upload_bytes("same.txt", b"second", explicitly_approved=True)
    assert exc.value.code == "object_exists"
    assert client.objects[(service.bucket, "unbound/same.txt")][0] == b"first"


def test_aws_error_messages_are_sanitized():
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "contains-secret-marker"}}

    safe = S3DocumentService._safe_aws_error(Denied("contains-secret-marker"), "upload_failed", "upload failed")
    assert safe.code == "permission_denied"
    assert "secret" not in str(safe)
