from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Response
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.config import get_settings
from app.db.connection import close_pool, get_pool
from app.models.tickets import TicketCreate, TicketResponse
from app.shared.partition import compute_partition_id

settings = get_settings()

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def _startup() -> None:
    get_pool()


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "api"}


@app.get("/metrics")
def metrics() -> Response:
    # Placeholder for Prometheus-style metrics; filled in by API owner.
    return Response(content="# metrics not implemented\n", media_type="text/plain")


@app.post("/v1/tickets", response_model=TicketResponse, status_code=201)
def create_ticket(
    ticket: TicketCreate,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
) -> TicketResponse:
    partition_id = compute_partition_id(
        x_tenant_id, ticket.region, ticket.queue_name, settings.matchmaking_partitions
    )
    pool = get_pool()
    try:
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Auto-provision tenant for the prototype so the slice runs end-to-end.
                cur.execute(
                    """
                    INSERT INTO tenants (tenant_id, name)
                    VALUES (%s, %s)
                    ON CONFLICT (tenant_id) DO NOTHING
                    """,
                    (x_tenant_id, x_tenant_id),
                )
                cur.execute(
                    """
                    INSERT INTO tickets
                        (tenant_id, player_id, region, queue_name, skill, partition_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'waiting')
                    RETURNING ticket_id, tenant_id, player_id, region, queue_name,
                              skill, partition_id, status, created_at, match_id
                    """,
                    (
                        x_tenant_id,
                        ticket.player_id,
                        ticket.region,
                        ticket.queue_name,
                        ticket.skill,
                        partition_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
    except pg_errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "uq_active_ticket_per_player_queue":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "active_ticket_exists",
                    "message": (
                        "This player already has an active ticket in this queue."
                    ),
                },
            ) from exc
        raise
    return TicketResponse(**row)


@app.get("/v1/tickets/{ticket_id}", response_model=TicketResponse)
def get_ticket(
    ticket_id: UUID,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
) -> TicketResponse:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT ticket_id, tenant_id, player_id, region, queue_name,
                       skill, partition_id, status, created_at, match_id,
                       matched_at, cancelled_at
                FROM tickets
                WHERE ticket_id = %s AND tenant_id = %s
                """,
                (str(ticket_id), x_tenant_id),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="ticket_not_found")
    return TicketResponse(**row)
