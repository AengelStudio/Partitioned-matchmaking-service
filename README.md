# Partitioned-matchmaking-service {PMS}

A scalable cell-based multi-tenant online game matchmaking platform

A prototype B2B matchmaking backend for multiplayer game studios. Studios submit matchmaking tickets for their players, and the service creates matches based on tenant, region, queue type, and skill rating.

The project focuses on scalability. It separates stateless API components from stateful queue/match storage, supports horizontal scaling through replicated API and worker pods, and includes overload protection so scaling one component does not overload another.

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