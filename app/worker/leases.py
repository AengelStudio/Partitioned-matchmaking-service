import logging

from psycopg import Connection

logger = logging.getLogger(__name__)


def renew_owned_partitions(
    conn: Connection, worker_id: str, lease_seconds: int
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE partition_leases
            SET lease_until = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE owned_by = %s
              AND lease_until > now()
            """,
            (lease_seconds, worker_id),
        )
        return cur.rowcount


def claim_available_partitions(
    conn: Connection,
    worker_id: str,
    lease_seconds: int,
    batch_size: int,
    partition_count: int = 0,
    claim_offset: int = 0,
) -> list[int]:
    with conn.cursor() as cur:
        if partition_count > 0:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT partition_id
                    FROM partition_leases
                    WHERE owned_by IS NULL
                       OR lease_until <= now()
                    ORDER BY (partition_id + %s) %% %s
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE partition_leases AS pl
                SET owned_by = %s,
                    lease_until = now() + make_interval(secs => %s),
                    updated_at = now()
                FROM candidates AS c
                WHERE pl.partition_id = c.partition_id
                RETURNING pl.partition_id
                """,
                (
                    claim_offset,
                    partition_count,
                    batch_size,
                    worker_id,
                    lease_seconds,
                ),
            )
        else:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT partition_id
                    FROM partition_leases
                    WHERE owned_by IS NULL
                       OR lease_until <= now()
                    ORDER BY partition_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE partition_leases AS pl
                SET owned_by = %s,
                    lease_until = now() + make_interval(secs => %s),
                    updated_at = now()
                FROM candidates AS c
                WHERE pl.partition_id = c.partition_id
                RETURNING pl.partition_id
                """,
                (batch_size, worker_id, lease_seconds),
            )
        claimed = [row[0] for row in cur.fetchall()]

    if claimed:
        logger.info(
            "partitions_claimed worker_id=%s partition_ids=%s count=%d",
            worker_id, claimed, len(claimed),
        )
    return claimed


def release_owned_partitions(conn: Connection, worker_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE partition_leases
            SET owned_by = NULL,
                lease_until = NULL,
                updated_at = now()
            WHERE owned_by = %s
            """,
            (worker_id,),
        )
        released = cur.rowcount

    logger.info(
        "partitions_released worker_id=%s count=%d", worker_id, released
    )
    return released


def list_owned_partitions(conn: Connection, worker_id: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT partition_id
            FROM partition_leases
            WHERE owned_by = %s
              AND lease_until > now()
            ORDER BY partition_id
            """,
            (worker_id,),
        )
        return [row[0] for row in cur.fetchall()]
