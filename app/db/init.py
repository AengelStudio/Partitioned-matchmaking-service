from pathlib import Path

from app.config import get_settings
from app.db.connection import get_pool

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def main() -> None:
    settings = get_settings()
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            cur.execute(
                """
                INSERT INTO partition_leases (partition_id)
                SELECT generate_series(0, %s)
                ON CONFLICT DO NOTHING
                """,
                (settings.matchmaking_partitions - 1,),
            )
            cur.execute(
                """
                INSERT INTO tenants (tenant_id, name)
                VALUES ('studio_a', 'studio_a')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            )
        conn.commit()


if __name__ == "__main__":
    main()
