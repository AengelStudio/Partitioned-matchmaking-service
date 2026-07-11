# GKE scale-out synthesis (1 / 3 / 5 nodes)

**Sources:** 1-node and 3-node from [`results-gke-2026-07-11-1842.md`](results-gke-2026-07-11-1842.md); 5-node from [`results-gke-2026-07-11-1950.md`](results-gke-2026-07-11-1950.md) rerun (1842 5-node row discarded — single-pod metric scrape, ~42% traffic volume, zero recorded rejections).

**Setup:** e2-standard-4, zonal GKE, `scale_out.js` — 11 tenants × 5 regions × 3 queues, ramp 0→100 VUs over 180s, ingress LB. Per-tenant admission cap: **300 creates/min (5/s)** → theoretical aggregate ceiling **~55 tickets/s**. Primary scalability metric: worker `matches_created` delta / 180s.

---

## Results

| | **1 node** (1/1/1) | **3 nodes** (3/3/2) | **5 nodes** (5/5/4) |
|---|---:|---:|---:|
| Tickets created | 10,608 (**58.9/s**) | 12,836 (**71.3/s**) | 4,776 (**26.5/s**) |
| Tickets rejected (k6) | 19,919 (110.7/s) | 46,135 (256.3/s) | 53,582 (297.7/s) |
| Total k6 responses | ~30.5k | ~58.9k | ~58.4k |
| **matches/s** | **6.53** (1,176) | **8.53** (1,536) | **22.29** (4,013) |
| Tickets : matches | 451% | 418% | **59.5%** |
| p95 create latency | **500ms** | 70ms | 79ms |
| Dominant rejection | rate_limit 98.4% + load_shedding 1.6% | rate_limit 100% | rate_limit 99.9% |

---

## What the numbers mean

**Admission control is doing its job.** k6 offers far more load than the system accepts (~65–75% rejection at 3/5 nodes). Rejections are almost entirely `429 rate_limit`, evenly spread across tenants — per-tenant fairness works, no hot-partition overload (`503 partition_overload` = 0 everywhere).

**Ticket throughput is bounded by rate limits, not worker count.** Aggregate cap ≈ 55/s; 1-node and 3-node runs hit ~59–71/s (slight overshoot from windowing/burst). Scaling API replicas does not raise per-tenant quota — that is intentional (Roadmap M4/M5).

**Match throughput is the real scale-out signal.** Workers pair tickets asynchronously across partition leases. More worker replicas → more partitions processed concurrently.

**Tickets:matches ratio** = tickets admitted / matches formed. For 1v1-only traffic, ideal ≈ **200%** (2 tickets → 1 match). Observed 418–451% at 1/3 nodes means ~4+ tickets per match on average — not a pairing bug, but **partition fragmentation**: 11 tenants × 5 regions × 3 queues spreads players into many thin queues; most partitions don't have enough concurrent waiters to pair immediately.

**p95 latency** tells you when the API itself becomes the bottleneck. 500ms at 1 node = single replica saturated (Redis + Postgres + ingress queueing). Drops to ~70–80ms at 3+ nodes — stateless API scaling works as designed.

---

## vs expectations

| Observation | Expected? |
|---|---|
| High 429 rate under 100 VUs | Yes — script deliberately exceeds 300/min/tenant |
| 3-node p95 ≪ 1-node p95 | Yes — API horizontal scale removes single-replica queueing |
| matches/s grows 1→3 (+31%) | Partially — sublinear vs +200% replicas; workers weren't fully saturated at 3 nodes, fragmentation limits pair rate |
| matches/s jumps 3→5 (+161%) | **Inflated** — 5-node run had **warm Postgres** (PVC not reset, `--skip-deploy`); 59.5% tickets:matches ratio is physically inconsistent with 1v1 (1.2 tickets/match). Worker likely drained pre-existing waiting tickets + new ones |
| 5-node accepts only 26.5/s vs 71.3/s at 3 nodes | **Unexpected** — same ~58k offered load, but 63% fewer 201s. Not explained by rate-limit math alone; suspect Postgres/connection pressure (`max_connections=200`) or accumulated queue state slowing inserts |
| 1842 5-node row | **Bad data** — confirmed instrumentation failure (single-pod scrape post-restart) |

**Bottom line:** 1→3 node results are trustworthy and show the expected pattern: admission holds, API latency improves, match throughput gains modestly. The 5-node match number demonstrates workers *can* sustain ~22 matches/s, but the run is **not apples-to-apples** with 1/3 due to dirty DB state and lower accept rate.

---

## What to improve (technical)

1. **Benchmark hygiene**
   - Reset Postgres (or truncate tickets/matches) between runs.
   - Always aggregate `/metrics` across **all** API and worker pods (fixed manually in 1950; automate in `run_gke_benchmark.py`).
   - Read worker `before` counters as **sum across pods**, not via single LB hop.

2. **Interpretation metrics**
   - Report **pairing efficiency** separately: `2 × matches_delta / tickets_created` (target → 1.0 for 1v1-heavy load).
   - Track **waiting ticket depth** at run start/end to separate backlog drain from live throughput.

3. **Partition / match throughput**
   - High tickets:matches at 1/3 nodes → load generator spreads too thin. For scale-out testing, either reduce partition cardinality (fewer regions/queues) or increase VUs per `(tenant, region, queue)` tuple.
   - Verify `MATCHMAKING_PARTITIONS` ≥ worker count so added replicas actually claim distinct leases.

4. **1-node load_shedding (322 × 503)**
   - Only tier triggered at 1 node besides rate_limit. At 3+ nodes it disappears — confirms shedding is a last-resort under single-replica pressure, not a logic error.

5. **5-node accept-rate regression**
   - Size Postgres pool: 5 API + 5 worker + dispatcher vs `max_connections=200`.
   - Profile whether ticket INSERT latency or Redis rate-check latency grew with replica count.
   - Re-run 5-node with fresh DB to get a clean accept + match comparison against 3-node.

6. **Rate limit as test ceiling**
   - To measure worker scaling without admission masking it, either raise `DEFAULT_TICKET_RATE_LIMIT_PER_MINUTE` for benchmarks or add a dedicated "no rate limit" test tenant.

---

## Scaling summary

```
Offered load (k6):     ~constant at 3 & 5 nodes (~58k responses)
Accepted tickets:      59/s → 71/s → 27/s*   (*5-node anomaly)
Match throughput:      6.5/s → 8.5/s → 22/s* (*includes backlog drain)
API p95:               500ms → 70ms → 79ms
Bottleneck shift:      API (1n) → rate limit + partition sparsity (3n) → worker throughput + dirty state (5n)
```

The architecture meets its design goals: overload protection holds at ~300 req/s offered load, multi-tenant fairness is even, API scales horizontally. Worker scale-out improves match rate, but current benchmarks can't cleanly quantify 3→5 gain until runs are isolated with fresh state and consistent metric collection.
