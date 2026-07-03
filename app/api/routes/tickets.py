import hashlib
from fastapi import APIRouter, Header, HTTPException, Response
from app.db.postgres import get_pool
from app.core.config import settings
from app.models.schemas import TicketRequest, TicketResponse

router = APIRouter()


@router.post("/tickets", response_model=TicketResponse, status_code=201)
async def create_ticket(body: TicketRequest, x_tenant_id: str | None = Header(None)):
    if x_tenant_id is None:
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")
    key = f"{body.tenant_id}{body.region}{body.queue_name}".encode()
    partition_id = int(hashlib.sha256(key).hexdigest(), 16) % settings.partition_count
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tickets (player_id, tenant_id, region, queue_name, skill_rating, partition_id)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING *""",
            body.player_id, body.tenant_id, body.region,
            body.queue_name, body.skill_rating, partition_id,
        )
    return TicketResponse(**{**dict(row), "ticket_id": str(row["ticket_id"])})


@router.get("/tickets/{ticket_id}", response_model=TicketResponse)
async def get_ticket(ticket_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE ticket_id = $1::uuid", ticket_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return TicketResponse(**{**dict(row), "ticket_id": str(row["ticket_id"])})


@router.delete("/tickets/{ticket_id}", status_code=204)
async def cancel_ticket(ticket_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE tickets SET status = 'cancelled' WHERE ticket_id = $1::uuid", ticket_id
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Ticket not found")
    return Response(status_code=204)
