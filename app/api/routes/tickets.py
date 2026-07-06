import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.admission import (
    check_partition_depth,
    check_rate_limit,
    check_tenant_quota,
)
from app.core.idempotency import (
    check_idempotency_key,
    compute_request_hash,
    store_idempotency_key,
)
from app.core.tenants import require_tenant
from app.db.postgres import get_pool
from app.db.redis import get_redis
from app.models.tickets import (
    TicketCancelResponse,
    TicketCreate,
    TicketResponse,
)
from app.shared.partition import compute_partition_id
from app.config import get_settings

router = APIRouter()
settings = get_settings()


def _ticket_response(row: dict, *, idempotent_replay: bool = False) -> TicketResponse:
    return TicketResponse(
        ticket_id=row["ticket_id"],
        tenant_id=row["tenant_id"],
        player_id=row["player_id"],
        region=row["region"],
        queue_name=row["queue_name"],
        skill=row["skill"],
        partition_id=row["partition_id"],
        status=row["status"],
        created_at=row["created_at"],
        match_id=row.get("match_id"),
        matched_at=row.get("matched_at"),
        cancelled_at=row.get("cancelled_at"),
        idempotent_replay=idempotent_replay or None,
    )


@router.post("/tickets", response_model=TicketResponse, status_code=201, response_model_exclude_none=True)
async def create_ticket(
    request: Request,
    body: TicketCreate,
    tenant: dict = Depends(require_tenant),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    tenant_id = tenant["tenant_id"]
    pool = get_pool()
    request_hash = None

    if idempotency_key is not None:
        request_hash = compute_request_hash(await request.body())
        found, stored_response, hash_matched = await check_idempotency_key(
            pool, idempotency_key, tenant_id, request_hash
        )
        if found:
            if hash_matched:
                replay = {**stored_response, "idempotent_replay": True}
                return JSONResponse(status_code=200, content=replay)
            return JSONResponse(
                status_code=409,
                content={
                    "error": "idempotency_key_conflict",
                    "message": "This idempotency key was already used with a different request body.",
                },
            )

    partition_id = compute_partition_id(
        tenant_id, body.region, body.queue_name, settings.matchmaking_partitions
    )

    rejection = await check_rate_limit(get_redis(), tenant)
    if rejection is not None:
        return rejection
    rejection = await check_tenant_quota(pool, tenant)
    if rejection is not None:
        return rejection
    rejection = await check_partition_depth(pool, partition_id)
    if rejection is not None:
        return rejection

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """INSERT INTO tickets
                           (tenant_id, player_id, region, queue_name, skill, partition_id, status)
                       VALUES ($1, $2, $3, $4, $5, $6, 'waiting')
                       RETURNING *""",
                    tenant_id,
                    body.player_id,
                    body.region,
                    body.queue_name,
                    body.skill,
                    partition_id,
                )
                response = _ticket_response(dict(row))
                response_body = response.model_dump(mode="json")

                if idempotency_key is not None:
                    await store_idempotency_key(
                        conn,
                        idempotency_key,
                        tenant_id,
                        request_hash,
                        201,
                        response_body,
                        row["ticket_id"],
                    )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="An active ticket already exists for this player in this queue",
        ) from None

    return response


@router.get("/tickets", response_model=list[TicketResponse])
async def list_tickets(tenant: dict = Depends(require_tenant)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tickets WHERE tenant_id = $1 AND status = 'waiting'",
            tenant["tenant_id"],
        )
    return [TicketResponse(**{**dict(row), "ticket_id": str(row["ticket_id"])}) for row in rows]


@router.get("/tickets/{ticket_id}", response_model=TicketResponse, response_model_exclude_none=True)
async def get_ticket(ticket_id: str, tenant: dict = Depends(require_tenant)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE ticket_id = $1::uuid AND tenant_id = $2",
            ticket_id,
            tenant["tenant_id"],
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return _ticket_response(dict(row))


@router.delete("/tickets/{ticket_id}", response_model=TicketCancelResponse)
async def cancel_ticket(ticket_id: str, tenant: dict = Depends(require_tenant)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE tickets
               SET status = 'cancelled', cancelled_at = now(), updated_at = now()
               WHERE ticket_id = $1::uuid
                 AND tenant_id = $2
                 AND status IN ('waiting', 'reserved')
               RETURNING ticket_id, status, cancelled_at""",
            ticket_id,
            tenant["tenant_id"],
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return TicketCancelResponse(**dict(row))
