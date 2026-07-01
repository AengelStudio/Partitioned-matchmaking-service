from dataclasses import dataclass, field


@dataclass
class WorkerMetrics:
    matches_created: int = 0
    leases_claimed: int = 0
    reservations_expired: int = 0
    loop_duration_ms: float = 0.0
    loops_completed: int = 0

    def record_match(self) -> None:
        self.matches_created += 1

    def record_leases_claimed(self, count: int) -> None:
        self.leases_claimed += count

    def record_reservations_expired(self, count: int) -> None:
        self.reservations_expired += count

    def record_loop(self, duration_ms: float) -> None:
        self.loop_duration_ms = duration_ms
        self.loops_completed += 1

    def format_prometheus(self) -> str:
        lines = [
            f"pms_worker_matches_created_total {self.matches_created}",
            f"pms_worker_leases_claimed_total {self.leases_claimed}",
            f"pms_worker_reservations_expired_total {self.reservations_expired}",
            f"pms_worker_loop_duration_ms {self.loop_duration_ms:.2f}",
            f"pms_worker_loops_completed_total {self.loops_completed}",
        ]
        return "\n".join(lines) + "\n"
