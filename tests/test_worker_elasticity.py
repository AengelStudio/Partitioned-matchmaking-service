import math

from app.worker.leases import fair_partition_cap


def test_fair_partition_cap_single_worker() -> None:
    assert fair_partition_cap(128, 1) == 128


def test_fair_partition_cap_even_split() -> None:
    assert fair_partition_cap(128, 4) == 32


def test_fair_partition_cap_rounds_up() -> None:
    assert fair_partition_cap(128, 5) == math.ceil(128 / 5)


def test_fair_partition_cap_handles_zero_workers() -> None:
    assert fair_partition_cap(128, 0) == 128


def test_fair_partition_cap_handles_zero_partitions() -> None:
    assert fair_partition_cap(0, 3) == 0
