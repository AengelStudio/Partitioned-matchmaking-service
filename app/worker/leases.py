import logging
import math

from psycopg import Connection

logger = logging.getLogger(__name__)


def fair_partition_cap(total_partitions: int, active_workers: int) -> int:
    if total_partitions <= 0:
        return 0
    if active_workers <= 0:
        return total_partitions
    return math.ceil(total_partitions / active_workers)


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


def record_worker_heartbeat(conn: Connection, worker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO worker_heartbeats (worker_id, last_seen_at)
            VALUES (%s, now())
            ON CONFLICT (worker_id) DO UPDATE
            SET last_seen_at = excluded.last_seen_at
            """,
            (worker_id,),
        )


def remove_worker_heartbeat(conn: Connection, worker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM worker_heartbeats WHERE worker_id = %s",
            (worker_id,),
        )


def count_active_workers(conn: Connection, heartbeat_seconds: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM worker_heartbeats
            WHERE last_seen_at > now() - make_interval(secs => %s)
            """,
            (heartbeat_seconds,),
        )
        row = cur.fetchone()
        return max(1, int(row[0] if row else 0))


def release_excess_partitions(
    conn: Connection, worker_id: str, max_partitions: int
) -> int:
    owned = list_owned_partitions(conn, worker_id)
    excess_count = len(owned) - max_partitions
    if excess_count <= 0:
        return 0

    to_release = sorted(owned, reverse=True)[:excess_count]
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE partition_leases
            SET owned_by = NULL,
                lease_until = NULL,
                updated_at = now()
            WHERE owned_by = %s
              AND partition_id = ANY(%s)
            """,
            (worker_id, to_release),
        )
        released = cur.rowcount

    if released:
        logger.info(
            "partitions_released_for_rebalance worker_id=%s partition_ids=%s count=%d max_partitions=%d",
            worker_id,
            to_release,
            released,
            max_partitions,
        )
    return released
