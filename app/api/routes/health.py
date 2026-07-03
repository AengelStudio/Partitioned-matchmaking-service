from fastapi import APIRouter
from app.db.postgres import get_pool
from app.db.redis import get_redis

router = APIRouter()


@router.get("/health")
async def health_check():
    result = {"postgres": "ok", "redis": "ok"}

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        result["postgres"] = f"error: {e}"

    try:
        r = get_redis()
        await r.ping()
    except Exception as e:
        result["redis"] = f"error: {e}"

    status = "ok" if all(v == "ok" for v in result.values()) else "degraded"
    return {"status": status, **result}
