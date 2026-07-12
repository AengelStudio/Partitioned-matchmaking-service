# GKE elasticity load test results

**Generated:** 2026-07-12 21:34:39 W. Europe Daylight Time  
**Cluster:** GKE zonal, `europe-west1-b`, project `se-proto`  
**Machine type:** e2-standard-4 (autoscaling 1–5 nodes)  
**Load script:** `loadtests/elasticity.js` (30 → 80 → 30 VUs, ~5.5 min)  
**BASE_URL:** http://35.186.247.227  
**k6 exit code:** 0

## Setup

```bash
cd infra/terraform && terraform apply \
  -var="enable_autoscaling=true" \
  -var="machine_type=e2-standard-4"
kubectl apply -f infra/k8s/autoscaling.yaml
k6 run -e BASE_URL="http://35.186.247.227" loadtests/elasticity.js
```

Baseline before load: **1 node**, api **1/1**, worker **1/1** (HPA min replicas). Cluster had briefly scaled to 3 nodes while pending pods from the prior benchmark were scheduling; autoscaler scaled back to 1 node before the test started.

## k6 summary

| Metric | Value |
|--------|------:|
| Test duration | 5m 30s |
| Tickets created | 16,497 (49.99/s) |
| Tickets rejected | 62,277 (188.71/s) |
| p95 ticket-create latency | 228.27ms |
| HTTP requests | 114,038 (345.6/s) |
| Threshold `p(95)<2000` | ✓ pass |

Load profile: 0→30 VUs (1m) → 80 VUs (3m hold) → 30 VUs (1m) → 0 (30s).

## Elasticity timeline

| Event | Timestamp (local) | Lag from test start (~21:29:07) |
|-------|-------------------|----------------------------------|
| k6 load begins | 21:29:07 | 0s |
| HPA **api** 1 → 2 replicas | 21:29:58 | **~51s** |
| HPA **api** 2 → 4 replicas | 21:31:08 | **~2m 1s** |
| HPA **api** 4 → 5 replicas (max) | 21:32:23 | **~3m 16s** |
| HPA **worker** scale-up | — | **not triggered** (CPU stayed ~7%) |
| Cluster autoscaler node add | — | **not triggered** (pods fit on 1 node) |
| k6 load ends | 21:34:37 | 5m 30s |

Post-test state: api **5/5** (CPU 105%), worker **1/1** (CPU 7%), nodes **1**.

## Throughput (matches/s)

Worker metrics sampling via `kubectl exec … wget` failed during the run (container has no `wget`). Post-test fetch via Python exec:

| Metric | Value |
|--------|------:|
| Total matches created (1 worker) | 1,276 |
| Implied average matches/s over test | **~3.9/s** (1276 ÷ 330s) |

Worker HPA did not scale, so match throughput stayed bounded by a single worker processing partitions. With only one worker replica, matches/s did not step up after the API HPA scale-out — admission rose (more API pods absorbing ticket creates) but pairing capacity did not increase.

## Observations

1. **HPA scale-up lag (API):** First replica added ~51s after load started, during the 0→30 VU ramp. Reached max (5) ~3m16s in, while VUs were still climbing toward the 80-VU plateau.
2. **Node add time:** No new nodes during the test. Five API pods plus worker/postgres/redis fit on one `e2-standard-4` node; cluster autoscaler had no pending unschedulable pods to react to.
3. **matches/s stabilization:** Without interval sampling, stabilization cannot be pinpointed. Single-worker ceiling (~4 matches/s average) implies throughput was flat throughout — API scale-out improved ticket admission latency (p95 228ms) but not match creation rate.
4. **Rejections:** 79% of requests rejected (mostly 429 rate limits), consistent with per-tenant caps under sustained multi-tenant load.

## Raw HPA events (UTC)

```
2026-07-12T19:29:58Z  SuccessfulRescale  api  New size: 2; reason: cpu above target
2026-07-12T19:31:08Z  SuccessfulRescale  api  New size: 4; reason: cpu above target
2026-07-12T19:32:23Z  SuccessfulRescale  api  New size: 5; reason: cpu above target
```

## Notes

- Use `py scripts/sample_worker_metrics.py --interval 15` with port-forward to `worker-metrics:9090`, or `kubectl exec … python -c "…"` (as in `run_gke_benchmark.py`) for reliable matches/s sampling.
- Helper script `scripts/run_elasticity_test.py` orchestrates deploy readiness, k6, and sampling (metrics fetch fixed to use Python exec).
