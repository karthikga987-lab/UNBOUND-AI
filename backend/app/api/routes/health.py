from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    runtime = request.app.state.runtime
    return {
        "status": "ok",
        "llm_configured": runtime.provider_is_configured(),
        "tools_registered": runtime.registered_tools_count() > 0,
        "registered_tools_count": runtime.registered_tools_count(),
    }
