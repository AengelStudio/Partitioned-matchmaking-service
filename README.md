# Partitioned-matchmaking-service {PMS}

A scalable cell-based multi-tenant online game matchmaking platform

A prototype B2B matchmaking backend for multiplayer game studios. Studios submit matchmaking tickets for their players, and the service creates matches based on tenant, region, queue type, and skill rating.

The project focuses on scalability. It separates stateless API components from stateful queue/match storage, supports horizontal scaling through replicated API and worker pods, and includes overload protection so scaling one component does not overload another.

Licensed for non-commercial use only — see [`LICENSE`](LICENSE).

## Tech Stack

- Python 3.12
- FastAPI + Uvicorn for the HTTP API
- PostgreSQL for tickets, matches, leases, and idempotency state
- Redis for short-lived counters and admission-control state
- Docker for containerization
- Kubernetes on GCP/GKE for deployment
- Terraform for infrastructure provisioning
- K6 for repeatable load testing

## Architecture Summary

Game studio backends create matchmaking tickets through stateless API pods. Tickets are stored in PostgreSQL and assigned to logical partitions based on tenant, region, and queue type. Matchmaking worker pods claim partitions, process compatible tickets, create matches, and schedule callback events.

Match results are delivered primarily through tenant callbacks instead of constant polling, reducing load on the service. Polling endpoints remain available as a fallback and for debugging.

## Quality Goals

- Horizontal scalability through replicated API pods and worker pods
- Clear separation of stateless services and stateful storage
- Overload protection through tenant quotas, queue-depth limits, load shedding and graceful degradation
- Multi-tenant fairness so one studio cannot consume all system capacity
- Idempotent ticket creation to make retries safe
- Callback-based match delivery to reduce unnecessary polling load
- Retry handling with backoff and jitter for failed callbacks
- Fault recovery through expiring worker leases and ticket reservations

## Main Scalability Metric

The primary metric is `matches_created_per_second`, measured under 1-node, 3-node, and 5-node deployments.

Secondary metrics include:

- successful ticket creations per second
- rejected requests per second
- p95 ticket creation latency
- queue depth per tenant and partition
- callback delivery success/failure rate

## Getting Started

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8080`.

On PowerShell, use `curl.exe` (not `curl` — that alias is `Invoke-WebRequest`).

**Health check**

```powershell
curl.exe http://localhost:8080/health
```

**Create a matchmaking ticket**

Use single quotes around the JSON body in PowerShell:

```powershell
curl.exe -X POST http://localhost:8080/v1/tickets -H "Content-Type: application/json" -H "X-Tenant-Id: studio_a" -d '{"player_id":"player_123","region":"eu-west","queue_name":"ranked_1v1","skill":1470}'
```

Or with native PowerShell:

```powershell
Invoke-RestMethod -Uri http://localhost:8080/v1/tickets -Method POST `
  -Headers @{ "X-Tenant-Id" = "studio_a" } `
  -ContentType "application/json" `
  -Body '{"player_id":"player_123","region":"eu-west","queue_name":"ranked_1v1","skill":1470}'
```

On Linux/Mac, use `curl` instead of `curl.exe`.

## Deployment on GKE

This reproduces the 1-node / 3-node / 5-node scalability benchmark. All commands below assume a shell with `gcloud`, `terraform`, `docker`, and `kubectl` installed and authenticated (`gcloud auth login` and `gcloud auth application-default login`).

### 1. Provision the cluster

```bash
cd infra/terraform
terraform init
terraform apply -var="node_count=1"
```

`node_count` is the only thing you change between benchmark runs (`1`, `3`, or `5`); `machine_type` must stay the same across those three runs so the comparison is fair. See `infra/terraform/variables.tf` for all tunables.

Point `kubectl` at the new cluster (Terraform prints this exact command as an output):

```bash
$(terraform output -raw get_credentials_command)
```

### 2. Build and push the image

```bash
REGISTRY=$(terraform output -raw artifact_registry_repository)
gcloud auth configure-docker "${REGISTRY%%/*}"
docker build -t "$REGISTRY/pms:local" .
docker push "$REGISTRY/pms:local"
```

Then update the `image:` field in `infra/k8s/api.yaml`, `worker.yaml`, `callback-dispatcher.yaml`, and `migrate-job.yaml` from the local placeholder `pms:local` to `$REGISTRY/pms:local`.

### 3. Create secrets and deploy

```bash
kubectl apply -f infra/k8s/namespace.yaml
kubectl create secret generic pms-secrets --namespace pms \
  --from-literal=DATABASE_URL='postgresql://pms:REPLACE_ME@postgres:5432/pms' \
  --from-literal=POSTGRES_PASSWORD='REPLACE_ME' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f infra/k8s/configmap.yaml
kubectl apply -f infra/k8s/postgres.yaml -f infra/k8s/redis.yaml
kubectl -n pms wait --for=condition=ready pod -l app=postgres --timeout=120s
kubectl apply -f infra/k8s/migrate-job.yaml
kubectl -n pms wait --for=condition=complete job/pms-migrate --timeout=120s
kubectl apply -f infra/k8s/api.yaml -f infra/k8s/worker.yaml -f infra/k8s/callback-dispatcher.yaml
kubectl apply -f infra/k8s/ingress.yaml
```

`infra/k8s/secret.yaml` is a template only (see the comments in that file) — the `kubectl create secret ... --dry-run=client | kubectl apply -f -` line above is the real, non-committed secret creation step. Replace `REPLACE_ME` with the actual Postgres password before running it.

Insert at least one tenant with a real `callback_url`/`callback_secret` directly into Postgres before running load tests, since there is no admin API for tenant management yet:

```bash
kubectl -n pms exec -it statefulset/postgres -- psql -U pms -d pms -c \
  "INSERT INTO tenants (tenant_id, name, callback_url, callback_secret) VALUES ('studio_a', 'Studio A', 'http://mock-callback:9000/tenant-matchmaking-callback', 'REPLACE_ME');"
```

### 4. Confirm it's up

```bash
kubectl -n pms get pods
kubectl -n pms get ingress pms-api
curl "http://$(kubectl -n pms get ingress pms-api -o jsonpath='{.status.loadBalancer.ingress[0].ip}')/health"
```

GCE ingress can take several minutes to get a public IP after `kubectl apply` — re-run the `get ingress` command until `ADDRESS` is populated.

### 5. Run the benchmark, then tear down

```bash
k6 run loadtests/scale_out.js   # see the load-testing section below
terraform destroy               # do this after every run — the cluster bills by the hour
```

Repeat steps 1-5 with `node_count=3` and `node_count=5` (same `machine_type`) to get the three comparison points. **Always run `terraform destroy` when you're done for the day** — this project has a fixed $50 GCP grant and a forgotten cluster burns through it in days, not weeks.

### Node identity in Kubernetes

`WORKER_ID` and `CALLBACK_DISPATCHER_ID` are set from each pod's own name via `fieldRef` in `infra/k8s/worker.yaml` and `infra/k8s/callback-dispatcher.yaml`, so replicas never collide on identity — no manual configuration needed when scaling `kubectl scale deployment/worker --replicas=N`.

### Known limitations

- No autoscaling anywhere on purpose (node pool, HPA, Autopilot) — node and pod counts are fixed and manually controlled for reproducible benchmark comparisons.
- The tenant table has no admin API yet; tenants are inserted directly via `psql` (see step 3).
- `infra/k8s/secret.yaml` is a template, not a real secret — see the comment at the top of that file.