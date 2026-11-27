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
    refresh_worker_health = getattr(runtime, "refresh_browser_worker_health", None)
    if callable(refresh_worker_health):
        refresh_worker_health()
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
        # Gate D requires a fresh signed guest-policy attestation. Gate E is
        # still independently false until host OS egress enforcement passes.
        "browser_public_execution_enabled": False,
        "browser_worker": {
            "installation": "configured" if runtime.browser_worker_configured else "unknown",
            "service": "authenticated" if runtime.browser_worker_authenticated else "unknown",
            "connection": "reachable" if runtime.browser_worker_reachable else "untested",
            "guest_proxy": "configured" if runtime.browser_worker_authenticated else "unavailable",
            "readiness_gates": {
                "A": "NOT TESTED", "B": "NOT TESTED", "C": "NOT TESTED",
                "D": "PASS" if runtime.browser_worker_guest_policy_verified else "FAIL",
                "E": "FAIL", "F": "FAIL",
                "G": "NOT TESTED", "H": "NOT TESTED", "I": "FAIL",
            },
            "configured": runtime.browser_worker_configured,
            "reachable": runtime.browser_worker_reachable,
            "authenticated": runtime.browser_worker_authenticated,
            "containment_verified": runtime.browser_worker_containment_verified,
            "guest_policy_verified": runtime.browser_worker_guest_policy_verified,
            "guest_policy_failure": runtime.browser_worker_guest_policy_failure,
            "guest_policy_attestation": {
                "policy_version": runtime.browser_worker_guest_policy_version,
                "policy_sha256": runtime.browser_worker_guest_policy_sha256,
                "boot_id": runtime.browser_worker_guest_policy_boot_id,
                "observed_at": runtime.browser_worker_guest_policy_observed_at,
            } if runtime.browser_worker_guest_policy_verified else None,
            "execution_enabled": runtime.browser_worker_execution_enabled,
            "worker_id": runtime.browser_worker_id,
            "last_health_check": runtime.browser_worker_last_health_check,
            "failure_category": runtime.browser_worker_failure_category,
            "allowed_domains": ["example.com", "www.python.org", "docs.python.org"],
            "fetch_gateway_active": runtime.browser_fetch_gateway_server is not None,
            "public_browsing_enabled": False,
        },
        "browser_integration_status": runtime.browser_integration_status,
        "browser_provider_mode": runtime.browser_provider_mode,
        "browserless_cloud_demo": {
            "selected": runtime.browser_provider_mode == "browserless_cloud_demo",
            "configured": runtime.browserless_configured,
            "connection_status": runtime.browserless_connection_status,
            "last_connection_at": runtime.browserless_last_connection_at,
            "allowed_hosts": list(runtime.browserless_allowed_hosts),
            "baseline_allowed_hosts": list(runtime.browserless_allowed_hosts),
            "task_scoped_domain_authorization_supported": True,
            "domain_authorization_scope": "UNBOUND per-task exact-host Playwright request interception; Browserless provider-level egress policy is not asserted",
            "execution_configured": bool(
                runtime.browser_provider_mode == "browserless_cloud_demo"
                and runtime.browserless_configured
                and runtime.browser_tools_registered
            ),
            "execution_available": runtime.browserless_connection_status == "connected",
            "containment_note": "Cloud browser demo uses application-level hostname/request restrictions; self-hosted Windows and Ubuntu containment gates remain independent and unverified.",
        },
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
        "browser_public_execution_status": "blocked_missing_process_containment",
        "s3_sync": runtime.s3_sync_status(),
    }
