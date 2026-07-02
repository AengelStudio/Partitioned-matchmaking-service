import asyncpg
from app.core.config import settings


async def get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(settings.DATABASE_URL)
