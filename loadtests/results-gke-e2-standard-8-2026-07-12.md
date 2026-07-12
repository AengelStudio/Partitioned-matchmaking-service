# GKE scale-out benchmark results — e2-standard-8 (vertical scale-up)

**Generated:** 2026-07-12 (partial — 5-node blocked by GCP quota)  
**Cluster:** GKE zonal, `europe-west1-b`, project `se-proto`  
**Machine type:** e2-standard-8 (8 vCPU, 32 GB RAM per node)  
**Load script:** `loadtests/scale_out.js` (multi-tenant)  
**Run duration:** 180s per node count  
**Access:** GCE ingress load balancer  

| Nodes | API / Worker / Dispatcher | Tickets created | Tickets rejected | matches_created/s | Tickets:matches ratio | p95 ticket-create latency |
|-------|---------------------------|-----------------|------------------|-------------------|----------------------|---------------------------|
| 1 | 1 / 1 / 1 | 9799 (54.4/s) | 32807 (182.2/s) | 5.79 (1043 matches / 180s) | 469.7% | 297.25ms |
| 3 | 3 / 3 / 2 | 12173 (67.6/s) | 50119 (278.2/s) | 30.36 (5465 matches / 180s) | 111.4% | 43.38ms |
| 5 | — | — | — | **not run** | — | — |

## Raw worker metrics

- **1 node(s):** matches before=0, after=1043, delta=1043; waiting tickets before=0, after=7713
- **3 node(s):** matches before=0, after=5465, delta=5465; waiting tickets before=0, after=1243

## Admission control rejections

From API `/metrics` after each run (`tickets_rejected_total`), summed across all API pods.

### 1 node(s)

| Reason | HTTP | Count | Share |
|--------|------|------:|------:|
| `rate_limit` | 429 | 32,699 | 99.7% |
| `load_shedding` | 503 | 108 | 0.3% |

### 3 node(s)

| Reason | HTTP | Count | Share |
|--------|------|------:|------:|
| `rate_limit` | 429 | 50,119 | 100.0% |

## Notes

- Postgres PVC reset before each run (`--reset-postgres`) for clean ticket/match state.
- Worker deployment restarted before each run so partition leases redistribute.
- Worker and API metrics are summed across all pods.
- **5-node run blocked:** `CPUS_ALL_REGIONS` quota is 32 vCPU; 5 × e2-standard-8 requires 40 vCPU. Resize fails even from a 0-node pool. Increase quota to ≥48 vCPU to complete the 5-node row, or compare using a machine type that fits 5 nodes within 32 vCPU (e.g. n2-standard-4 vs e2-standard-4 at same vCPU count).
