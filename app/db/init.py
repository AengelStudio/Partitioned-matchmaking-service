from pathlib import Path

from app.config import get_settings
from app.db.connection import get_pool

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

DEFAULT_CALLBACK_URL = "http://mock-callback:9000/tenant-matchmaking-callback"
DEFAULT_CALLBACK_SECRET = "dev-callback-secret"

TENANTS = (
  "studio_a",
  "studio_noisy",
  "studio_quiet",
  "studio_01",
  "studio_02",
  "studio_03",
  "studio_04",
  "studio_05",
  "studio_06",
  "studio_07",
  "studio_08",
  "studio_09",
  "studio_10",
)


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
            for tenant_id in TENANTS:
                rate_limit = settings.default_ticket_rate_limit_per_minute
                max_in_flight = settings.default_max_waiting_tickets
                max_depth = settings.default_max_partition_depth
                if tenant_id == "studio_noisy":
                    # Elevated quota so loadtests/noisy_tenant.js can hammer before
                    # per-tenant throttling kicks in; still isolated from others.
                    rate_limit = settings.default_ticket_rate_limit_per_minute * 2
                # studio_quiet keeps the default limit: fairness is per-tenant, so
                # "quiet" means low offered load (5 req/s in noisy_tenant.js), not
                # a smaller contractual quota.

                cur.execute(
                    """
                    INSERT INTO tenants (
                        tenant_id,
                        name,
                        max_tickets_per_second,
                        max_tickets_in_flight,
                        max_partition_depth,
                        callback_url,
                        callback_secret
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        max_tickets_per_second = EXCLUDED.max_tickets_per_second,
                        max_tickets_in_flight = EXCLUDED.max_tickets_in_flight,
                        max_partition_depth = EXCLUDED.max_partition_depth,
                        callback_url = EXCLUDED.callback_url,
                        callback_secret = EXCLUDED.callback_secret
                    """,
                    (
                        tenant_id,
                        tenant_id,
                        rate_limit,
                        max_in_flight,
                        max_depth,
                        DEFAULT_CALLBACK_URL,
                        DEFAULT_CALLBACK_SECRET,
                    ),
                )
        conn.commit()


if __name__ == "__main__":
    main()
