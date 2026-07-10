# Shared Contracts

These are the shared contract templates that each part of the service needs. This is a first mockup and may still be adjusted during implementation.

---

# 1. Database schema

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,

    ticket_rate_limit_per_minute INT NOT NULL DEFAULT 300,
    max_waiting_tickets INT NOT NULL DEFAULT 5000,
    max_partition_depth INT NOT NULL DEFAULT 1000,

    callback_url TEXT,
    callback_secret TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    player_id TEXT NOT NULL,

    region TEXT NOT NULL,
    queue_name TEXT NOT NULL,
    skill INT NOT NULL,

    partition_id INT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('waiting', 'reserved', 'matched', 'cancelled')
    ),

    reserved_by TEXT,
    reserved_until TIMESTAMPTZ,

    match_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    matched_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);

CREATE INDEX idx_tickets_partition_status_created
ON tickets (partition_id, status, created_at);

CREATE INDEX idx_tickets_tenant_status
ON tickets (tenant_id, status);

CREATE INDEX idx_tickets_player_lookup
ON tickets (tenant_id, player_id, status);

CREATE UNIQUE INDEX uq_active_ticket_per_player_queue
ON tickets (tenant_id, player_id, region, queue_name)
WHERE status IN ('waiting', 'reserved');

CREATE TABLE matches (
    match_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    region TEXT NOT NULL,
    queue_name TEXT NOT NULL,
    partition_id INT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('created', 'callback_pending', 'callback_delivered', 'callback_failed')
    ) DEFAULT 'created',

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tickets
ADD CONSTRAINT fk_tickets_match
FOREIGN KEY (match_id) REFERENCES matches(match_id);

CREATE TABLE match_players (
    match_id UUID NOT NULL REFERENCES matches(match_id),
    ticket_id UUID NOT NULL REFERENCES tickets(ticket_id),
    player_id TEXT NOT NULL,

    PRIMARY KEY (match_id, ticket_id)
);

CREATE TABLE partition_leases (
    partition_id INT PRIMARY KEY,

    owned_by TEXT,
    lease_until TIMESTAMPTZ,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    idempotency_key TEXT NOT NULL,

    request_hash TEXT NOT NULL,
    response_status INT NOT NULL,
    response_body JSONB NOT NULL,

    ticket_id UUID REFERENCES tickets(ticket_id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE callback_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    match_id UUID NOT NULL REFERENCES matches(match_id),

    event_type TEXT NOT NULL DEFAULT 'match.created',
    callback_url TEXT NOT NULL,
    payload JSONB NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('pending', 'in_progress', 'delivered', 'failed')
    ) DEFAULT 'pending',

    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    locked_by TEXT,
    locked_until TIMESTAMPTZ,

    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX idx_callback_events_pending
ON callback_events (status, next_attempt_at);

INSERT INTO partition_leases (partition_id)
SELECT generate_series(0, 127)
ON CONFLICT DO NOTHING;
```

---

# 2. Ticket JSON request / response

Tenant identity is passed through a header for the prototype. In a production version, this would normally come from an API key or authentication token.

## `POST /v1/tickets`

Headers:

```http
X-Tenant-Id: studio_a
Idempotency-Key: studio-a-player-123-ranked-eu-west-001
```

Request:

```json
{
  "player_id": "player_123",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "skill": 1470
}
```

Success response `201 Created`:

```json
{
  "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2",
  "tenant_id": "studio_a",
  "player_id": "player_123",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "skill": 1470,
  "partition_id": 42,
  "status": "waiting",
  "created_at": "2026-06-24T14:32:10Z"
}
```

Idempotent replay response `200 OK`:

```json
{
  "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2",
  "tenant_id": "studio_a",
  "player_id": "player_123",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "skill": 1470,
  "partition_id": 42,
  "status": "waiting",
  "created_at": "2026-06-24T14:32:10Z",
  "idempotent_replay": true
}
```

Idempotency conflict response `409 Conflict`:

```json
{
  "error": "idempotency_key_conflict",
  "message": "This idempotency key was already used with a different request body."
}
```

Rejected by tenant quota `429 Too Many Requests`:

```json
{
  "error": "tenant_rate_limit_exceeded",
  "message": "Tenant studio_a exceeded its ticket creation quota.",
  "retry_after_seconds": 10
}
```

Rejected by overload protection `503 Service Unavailable`:

```json
{
  "error": "partition_overloaded",
  "message": "The target matchmaking partition is temporarily overloaded.",
  "retry_after_seconds": 5
}
```

---

## `GET /v1/tickets/{ticket_id}`

Headers:

```http
X-Tenant-Id: studio_a
```

Response while waiting:

```json
{
  "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2",
  "tenant_id": "studio_a",
  "player_id": "player_123",
  "status": "waiting",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "skill": 1470,
  "created_at": "2026-06-24T14:32:10Z",
  "match_id": null
}
```

Response after match:

```json
{
  "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2",
  "tenant_id": "studio_a",
  "player_id": "player_123",
  "status": "matched",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "skill": 1470,
  "created_at": "2026-06-24T14:32:10Z",
  "matched_at": "2026-06-24T14:32:44Z",
  "match_id": "09b61c8f-c4f5-45e8-a8b7-ff40debb6b44"
}
```

---

## `DELETE /v1/tickets/{ticket_id}`

Headers:

```http
X-Tenant-Id: studio_a
```

Response:

```json
{
  "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2",
  "status": "cancelled",
  "cancelled_at": "2026-06-24T14:33:01Z"
}
```

---

# 3. Match JSON response

## `GET /v1/matches/{match_id}`

Headers:

```http
X-Tenant-Id: studio_a
```

Response:

```json
{
  "match_id": "09b61c8f-c4f5-45e8-a8b7-ff40debb6b44",
  "tenant_id": "studio_a",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "partition_id": 42,
  "status": "callback_delivered",
  "created_at": "2026-06-24T14:32:44Z",
  "players": [
    {
      "player_id": "player_123",
      "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2"
    },
    {
      "player_id": "player_456",
      "ticket_id": "b393dd7c-c1d6-4f05-a084-b64fb7dfacff"
    }
  ]
}
```

---

# 4. Callback event payload

The service sends this to the tenant’s `callback_url`.

Callbacks are at-least-once delivery. Tenants should deduplicate received callbacks using `event_id`.

Request:

```http
POST /tenant-matchmaking-callback HTTP/1.1
Content-Type: application/json
X-PMS-Event-Id: 4897f958-1302-4186-b76b-f40b95df1404
X-PMS-Timestamp: 2026-06-24T14:32:45Z
X-PMS-Signature: sha256=<hmac_signature>
```

Signature input:

```text
timestamp + "." + raw_body
```

Payload:

```json
{
  "event_id": "4897f958-1302-4186-b76b-f40b95df1404",
  "event_type": "match.created",
  "tenant_id": "studio_a",
  "match_id": "09b61c8f-c4f5-45e8-a8b7-ff40debb6b44",
  "created_at": "2026-06-24T14:32:44Z",
  "region": "eu-west",
  "queue_name": "ranked_1v1",
  "players": [
    {
      "player_id": "player_123",
      "ticket_id": "6c66d4af-9b70-487a-91c8-1ad1199260d2"
    },
    {
      "player_id": "player_456",
      "ticket_id": "b393dd7c-c1d6-4f05-a084-b64fb7dfacff"
    }
  ]
}
```

Expected tenant response:

```http
204 No Content
```

or:

```http
200 OK
```

Any non-2xx response should trigger retry with backoff and jitter.

---

# 5. Worker HTTP endpoints

The matchmaking worker exposes observability endpoints on `WORKER_METRICS_HOST` / `WORKER_METRICS_PORT` (default `9090`).

## `GET /health`

```json
{
  "status": "ok",
  "service": "worker",
  "worker_id": "worker-local-1"
}
```

## `GET /metrics`

Prometheus-style plain text counters:

```text
pms_worker_info{worker_id="worker-local-1"} 1
pms_worker_matches_created_total{worker_id="worker-local-1"} 12
pms_worker_matches_failed_total{worker_id="worker-local-1"} 1
pms_worker_rollbacks_total{worker_id="worker-local-1"} 1
pms_worker_leases_claimed_total{worker_id="worker-local-1"} 40
pms_worker_leases_renewed_total{worker_id="worker-local-1"} 812
pms_worker_lease_claim_failures_total{worker_id="worker-local-1"} 0
pms_worker_reservations_expired_total{worker_id="worker-local-1"} 0
pms_worker_reservations_cleaned_total{worker_id="worker-local-1"} 0
pms_worker_pair_search_runs_total{worker_id="worker-local-1"} 260
pms_worker_loop_duration_ms{worker_id="worker-local-1"} 4.10
pms_worker_loops_completed_total{worker_id="worker-local-1"} 260
pms_worker_tickets_fetched_total{worker_id="worker-local-1"} 5400
pms_worker_pairs_found_total{worker_id="worker-local-1"} 130
pms_worker_pairs_skipped_total{worker_id="worker-local-1"} 6
pms_worker_loop_budget_exceeded_total{worker_id="worker-local-1"} 2
pms_worker_owned_partitions_count{worker_id="worker-local-1"} 16
pms_worker_tickets_fetched_last_loop{worker_id="worker-local-1"} 100
pms_worker_pairs_found_last_loop{worker_id="worker-local-1"} 22
pms_worker_matches_created_last_loop{worker_id="worker-local-1"} 20
pms_worker_max_ticket_wait_seconds{worker_id="worker-local-1"} 41.30
pms_worker_avg_ticket_wait_seconds{worker_id="worker-local-1"} 12.85
pms_worker_jittered_sleep_ms{worker_id="worker-local-1"} 517.00
pms_worker_lease_ops_ms{worker_id="worker-local-1"} 3.20
pms_worker_lease_ops_ms_sum{worker_id="worker-local-1"} 832.00
pms_worker_lease_ops_ms_count{worker_id="worker-local-1"} 260
pms_worker_ticket_fetch_ms{worker_id="worker-local-1"} 2.10
pms_worker_ticket_fetch_ms_sum{worker_id="worker-local-1"} 546.00
pms_worker_ticket_fetch_ms_count{worker_id="worker-local-1"} 260
pms_worker_pair_search_ms{worker_id="worker-local-1"} 5.40
pms_worker_pair_search_ms_sum{worker_id="worker-local-1"} 1404.00
pms_worker_pair_search_ms_count{worker_id="worker-local-1"} 260
pms_worker_match_creation_ms{worker_id="worker-local-1"} 8.90
pms_worker_match_creation_ms_sum{worker_id="worker-local-1"} 2314.00
pms_worker_match_creation_ms_count{worker_id="worker-local-1"} 260
```

`worker_pairs_skipped_total` and `worker_loop_budget_exceeded_total` indicate a worker is hitting `WORKER_LOOP_BUDGET_MS` / `WORKER_MAX_PAIRS_PER_LOOP` caps and shedding excess pairs to the next loop rather than growing an unbounded backlog.

`pms_worker_jittered_sleep_ms` reports the per-loop sleep duration each replica actually uses. It is derived deterministically from `WORKER_ID` and `WORKER_LOOP_JITTER_PCT`, so it stays the same across restarts of the same replica but differs between replicas, preventing every worker from waking up, renewing leases, and hitting PostgreSQL at the exact same moment.

The `_ms` / `_ms_sum` / `_ms_count` triples (`lease_ops_ms`, `ticket_fetch_ms`, `pair_search_ms`, `match_creation_ms`) break the loop down by stage: `_ms` is the most recent duration, `_ms_sum` divided by `_ms_count` gives the average duration per loop for that stage. Comparing these across stages answers "where is time spent?" and which stage is causing a growing backlog.

`pms_worker_leases_renewed_total`, `pms_worker_lease_claim_failures_total`, `pms_worker_reservations_cleaned_total` (an alias of `reservations_expired_total`), and `pms_worker_owned_partitions_count` answer "are leases/reservations healthy?". `pms_worker_matches_failed_total` and `pms_worker_rollbacks_total` count reservation conflicts and failed match transactions, so `matches_created_total` alone is not mistaken for a 100% success rate.

Structured worker logs (`partitions_claimed`, `partitions_released`, `reservations_cleaned`, `tickets_fetched`, `pair_search_completed`, `match_created`, `match_creation_failed`, `worker_lease_ops_failed`) include `worker_id`, `partition_id`/`partition_ids`, ticket counts, `match_id`, and `exception_type` where relevant. They are emitted at `INFO`/`ERROR` level; set `LOG_LEVEL=info` to see them.

Validate the endpoint under load with `python scripts/validate_worker_metrics.py --host localhost --port 9090`, see the README for details.

---

# 6. Environment variables

```env
APP_NAME=pms
APP_ENV=local

API_HOST=0.0.0.0
API_PORT=8080

DATABASE_URL=postgresql://pms:pms@postgres:5432/pms
REDIS_URL=redis://redis:6379/0

MATCHMAKING_PARTITIONS=128

DEFAULT_TICKET_RATE_LIMIT_PER_MINUTE=300
DEFAULT_MAX_WAITING_TICKETS=5000
DEFAULT_MAX_PARTITION_DEPTH=1000

LOAD_SHEDDING_ENABLED=true
DB_LATENCY_SHED_THRESHOLD_MS=200

WORKER_ID=worker-local-1
WORKER_METRICS_HOST=0.0.0.0
WORKER_METRICS_PORT=9090
WORKER_LEASE_SECONDS=15
WORKER_LOOP_INTERVAL_MS=500
WORKER_LOOP_JITTER_PCT=0.25
WORKER_LEASE_RENEW_JITTER_PCT=0.1
WORKER_PARTITION_BATCH_SIZE=8
WORKER_TICKET_BATCH_SIZE=100
WORKER_LOOP_BUDGET_MS=2000
WORKER_MAX_PAIRS_PER_LOOP=20
WORKER_FRESHNESS_BIAS=true

MATCH_SIZE=2
SKILL_DELTA_INITIAL=100
SKILL_DELTA_AFTER_30S=200
SKILL_DELTA_AFTER_60S=400

CALLBACK_DISPATCHER_ID=callback-local-1
CALLBACK_BATCH_SIZE=50
CALLBACK_TIMEOUT_SECONDS=3
CALLBACK_MAX_ATTEMPTS=5
CALLBACK_BASE_BACKOFF_SECONDS=2
CALLBACK_MAX_BACKOFF_SECONDS=60
CALLBACK_JITTER_SECONDS=3

METRICS_ENABLED=true
LOG_LEVEL=error
```

For Kubernetes, non-secret values should be in a `ConfigMap` and secrets in a `Secret`.

`WORKER_ID` and `CALLBACK_DISPATCHER_ID` should be generated from the pod name in Kubernetes so each replica has a unique identity.

Example:

```yaml
env:
  - name: WORKER_ID
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
```

---

# 7. Docker / Compose commands

One shared Docker image is used for the API, matchmaking worker, callback dispatcher, and migration job. Each container runs a different command from the same image.

## Recommended local workflow

```bash
docker compose up --build
```

Scale workers locally:

```bash
docker compose up --build --scale worker=3
```

Scale callback dispatchers locally:

```bash
docker compose up --build --scale callback-dispatcher=2
```

## Build image manually

```bash
docker build -t pms:local .
```

## Local dependency containers

```bash
docker network create pms-net
```

```bash
docker run -d \
  --name postgres \
  --network pms-net \
  -e POSTGRES_USER=pms \
  -e POSTGRES_PASSWORD=pms \
  -e POSTGRES_DB=pms \
  -p 5432:5432 \
  postgres:16
```

```bash
docker run -d \
  --name redis \
  --network pms-net \
  -p 6379:6379 \
  redis:7
```

When using the Docker network, set:

```env
DATABASE_URL=postgresql://pms:pms@postgres:5432/pms
REDIS_URL=redis://redis:6379/0
```

## Run migrations / schema init manually

```bash
docker run --rm \
  --network pms-net \
  --env-file .env \
  pms:local \
  python -m app.db.init
```

## Run API manually

```bash
docker run --rm \
  --name pms-api \
  --network pms-net \
  --env-file .env \
  -p 8080:8080 \
  pms:local \
  uvicorn app.api.main:app --host 0.0.0.0 --port 8080
```

## Run matchmaking worker manually

```bash
docker run --rm \
  --name pms-worker \
  --network pms-net \
  --env-file .env \
  -p 9090:9090 \
  pms:local \
  python -m app.worker.main
```

Worker metrics are available at `http://<worker-host>:9090/metrics` inside the Docker network (`http://worker:9090/metrics` in Compose). Publish `-p 9090:9090` when running a single worker locally; omit the port mapping when scaling workers in Compose.

## Run callback dispatcher manually

```bash
docker run --rm \
  --name pms-callback-dispatcher \
  --network pms-net \
  --env-file .env \
  pms:local \
  python -m app.callback_dispatcher.main
```
