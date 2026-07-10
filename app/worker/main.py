import logging
import socket
import time

from app.config import Settings, get_settings
from app.db.connection import close_pool, get_pool
from app.worker import leases, matching, scheduling
from app.worker.http import start_metrics_server
from app.worker.metrics import WorkerMetrics
from app.worker.scheduling import WorkerSchedule

logger = logging.getLogger(__name__)


def resolve_worker_id(settings: Settings) -> str:
    if settings.worker_id and settings.worker_id != "worker-local-1":
        return settings.worker_id
    return socket.gethostname()


def reservation_seconds(settings: Settings) -> int:
    return max(5, settings.worker_lease_seconds // 2)


def run_loop(
    settings: Settings,
    worker_id: str,
    metrics: WorkerMetrics,
    schedule: WorkerSchedule,
) -> None:
    pool = get_pool()
    lease_batch = settings.worker_partition_batch_size
    ticket_batch = settings.worker_ticket_batch_size
    reservation_ttl = reservation_seconds(settings)
    loop_start = time.perf_counter()

    if schedule.renew_delay_seconds > 0:
        time.sleep(schedule.renew_delay_seconds)

    with pool.connection() as conn:
        lease_ops_start = time.perf_counter()
        try:
            renewed = leases.renew_owned_partitions(
                conn, worker_id, settings.worker_lease_seconds
            )
            newly_claimed = leases.claim_available_partitions(
                conn,
                worker_id,
                settings.worker_lease_seconds,
                lease_batch,
                settings.matchmaking_partitions,
                schedule.partition_offset,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            metrics.record_lease_claim_failure()
            logger.exception("worker_lease_ops_failed worker_id=%s", worker_id)
            raise
        metrics.lease_ops_ms.record((time.perf_counter() - lease_ops_start) * 1000.0)
        metrics.record_leases_renewed(renewed)
        metrics.record_leases_claimed(len(newly_claimed))

        expired = matching.cleanup_expired_reservations(conn)
        metrics.record_reservations_expired(expired)
        conn.commit()

        owned_partitions = leases.list_owned_partitions(conn, worker_id)
        metrics.record_owned_partitions(len(owned_partitions))

        fetch_start = time.perf_counter()
        tickets = matching.fetch_waiting_tickets(
            conn, owned_partitions, ticket_batch, settings.worker_freshness_bias
        )
        metrics.ticket_fetch_ms.record((time.perf_counter() - fetch_start) * 1000.0)

        pair_search_start = time.perf_counter()
        pairs = matching.find_pairs(tickets, settings)
        metrics.pair_search_ms.record(
            (time.perf_counter() - pair_search_start) * 1000.0
        )
        metrics.record_pair_search_run()

        wait_times = [matching.wait_seconds(ticket.created_at) for ticket in tickets]
        max_ticket_wait_s = max(wait_times, default=0.0)
        avg_ticket_wait_s = sum(wait_times) / len(wait_times) if wait_times else 0.0

        matches_created_this_loop = 0
        processed_pairs = 0
        budget_exceeded = False
        match_creation_start = time.perf_counter()

        for first, second in pairs:
            if matches_created_this_loop >= settings.worker_max_pairs_per_loop:
                break
            elapsed_ms = (time.perf_counter() - loop_start) * 1000.0
            if elapsed_ms >= settings.worker_loop_budget_ms:
                budget_exceeded = True
                break

            processed_pairs += 1
            try:
                created = matching.create_match_from_pair(
                    conn, first, second, worker_id, reservation_ttl
                )
            except Exception:
                metrics.record_match_failure()
                metrics.record_rollback()
                conn.rollback()
                continue

            if created:
                metrics.record_match()
                matches_created_this_loop += 1
                conn.commit()
            else:
                metrics.record_match_failure()
                metrics.record_rollback()
                conn.rollback()

        metrics.match_creation_ms.record(
            (time.perf_counter() - match_creation_start) * 1000.0
        )

        metrics.record_loop_stats(
            tickets_fetched=len(tickets),
            pairs_found=len(pairs),
            matches_created=matches_created_this_loop,
            pairs_skipped=len(pairs) - processed_pairs,
            max_ticket_wait_s=max_ticket_wait_s,
            avg_ticket_wait_s=avg_ticket_wait_s,
            budget_exceeded=budget_exceeded,
        )

        if settings.metrics_enabled and (budget_exceeded or len(pairs) - processed_pairs > 0):
            logger.info(
                "worker_backlog_loop worker_id=%s tickets_fetched=%d pairs_found=%d "
                "matches_created=%d pairs_skipped=%d max_ticket_wait_s=%.2f "
                "avg_ticket_wait_s=%.2f budget_exceeded=%s owned_partitions=%d "
                "lease_ops_ms=%.2f ticket_fetch_ms=%.2f pair_search_ms=%.2f "
                "match_creation_ms=%.2f",
                worker_id,
                len(tickets),
                len(pairs),
                matches_created_this_loop,
                len(pairs) - processed_pairs,
                max_ticket_wait_s,
                avg_ticket_wait_s,
                budget_exceeded,
                len(owned_partitions),
                metrics.lease_ops_ms.last_ms,
                metrics.ticket_fetch_ms.last_ms,
                metrics.pair_search_ms.last_ms,
                metrics.match_creation_ms.last_ms,
            )


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
    schedule = scheduling.build_schedule(
        worker_id,
        settings.worker_loop_interval_ms,
        settings.worker_loop_jitter_pct,
        settings.worker_lease_renew_jitter_pct,
        settings.matchmaking_partitions,
    )

    logger.info(
        "worker_started worker_id=%s loop_interval_ms=%d partition_offset=%d "
        "renew_delay_ms=%d",
        worker_id,
        schedule.loop_interval_ms,
        schedule.partition_offset,
        schedule.renew_delay_ms,
    )
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
                run_loop(settings, worker_id, metrics, schedule)
            except Exception:
                logger.exception("worker_loop_failed worker_id=%s", worker_id)

            metrics.record_loop((time.perf_counter() - loop_start) * 1000.0)
            metrics.record_jittered_sleep(schedule.loop_interval_ms)

            if settings.metrics_enabled and metrics.loops_completed % 10 == 0:
                logger.info(
                    "worker_metrics %s",
                    metrics.format_prometheus(worker_id).strip(),
                )

            logger.debug(
                "worker_loop_sleep worker_id=%s jittered_sleep_ms=%d "
                "next_loop_at_ms=%.0f",
                worker_id,
                schedule.loop_interval_ms,
                (time.perf_counter() + schedule.loop_interval_seconds) * 1000.0,
            )
            time.sleep(schedule.loop_interval_seconds)
    finally:
        shutdown_worker(worker_id)


if __name__ == "__main__":
    main()
