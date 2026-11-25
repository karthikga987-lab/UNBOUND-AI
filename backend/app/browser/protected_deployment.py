"""Fail-closed verification for the administrator-owned browser deployment."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


class ProtectedDeploymentError(RuntimeError):
    """Protected browser files are missing, changed, or not ACL protected."""


@dataclass(frozen=True)
class ProtectedBrowserDeployment:
    root: Path
    manifest_path: Path
    deployment_id: str
    proxy_executable: Path
    proxy_sha256: str
    chromium_executable: Path
    chromium_sha256: str
    playwright_browsers_path: Path


_ACL_CHECK = r"""
$ErrorActionPreference = 'Stop'
$root = $env:UNBOUND_PROTECTED_ROOT
$privileged = @('S-1-5-18', 'S-1-5-32-544')
$allowed = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-32-545')
$writeMask = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
$root = [IO.Path]::GetFullPath($root)
$paths = @((Get-Item -LiteralPath $root -Force)) + @(Get-ChildItem -LiteralPath $root -Force -Recurse)
foreach ($item in $paths) {
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse_point' }
  $acl = Get-Acl -LiteralPath $item.FullName
  if (-not $acl.AreAccessRulesProtected) { throw 'inherited_acl' }
  $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  if ($owner -notin $privileged) { throw 'untrusted_owner' }
  $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
  if (@($rules | Where-Object { $_.IdentityReference.Value -notin $allowed }).Count -gt 0) { throw 'unexpected_acl_principal' }
  if (@($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny }).Count -gt 0) { throw 'deny_acl_rule' }
  if (@($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $_.IdentityReference.Value -eq 'S-1-5-32-545' -and (($_.FileSystemRights -band $writeMask) -ne 0) }).Count -gt 0) { throw 'users_can_write' }
  if (@($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $_.IdentityReference.Value -eq 'S-1-5-32-545' -and (($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq [Security.AccessControl.FileSystemRights]::ReadAndExecute) }).Count -eq 0) { throw 'users_lack_read_execute' }
}
@{ verified = $true } | ConvertTo-Json -Compress
"""


def protected_mode_requested() -> bool:
    return os.name == "nt" and os.getenv("UNBOUND_BROWSER_PROTECTED_MODE") == "1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _under_root(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative or ":" in relative:
        raise ProtectedDeploymentError("Protected deployment manifest contains an unsafe path")
    lexical = root / Path(*candidate.parts)
    if lexical.is_symlink() or getattr(lexical, "is_junction", lambda: False)():
        raise ProtectedDeploymentError("Protected deployment contains a reparse point")
    try:
        result = lexical.resolve(strict=True)
    except OSError:
        raise ProtectedDeploymentError("Protected browser deployment file verification failed") from None
    try:
        result.relative_to(root.resolve(strict=True))
    except ValueError:
        raise ProtectedDeploymentError("Protected deployment path escapes its root") from None
    return result


def _verify_acl(root: Path) -> None:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise ProtectedDeploymentError("Protected deployment ACL verifier is unavailable")
    encoded = __import__("base64").b64encode(_ACL_CHECK.encode("utf-16le")).decode("ascii")
    child_env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    child_env["UNBOUND_PROTECTED_ROOT"] = str(root)
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        response = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
    except Exception:
        raise ProtectedDeploymentError("Protected deployment ACL verification failed") from None
    if result.returncode != 0 or response != {"verified": True}:
        raise ProtectedDeploymentError("Protected deployment ACL verification failed")


def _verify_root(root_text: str) -> ProtectedBrowserDeployment:
    root_input = Path(root_text)
    if root_input.is_symlink() or getattr(root_input, "is_junction", lambda: False)():
        raise ProtectedDeploymentError("Protected browser deployment root is a reparse point")
    root = root_input
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files")).resolve(strict=True)
    expected_root = (program_files / "UNBOUND AI").resolve(strict=True)
    if root.resolve(strict=True) != expected_root or root.is_symlink():
        raise ProtectedDeploymentError("Protected browser deployment is not at the expected Program Files path")
    manifest_path = root / "deployment.json"
    if manifest_path.is_symlink() or getattr(manifest_path, "is_junction", lambda: False)() or not manifest_path.is_file():
        raise ProtectedDeploymentError("Protected browser deployment manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception:
        raise ProtectedDeploymentError("Protected browser deployment manifest is invalid") from None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("application") != "UNBOUND-AI"
        or not isinstance(manifest.get("deployment_id"), str)
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", manifest["deployment_id"])
        or not isinstance(manifest.get("files"), list)
    ):
        raise ProtectedDeploymentError("Protected browser deployment manifest is invalid")
    inventory: dict[str, str] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            raise ProtectedDeploymentError("Protected browser deployment inventory is invalid")
        name = entry["path"]
        digest = entry["sha256"].lower()
        if name != name.lower() or not re.fullmatch(r"[0-9a-f]{64}", digest) or name in inventory:
            raise ProtectedDeploymentError("Protected browser deployment inventory is invalid")
        path = _under_root(root, name)
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise ProtectedDeploymentError("Protected browser deployment file verification failed")
        inventory[name] = digest
    actual: set[str] = set()
    for base, dirs, files in os.walk(root, followlinks=False):
        for directory in dirs:
            child = Path(base) / directory
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise ProtectedDeploymentError("Protected browser deployment contains a reparse point")
        for file in files:
            path = Path(base) / file
            if path == manifest_path:
                continue
            actual.add(path.relative_to(root).as_posix())
    if actual != set(inventory):
        raise ProtectedDeploymentError("Protected browser deployment has an unexpected or missing file")
    proxy_name = manifest.get("proxy_path")
    chromium_name = manifest.get("chromium_path")
    if proxy_name not in inventory or chromium_name not in inventory:
        raise ProtectedDeploymentError("Protected browser executable paths are not inventoried")
    proxy = _under_root(root, proxy_name)
    chromium = _under_root(root, chromium_name)
    if proxy.name.lower() != "unbound-egress-proxy.exe" or chromium.name.lower() != "chrome.exe":
        raise ProtectedDeploymentError("Protected browser executable names are invalid")
    if _sha256(proxy) != manifest.get("proxy_sha256") or _sha256(chromium) != manifest.get("chromium_sha256"):
        raise ProtectedDeploymentError("Protected browser executable hash verification failed")
    _verify_acl(root)
    return ProtectedBrowserDeployment(
        root=root,
        manifest_path=manifest_path,
        deployment_id=manifest["deployment_id"],
        proxy_executable=proxy,
        proxy_sha256=inventory[proxy_name],
        chromium_executable=chromium,
        chromium_sha256=inventory[chromium_name],
        playwright_browsers_path=root / "playwright-browsers",
    )


def verify_protected_deployment() -> ProtectedBrowserDeployment:
    if os.name != "nt":
        raise ProtectedDeploymentError("Protected Windows browser deployment is unavailable on this platform")
    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "UNBOUND AI"
    try:
        manifest = root / "deployment.json"
        manifest.stat()
    except OSError:
        raise ProtectedDeploymentError("Protected browser deployment is missing") from None
    return _verify_root(str(root))
