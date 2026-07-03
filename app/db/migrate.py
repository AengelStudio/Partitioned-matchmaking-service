from pathlib import Path
from app.db.postgres import get_pool

_SQL = (Path(__file__).parent / "init.sql").read_text()


async def migrate() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SQL)
