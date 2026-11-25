from fastapi import APIRouter, Request
import os


router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    notification = runtime.get_notification_agent_status()
    return {
        "status": "ok",
        "llm_configured": runtime.provider_is_configured(),
        "tools_registered": runtime.registered_tools_count() > 0,
        "registered_tools_count": runtime.registered_tools_count(),
        "notification_agent_installed": notification["installed"],
        "notification_agent_running": notification["running"],
        "browser_public_execution_enabled": runtime.browser_egress_security_enabled,
        "browser_integration_status": runtime.browser_integration_status,
        "browser_local_demo_enabled": os.getenv("UNBOUND_BROWSER_LOCAL_DEMO") == "1",
        "browser_public_execution_status": (
            "enabled_verified" if runtime.browser_egress_security_enabled
            else "blocked_missing_process_containment"
        ),
        "s3_sync": runtime.s3_sync_status(),
    }
