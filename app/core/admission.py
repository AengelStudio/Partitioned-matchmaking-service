import time

import asyncpg
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.config import get_settings

settings = get_settings()


async def check_rate_limit(redis: Redis, tenant: dict) -> JSONResponse | None:
    tenant_id = tenant["tenant_id"]
    now = int(time.time())
    key = f"rate:{tenant_id}:{now}"

    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, 2)
    count, _ = await pipe.execute()

    if count > tenant["max_tickets_per_second"]:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded", "retry_after": 1},
        )
    return None


async def check_tenant_quota(pool: asyncpg.Pool, tenant: dict) -> JSONResponse | None:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE tenant_id = $1 AND status = 'waiting'",
            tenant["tenant_id"],
        )

    if count >= tenant["max_tickets_in_flight"]:
        return JSONResponse(status_code=429, content={"detail": "Tenant quota exceeded"})
    return None


async def check_partition_depth(pool: asyncpg.Pool, partition_id: int) -> JSONResponse | None:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE partition_id = $1 AND status = 'waiting'",
            partition_id,
        )

    if count >= settings.max_partition_depth:
        return JSONResponse(
            status_code=503,
            content={"detail": "Partition overloaded, try again later"},
        )
    return None
