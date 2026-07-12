# Partitioned-matchmaking-service {PMS}

A scalable cell-based multi-tenant online game matchmaking platform

A prototype B2B matchmaking backend for multiplayer game studios. Studios submit matchmaking tickets for their players, and the service creates matches based on tenant, region, queue type, and skill rating.

The project focuses on scalability. It separates stateless API components from stateful queue/match storage, supports horizontal scaling through replicated API and worker pods, and includes overload protection so scaling one component does not overload another.

Licensed for non-commercial use only — see `[LICENSE](LICENSE)`.

## Tech Stack

- Python 3.12
- FastAPI + Uvicorn for the HTTP API
- PostgreSQL for tickets, matches, leases, and idempotency state
- Redis for short-lived counters and admission-control state
- Docker for containerization
- Kubernetes on GCP/GKE for deployment
- Terraform for infrastructure provisioning
- K6 for repeatable load testing

## Architecture

The diagram below shows how PMS is deployed on GKE: external tenants and tooling on the left, stateless compute pods in the upper half of the cluster, and durable storage in the lower half.

![PMS architecture diagram](docs/pms-architecture-diagram.png)

Source file: [`docs/pms-architecture-diagram.png`](docs/pms-architecture-diagram.png) (exported from [draw.io](https://app.diagrams.net/)).

### Request path (ticket creation)

1. A **game studio backend** (tenant client) sends matchmaking requests to **GCE Ingress / Cloud Load Balancer**, which routes traffic to an available **API pod**.
2. The API pod runs **admission control** against **Redis** — short-lived, per-tenant rate-limit counters that protect the system from overload without hitting PostgreSQL on every check.
3. On acceptance, the API pod **registers the ticket** in **PostgreSQL**, the durable source of truth. Tickets are keyed by tenant, region, and queue type so they land in the correct logical partition. Idempotency keys make retries safe.

### Matchmaking path

1. **Worker pods** **lease partitions** in PostgreSQL so each partition is owned by at most one worker at a time. Leases expire automatically if a worker dies, allowing recovery without manual intervention.
2. Workers **poll their leased partitions**, **reserve** compatible tickets, and **create matches**. All state changes (reservations, matches, partition leases) are written back to PostgreSQL.

### Delivery path (callbacks)

1. When a match is created, PostgreSQL records it in a **callback outbox**. **Callback dispatcher pods** read pending callbacks, **verify matched tickets**, and **POST results to the tenant's callback URL**.
2. Failed deliveries are retried with **backoff and jitter**. Polling endpoints on the API remain available as a fallback and for debugging.

### Cluster layout

The GKE cluster separates concerns into two layers:


| Layer                 | Components                                | Role                                                                                                                                          |
| --------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **Stateless compute** | API, worker, and callback-dispatcher pods | Horizontally scalable; no local state between requests                                                                                        |
| **Stateful services** | PostgreSQL StatefulSet, Redis             | PostgreSQL holds tickets, matches, leases, reservations, idempotency state, and the callback outbox; Redis holds ephemeral admission counters |


**Terraform** provisions the cluster and supporting infrastructure (dashed *Blueprint* arrow). **K6** drives repeatable load tests against the ingress (dashed *Simulate tenant load* arrow), used for the scale-out benchmarks documented under `loadtests/`.

## Quality Goals

- Horizontal scalability through replicated API pods and worker pods
- Clear separation of stateless services and stateful storage
- Overload protection through tenant quotas, queue-depth limits, load shedding and graceful degradation
- Multi-tenant fairness so one studio cannot consume all system capacity
- Idempotent ticket creation to make retries safe
- Callback-based match delivery to reduce unnecessary polling load
- Retry handling with backoff and jitter for failed callbacks
- Fault recovery through expiring worker leases and ticket reservations

### Overload protection

Admission runs in the API layer **before** PostgreSQL is touched on the hot path. Under sustained hostile load the system rejects most requests with `429` (per-tenant rate limit) or `503` (partition queue depth / load shedding) instead of letting traffic cascade into the database or workers.

1. **Per-tenant rate limits** — Redis counters enforce each studio's ticket-creation quota (`app/core/admission.py`, `app/db/redis.py`).
2. **Partition depth checks** — when a partition's waiting-ticket count exceeds the tenant's `max_partition_depth`, new tickets for that partition are rejected with `503` before insert.
3. **Load shedding** — a last-resort gate when the API itself is under extreme pressure (visible mainly at single-replica deployments).

This keeps Redis and PostgreSQL from becoming accidental bottlenecks for each other when API or worker replicas are scaled out.

### Scalability strategies in code

Beyond admission control and horizontal replication, two coordination patterns keep the system stable at scale:

- **Partition leasing** — workers claim exclusive ownership of logical partitions in PostgreSQL (`app/worker/leases.py`, `app/shared/partition.py`). Expiring leases let crashed workers recover without double-processing; only one worker polls a partition at a time.
- **Callback outbox** — matches are written to an outbox table and delivered asynchronously by dispatcher pods (`app/callback_dispatcher/main.py`). Retries use exponential backoff with jitter so a slow tenant callback URL cannot block matchmaking.

Other supporting mechanisms: idempotent ticket creation (`app/core/idempotency.py`), expiring ticket reservations (`app/worker/matching.py`), and per-tenant fairness via seeded quotas (`app/db/init.py`, `app/core/tenants.py`). API contracts and schema details are in [`Contracts.md`](Contracts.md).

## Main Scalability Metric

The primary metric is `matches_created_per_second`, measured under 1-node, 3-node, and 5-node deployments (and optionally on more performant machine types at the same node counts).

Secondary metrics include:

- successful ticket creations per second
- rejected requests per second
- p95 ticket creation latency
- queue depth per tenant and partition
- callback delivery success/failure rate

### Benchmark results (GKE)

Load script: `loadtests/scale_out.js` (11 tenants × 5 regions × 3 queues, 180s ramp to 100 VUs). Cluster: zonal GKE `europe-west1-b`, **`e2-standard-4`** nodes unless noted. Full write-ups: [`loadtests/results-gke-synthesis-2026-07-11.md`](loadtests/results-gke-synthesis-2026-07-11.md) (horizontal scale-out), [`loadtests/results-gke-scale-up-synthesis-2026-07-12.md`](loadtests/results-gke-scale-up-synthesis-2026-07-12.md) (vertical scale-up).

**Horizontal scale-out** (`e2-standard-4`, replicas scale with node count via `scripts/scale_k8s_benchmark.sh`):

| Nodes | matches/s | p95 ticket-create latency | Notes |
| ----- | --------- | ------------------------- | ----- |
| 1     | 6.53      | 500ms                     | Single API replica saturated; load shedding active |
| 3     | 8.53      | 70ms                      | API latency drops; match rate still partition-limited |
| 5     | 33.05     | 38ms                      | Worker scale-out unlocks match throughput |

Ticket admission stays near ~60–70/s across node counts because per-tenant rate limits cap aggregate intake — by design, not a scaling failure. Under ~300 req/s offered load, 65–75% of requests are rejected cleanly with `429` rather than the cluster falling over.

**Vertical scale-up** (same 4 vCPU / 16 GB per node; **n2-standard-4** vs **e2-standard-4**):

| Config | matches/s (E2 → N2) | p95 latency (E2 → N2) |
| ------ | ------------------- | --------------------- |
| 1 node | 6.5 → 6.3           | 500ms → 196ms         |
| 3 node | 8.5 → 28.8          | 70ms → 34ms           |
| 5 node | 33.1 → 29.2         | 38ms → 34ms           |

Faster per-core performance mainly helps API latency at 1 node and worker throughput at 3 nodes; both machine families plateau near ~30 matches/s at 5 nodes. Partial `e2-standard-8` runs (blocked at 5 nodes by GCP vCPU quota) are in [`loadtests/results-gke-e2-standard-8-2026-07-12.md`](loadtests/results-gke-e2-standard-8-2026-07-12.md).

## Local deployment (Docker)

**Prerequisites:** Docker Engine / Docker Desktop with Compose v2 (`docker compose`). All commands run from the repo root.

1. **Config** - copy env file (PowerShell: `Copy-Item .env.example .env`):
  ```bash
   cp .env.example .env
  ```
2. **Start** - builds images, runs DB migration, waits until services are healthy:
  ```bash
   docker compose up --build -d --wait
  ```
3. **Health** :
  ```bash
   curl -s http://localhost:8080/health
  ```
   On Windows PowerShell you should use `curl.exe` instead of `curl` (the alias is `Invoke-WebRequest`).
4. **Full E2E** — match + callback on the host (`pip install httpx` if needed):
  ```bash
   python scripts/integration_test_e2e.py
  ```
5. **Stop**
  ```bash
   docker compose down
  ```
   Add `-v` to wipe Postgres data.

**Create a ticket manually**

```bash
curl -s -X POST http://localhost:8080/v1/tickets \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: studio_a" \
  -d '{"player_id":"player_123","region":"eu-west","queue_name":"ranked_1v1","skill":1470}'
```

PowerShell (`Invoke-RestMethod` because inline JSON is unreliable with `curl.exe` on Windows):

```powershell
Invoke-RestMethod -Uri http://localhost:8080/v1/tickets -Method POST `
  -Headers @{ "X-Tenant-Id" = "studio_a" } -ContentType "application/json" `
  -Body '{"player_id":"player_123","region":"eu-west","queue_name":"ranked_1v1","skill":1470}'
```

**Load test (optional, ~3 min)**

```bash
docker compose --profile loadtest run --rm k6 run /scripts/scale_out.js
```

Tenants are seeded automatically by the migrate step (`app/db/init.py`). API: `http://localhost:8080`.

---

## GKE deployment (manual)

**Prerequisites:** `gcloud`, `kubectl`, `terraform`, `docker`. Authenticate: `gcloud auth login` and `gcloud auth application-default login`. Commands use **bash** (macOS, Linux, or Git Bash/WSL on Windows). Run from repo root.

Set `project_id` in `infra/terraform/variables.tf` first.

1. **Cluster** — set `node_count` to `1`, `3`, or `5` for benchmarks. Use `machine_type=e2-standard-4` to match the documented results (Terraform default is `e2-medium`):
  ```bash
   cd infra/terraform && terraform init && terraform apply -var="node_count=1" -var="machine_type=e2-standard-4"
   eval "$(terraform output -raw get_credentials_command)"
   cd ../..
  ```
2. **Image** - build and push (re-run after code changes):
  ```bash
   REGISTRY=$(terraform -chdir=infra/terraform output -raw artifact_registry_repository)
   gcloud auth configure-docker "${REGISTRY%%/*}"
   docker build -t "$REGISTRY/pms:local" .
   docker push "$REGISTRY/pms:local"
  ```
   Set every app `image` in `infra/k8s/{api,worker,callback-dispatcher,migrate-job,mock-callback}.yaml` to `$REGISTRY/pms:local`.
3. **Secrets + data stores** - pick one password and use the same value in both literals:
  ```bash
   export PGPASS='your-strong-password'
   kubectl apply -f infra/k8s/namespace.yaml
   kubectl create secret generic pms-secrets -n pms \
     --from-literal=DATABASE_URL="postgresql://pms:${PGPASS}@postgres:5432/pms" \
     --from-literal=POSTGRES_PASSWORD="${PGPASS}" \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl apply -f infra/k8s/configmap.yaml
   kubectl apply -f infra/k8s/postgres.yaml -f infra/k8s/redis.yaml
   kubectl -n pms wait --for=condition=ready pod -l app=postgres --timeout=180s
  ```
4. **Migrate** - schema + tenant seed (includes `studio_a` and benchmark tenants):
  ```bash
   kubectl apply -f infra/k8s/migrate-job.yaml
   kubectl -n pms wait --for=condition=complete job/pms-migrate --timeout=180s
  ```
5. **Apps** - deploy all services, then scale replicas to match `node_count`:
  ```bash
   kubectl apply -f infra/k8s/mock-callback.yaml \
     -f infra/k8s/api.yaml -f infra/k8s/worker.yaml -f infra/k8s/callback-dispatcher.yaml
   bash scripts/scale_k8s_benchmark.sh 1
   kubectl apply -f infra/k8s/ingress.yaml
  ```
   Use `3` or `5` instead of `1` when benchmarking. The script restarts workers so partition leases redistribute fairly.
6. **Verify** - GCE ingress can take several minutes to get an IP:
  ```bash
   kubectl -n pms get pods
   kubectl -n pms get ingress pms-api -w
   curl "http://$(kubectl -n pms get ingress pms-api -o jsonpath='{.status.loadBalancer.ingress[0].ip}')/health"
  ```
7. **Tear down** when finished :
  ```bash
   cd infra/terraform && terraform destroy
  ```

### GKE benchmark (optional)

Manual load test against ingress (needs [k6](https://k6.io/) on the host):

```bash
BASE_URL="http://$(kubectl -n pms get ingress pms-api -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" \
  k6 run loadtests/scale_out.js
```

Repeat steps 1 and 5 with `node_count=3` and `5` (keep the same `machine_type`). See **Benchmark results** above and the linked synthesis files under `loadtests/`.

**Vertical scale-up:** recreate the cluster with a more performant machine type at the same node counts, e.g. `n2-standard-4`:

```bash
cd infra/terraform && terraform apply -var="node_count=3" -var="machine_type=n2-standard-4"
```

Re-run steps 2–6, then compare against the `e2-standard-4` baseline. Use `--reset-postgres` (via `scripts/run_gke_benchmark.py`) between runs for comparable pairing metrics.

**Automated alternative:** `scripts/run_gke_benchmark.cmd` / `scripts/run_gke_benchmark.py` run deploy → k6 → results file → scale nodes to 0. Password from `PG_PASS` in `.env`. Windows: `$env:USE_GKE_GCLOUD_AUTH_PLUGIN = "True"`.

### Known limitations

- No autoscaling (node pool, HPA) — counts are fixed for reproducible benchmarks.
- No tenant admin API — tenants are seeded by migrate, not managed at runtime.
- Worker partition leases do not rebalance on mid-flight scale-up; `scale_k8s_benchmark.sh` restarts workers before benchmarks.
- Per-tenant rate limits cap aggregate ticket throughput — raising `matches_created_per_second` in benchmarks requires multiple concurrent tenants (as `scale_out.js` does) or temporarily higher quotas.
- Partition fragmentation (many thin queues across tenants/regions) can depress pairing efficiency until enough worker replicas cover distinct partitions.