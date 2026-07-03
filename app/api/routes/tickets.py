import hashlib
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from app.db.postgres import get_pool
from app.core.config import settings
from app.core.idempotency import (
    check_idempotency_key,
    compute_request_hash,
    store_idempotency_key,
)
from app.models.schemas import TicketRequest, TicketResponse

router = APIRouter()


@router.post("/tickets", response_model=TicketResponse, status_code=201)
async def create_ticket(
    request: Request,
    body: TicketRequest,
    x_tenant_id: str | None = Header(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    if x_tenant_id is None:
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")

    pool = get_pool()
    request_hash = None

    if idempotency_key is not None:
        request_hash = compute_request_hash(await request.body())
        found, stored_response, hash_matched = await check_idempotency_key(
            pool, idempotency_key, body.tenant_id, request_hash
        )
        if found:
            if hash_matched:
                return JSONResponse(status_code=200, content=stored_response)
            raise HTTPException(
                status_code=409,
                detail="Idempotency key reused with different request body",
            )

    key = f"{body.tenant_id}{body.region}{body.queue_name}".encode()
    partition_id = int(hashlib.sha256(key).hexdigest(), 16) % settings.partition_count

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO tickets (player_id, tenant_id, region, queue_name, skill_rating, partition_id)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   RETURNING *""",
                body.player_id, body.tenant_id, body.region,
                body.queue_name, body.skill_rating, partition_id,
            )
            response = TicketResponse(**{**dict(row), "ticket_id": str(row["ticket_id"])})

            if idempotency_key is not None:
                # Known limitation: a concurrent request with the same new key could
                # also pass the pre-check above before this commits, causing a unique
                # violation here. Not handled — no locking/retry logic in this phase.
                await store_idempotency_key(
                    conn, idempotency_key, body.tenant_id, request_hash,
                    response.model_dump(mode="json"),
                )

    return response


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
