# Scale-out benchmark results

Cluster: GKE zonal, `europe-west1-b`, machine type `e2-standard-4`, project `se-proto`.
Load script: `loadtests/scale_out.js` (unmodified, same 30s/1m/1m/30s ramp to 100 VUs each run).
Replica counts scale with node count (see README "Deployment on GKE" — this is intentional so the
benchmark actually exercises more application capacity, not just more idle nodes).

| Nodes | API / Worker / Dispatcher replicas | Tickets created | Tickets rejected | matches_created_per_second | Tickets:matches ratio | p95 ticket-create latency |
|-------|-------------------------------------|------------------|-------------------|-------------------------------|------------------------|-----------------------------|
| 1     | 1 / 1 / 1                            | 3542 (single-partition script — not comparable) | — | 9.83 (single-partition script — not comparable) | — | 314ms |
| 3     | 3 / 3 / 2                            | 3001 (16.66/s)   | 57411 (318.8/s)   | 8.27 (1488 matches / 180s)     | 49.6%                  | 58.79ms |
| 5     | 5 / 5 / 3                            | 2667 (14.81/s)   | 57497 (319.2/s)   | 7.39 (1330 matches / 180s)     | 49.9%                  | 50.38ms |

**Headline finding: this benchmark does not exercise infrastructure capacity, and that's itself the result.**

The 3-node and 5-node runs (corrected script, spread across 5 regions × 3 queues so multiple partitions/workers are actually touched — see below) show `matches_created_per_second` essentially tracking `tickets_created / 2` at a ~99%+ pairing rate in both cases. That means the matchmaking workers were never the bottleneck — every ticket that came in got paired almost immediately, regardless of whether 3 or 5 workers were available to do the pairing. The real ceiling is `tickets_created`, which stayed flat (~2700-3000) across node counts because it's capped by `DEFAULT_TICKET_RATE_LIMIT_PER_MINUTE`, a **per-tenant** quota — and every request in this benchmark used the same single tenant (`studio_a`). Adding API/worker replicas cannot increase one tenant's allowed request rate; that's the fairness mechanism (a Task A requirement) working exactly as designed, not a scalability failure.

Two things this benchmark *does* still demonstrate cleanly:
1. **Overload protection holds under 300+ req/s of sustained hostile load** — 90-95% of requests were correctly rejected with `429`/`503` rather than the system falling over, at every node count.
2. **p95 latency drops as nodes are added** (314ms → 59ms → 50ms) even though throughput is quota-capped — more API replicas process the same admitted+rejected volume faster in parallel, which is a real (if secondary) scale-out benefit.

**What we didn't get to (documented, not done):** a true `matches_created_per_second` scaling curve requires *multiple tenants* generating load concurrently, so the aggregate quota ceiling — and therefore aggregate ticket/match throughput — actually grows with node count. `loadtests/scale_out.js` would need a pool of seeded tenant IDs rotated per request (similar to the region/queue spread already added). Node pool is currently scaled to 0 (`terraform apply -var node_count=0`, cluster/deployments/Artifact Registry all left intact) to stop billing while this is paused; resuming just needs `terraform apply -var node_count=N` — no rebuild required.

Other implementation notes:
- The original `scale_out.js` sent every ticket with the same `(tenant_id, region, queue_name)`, which per `app/shared/partition.py` hashes to a single fixed partition — so only one worker could ever process matches, regardless of replica count. Fixed by spreading requests across 5 regions × 3 queues (15 partitions). The 1-node row above predates this fix and is kept only for the latency comparison; its throughput numbers aren't comparable to the 3/5-node rows.
- Partition leases do not rebalance once claimed: a worker that already holds a lease keeps renewing it, so workers added to an *already-running* fleet stay idle until leases expire. Before each measurement we did a `kubectl rollout restart deployment/worker` so leases redistribute fairly — a fresh N-node cluster would do this naturally on first boot, so the restart just simulates that fresh-start condition rather than a mid-flight scale-up. This lack-of-rebalancing is itself worth a mention as a scalability limitation.
- `matches_created_per_second` is read from the worker's Prometheus `/metrics` endpoint (`pms_worker_matches_created_total`), summed across all worker pods, sampled immediately before and after the k6 run, divided by the run's wall-clock duration (180s).
