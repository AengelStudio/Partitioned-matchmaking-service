from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.metrics import registry

router = APIRouter()


@router.get("/metrics")
async def get_metrics():
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
