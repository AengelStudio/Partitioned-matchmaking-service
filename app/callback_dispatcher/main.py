import hashlib
import hmac
import json
import logging
import random
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx
from psycopg import Connection
from psycopg.rows import dict_row

from app.callback_dispatcher.http import start_metrics_server
from app.callback_dispatcher.metrics import CallbackDispatcherMetrics
from app.config import Settings, get_settings
from app.db.connection import close_pool, get_pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallbackEvent:
    event_id: UUID
    tenant_id: str
    match_id: UUID
    event_type: str
    callback_url: str
    payload: dict
    attempts: int
    callback_secret: str | None


def resolve_dispatcher_id(settings: Settings) -> str:
    if settings.callback_dispatcher_id and settings.callback_dispatcher_id != "callback-local-1":
        return settings.callback_dispatcher_id
    return socket.gethostname()


def claim_callback_events(
    conn: Connection, dispatcher_id: str, settings: Settings
) -> list[CallbackEvent]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH candidates AS (
                SELECT event_id
                FROM callback_events
                WHERE (
                    status = 'pending'
                    OR (status = 'in_progress' AND locked_until <= now())
                )
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at, created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE callback_events AS ce
            SET status = 'in_progress',
                locked_by = %s,
                locked_until = now() + make_interval(secs => %s)
            FROM candidates AS c
            WHERE ce.event_id = c.event_id
            RETURNING ce.event_id,
                      ce.tenant_id,
                      ce.match_id,
                      ce.event_type,
                      ce.callback_url,
                      ce.payload,
                      ce.attempts,
                      (
                          SELECT callback_secret
                          FROM tenants
                          WHERE tenants.tenant_id = ce.tenant_id
                      ) AS callback_secret
            """,
            (
                settings.callback_batch_size,
                dispatcher_id,
                settings.callback_timeout_seconds,
            ),
        )
        rows = cur.fetchall()

    return [CallbackEvent(**row) for row in rows]


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def signature(secret: str, timestamp: str, body: str) -> str:
    message = f"{timestamp}.{body}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_headers(event: CallbackEvent, body: str) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    headers = {
        "Content-Type": "application/json",
        "X-PMS-Event-Id": str(event.event_id),
        "X-PMS-Timestamp": timestamp,
    }
    headers["X-PMS-Signature"] = (
        f"sha256={signature(event.callback_secret or '', timestamp, body)}"
    )
    return headers


def next_backoff_seconds(attempts: int, settings: Settings) -> int:
    exponential = settings.callback_base_backoff_seconds * (2 ** max(0, attempts - 1))
    capped = min(exponential, settings.callback_max_backoff_seconds)
    return capped + random.randint(0, settings.callback_jitter_seconds)


def mark_delivered(conn: Connection, event: CallbackEvent) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE callback_events
            SET status = 'delivered',
                attempts = attempts + 1,
                locked_by = NULL,
                locked_until = NULL,
                last_error = NULL,
                delivered_at = now()
            WHERE event_id = %s
            """,
            (str(event.event_id),),
        )
        cur.execute(
            """
            UPDATE matches
            SET status = 'callback_delivered'
            WHERE match_id = %s
            """,
            (str(event.match_id),),
        )


def mark_failed_attempt(
    conn: Connection, event: CallbackEvent, settings: Settings, error: str
) -> bool:
    """Return True when the event has exhausted retries and is final failed."""
    next_attempt = event.attempts + 1
    if next_attempt >= settings.callback_max_attempts:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE callback_events
                SET status = 'failed',
                    attempts = attempts + 1,
                    locked_by = NULL,
                    locked_until = NULL,
                    last_error = %s
                WHERE event_id = %s
                """,
                (error, str(event.event_id)),
            )
            cur.execute(
                """
                UPDATE matches
                SET status = 'callback_failed'
                WHERE match_id = %s
                """,
                (str(event.match_id),),
            )
        return True

    backoff = next_backoff_seconds(next_attempt, settings)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE callback_events
            SET status = 'pending',
                attempts = attempts + 1,
                next_attempt_at = now() + make_interval(secs => %s),
                locked_by = NULL,
                locked_until = NULL,
                last_error = %s
            WHERE event_id = %s
            """,
            (backoff, error, str(event.event_id)),
        )
    return False


def deliver_event(
    client: httpx.Client, event: CallbackEvent, settings: Settings
) -> tuple[str | None, float]:
    body = encode_payload(event.payload)
    start = time.perf_counter()
    try:
        response = client.post(
            event.callback_url,
            content=body,
            headers=build_headers(event, body),
            timeout=settings.callback_timeout_seconds,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0
        if 200 <= response.status_code < 300:
            return None, latency_ms
        return f"callback returned HTTP {response.status_code}", latency_ms
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return str(exc), latency_ms


def run_once(
    settings: Settings,
    dispatcher_id: str,
    client: httpx.Client,
    metrics: CallbackDispatcherMetrics,
) -> int:
    pool = get_pool()
    with pool.connection() as conn:
        events = claim_callback_events(conn, dispatcher_id, settings)
        conn.commit()
    metrics.record_claimed(len(events))

    for event in events:
        error, latency_ms = deliver_event(client, event, settings)
        with pool.connection() as conn:
            if error is None:
                mark_delivered(conn, event)
                metrics.record_delivered(latency_ms)
                logger.info("callback_delivered event_id=%s", event.event_id)
            else:
                final_failed = mark_failed_attempt(conn, event, settings, error[:500])
                if final_failed:
                    metrics.record_failed()
                else:
                    metrics.record_retry()
                logger.warning(
                    "callback_delivery_failed event_id=%s error=%s",
                    event.event_id,
                    error,
                )
            conn.commit()

    return len(events)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.ERROR),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dispatcher_id = resolve_dispatcher_id(settings)
    metrics = CallbackDispatcherMetrics()
    logger.info("callback_dispatcher_started dispatcher_id=%s", dispatcher_id)

    get_pool()
    start_metrics_server(
        settings.callback_metrics_host,
        settings.callback_metrics_port,
        metrics,
        dispatcher_id,
        settings.metrics_enabled,
    )
    try:
        with httpx.Client() as client:
            while True:
                loop_start = time.perf_counter()
                processed = run_once(settings, dispatcher_id, client, metrics)
                metrics.record_loop((time.perf_counter() - loop_start) * 1000.0)
                if processed == 0:
                    time.sleep(1.0)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
