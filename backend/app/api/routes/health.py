from fastapi import APIRouter, Request


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
    }
