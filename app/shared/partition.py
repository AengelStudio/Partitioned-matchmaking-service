import hashlib


def compute_partition_id(
    tenant_id: str, region: str, queue_name: str, partition_count: int
) -> int:
    """Map a (tenant, region, queue) tuple to a stable logical partition.

    Uses a stable hash (not Python's salted hash) so API and worker agree
    across processes and restarts.
    """
    key = f"{tenant_id}:{region}:{queue_name}".encode()
    digest = hashlib.sha256(key).digest()
    value = int.from_bytes(digest[:8], "big")
    return value % partition_count
