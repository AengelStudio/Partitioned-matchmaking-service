import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_URL = "http://mock-callback:9000/tenant-matchmaking-callback"


@dataclass(frozen=True)
class WaitingTicket:
    ticket_id: UUID
    tenant_id: str
    player_id: str
    region: str
    queue_name: str
    skill: int
    partition_id: int
    created_at: datetime


def cleanup_expired_reservations(conn: Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tickets
            SET status = 'waiting',
                reserved_by = NULL,
                reserved_until = NULL,
                updated_at = now()
            WHERE status = 'reserved'
              AND reserved_until <= now()
            """
        )
        expired = cur.rowcount

    if expired:
        logger.info("reservations_cleaned count=%d", expired)
    return expired


def _fetch_tickets_ordered(
    conn: Connection,
    partition_ids: list[int],
    limit: int,
    order: str,
    max_wait_seconds: float | None,
) -> list[WaitingTicket]:
    if limit <= 0:
        return []

    query = """
        SELECT ticket_id, tenant_id, player_id, region, queue_name,
               skill, partition_id, created_at
        FROM tickets
        WHERE partition_id = ANY(%s)
          AND status = 'waiting'
    """
    params: list = [partition_ids]

    if max_wait_seconds is not None:
        query += " AND created_at >= now() - make_interval(secs => %s)"
        params.append(max_wait_seconds)

    query += f" ORDER BY created_at {order} LIMIT %s"
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    return [WaitingTicket(**row) for row in rows]


def fetch_waiting_tickets(
    conn: Connection,
    partition_ids: list[int],
    batch_size: int,
    freshness_bias: bool = False,
    max_wait_seconds: float | None = None,
) -> list[WaitingTicket]:
    if not partition_ids:
        return []

    if not freshness_bias:
        tickets = _fetch_tickets_ordered(
            conn, partition_ids, batch_size, "ASC", max_wait_seconds
        )
        _log_tickets_fetched(tickets, partition_ids)
        return tickets

    newest_limit = (batch_size + 1) // 2
    oldest_limit = batch_size - newest_limit

    newest = _fetch_tickets_ordered(
        conn, partition_ids, newest_limit, "DESC", max_wait_seconds
    )
    seen_ids = {ticket.ticket_id for ticket in newest}

    oldest = _fetch_tickets_ordered(
        conn, partition_ids, oldest_limit, "ASC", max_wait_seconds
    )
    oldest = [ticket for ticket in oldest if ticket.ticket_id not in seen_ids]

    tickets = newest + oldest
    _log_tickets_fetched(tickets, partition_ids)
    return tickets


def _log_tickets_fetched(
    tickets: list[WaitingTicket], partition_ids: list[int]
) -> None:
    if not tickets:
        return
    per_partition: dict[int, int] = {}
    for ticket in tickets:
        per_partition[ticket.partition_id] = per_partition.get(ticket.partition_id, 0) + 1
    logger.info(
        "tickets_fetched count=%d partitions_owned=%d per_partition=%s",
        len(tickets), len(partition_ids), per_partition,
    )


def skill_delta(wait_seconds: float, settings: Settings) -> int:
    if wait_seconds >= 60:
        return settings.skill_delta_after_60s
    if wait_seconds >= 30:
        return settings.skill_delta_after_30s
    return settings.skill_delta_initial


def wait_seconds(created_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created_at).total_seconds())


def tickets_compatible(
    left: WaitingTicket, right: WaitingTicket, settings: Settings
) -> bool:
    if (
        left.tenant_id != right.tenant_id
        or left.region != right.region
        or left.queue_name != right.queue_name
    ):
        return False

    threshold = max(
        skill_delta(wait_seconds(left.created_at), settings),
        skill_delta(wait_seconds(right.created_at), settings),
    )
    return abs(left.skill - right.skill) <= threshold


def find_pairs(
    tickets: list[WaitingTicket], settings: Settings
) -> list[tuple[WaitingTicket, WaitingTicket]]:
    if settings.match_size != 2:
        raise ValueError("only 1v1 matching is supported")

    pairs: list[tuple[WaitingTicket, WaitingTicket]] = []
    used: set[UUID] = set()

    for index, first in enumerate(tickets):
        if first.ticket_id in used:
            continue
        for second in tickets[index + 1 :]:
            if second.ticket_id in used:
                continue
            if tickets_compatible(first, second, settings):
                pairs.append((first, second))
                used.add(first.ticket_id)
                used.add(second.ticket_id)
                break

    if tickets:
        logger.info(
            "pair_search_completed tickets_considered=%d pairs_found=%d",
            len(tickets), len(pairs),
        )
    return pairs


def _reserve_ticket(
    conn: Connection,
    ticket_id: UUID,
    worker_id: str,
    reservation_seconds: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tickets
            SET status = 'reserved',
                reserved_by = %s,
                reserved_until = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE ticket_id = %s
              AND status = 'waiting'
            """,
            (worker_id, reservation_seconds, str(ticket_id)),
        )
        return cur.rowcount == 1


def _release_ticket(conn: Connection, ticket_id: UUID, worker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tickets
            SET status = 'waiting',
                reserved_by = NULL,
                reserved_until = NULL,
                updated_at = now()
            WHERE ticket_id = %s
              AND status = 'reserved'
              AND reserved_by = %s
            """,
            (str(ticket_id), worker_id),
        )


def _build_callback_payload(
    event_id: UUID,
    match_id: UUID,
    tenant_id: str,
    region: str,
    queue_name: str,
    created_at: datetime,
    players: list[dict[str, str]],
) -> dict:
    created = created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id": str(event_id),
        "event_type": "match.created",
        "tenant_id": tenant_id,
        "match_id": str(match_id),
        "created_at": created,
        "region": region,
        "queue_name": queue_name,
        "players": players,
    }


def create_match_from_pair(
    conn: Connection,
    first: WaitingTicket,
    second: WaitingTicket,
    worker_id: str,
    reservation_seconds: int,
) -> bool:
    if not _reserve_ticket(conn, first.ticket_id, worker_id, reservation_seconds):
        return False
    if not _reserve_ticket(conn, second.ticket_id, worker_id, reservation_seconds):
        _release_ticket(conn, first.ticket_id, worker_id)
        return False

    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO matches
                        (tenant_id, region, queue_name, partition_id, status)
                    VALUES (%s, %s, %s, %s, 'created')
                    RETURNING match_id, created_at
                    """,
                    (
                        first.tenant_id,
                        first.region,
                        first.queue_name,
                        first.partition_id,
                    ),
                )
                match_row = cur.fetchone()
                match_id = match_row["match_id"]
                match_created_at = match_row["created_at"]

                players = [
                    {
                        "player_id": first.player_id,
                        "ticket_id": str(first.ticket_id),
                    },
                    {
                        "player_id": second.player_id,
                        "ticket_id": str(second.ticket_id),
                    },
                ]

                for ticket in (first, second):
                    cur.execute(
                        """
                        INSERT INTO match_players (match_id, ticket_id, player_id)
                        VALUES (%s, %s, %s)
                        """,
                        (str(match_id), str(ticket.ticket_id), ticket.player_id),
                    )
                    cur.execute(
                        """
                        UPDATE tickets
                        SET status = 'matched',
                            match_id = %s,
                            matched_at = now(),
                            reserved_by = NULL,
                            reserved_until = NULL,
                            updated_at = now()
                        WHERE ticket_id = %s
                          AND status = 'reserved'
                          AND reserved_by = %s
                        """,
                        (str(match_id), str(ticket.ticket_id), worker_id),
                    )

                cur.execute(
                    """
                    SELECT COALESCE(callback_url, %s) AS callback_url
                    FROM tenants
                    WHERE tenant_id = %s
                    """,
                    (DEFAULT_CALLBACK_URL, first.tenant_id),
                )
                tenant_row = cur.fetchone()
                callback_url = tenant_row["callback_url"]

                event_id = uuid4()
                payload = _build_callback_payload(
                    event_id,
                    match_id,
                    first.tenant_id,
                    first.region,
                    first.queue_name,
                    match_created_at,
                    players,
                )
                cur.execute(
                    """
                    INSERT INTO callback_events
                        (event_id, tenant_id, match_id, event_type,
                         callback_url, payload, status)
                    VALUES (%s, %s, %s, 'match.created', %s, %s, 'pending')
                    """,
                    (
                        str(event_id),
                        first.tenant_id,
                        str(match_id),
                        callback_url,
                        Json(payload),
                    ),
                )

        logger.info(
            "match_created worker_id=%s match_id=%s tenant_id=%s partition_id=%s "
            "ticket_ids=%s,%s",
            worker_id,
            match_id,
            first.tenant_id,
            first.partition_id,
            first.ticket_id,
            second.ticket_id,
        )
        return True
    except Exception as exc:
        logger.exception(
            "match_creation_failed worker_id=%s partition_id=%s "
            "ticket_ids=%s,%s exception_type=%s",
            worker_id,
            first.partition_id,
            first.ticket_id,
            second.ticket_id,
            type(exc).__name__,
        )
        _release_ticket(conn, first.ticket_id, worker_id)
        _release_ticket(conn, second.ticket_id, worker_id)
        raise
