# Decoupled Architecture

The project can be split up in 3 major components.

---

## A) Public API + Admission Control

A client-facing API service that can accept/reject tickets correctly without needing the worker to be finished. This component should stay decoupled from the matchmaking worker.

Qualities:

```text
stateless API layer
overload protection
multi-tenant fairness
idempotent API retries
```

Scope:

```text
FastAPI app setup
POST /v1/tickets
GET /v1/tickets/{ticket_id}
DELETE /v1/tickets/{ticket_id}
GET /v1/matches/{match_id}
request validation
tenant quotas
queue-depth checks
load shedding
Redis counters
idempotency key handling
```

Chronological TODO:

```text
1. Create the FastAPI project structure.
2. Add PostgreSQL and Redis connection modules.
3. Implement request/response models for tickets and matches.
4. Implement tenant lookup using X-Tenant-Id.
5. Implement POST /v1/tickets without admission control first.
6. Compute partition_id from tenant_id + region + queue_name.
7. Insert tickets into PostgreSQL with status = waiting.
8. Implement GET /v1/tickets/{ticket_id}.
9. Implement DELETE /v1/tickets/{ticket_id}.
10. Implement GET /v1/matches/{match_id}.
11. Add Idempotency-Key support for POST /v1/tickets.
12. Store and replay idempotent responses.
13. Return 409 Conflict if an idempotency key is reused with a different request body.
14. Add Redis tenant rate counters.
15. Add tenant quota checks.
16. Add queue-depth checks for tenant and partition limits.
17. Add load shedding responses for overloaded partitions.
18. Add /health endpoint.
19. Add /metrics endpoint for API-level metrics.
20. Write small API tests using curl, pytest, or simple scripts.
```

Core milestones:

```text
M1: API boots and connects to PostgreSQL/Redis.
M2: Tickets can be created, read, and cancelled.
M3: Idempotency works correctly.
M4: Tenant quota and load shedding reject excess traffic.
M5: API can run as multiple stateless replicas.
```

---

## B) Matchmaking Workers + Stateful Queue Logic

Responsible for the actual matchmaking engine. A worker process that can run independently.

It should be possible to seed tickets into PostgreSQL and watch the worker create matches.

Qualities:

```text
stateful processing
horizontal worker scaling
partitioned workload processing
fault recovery through leases/reservations
```

Scope:

```text
PostgreSQL schema for:
    tickets
    matches
    match_players
    partition_leases
logical partitioning:
    hash(tenant_id + region + queue_name) % partition_count
worker lease claiming
ticket reservation with reserved_until
1v1 matching algorithm
graceful degradation of skill range over waiting time
expired reservation cleanup
insert callback event when match is created
bounded per-loop work budget to prevent insurmountable backlog
freshness-biased ticket fetch under backlog
```

Chronological TODO:

```text
1. Finalize the PostgreSQL schema needed by tickets, matches, match_players, partition_leases, and callback_events.
2. Implement database initialization / migration script.
3. Seed the partition_leases table for all logical partitions.
4. Create a standalone worker entry point: python -m app.worker.main.
5. Implement worker configuration from environment variables.
6. Implement partition lease claiming.
7. Make workers renew or release partition leases.
8. Add logic to skip partitions owned by another live worker.
9. Fetch waiting tickets from owned partitions.
10. Implement expired reservation cleanup.
11. Implement ticket reservation using reserved_by and reserved_until.
12. Implement the basic 1v1 matching rule.
13. Add skill compatibility threshold.
14. Add graceful degradation of skill threshold based on waiting time.
15. Create match rows in the matches table.
16. Insert matched players into match_players.
17. Update matched tickets with status = matched and match_id.
18. Insert a callback_events row when a match is created.
19. Ensure match creation and callback event insertion happen in one transaction.
20. [DONE] Add worker metrics: matches_created, loop_duration, leases_claimed, reservation_expired.
21. Test with manually seeded tickets.
22. Test with multiple worker processes running at the same time.
23. Test worker crash behavior by killing a worker and waiting for lease expiry.
24. [DONE] Add a per-loop time budget (worker_loop_budget_ms) and max pairs cap (worker_max_pairs_per_loop) so a single loop cannot process an unbounded amount of work.
25. [DONE] Add freshness-biased ticket fetching (worker_freshness_bias) that mixes newest and oldest waiting tickets instead of always draining oldest-first, so fresh traffic keeps getting matched during a backlog.
26. [DONE] Add optional max_wait_seconds filter in fetch_waiting_tickets to exclude very old tickets from a fetch when backlog is detected.
27. [DONE] Extend worker metrics with backlog/age signals: tickets_fetched_total, pairs_found_total, pairs_skipped_total, loop_budget_exceeded_total, and last-loop/age gauges (tickets_fetched_last_loop, pairs_found_last_loop, matches_created_last_loop, max_ticket_wait_seconds, avg_ticket_wait_seconds).
28. [DONE] Add deterministic per-worker jitter (worker_loop_jitter_pct, worker_lease_renew_jitter_pct) to loop sleep and lease renewal timing so replicas don't stay lock-step.
29. [DONE] Randomize partition claim order per worker using a deterministic worker offset to reduce contention on low-numbered partitions.
30. [DONE] Add stage timing metrics (lease_ops_ms, ticket_fetch_ms, pair_search_ms, match_creation_ms) so backlog root cause can be attributed to a specific loop stage.
31. [DONE] Add failure/rollback counters (lease_claim_failures, matches_failed, rollbacks) and a leases_renewed counter, distinct from success-only counters.
32. [DONE] Add structured logging with worker_id, partition_id(s), ticket counts, match_id, and exception type on lease claim failures, match creation failures, and per-partition ticket fetch summaries.
33. [DONE] Add scripts/validate_worker_metrics.py to check /metrics exposes all expected metric names and to report counter deltas across a sample interval.
```

Backlog behavior notes:

```text
Ticket batches are bounded by worker_ticket_batch_size, and match creation within a loop is bounded by worker_loop_budget_ms (wall-clock) and worker_max_pairs_per_loop (count), so one loop can no longer balloon into a long "catch-up" pass.
When worker_freshness_bias is enabled (default), each fetch pulls roughly half of its batch from the newest waiting tickets and half from the oldest, so new arrivals keep getting matched even while a backlog of old tickets is present.
When a loop hits its time budget or pair cap, it logs worker_backlog_loop with tickets_fetched, pairs_found, pairs_skipped, and ticket age stats, and increments pms_worker_loop_budget_exceeded_total / pms_worker_pairs_skipped_total so backlog pressure is visible in metrics.
```

Core milestones:

```text
M1: Schema and partition leases are initialized.
M2: One worker can create matches from seeded tickets.
M3: Multiple workers can process different partitions safely.
M4: Expired reservations and worker crashes recover correctly.
M5: Match creation schedules callback events.
```

---

## C) Callback Delivery + Infrastructure + Benchmarking

Responsible for deployment, demo reproducibility, and proving scalability.

A reproducible deployment and demo setup:

```bash
terraform apply
kubectl apply -f infra/k8s/
k6 run loadtests/scale_out.js
```

Qualities:

```text
callback-based delivery instead of polling
retry/backoff strategy
deployment reproducibility
1-node / 3-node / 5-node scalability results
```

Scope:

```text
Application side:
    callback_events table
    callback dispatcher worker
    retry with backoff + jitter
    per-tenant callback concurrency limit
    callback delivery metrics
    mock tenant callback receiver for demos

Infrastructure side:
    Dockerfile
    Kubernetes manifests:
        API Deployment
        Worker Deployment
        Callback Dispatcher Deployment
        PostgreSQL StatefulSet
        Redis StatefulSet
        Services / Ingress
    Terraform:
        GKE cluster
        fixed node pools
        artifact registry if needed
    K6 load tests:
        scale-out test
        noisy-tenant test
        idempotency test
```

Chronological TODO:

```text
1. Create the shared Dockerfile for API, worker, dispatcher, and migration job.
2. Create docker-compose.yml for local development.
3. Add PostgreSQL and Redis services to docker-compose.yml.
4. Add API, worker, callback-dispatcher, and migrate services to docker-compose.yml.
5. Implement a mock tenant callback receiver for local and demo use.
6. Create the callback dispatcher entry point: python -m app.callback_dispatcher.main.
7. Implement callback event claiming with locked_by and locked_until.
8. Implement HTTP POST delivery to tenant callback_url.
9. Add X-PMS-Event-Id, X-PMS-Timestamp, and X-PMS-Signature headers.
10. Treat 2xx responses as delivered.
11. Retry non-2xx responses and timeouts.
12. Implement exponential backoff with jitter.
13. Add max callback attempts.
14. Add per-tenant callback concurrency limit.
15. Add callback metrics: delivered, failed, retries, delivery_latency.
16. Write Kubernetes manifests for API, worker, dispatcher, PostgreSQL, Redis, services, and ingress.
17. Add Kubernetes ConfigMap and Secret templates.
18. Configure worker and dispatcher IDs from Kubernetes pod names.
19. Write Terraform for GKE cluster and fixed-size node pools.
20. Add deployment instructions to the README.
21. Write K6 scale-out load test.
22. Write K6 noisy-tenant load test.
23. Write K6 idempotency load test.
24. Run local docker-compose integration test.
25. Run 1-node GKE benchmark.
26. Run 3-node GKE benchmark.
27. Run 5-node GKE benchmark.
28. Collect throughput, latency, rejection, and match-creation metrics.
29. Create the final result table/graph for the presentation.
30. Document known bottlenecks and limitations.
```

Core milestones:

```text
M1: Local docker-compose setup runs the full system.
M2: Callback dispatcher delivers match.created events.
M3: Failed callbacks retry with backoff and jitter.
M4: Kubernetes deployment works on GKE.
M5: 1-node, 3-node, and 5-node benchmarks are reproducible.
M6: Final scalability results are ready for presentation.
```

