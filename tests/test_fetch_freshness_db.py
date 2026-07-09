import os

import pytest
from psycopg.rows import dict_row

from app.db.connection import get_pool
from app.worker.matching import fetch_waiting_tickets, wait_seconds


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL required for DB integration tests",
)


PARTITION_ID = 99


@pytest.fixture()
def conn():
    pool = get_pool()
    with pool.connection() as connection:
        yield connection
        connection.rollback()


def _clear_partition(conn, partition_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM match_players WHERE match_id IN "
            "(SELECT match_id FROM matches WHERE partition_id = %s)",
            (partition_id,),
        )
        cur.execute(
            "DELETE FROM callback_events WHERE match_id IN "
            "(SELECT match_id FROM matches WHERE partition_id = %s)",
            (partition_id,),
        )
        cur.execute("DELETE FROM tickets WHERE partition_id = %s", (partition_id,))
        cur.execute("DELETE FROM matches WHERE partition_id = %s", (partition_id,))


def _insert_waiting(conn, player_id: str, age_seconds: int, skill: int = 1500) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tickets
                (tenant_id, player_id, region, queue_name, skill,
                 partition_id, status, created_at)
            VALUES
                ('studio_a', %s, 'eu-west', 'ranked_1v1', %s, %s, 'waiting',
                 now() - make_interval(secs => %s))
            """,
            (player_id, skill, PARTITION_ID, age_seconds),
        )


def test_freshness_bias_includes_recent_tickets_under_old_backlog(conn) -> None:
    _clear_partition(conn, PARTITION_ID)
    for index in range(30):
        _insert_waiting(conn, f"old_{index}", age_seconds=7200 + index, skill=1500)
    for index in range(4):
        _insert_waiting(conn, f"fresh_{index}", age_seconds=index + 1, skill=1500)
    conn.commit()

    tickets = fetch_waiting_tickets(
        conn, [PARTITION_ID], batch_size=20, freshness_bias=True
    )
    assert len(tickets) == 20

    fresh_players = {ticket.player_id for ticket in tickets if ticket.player_id.startswith("fresh_")}
    assert len(fresh_players) >= 2

    waits = [wait_seconds(ticket.created_at) for ticket in tickets]
    assert max(waits) > 3600
    assert min(waits) < 60


def test_fifo_fetch_prefers_oldest_without_freshness_bias(conn) -> None:
    _clear_partition(conn, PARTITION_ID)
    _insert_waiting(conn, "old_player", age_seconds=7200, skill=1500)
    _insert_waiting(conn, "fresh_player", age_seconds=5, skill=1500)
    conn.commit()

    tickets = fetch_waiting_tickets(
        conn, [PARTITION_ID], batch_size=10, freshness_bias=False
    )
    assert tickets[0].player_id == "old_player"
