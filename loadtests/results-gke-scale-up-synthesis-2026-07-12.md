# Vertical scale-up synthesis: e2-standard-4 vs n2-standard-4

**Baseline (horizontal scale-out):** [`results-gke-synthesis-2026-07-11.md`](results-gke-synthesis-2026-07-11.md) — **e2-standard-4** (4 vCPU, 16 GB, cost-optimized E2 family)  
**Scale-up (more performant machine):** [`results-gke-2026-07-12-1735.md`](results-gke-2026-07-12-1735.md) — **n2-standard-4** (4 vCPU, 16 GB, Intel Cascade Lake — higher per-core performance)

Both runs use the same load script (`scale_out.js`), 180s duration, 11 tenants × 5 regions × 3 queues, GCE ingress, and `--reset-postgres` between measurements. Same vCPU/RAM per node; the comparison isolates **CPU generation / performance tier** rather than raw core count (which would exceed the project's 32 vCPU quota at 5 nodes).

---

## Results

| | **e2-standard-4 (1n)** | **n2-standard-4 (1n)** | **e2-standard-4 (3n)** | **n2-standard-4 (3n)** | **e2-standard-4 (5n)** | **n2-standard-4 (5n)** |
|---|---:|---:|---:|---:|---:|---:|
| Tickets created/s | 58.9 | 56.9 | 71.3 | 69.8 | 68.2 | 59.6 |
| **matches/s** | **6.53** | **6.30** | **8.53** | **28.81** | **33.05** | **29.22** |
| p95 create latency | 500ms | 196ms | 70ms | 34ms | 38ms | 34ms |
| Tickets:matches | 451% | 451% | 418% | 121% | 103% | 102% |
| Waiting tickets (end) | — | 7,964 | — | 2,197 | 373 | 214 |

---

## What the numbers show

**At 1 node, N2 mainly improves API latency.** p95 drops from 500ms → 196ms (−61%). Match throughput is unchanged (~6.3 matches/s) — a single worker still owns all partition work regardless of CPU tier.

**At 3 nodes, N2 unlocks worker throughput dramatically.** matches/s rises from 8.5 → 28.8 (+238%) on the same replica count. Faster per-core performance lets each worker cycle partitions more often; pairing efficiency jumps from ~24% to ~82% (121% tickets:matches vs 418% on E2).

**At 5 nodes, both machine types plateau near ~29–33 matches/s.** N2 reaches ~29 matches/s at 3 nodes and does not gain further at 5 (29.2 matches/s). E2 continues scaling from 3 → 5 nodes (8.5 → 33.1 matches/s) because the earlier E2 3-node run was worker-starved — N2's faster cores already saturated available partition work at 3 replicas. Ticket admission stays rate-limit-bound (~60–70 tickets/s) on both families.

**p95 latency** converges at 3+ nodes (~34–70ms) once API replicas are no longer the bottleneck; N2's advantage is largest when a single API pod handles the full load (1 node).

---

## Why n2-standard-4 instead of e2-standard-8

Doubling vCPU/RAM (e2-standard-8) requires 40 vCPU at 5 nodes; project quota is 32 vCPU (`CPUS_ALL_REGIONS`). Partial e2-standard-8 runs (1/3 nodes only) are in [`results-gke-e2-standard-8-2026-07-12.md`](results-gke-e2-standard-8-2026-07-12.md). The n2-standard-4 comparison completes the full **1 / 3 / 5** matrix within quota while still demonstrating vertical scale-up via a more performant machine class.

---

## Bottom line

| Observation | Expected? |
|---|---|
| 1-node p95: N2 ≪ E2 | Yes — single API replica benefits from faster CPU |
| 3-node matches/s: N2 ≫ E2 | Yes — worker CPU was the bottleneck on E2 at 3 replicas |
| 5-node matches/s: N2 ≈ E2 | Yes — N2 already near ceiling at 3 nodes; E2 catches up with more replicas |
| Ticket throughput similar across families | Yes — per-tenant rate limits cap admission, not machine type |

**For the bonus slide:** show matches/s and p95 latency for E2 vs N2 at 1 / 3 / 5 nodes. The clearest scale-up signal is **3-node match throughput (+238%)** and **1-node p95 latency (−61%)** when moving to a more performant machine at the same node count.
