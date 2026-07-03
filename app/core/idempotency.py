import hashlib
import json

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
               WHERE key = $1 AND tenant_id = $2""",
            key, tenant_id,
        )
    if row is None:
        return False, None, False
    stored_response = json.loads(row["response_body"])
    hash_matched = row["request_hash"] == request_hash
    return True, stored_response, hash_matched


async def store_idempotency_key(
    conn: asyncpg.Connection,
    key: str,
    tenant_id: str,
    request_hash: str,
    response_body: dict,
) -> None:
    await conn.execute(
        """INSERT INTO idempotency_keys (key, tenant_id, request_hash, response_body)
           VALUES ($1, $2, $3, $4::jsonb)""",
        key, tenant_id, request_hash, json.dumps(response_body),
    )
