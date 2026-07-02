import asyncpg
from app.core.config import settings

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    dsn = settings.database_url.replace("+asyncpg", "", 1)
    _pool = await asyncpg.create_pool(dsn=dsn)


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("PostgreSQL pool not initialized. Call init_db() first.")
    return _pool
