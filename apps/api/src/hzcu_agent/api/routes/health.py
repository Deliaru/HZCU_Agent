from fastapi import APIRouter, Request

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    model_config = request.app.state.models.config
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
        "model_provider": model_config.provider,
        "model_configured": model_config.configured,
    }
