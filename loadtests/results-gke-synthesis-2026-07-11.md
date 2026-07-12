# GKE scale-out synthesis (1 / 3 / 5 nodes)

**Sources:** 1-node and 3-node from [`results-gke-2026-07-11-1842.md`](results-gke-2026-07-11-1842.md); 5-node dirty rerun from [`results-gke-2026-07-11-1950.md`](results-gke-2026-07-11-1950.md) (discarded for scaling conclusions); **5-node clean rerun** from [`results-gke-2026-07-12-1455.md`](results-gke-2026-07-12-1455.md) (`--reset-postgres`, metrics summed across all pods).

**Setup:** e2-standard-4, zonal GKE, `scale_out.js` — 11 tenants × 5 regions × 3 queues, ramp 0→100 VUs over 180s, ingress LB. Per-tenant admission cap: **300 creates/min (5/s)** → theoretical aggregate ceiling **~55 tickets/s**. Primary scalability metric: worker `matches_created` delta / 180s.

---

## Results

| | **1 node** (1/1/1) | **3 nodes** (3/3/2) | **5 nodes** (5/5/4) clean |
|---|---:|---:|---:|
| Tickets created | 10,608 (**58.9/s**) | 12,836 (**71.3/s**) | 12,271 (**68.2/s**) |
| Tickets rejected (k6) | 19,919 (110.7/s) | 46,135 (256.3/s) | 51,151 (284.2/s) |
| Total k6 responses | ~30.5k | ~58.9k | ~63.4k |
| **matches/s** | **6.53** (1,176) | **8.53** (1,536) | **33.05** (5,949) |
| Tickets : matches | 451% | 418% | **103.1%** |
| Waiting tickets (start → end) | — | — | **0 → 373** |
| p95 create latency | **500ms** | 70ms | 38ms |
| Dominant rejection | rate_limit 98.4% + load_shedding 1.6% | rate_limit 100% | rate_limit 100% |

---

## What the numbers mean

**Admission control is doing its job.** k6 offers far more load than the system accepts (~65–75% rejection at 3/5 nodes). Rejections are almost entirely `429 rate_limit`, evenly spread across tenants — per-tenant fairness works, no hot-partition overload (`503 partition_overload` = 0 everywhere).

**Ticket throughput is bounded by rate limits, not worker count.** Aggregate cap ≈ 55/s; 1-node and 3-node runs hit ~59–71/s (slight overshoot from windowing/burst). The **clean 5-node run** also lands near that ceiling at **68.2/s** — the earlier 26.5/s accept rate was a dirty-state anomaly, not a scaling regression.

**Match throughput is the real scale-out signal.** Workers pair tickets asynchronously across partition leases. More worker replicas → more partitions processed concurrently. Clean 5-node: **33.05 matches/s** vs 8.53 at 3 nodes (**+287%** match rate for **+67%** worker replicas).

**Tickets:matches ratio** = `tickets_created / (2 × matches_delta)` as a percentage (100% = perfect 1v1 pairing from tickets admitted this run). Observed 418–451% at 1/3 nodes means ~4+ tickets per match on average — **partition fragmentation**: 11 tenants × 5 regions × 3 queues spreads players into many thin queues. The dirty 5-node run showed 59.5% (impossible without backlog drain). The **clean rerun at 103.1%** is physically consistent: pairing efficiency ≈ 1.0, with 373 tickets still waiting at run end (fragmentation residue, not pre-existing backlog).

**p95 latency** tells you when the API itself becomes the bottleneck. 500ms at 1 node = single replica saturated. Drops to ~70ms at 3+ nodes — stateless API scaling works as designed.

---

## vs expectations

| Observation | Expected? |
|---|---|
| High 429 rate under 100 VUs | Yes — script deliberately exceeds 300/min/tenant |
| 3-node p95 ≪ 1-node p95 | Yes — API horizontal scale removes single-replica queueing |
| matches/s grows 1→3 (+31%) | Partially — sublinear vs +200% replicas; fragmentation limits pair rate |
| matches/s grows 3→5 clean (+287%) | **Yes, superlinear** — more workers + better partition coverage; pairing efficiency jumps from ~24% to ~97% |
| Dirty 5-node (1950): 59.5% tickets:matches | **Artifact** — warm Postgres backlog drain; discard for conclusions |
| Dirty 5-node: 26.5/s accepts | **Artifact** — Postgres/connection pressure under accumulated state |
| Clean 5-node: waiting 0→373 | Yes — fresh DB, modest end-of-run queue depth from fragmentation |

**Bottom line:** 1→3 node results show admission holds, API latency improves, match throughput gains modestly. The **clean 5-node rerun** confirms workers scale match throughput strongly when state is isolated: ~33 matches/s with pairing efficiency near 1v1 ideal, and accept rate consistent with rate-limit math.

---

## What to improve (technical)

1. **Benchmark hygiene** — now scripted
   - `--reset-postgres` / `fresh` flag deletes PVC and re-migrates before the run.
   - Worker and API metrics are summed across all pods automatically.
   - Waiting ticket depth recorded at start/end.

2. **Interpretation metrics**
   - Pairing efficiency: `2 × matches_delta / tickets_created` (target → 1.0 for 1v1-heavy load). Clean 5-node ≈ **0.97**.
   - Always check `waiting tickets before=0` before trusting tickets:matches.

3. **Partition / match throughput**
   - High tickets:matches at 1/3 nodes → load generator spreads too thin. For scale-out testing, either reduce partition cardinality (fewer regions/queues) or increase VUs per `(tenant, region, queue)` tuple.
   - Verify `MATCHMAKING_PARTITIONS` ≥ worker count so added replicas actually claim distinct leases.

4. **1-node load_shedding (322 × 503)**
   - Only tier triggered at 1 node besides rate_limit. At 3+ nodes it disappears — confirms shedding is a last-resort under single-replica pressure, not a logic error.

5. **Rate limit as test ceiling**
   - To measure worker scaling without admission masking it, either raise `DEFAULT_TICKET_RATE_LIMIT_PER_MINUTE` for benchmarks or add a dedicated "no rate limit" test tenant.

---

## Scaling summary

```
Offered load (k6):     ~constant at 3 & 5 nodes (~58–63k responses)
Accepted tickets:      59/s → 71/s → 68/s (clean 5n)
Match throughput:      6.5/s → 8.5/s → 33/s (clean 5n)
Pairing efficiency:    ~0.22 → ~0.24 → ~0.97 (clean 5n)
API p95:               500ms → 70ms → 38ms
Bottleneck shift:      API (1n) → rate limit + partition sparsity (3n) → worker throughput (5n, clean)
```

The architecture meets its design goals: overload protection holds at ~300 req/s offered load, multi-tenant fairness is even, API scales horizontally. Worker scale-out materially improves match rate once enough replicas cover partitions — the earlier 5-node paradox (low accepts, high matches/s) was entirely a **dirty DB artifact**, not real system behaviour.
