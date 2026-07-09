import zlib
from dataclasses import dataclass


def _stable_fraction(key: str) -> float:
    digest = zlib.crc32(key.encode("utf-8"))
    return (digest % 1_000_000) / 1_000_000.0


def deterministic_jitter_ms(worker_id: str, base_ms: int, jitter_pct: float) -> int:
    fraction = _stable_fraction(f"{worker_id}:loop-jitter")
    span_ms = base_ms * jitter_pct
    offset_ms = (fraction * 2.0 - 1.0) * span_ms
    return int(round(offset_ms))


def jittered_loop_interval_ms(worker_id: str, base_ms: int, jitter_pct: float) -> int:
    offset_ms = deterministic_jitter_ms(worker_id, base_ms, jitter_pct)
    min_interval_ms = max(1, int(base_ms * (1.0 - jitter_pct)))
    return max(min_interval_ms, base_ms + offset_ms)


def deterministic_renew_delay_ms(worker_id: str, base_ms: int, jitter_pct: float) -> int:
    fraction = _stable_fraction(f"{worker_id}:renew-jitter")
    span_ms = base_ms * jitter_pct
    return max(0, int(round(fraction * span_ms)))


def partition_claim_offset(worker_id: str, partition_count: int) -> int:
    if partition_count <= 0:
        return 0
    digest = zlib.crc32(f"{worker_id}:partition-offset".encode("utf-8"))
    return digest % partition_count


@dataclass(frozen=True)
class WorkerSchedule:
    worker_id: str
    loop_interval_ms: int
    partition_offset: int
    renew_delay_ms: int

    @property
    def loop_interval_seconds(self) -> float:
        return self.loop_interval_ms / 1000.0

    @property
    def renew_delay_seconds(self) -> float:
        return self.renew_delay_ms / 1000.0


def build_schedule(
    worker_id: str,
    base_interval_ms: int,
    loop_jitter_pct: float,
    renew_jitter_pct: float,
    partition_count: int,
) -> WorkerSchedule:
    return WorkerSchedule(
        worker_id=worker_id,
        loop_interval_ms=jittered_loop_interval_ms(
            worker_id, base_interval_ms, loop_jitter_pct
        ),
        partition_offset=partition_claim_offset(worker_id, partition_count),
        renew_delay_ms=deterministic_renew_delay_ms(
            worker_id, base_interval_ms, renew_jitter_pct
        ),
    )
