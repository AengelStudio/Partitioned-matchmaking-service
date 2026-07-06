import hashlib
import json
from uuid import UUID

import asyncpg


def compute_request_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def check_idempotency_key(
    pool: asyncpg.Pool, key: str, tenant_id: str, request_hash: str
) -> tuple[bool, dict | None, bool]:
    """Returns (found, stored_response, hash_matched)."""
    async with pool.acquire() as conn:
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


async def store_idempotency_key(
    conn: asyncpg.Connection,
    key: str,
    tenant_id: str,
    request_hash: str,
    response_status: int,
    response_body: dict,
    ticket_id: UUID,
) -> None:
    await conn.execute(
        """INSERT INTO idempotency_keys
               (tenant_id, idempotency_key, request_hash, response_status, response_body, ticket_id)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
        tenant_id,
        key,
        request_hash,
        response_status,
        json.dumps(response_body),
        ticket_id,
    )
