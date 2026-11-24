"""Persistent, path-safe storage for Playwright download events."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from threading import RLock
from typing import Any
from uuid import uuid4


_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DOWNLOAD_LOCK = RLock()


@dataclass(frozen=True)
class BrowserDownloadResult:
    """Safe result metadata; contains no browser object, source URL, or absolute path."""

    success: bool
    file_name: str | None = None
    file_path: str | None = None
    size_bytes: int | None = None
    error: str | None = None

    @classmethod
    def failed(cls, message: str) -> "BrowserDownloadResult":
        return cls(success=False, error=message)


class BrowserDownloadStore:
    """Save completed Playwright downloads in a controlled generated-data folder."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(__file__).resolve().parents[2] / "generated_data" / "downloads"
        )

    def save(self, download: Any) -> BrowserDownloadResult:
        """Save via Playwright to a same-directory temporary file, then publish atomically."""
        temporary_path: Path | None = None
        try:
            root = self._prepare_root()
            extension = self._safe_extension(getattr(download, "suggested_filename", ""))
            failure = download.failure()
            if failure:
                return BrowserDownloadResult.failed("The browser download did not complete")

            with _DOWNLOAD_LOCK:
                file_name, final_path = self._new_destination(root, extension)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".download-", suffix=".part", dir=root
                )
                temporary_path = Path(temporary_name)
                os.close(descriptor)
                os.chmod(temporary_path, 0o600)
                download.save_as(str(temporary_path))
                if not temporary_path.is_file():
                    return BrowserDownloadResult.failed("The browser download was not saved")
                size_bytes = temporary_path.stat().st_size
                self._publish_without_overwrite(temporary_path, final_path, root)
                temporary_path = None
                return BrowserDownloadResult(
                    success=True,
                    file_name=file_name,
                    file_path=file_name,
                    size_bytes=size_bytes,
                )
        except Exception:
            # Playwright errors can contain URLs, headers, or page-provided text.
            return BrowserDownloadResult.failed("The browser download could not be saved safely")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def resolve_result_path(self, result: BrowserDownloadResult) -> Path:
        """Resolve a result path after rejecting absolute paths and traversal."""
        if not isinstance(result, BrowserDownloadResult) or not result.success:
            raise ValueError("A successful browser download result is required")
        relative_name = result.file_path
        if not isinstance(relative_name, str) or not relative_name:
            raise ValueError("Download result has no stored file path")
        # Result paths are deliberately limited to one generated filename. Check
        # both separator conventions regardless of the host operating system.
        if "/" in relative_name or "\\" in relative_name or relative_name in {".", ".."}:
            raise ValueError("Download result path must be a generated filename")
        if Path(relative_name).is_absolute() or Path(relative_name).drive:
            raise ValueError("Download result path must be relative")
        if not self.directory.exists():
            raise ValueError("Download storage directory does not exist")
        configured = self.directory.absolute()
        root = self.directory.resolve(strict=True)
        if configured != root or not root.is_dir():
            raise ValueError("Download storage directory is invalid")
        untrusted_candidate = root / relative_name
        if untrusted_candidate.is_symlink():
            raise ValueError("Download result cannot refer to a symbolic link")
        candidate = untrusted_candidate.resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise ValueError("Download result path is outside the download directory")
        return candidate

    def _prepare_root(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        configured = self.directory.absolute()
        root = self.directory.resolve(strict=True)
        if configured != root:
            raise OSError("Download storage directory must not be a symlink")
        if not root.is_dir():
            raise OSError("Download storage location is not a directory")
        return root

    @staticmethod
    def _safe_extension(suggested_filename: str) -> str:
        if not isinstance(suggested_filename, str):
            return ""
        # Normalize both slash styles before inspecting only the final component.
        leaf = suggested_filename.replace("\\", "/").rsplit("/", 1)[-1]
        leaf = unicodedata.normalize("NFKC", leaf)
        leaf = _CONTROL_CHARS.sub("", leaf).strip().rstrip(". ")
        suffix = Path(leaf).suffix
        return suffix if _SAFE_EXTENSION.fullmatch(suffix) else ""

    @staticmethod
    def _new_destination(root: Path, extension: str) -> tuple[str, Path]:
        # UUID-derived names avoid traversal, reserved device names, and collisions;
        # suggested names are never used as any part of the final path.
        for _ in range(5):
            file_name = f"{uuid4().hex}{extension.lower()}"
            path = root / file_name
            if not path.exists() and not path.is_symlink():
                return file_name, path
        raise OSError("Could not allocate a unique download filename")

    @staticmethod
    def _publish_without_overwrite(temporary_path: Path, final_path: Path, root: Path) -> None:
        if not final_path.resolve(strict=False).is_relative_to(root):
            raise OSError("Generated download path escaped its storage directory")
        # A hard link is an atomic no-overwrite publication on the same volume.
        # If the extremely unlikely UUID collision occurs, fail safely.
        os.link(temporary_path, final_path)
        temporary_path.unlink()
