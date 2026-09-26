from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness probe. Intentionally reports nothing about config or data."""
    return HealthResponse(status="ok")
