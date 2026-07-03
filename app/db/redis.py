import redis.asyncio as redis
from redis.asyncio import Redis
from app.core.config import settings

_redis: Redis | None = None


async def init_redis() -> None:
    global _redis
    _redis = redis.from_url(settings.redis_url, decode_responses=True)


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis client not initialized. Call init_redis() first.")
    return _redis
