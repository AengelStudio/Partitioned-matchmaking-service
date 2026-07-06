from pathlib import Path

from app.config import get_settings
from app.db.postgres import get_pool

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def migrate() -> None:
    settings = get_settings()
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)
        await conn.execute(
            """
            INSERT INTO partition_leases (partition_id)
            SELECT generate_series(0, $1)
            ON CONFLICT DO NOTHING
            """,
            settings.matchmaking_partitions - 1,
        )
        await conn.execute(
            """
            INSERT INTO tenants (tenant_id, name)
            VALUES ('studio_a', 'studio_a')
            ON CONFLICT (tenant_id) DO NOTHING
            """
        )
