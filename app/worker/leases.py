from psycopg import Connection


def renew_owned_partitions(
    conn: Connection, worker_id: str, lease_seconds: int
) -> None:
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


def claim_available_partitions(
    conn: Connection, worker_id: str, lease_seconds: int, batch_size: int
) -> list[int]:
    with conn.cursor() as cur:
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
        return [row[0] for row in cur.fetchall()]


def release_owned_partitions(conn: Connection, worker_id: str) -> None:
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
