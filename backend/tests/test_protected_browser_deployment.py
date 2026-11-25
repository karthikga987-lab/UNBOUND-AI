from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.app.browser import protected_deployment as deployment


def _write_file(root: Path, relative: str, content: bytes) -> str:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _manifest(root: Path) -> dict[str, object]:
    proxy = "browser/proxy/unbound-egress-proxy.exe"
    chrome = "playwright-browsers/chromium-1255/chrome-win64/chrome.exe"
    proxy_hash = _write_file(root, proxy, b"synthetic proxy image")
    chrome_hash = _write_file(root, chrome, b"synthetic chromium image")
    extra = "playwright-browsers/chromium-1255/chrome-win64/resources.pak"
    extra_hash = _write_file(root, extra, b"synthetic required resource")
    return {
        "schema_version": 1,
        "application": "UNBOUND-AI",
        "deployment_id": "9d69ffb5-6daa-4f88-9960-7c0a9fe7b2a4",
        "proxy_path": proxy,
        "proxy_sha256": proxy_hash,
        "chromium_path": chrome,
        "chromium_sha256": chrome_hash,
        "files": [
            {"path": proxy, "sha256": proxy_hash},
            {"path": chrome, "sha256": chrome_hash},
            {"path": extra, "sha256": extra_hash},
        ],
    }


def test_protected_manifest_validates_full_file_inventory_and_executable_hashes(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    root = program_files / "UNBOUND AI"
    root.mkdir(parents=True)
    manifest = _manifest(root)
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    acl_roots: list[Path] = []
    monkeypatch.setattr(deployment, "_verify_acl", lambda candidate: acl_roots.append(candidate))

    verified = deployment._verify_root(str(root))

    assert verified.deployment_id == manifest["deployment_id"]
    assert verified.proxy_executable == root / "browser" / "proxy" / "unbound-egress-proxy.exe"
    assert verified.chromium_executable == root / "playwright-browsers" / "chromium-1255" / "chrome-win64" / "chrome.exe"
    assert isinstance(verified.chromium_sha256, str)
    assert verified.chromium_sha256 == manifest["chromium_sha256"]
    assert verified.playwright_browsers_path == root / "playwright-browsers"
    assert acl_roots == [root]


def test_protected_manifest_rejects_modified_executable_and_unexpected_files(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    root = program_files / "UNBOUND AI"
    root.mkdir(parents=True)
    manifest = _manifest(root)
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setattr(deployment, "_verify_acl", lambda _candidate: None)

    (root / manifest["proxy_path"]).write_bytes(b"changed image")
    with pytest.raises(deployment.ProtectedDeploymentError, match="file verification failed"):
        deployment._verify_root(str(root))

    (root / manifest["proxy_path"]).write_bytes(b"synthetic proxy image")
    (root / "extra.dll").write_bytes(b"unmanifested")
    with pytest.raises(deployment.ProtectedDeploymentError, match="unexpected or missing file"):
        deployment._verify_root(str(root))


def test_protected_manifest_rejects_path_traversal_and_missing_files(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    root = program_files / "UNBOUND AI"
    root.mkdir(parents=True)
    manifest = _manifest(root)
    manifest["files"][0]["path"] = "../outside.exe"
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setattr(deployment, "_verify_acl", lambda _candidate: None)
    with pytest.raises(deployment.ProtectedDeploymentError, match="unsafe path"):
        deployment._verify_root(str(root))

    manifest = _manifest(root)
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / manifest["chromium_path"]).unlink()
    with pytest.raises(deployment.ProtectedDeploymentError, match="file verification failed"):
        deployment._verify_root(str(root))


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.exe",
        "browser//proxy.exe",
        "browser/./proxy.exe",
        "C:/outside.exe",
        "//server/share.exe",
        "browser/proxy.exe:stream",
        "browser/CON.txt",
        "browser/trailing./proxy.exe",
        "browser/trailing /proxy.exe",
        "browser\\proxy.exe",
    ],
)
def test_manifest_paths_must_be_canonical_windows_safe_relative_paths(tmp_path, monkeypatch, unsafe_path):
    program_files = tmp_path / "Program Files"
    root = program_files / "UNBOUND AI"
    root.mkdir(parents=True)
    manifest = _manifest(root)
    manifest["files"][0]["path"] = unsafe_path
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setattr(deployment, "_verify_acl", lambda _candidate: None)
    with pytest.raises(deployment.ProtectedDeploymentError, match="unsafe path"):
        deployment._verify_root(str(root))


def test_acl_verification_requires_both_privileged_full_control_groups():
    valid = {
        "verified": True,
        "administrators_full_control": True,
        "system_full_control": True,
        "users_read_execute_only": True,
    }
    assert deployment._acl_report_is_valid(valid)
    missing_administrators = {**valid, "administrators_full_control": False}
    missing_system = {**valid, "system_full_control": False}
    assert not deployment._acl_report_is_valid(missing_administrators)
    assert not deployment._acl_report_is_valid(missing_system)
    assert not deployment._acl_report_is_valid({**valid, "extra": True})
