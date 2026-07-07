from prometheus_client import CollectorRegistry, Counter, Gauge

registry = CollectorRegistry()

tickets_created_total = Counter(
    "tickets_created_total",
    "Total number of tickets created",
    ["tenant_id", "region", "queue_name"],
    registry=registry,
)

tickets_rejected_total = Counter(
    "tickets_rejected_total",
    "Total number of tickets rejected by admission control",
    ["tenant_id", "reason"],
    registry=registry,
)

tickets_cancelled_total = Counter(
    "tickets_cancelled_total",
    "Total number of tickets cancelled",
    ["tenant_id"],
    registry=registry,
)

active_tickets = Gauge(
    "active_tickets",
    "Number of tickets currently active (waiting)",
    ["tenant_id", "partition_id"],
    registry=registry,
)
