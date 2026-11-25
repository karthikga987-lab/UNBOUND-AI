from fastapi import APIRouter, Request
import hashlib
import os
from pathlib import Path
import sys


router = APIRouter()


def _chromium_binary_identity() -> tuple[str | None, str | None]:
    """Report the installed Windows Chromium binary path and content hash.

    This is inventory only. It is deliberately not treated as proof that WFP
    binds a policy to the process or that the process cannot bypass a proxy.
    """
    if os.name != "nt":
        return None, None
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    cache = Path(configured) if configured else Path(__file__).resolve().parents[4] / ".playwright-browsers"
    try:
        candidates = sorted(cache.glob("chromium-*/chrome-win64/chrome.exe"))
        if len(candidates) != 1 or not candidates[0].is_file() or candidates[0].is_symlink():
            return None, None
        binary = candidates[0].resolve(strict=True)
        digest = hashlib.sha256()
        with binary.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return str(binary), digest.hexdigest()
    except OSError:
        return None, None


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    notification = runtime.get_notification_agent_status()
    proxy = runtime.browser_egress_proxy
    proxy_running = bool(proxy is not None and proxy.is_running)
    proxy_validation_working = bool(
        proxy_running
        and callable(getattr(proxy, "probe_validation", None))
        and proxy.probe_validation()
    )
    chromium_path, chromium_sha256 = _chromium_binary_identity()
    backend_executable = str(Path(sys.executable).resolve())
    return {
        "status": "ok",
        "llm_configured": runtime.provider_is_configured(),
        "tools_registered": runtime.registered_tools_count() > 0,
        "registered_tools_count": runtime.registered_tools_count(),
        "notification_agent_installed": notification["installed"],
        "notification_agent_running": notification["running"],
        "browser_public_execution_enabled": runtime.browser_egress_security_enabled,
        "browser_integration_status": runtime.browser_integration_status,
        "browser_deployment_mode": runtime.browser_deployment_mode,
        "browser_protected_deployment_verified": runtime.browser_protected_deployment_verified,
        "browser_deployment_acl_verified": runtime.browser_deployment_acl_verified,
        "browser_deployment_failure_code": runtime.browser_deployment_failure_code,
        "browser_deployment_root": runtime.browser_deployment_root,
        "browser_local_demo_enabled": os.getenv("UNBOUND_BROWSER_LOCAL_DEMO") == "1",
        "browser_egress_proxy_active": proxy_running,
        "browser_egress_proxy_endpoint": proxy.server if proxy_running else None,
        "browser_egress_proxy_pid": proxy.process_id if proxy_running else None,
        "browser_egress_proxy_executable": (
            str(proxy.executable_path) if proxy is not None and hasattr(proxy, "executable_path") else None
        ),
        "browser_egress_proxy_sha256": (
            proxy.executable_sha256 if proxy_running and hasattr(proxy, "executable_sha256") else None
        ),
        "browser_egress_proxy_identity_verified": bool(
            proxy_running and getattr(proxy, "identity_verified", False)
        ),
        "browser_egress_proxy_validation_working": proxy_validation_working,
        "browser_chromium_executable": chromium_path,
        "browser_chromium_sha256": chromium_sha256,
        "browser_backend_pid": os.getpid(),
        "browser_backend_executable": backend_executable,
        "browser_backend_identity_verified": Path(backend_executable).is_file(),
        "browser_wfp_policy_installed": False,
        "browser_wfp_policy_verified": False,
        "browser_wfp_policy_integrity_verified": False,
        "browser_wfp_policy_ownership_verified": False,
        "browser_chromium_identity_verified": False,
        "browser_ipv4_containment_verified": False,
        "browser_ipv6_containment_verified": False,
        "browser_dns_bypass_blocked": False,
        "browser_udp_quic_bypass_blocked": False,
        "browser_direct_bypass_blocked": False,
        "browser_proxy_outbound_restrictions_verified": False,
        "browser_proxy_shutdown_fail_closed_verified": False,
        # This process has no independently verified OS-level Chromium
        # containment signal. Keep it explicitly false until such a policy
        # is installed and verified outside application-level tests.
        "browser_process_containment_verified": False,
        "browser_public_execution_status": (
            "enabled_verified" if runtime.browser_egress_security_enabled
            else "blocked_missing_process_containment"
        ),
        "s3_sync": runtime.s3_sync_status(),
    }
