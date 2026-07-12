from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.metrics import active_tickets, registry
from app.db.postgres import get_pool

router = APIRouter()


async def _refresh_active_tickets() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT tenant_id, partition_id, COUNT(*) AS ticket_count
               FROM tickets
               WHERE status = 'waiting'
               GROUP BY tenant_id, partition_id"""
        )
    active_tickets.clear()
    for row in rows:
        active_tickets.labels(
            tenant_id=row["tenant_id"], partition_id=str(row["partition_id"])
        ).set(row["ticket_count"])


@router.get("/metrics")
async def get_metrics():
    await _refresh_active_tickets()
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
