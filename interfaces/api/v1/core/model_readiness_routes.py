"""Offline model setup status; no provider request or model initialization."""
from fastapi import APIRouter, Depends

from application.ai.model_readiness_service import ModelReadinessService

router = APIRouter(prefix="/settings", tags=["settings"])


def get_model_readiness_service() -> ModelReadinessService:
    return ModelReadinessService()


@router.get("/model-readiness")
def get_model_readiness(service: ModelReadinessService = Depends(get_model_readiness_service)):
    return service.get_readiness()
