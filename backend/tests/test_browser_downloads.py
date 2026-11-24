from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from backend.app.browser import (
    BrowserController,
    BrowserDownloadResult,
    BrowserDownloadStore,
    BrowserVerifier,
)


class FakeDownload:
    def __init__(self, suggested_filename="report.pdf", *, fail=None, fail_after_write=False):
        self.suggested_filename = suggested_filename
        self.failure_value = fail
        self.fail_after_write = fail_after_write
        self.saved_to = None

    def failure(self):
        return self.failure_value

    def save_as(self, path):
        self.saved_to = Path(path)
        self.saved_to.write_bytes(b"mock downloaded content")
        if self.fail_after_write:
            raise RuntimeError("PRIVATE-DIAGNOSTIC https://private.example/?token=SECRET")


class FakeExpectDownload:
    def __init__(self, page, download):
        self.page = page
        self.value = download

    def __enter__(self):
        self.page.waiting_for_download = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.page.waiting_for_download = False
        return False


class FakeLocator:
    def __init__(self, page):
        self.page = page

    def click(self, **kwargs):
        assert self.page.waiting_for_download is True
        self.page.clicked = True


class FakePage:
    url = "https://private.example/download?token=SECRET"

    def __init__(self, download=None, *, expect_error=False):
        self.download = download or FakeDownload()
        self.expect_error = expect_error
        self.waiting_for_download = False
        self.clicked = False
        self.expect_timeout = None

    def expect_download(self, *, timeout):
        self.expect_timeout = timeout
        if self.expect_error:
            raise TimeoutError("PRIVATE-DIAGNOSTIC https://private.example/?token=SECRET")
        return FakeExpectDownload(self, self.download)

    def get_by_role(self, role, **kwargs):
        return FakeLocator(self)


@pytest.fixture
def download_store(tmp_path):
    return BrowserDownloadStore(tmp_path / "downloads")


def assert_result_path_is_safe(result, store):
    assert result.success is True
    assert result.file_path == result.file_name
    assert "/" not in result.file_path and "\\" not in result.file_path
    path = store.resolve_result_path(result)
    assert path.parent == store.directory.resolve()
    assert UUID(path.stem).hex == path.stem
    return path


def test_controller_captures_and_saves_playwright_download(download_store):
    controller = BrowserController(timeout_ms=456, download_store=download_store)
    page = FakePage(FakeDownload("quarterly-report.pdf"))
    controller._page = page

    result = controller.download({"type": "role", "role": "link", "name": "Download"})

    path = assert_result_path_is_safe(result, download_store)
    assert page.clicked is True
    assert page.expect_timeout == 456
    assert result.size_bytes == len(b"mock downloaded content")
    assert path.read_bytes() == b"mock downloaded content"
    assert result.file_name.endswith(".pdf")


def test_server_generated_names_confine_traversal_and_reserved_suggestions(download_store):
    suggestions = [
        "../../escape.txt",
        r"C:\\..\\CON.txt",
        "/etc/passwd",
        "CON.txt",
        ".. ",
        "bad\x00name.PDF",
    ]
    results = [download_store.save(FakeDownload(name)) for name in suggestions]

    paths = [assert_result_path_is_safe(result, download_store) for result in results]
    assert len({path.name for path in paths}) == len(suggestions)
    assert not (download_store.directory.parent / "escape.txt").exists()
    assert all(path.name.upper() != "CON.TXT" for path in paths)
    assert paths[-1].suffix == ".pdf"


def test_duplicate_suggested_names_receive_unique_server_names(download_store):
    first = download_store.save(FakeDownload("same.pdf"))
    second = download_store.save(FakeDownload("same.pdf"))

    assert first.success and second.success
    assert first.file_name != second.file_name
    assert_result_path_is_safe(first, download_store)
    assert_result_path_is_safe(second, download_store)


def test_failed_download_and_partial_save_are_cleaned_safely(download_store):
    incomplete = download_store.save(FakeDownload("secret.pdf", fail="RAW-SECRET-DIAGNOSTIC"))
    partial = download_store.save(FakeDownload("secret.pdf", fail_after_write=True))

    assert incomplete.success is False
    assert partial.success is False
    assert "RAW-SECRET-DIAGNOSTIC" not in repr(incomplete)
    assert "PRIVATE-DIAGNOSTIC" not in repr(partial)
    assert "SECRET" not in repr(partial)
    assert list(download_store.directory.glob("*.part")) == []
    assert list(download_store.directory.glob("*.pdf")) == []


def test_controller_download_timeout_returns_safe_result_without_diagnostics(download_store):
    controller = BrowserController(download_store=download_store)
    controller._page = FakePage(expect_error=True)

    result = controller.download({"type": "role", "role": "link", "name": "Download"})

    assert result.success is False
    assert result.error == "No completed browser download was captured"
    assert "SECRET" not in repr(result)
    assert list(download_store.directory.glob("*.pdf")) == []


def test_store_failure_result_contains_no_absolute_paths_or_download_urls(download_store):
    result = download_store.save(FakeDownload("report.pdf", fail_after_write=True))
    rendered = repr(result)

    assert result.success is False
    assert str(download_store.directory) not in rendered
    assert "https://" not in rendered
    assert "SECRET" not in rendered


@pytest.mark.parametrize("unsafe_path", [
    "../outside.pdf",
    r"..\outside.pdf",
    r"C:\\outside.pdf",
    "/outside.pdf",
])
def test_verifier_rejects_paths_outside_download_root(download_store, unsafe_path):
    controller = BrowserController()
    verifier = BrowserVerifier(controller, download_directory=download_store.directory)
    untrusted = BrowserDownloadResult(
        success=True, file_name=unsafe_path, file_path=unsafe_path, size_bytes=20
    )

    result = verifier.verify_download(untrusted)

    assert result.success is False
    assert result.error == "Download path is invalid or outside the download directory"
    assert unsafe_path not in repr(result)


def test_verifier_checks_persisted_file_and_recorded_size(download_store):
    saved = download_store.save(FakeDownload("report.pdf"))
    verifier = BrowserVerifier(BrowserController(), download_directory=download_store.directory)

    verified = verifier.verify_download(saved)
    wrong_size = verifier.verify_download(BrowserDownloadResult(
        success=True,
        file_name=saved.file_name,
        file_path=saved.file_path,
        size_bytes=saved.size_bytes + 1,
    ))

    assert verified.success is True
    assert verified.evidence["download"] == {"exists": True, "size_bytes": saved.size_bytes}
    assert wrong_size.success is False
    assert "size did not match" in wrong_size.error
    assert str(download_store.directory) not in repr(verified)


def test_verifier_rejects_missing_or_empty_download_file(download_store):
    download_store.directory.mkdir(parents=True)
    empty_name = "00000000000000000000000000000000.pdf"
    (download_store.directory / empty_name).write_bytes(b"")
    verifier = BrowserVerifier(BrowserController(), download_directory=download_store.directory)

    empty = verifier.verify_download(BrowserDownloadResult(
        success=True, file_name=empty_name, file_path=empty_name, size_bytes=0
    ))
    missing = verifier.verify_download(BrowserDownloadResult(
        success=True,
        file_name="11111111111111111111111111111111.pdf",
        file_path="11111111111111111111111111111111.pdf",
        size_bytes=12,
    ))

    assert empty.success is False
    assert empty.error == "Downloaded file is empty"
    assert missing.success is False
    assert missing.error == "Downloaded file is missing"
