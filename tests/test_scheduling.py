from app.worker.scheduling import (
    build_schedule,
    deterministic_jitter_ms,
    deterministic_renew_delay_ms,
    jittered_loop_interval_ms,
    partition_claim_offset,
)


def test_loop_jitter_is_deterministic_per_worker() -> None:
    a = jittered_loop_interval_ms("worker-a", 500, 0.25)
    b = jittered_loop_interval_ms("worker-a", 500, 0.25)
    c = jittered_loop_interval_ms("worker-b", 500, 0.25)
    assert a == b
    assert a != c or c != 500


def test_loop_jitter_stays_within_bounds() -> None:
    base_ms = 500
    jitter_pct = 0.25
    min_ms = int(base_ms * (1.0 - jitter_pct))
    for worker_id in ("worker-1", "worker-2", "worker-3", "pod-abc-xyz"):
        interval = jittered_loop_interval_ms(worker_id, base_ms, jitter_pct)
        assert min_ms <= interval <= base_ms + int(base_ms * jitter_pct)


def test_renew_delay_is_bounded_and_safe_for_lease_ttl() -> None:
    lease_seconds = 15
    base_ms = 500
    jitter_pct = 0.1
    for worker_id in ("worker-1", "worker-2", "worker-3"):
        delay_ms = deterministic_renew_delay_ms(worker_id, base_ms, jitter_pct)
        assert 0 <= delay_ms <= base_ms * jitter_pct
        assert delay_ms < lease_seconds * 1000


def test_partition_claim_offset_within_range() -> None:
    partition_count = 128
    offsets = {
        partition_claim_offset(f"worker-{i}", partition_count) for i in range(20)
    }
    assert all(0 <= offset < partition_count for offset in offsets)
    assert len(offsets) > 1


def test_build_schedule_packages_all_fields() -> None:
    schedule = build_schedule("worker-test", 500, 0.25, 0.1, 128)
    assert schedule.worker_id == "worker-test"
    assert schedule.loop_interval_ms > 0
    assert 0 <= schedule.partition_offset < 128
    assert schedule.renew_delay_ms >= 0
    assert schedule.loop_interval_seconds == schedule.loop_interval_ms / 1000.0


def test_jitter_offset_sign_can_be_negative_or_positive() -> None:
    offsets = {deterministic_jitter_ms(f"w-{i}", 500, 0.25) for i in range(50)}
    assert any(offset < 0 for offset in offsets)
    assert any(offset > 0 for offset in offsets)
