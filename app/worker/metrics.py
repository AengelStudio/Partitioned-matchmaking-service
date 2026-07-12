from dataclasses import dataclass, field


@dataclass
class StageTimer:
    last_ms: float = 0.0
    total_ms: float = 0.0
    count: int = 0

    def record(self, duration_ms: float) -> None:
        self.last_ms = duration_ms
        self.total_ms += duration_ms
        self.count += 1


@dataclass
class WorkerMetrics:
    matches_created: int = 0
    matches_failed: int = 0
    leases_claimed: int = 0
    leases_renewed: int = 0
    partitions_released: int = 0
    lease_claim_failures: int = 0
    reservations_expired: int = 0
    pair_search_runs: int = 0
    rollbacks: int = 0
    loop_duration_ms: float = 0.0
    loops_completed: int = 0

    tickets_fetched_total: int = 0
    pairs_found_total: int = 0
    pairs_skipped_total: int = 0
    loop_budget_exceeded_total: int = 0

    tickets_fetched_last_loop: int = 0
    pairs_found_last_loop: int = 0
    matches_created_last_loop: int = 0
    max_ticket_wait_seconds: float = 0.0
    avg_ticket_wait_seconds: float = 0.0
    jittered_sleep_ms: float = 0.0
    owned_partitions_count: int = 0

    lease_ops_ms: StageTimer = field(default_factory=StageTimer)
    ticket_fetch_ms: StageTimer = field(default_factory=StageTimer)
    pair_search_ms: StageTimer = field(default_factory=StageTimer)
    match_creation_ms: StageTimer = field(default_factory=StageTimer)

    def record_match(self) -> None:
        self.matches_created += 1

    def record_match_failure(self) -> None:
        self.matches_failed += 1

    def record_rollback(self) -> None:
        self.rollbacks += 1

    def record_leases_claimed(self, count: int) -> None:
        self.leases_claimed += count

    def record_leases_renewed(self, count: int) -> None:
        self.leases_renewed += count

    def record_partitions_released(self, count: int) -> None:
        self.partitions_released += count

    def record_lease_claim_failure(self) -> None:
        self.lease_claim_failures += 1

    def record_reservations_expired(self, count: int) -> None:
        self.reservations_expired += count

    def record_pair_search_run(self) -> None:
        self.pair_search_runs += 1

    def record_owned_partitions(self, count: int) -> None:
        self.owned_partitions_count = count

    def record_loop(self, duration_ms: float) -> None:
        self.loop_duration_ms = duration_ms
        self.loops_completed += 1

    def record_jittered_sleep(self, sleep_ms: float) -> None:
        self.jittered_sleep_ms = sleep_ms

    def record_loop_stats(
        self,
        tickets_fetched: int,
        pairs_found: int,
        matches_created: int,
        pairs_skipped: int,
        max_ticket_wait_s: float,
        avg_ticket_wait_s: float,
        budget_exceeded: bool,
    ) -> None:
        self.tickets_fetched_last_loop = tickets_fetched
        self.pairs_found_last_loop = pairs_found
        self.matches_created_last_loop = matches_created
        self.max_ticket_wait_seconds = max_ticket_wait_s
        self.avg_ticket_wait_seconds = avg_ticket_wait_s

        self.tickets_fetched_total += tickets_fetched
        self.pairs_found_total += pairs_found
        self.pairs_skipped_total += pairs_skipped
        if budget_exceeded:
            self.loop_budget_exceeded_total += 1

    def format_prometheus(self, worker_id: str) -> str:
        labels = f'worker_id="{worker_id}"'
        lines = [
            f"pms_worker_info{{{labels}}} 1",
            f"pms_worker_matches_created_total{{{labels}}} {self.matches_created}",
            f"pms_worker_matches_failed_total{{{labels}}} {self.matches_failed}",
            f"pms_worker_rollbacks_total{{{labels}}} {self.rollbacks}",
            f"pms_worker_leases_claimed_total{{{labels}}} {self.leases_claimed}",
            f"pms_worker_leases_renewed_total{{{labels}}} {self.leases_renewed}",
            f"pms_worker_partitions_released_total{{{labels}}} {self.partitions_released}",
            f"pms_worker_lease_claim_failures_total{{{labels}}} {self.lease_claim_failures}",
            f"pms_worker_reservations_expired_total{{{labels}}} {self.reservations_expired}",
            f"pms_worker_reservations_cleaned_total{{{labels}}} {self.reservations_expired}",
            f"pms_worker_pair_search_runs_total{{{labels}}} {self.pair_search_runs}",
            f"pms_worker_loop_duration_ms{{{labels}}} {self.loop_duration_ms:.2f}",
            f"pms_worker_loops_completed_total{{{labels}}} {self.loops_completed}",
            f"pms_worker_tickets_fetched_total{{{labels}}} {self.tickets_fetched_total}",
            f"pms_worker_pairs_found_total{{{labels}}} {self.pairs_found_total}",
            f"pms_worker_pairs_skipped_total{{{labels}}} {self.pairs_skipped_total}",
            f"pms_worker_loop_budget_exceeded_total{{{labels}}} {self.loop_budget_exceeded_total}",
            f"pms_worker_owned_partitions_count{{{labels}}} {self.owned_partitions_count}",
            f"pms_worker_tickets_fetched_last_loop{{{labels}}} {self.tickets_fetched_last_loop}",
            f"pms_worker_pairs_found_last_loop{{{labels}}} {self.pairs_found_last_loop}",
            f"pms_worker_matches_created_last_loop{{{labels}}} {self.matches_created_last_loop}",
            f"pms_worker_max_ticket_wait_seconds{{{labels}}} {self.max_ticket_wait_seconds:.2f}",
            f"pms_worker_avg_ticket_wait_seconds{{{labels}}} {self.avg_ticket_wait_seconds:.2f}",
            f"pms_worker_jittered_sleep_ms{{{labels}}} {self.jittered_sleep_ms:.2f}",
        ]
        for name, timer in (
            ("lease_ops_ms", self.lease_ops_ms),
            ("ticket_fetch_ms", self.ticket_fetch_ms),
            ("pair_search_ms", self.pair_search_ms),
            ("match_creation_ms", self.match_creation_ms),
        ):
            lines.append(f"pms_worker_{name}{{{labels}}} {timer.last_ms:.2f}")
            lines.append(f"pms_worker_{name}_sum{{{labels}}} {timer.total_ms:.2f}")
            lines.append(f"pms_worker_{name}_count{{{labels}}} {timer.count}")
        return "\n".join(lines) + "\n"
