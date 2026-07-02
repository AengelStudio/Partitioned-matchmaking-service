import logging
import socket
import time

from app.config import Settings, get_settings
from app.db.connection import close_pool, get_pool
from app.worker import leases, matching
from app.worker.http import start_metrics_server
from app.worker.metrics import WorkerMetrics

logger = logging.getLogger(__name__)


def resolve_worker_id(settings: Settings) -> str:
    if settings.worker_id and settings.worker_id != "worker-local-1":
        return settings.worker_id
    return socket.gethostname()


def reservation_seconds(settings: Settings) -> int:
    return max(5, settings.worker_lease_seconds // 2)


def run_loop(
    settings: Settings, worker_id: str, metrics: WorkerMetrics
) -> None:
    pool = get_pool()
    lease_batch = settings.worker_partition_batch_size
    ticket_batch = settings.worker_ticket_batch_size
    reservation_ttl = reservation_seconds(settings)

    with pool.connection() as conn:
        leases.renew_owned_partitions(
            conn, worker_id, settings.worker_lease_seconds
        )
        newly_claimed = leases.claim_available_partitions(
            conn,
            worker_id,
            settings.worker_lease_seconds,
            lease_batch,
        )
        metrics.record_leases_claimed(len(newly_claimed))
        conn.commit()

        expired = matching.cleanup_expired_reservations(conn)
        metrics.record_reservations_expired(expired)
        conn.commit()

        owned_partitions = leases.list_owned_partitions(conn, worker_id)
        tickets = matching.fetch_waiting_tickets(
            conn, owned_partitions, ticket_batch
        )
        pairs = matching.find_pairs(tickets, settings)

        for first, second in pairs:
            if matching.create_match_from_pair(
                conn, first, second, worker_id, reservation_ttl
            ):
                metrics.record_match()
                conn.commit()
            else:
                conn.rollback()


def shutdown_worker(worker_id: str) -> None:
    try:
        pool = get_pool()
        with pool.connection() as conn:
            leases.release_owned_partitions(conn, worker_id)
            conn.commit()
    except Exception:
        logger.exception("worker_shutdown_release_failed worker_id=%s", worker_id)
    finally:
        close_pool()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.ERROR),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    worker_id = resolve_worker_id(settings)
    metrics = WorkerMetrics()
    interval = settings.worker_loop_interval_ms / 1000.0

    logger.info("worker_started worker_id=%s", worker_id)
    get_pool()
    start_metrics_server(
        settings.worker_metrics_host,
        settings.worker_metrics_port,
        metrics,
        worker_id,
        settings.metrics_enabled,
    )

    try:
        while True:
            loop_start = time.perf_counter()
            try:
                run_loop(settings, worker_id, metrics)
            except Exception:
                logger.exception("worker_loop_failed worker_id=%s", worker_id)

            metrics.record_loop((time.perf_counter() - loop_start) * 1000.0)

            if settings.metrics_enabled and metrics.loops_completed % 10 == 0:
                logger.info(
                    "worker_metrics %s",
                    metrics.format_prometheus(worker_id).strip(),
                )

            time.sleep(interval)
    finally:
        shutdown_worker(worker_id)


if __name__ == "__main__":
    main()
