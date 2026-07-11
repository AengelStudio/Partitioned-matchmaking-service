import hashlib
import json
from uuid import UUID

import asyncpg


def compute_request_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def compute_request_hash_from_model(body: dict) -> str:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def fetch_stored_response(
    conn: asyncpg.Connection, key: str, tenant_id: str, request_hash: str
) -> tuple[bool, dict | None, bool]:
    """Returns (found, stored_response, hash_matched) using a connection
    the caller already holds (does not acquire a new one)."""
    row = await conn.fetchrow(
        """SELECT request_hash, response_body
           FROM idempotency_keys
           WHERE tenant_id = $1 AND idempotency_key = $2""",
        tenant_id,
        key,
    )
    if row is None:
        return False, None, False
    body = row["response_body"]
    stored_response = body if isinstance(body, dict) else json.loads(body)
    hash_matched = row["request_hash"] == request_hash
    return True, stored_response, hash_matched


async def check_idempotency_key(
    pool: asyncpg.Pool, key: str, tenant_id: str, request_hash: str
) -> tuple[bool, dict | None, bool]:
    """Returns (found, stored_response, hash_matched).

    Acquires its own connection — only call this when you are not
    already holding one, to avoid exhausting the pool. If you already
    hold a connection (e.g. inside a transaction), call
    `fetch_stored_response(conn, ...)` directly instead.
    """
    async with pool.acquire() as conn:
        return await fetch_stored_response(conn, key, tenant_id, request_hash)


async def reserve_idempotency_key(
    conn: asyncpg.Connection, key: str, tenant_id: str, request_hash: str
) -> bool:
    """Atomically claim (tenant_id, key) before creating the ticket.

    Postgres blocks a conflicting `ON CONFLICT DO NOTHING` insert until
    the other transaction holding that key commits or rolls back, so a
    caller that gets False back is guaranteed to see the winner's
    finalized row on a subsequent read — no separate locking needed.
    """
    row = await conn.fetchrow(
        """INSERT INTO idempotency_keys
               (tenant_id, idempotency_key, request_hash, response_status, response_body)
           VALUES ($1, $2, $3, 0, '{}'::jsonb)
           ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
           RETURNING tenant_id""",
        tenant_id,
        key,
        request_hash,
    )
    return row is not None


async def finalize_idempotency_key(
    conn: asyncpg.Connection,
    key: str,
    tenant_id: str,
    response_status: int,
    response_body: dict,
    ticket_id: UUID,
) -> None:
    await conn.execute(
        """UPDATE idempotency_keys
           SET response_status = $1, response_body = $2::jsonb, ticket_id = $3
           WHERE tenant_id = $4 AND idempotency_key = $5""",
        response_status,
        json.dumps(response_body),
        ticket_id,
        tenant_id,
        key,
    )
