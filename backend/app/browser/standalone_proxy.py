"""Parent-side lifecycle and identity checks for the packaged egress proxy."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading


logger = logging.getLogger(__name__)
_STARTUP_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def default_proxy_executable() -> Path:
    return Path(__file__).resolve().parent / "bin" / "unbound-egress-proxy" / "unbound-egress-proxy.exe"


class StandaloneProxyError(RuntimeError):
    """A safe diagnostic for a standalone proxy lifecycle failure."""


class _ExecutableLock:
    """Hold the packaged image read-only without write/delete sharing on Windows."""

    def __init__(self, path: Path) -> None:
        self._handles: list[object] = []
        self._files = []
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            try:
                # PyInstaller one-directory builds load Python and extension
                # modules from _internal. Lock every existing file, not just
                # the launcher, so a running proxy cannot have its package
                # replaced underneath it.
                for item in sorted(path.parent.rglob("*")):
                    if item.is_symlink():
                        raise OSError("proxy package contains a symbolic link")
                    if not item.is_file():
                        continue
                    handle = create_file(str(item), 0x80000000, 0x00000001, None, 3, 0x80, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise OSError(ctypes.get_last_error(), "could not lock proxy package")
                    self._handles.append(handle)
            except Exception:
                self.close()
                raise
        else:
            self._files.append(path.open("rb"))

    def close(self) -> None:
        if self._handles:
            close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            for handle in reversed(self._handles):
                close_handle(handle)
            self._handles.clear()
        for file in self._files:
            file.close()
        self._files.clear()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StandaloneEgressProxy:
    """Own a packaged proxy child, its private stdio control pipe, and image lock."""

    def __init__(
        self,
        *,
        port: int,
        demo_only: bool = False,
        max_body_bytes: int = 20 * 1024 * 1024,
        executable: Path | None = None,
    ) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("standalone proxy port must be between 1 and 65535")
        if not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool) or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.port = port
        self.demo_only = bool(demo_only)
        self.max_body_bytes = max_body_bytes
        self.executable_path = (executable or default_proxy_executable()).resolve()
        self._expected_root = default_proxy_executable().parent.resolve()
        if executable is None and self.executable_path.parent != self._expected_root:
            raise StandaloneProxyError("Proxy executable is outside the managed application directory")
        self._secret = __import__("secrets").token_urlsafe(32)
        self._process: subprocess.Popen[str] | None = None
        self._image_lock: _ExecutableLock | None = None
        self._sha256: str | None = None
        self._ready = False
        self._startup_messages: queue.Queue[dict[str, object]] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    @property
    def server(self) -> str:
        if not self.is_running:
            raise RuntimeError("Standalone egress proxy is not running")
        return f"http://127.0.0.1:{self.port}"

    @property
    def proxy_credentials(self) -> dict[str, str]:
        return {"username": "unbound", "password": self._secret}

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self.is_running and self._process is not None else None

    @property
    def executable_sha256(self) -> str | None:
        return self._sha256 if self.is_running else None

    @property
    def identity_verified(self) -> bool:
        if not self.is_running or self._sha256 is None:
            return False
        try:
            return _sha256(self.executable_path) == self._sha256
        except OSError:
            return False

    @property
    def is_running(self) -> bool:
        return bool(self._ready and self._process is not None and self._process.poll() is None)

    def _safe_child_environment(self) -> dict[str, str]:
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            line = process.stdout.readline(16_385)
            if len(line) > 16_384:
                self._startup_messages.put({"event": "error", "code": "invalid_child_response"})
                return
            message = json.loads(line) if line else {"event": "error", "code": "child_exited"}
            if not isinstance(message, dict):
                message = {"event": "error", "code": "invalid_child_response"}
            self._startup_messages.put(message)
            for extra in process.stdout:
                if extra:
                    logger.debug("Standalone proxy control event received")
        except Exception:
            self._startup_messages.put({"event": "error", "code": "invalid_child_response"})

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            # Do not forward child-controlled text to backend logs: even an
            # unexpected exception must not leak auth headers, URLs, or data.
            value = line.rstrip()
            if value.startswith("WARNING backend.app.egress_proxy browser egress denied category="):
                logger.warning("standalone egress proxy reported a denied request")
            elif value.startswith("INFO backend.app.egress_proxy browser egress allowed host="):
                logger.info("standalone egress proxy allowed a validated destination")
            else:
                logger.warning("standalone egress proxy emitted a diagnostic")

    def start(self) -> "StandaloneEgressProxy":
        if self.is_running:
            return self
        if not self.executable_path.is_file() or self.executable_path.is_symlink():
            raise StandaloneProxyError("Packaged egress proxy executable is missing or is not a regular file")
        if os.name == "nt" and self.executable_path.parent != self._expected_root:
            raise StandaloneProxyError("Proxy executable path does not match the managed project location")
        try:
            self._image_lock = _ExecutableLock(self.executable_path)
            self._sha256 = _sha256(self.executable_path)
            if len(self._sha256) != 64:
                raise StandaloneProxyError("Proxy executable identity could not be established")
            self._process = subprocess.Popen(
                [str(self.executable_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self.executable_path.parent),
                env=self._safe_child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            self._stdout_thread = threading.Thread(target=self._read_stdout, name="unbound-proxy-control", daemon=True)
            self._stderr_thread = threading.Thread(target=self._read_stderr, name="unbound-proxy-logs", daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
            if self._process.stdin is None:
                raise StandaloneProxyError("Proxy control pipe was not created")
            startup = {
                "host": "127.0.0.1",
                "port": self.port,
                "demo_only": self.demo_only,
                "proxy_secret": self._secret,
                "max_body_bytes": self.max_body_bytes,
            }
            self._process.stdin.write(json.dumps(startup, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
            try:
                response = self._startup_messages.get(timeout=_STARTUP_TIMEOUT_SECONDS)
            except queue.Empty:
                raise StandaloneProxyError("Standalone egress proxy did not become ready before timeout") from None
            if (
                response.get("event") != "ready"
                or response.get("pid") != self._process.pid
                or response.get("host") != "127.0.0.1"
                or response.get("port") != self.port
            ):
                code = response.get("code")
                raise StandaloneProxyError(
                    f"Standalone egress proxy startup rejected ({code if isinstance(code, str) else 'invalid_ready_response'})"
                )
            self._ready = True
            if not self.identity_verified:
                raise StandaloneProxyError("Proxy executable identity changed during startup")
            return self
        except Exception:
            self.close()
            raise

    def probe_validation(self) -> bool:
        """Authenticate to loopback and confirm a private destination is denied."""
        if not self.is_running:
            return False
        token = base64.b64encode(
            f"{self._credentials_user()}:{self._secret}".encode("utf-8")
        ).decode("ascii")
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1.5) as connection:
                connection.sendall(
                    (
                        "GET http://127.0.0.1/ HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        f"Proxy-Authorization: Basic {token}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                response = connection.recv(128)
            return response.startswith(b"HTTP/1.1 403")
        except OSError:
            return False

    @staticmethod
    def _credentials_user() -> str:
        return "unbound"

    def close(self) -> None:
        process, self._process = self._process, None
        self._ready = False
        if process is not None:
            try:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write('{"command":"shutdown"}\n')
                    process.stdin.flush()
                    process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=2.0)
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._stdout_thread = self._stderr_thread = None
        if self._image_lock is not None:
            self._image_lock.close()
            self._image_lock = None

    shutdown = close
