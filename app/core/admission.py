import time

import asyncpg
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.config import get_settings

settings = get_settings()

# max_tickets_per_second stores tickets-per-minute limit (legacy column name).
_RATE_LIMIT_COLUMN = "max_tickets_per_second"


def _rate_limit_per_minute(tenant: dict) -> int:
    return tenant[_RATE_LIMIT_COLUMN]


def _tenant_rate_limit_response(tenant_id: str, retry_after_seconds: int = 10) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": "tenant_rate_limit_exceeded",
            "message": f"Tenant {tenant_id} exceeded its ticket creation quota.",
            "retry_after_seconds": retry_after_seconds,
        },
    )


def _partition_overloaded_response(retry_after_seconds: int = 5) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "partition_overloaded",
            "message": "The target matchmaking partition is temporarily overloaded.",
            "retry_after_seconds": retry_after_seconds,
        },
    )


async def check_rate_limit(redis: Redis, tenant: dict) -> JSONResponse | None:
    tenant_id = tenant["tenant_id"]
    minute_bucket = int(time.time()) // 60
    key = f"rate:{tenant_id}:{minute_bucket}"
    limit = _rate_limit_per_minute(tenant)

    async with redis.pipeline() as pipe:
        pipe.incr(key)
        pipe.expire(key, 60)
        count, _ = await pipe.execute()

    if count > limit:
        seconds_left = 60 - (int(time.time()) % 60)
        return _tenant_rate_limit_response(tenant_id, retry_after_seconds=max(1, seconds_left))
    return None


async def check_tenant_quota(pool: asyncpg.Pool, tenant: dict) -> JSONResponse | None:
    tenant_id = tenant["tenant_id"]
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE tenant_id = $1 AND status = 'waiting'",
            tenant_id,
        )

    if count >= tenant["max_tickets_in_flight"]:
        return _tenant_rate_limit_response(tenant_id)
    return None


async def check_partition_depth(
    pool: asyncpg.Pool, tenant: dict, partition_id: int
) -> JSONResponse | None:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE partition_id = $1 AND status = 'waiting'",
            partition_id,
        )

    if count >= tenant["max_partition_depth"]:
        return _partition_overloaded_response()
    return None


async def check_db_load_shedding(pool: asyncpg.Pool) -> JSONResponse | None:
    """Global overload valve. Run after per-tenant admission so excess noisy
    traffic is rejected via Redis rate limits without consuming DB pool slots."""
    if not settings.load_shedding_enabled:
        return None

    start = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    latency_ms = (time.perf_counter() - start) * 1000.0

    if latency_ms > settings.db_latency_shed_threshold_ms:
        return _partition_overloaded_response()
    return None
