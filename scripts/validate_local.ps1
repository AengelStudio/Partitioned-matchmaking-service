# Full local validation: docker-compose stack, unit + DB tests, stateful integration, E2E, and k6.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example"
}

Write-Host "==> Starting docker-compose stack (3 workers for stateful integration)..."
docker compose up -d --build --scale worker=3 --wait
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running pytest (including DB integration tests in container)..."
docker compose exec -T api pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Validating worker /metrics surface..."
py scripts/validate_worker_metrics.py --host localhost --port 9090 --interval 5
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running worker backlog / lease integration test..."
py scripts/integration_test_worker.py --observe-seconds 10
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running E2E smoke test..."
py scripts/integration_test_e2e.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$runId = [guid]::NewGuid().ToString("N").Substring(0, 8)

Write-Host "==> Running k6 idempotency test (RUN_ID=$runId)..."
docker compose --profile loadtest run --rm k6 run /scripts/idempotency.js -e "RUN_ID=$runId"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running k6 noisy-tenant test (~2 min)..."
docker compose --profile loadtest run --rm k6 run /scripts/noisy_tenant.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Running k6 scale-out test (~3 min)..."
docker compose --profile loadtest run --rm k6 run /scripts/scale_out.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "All local validation checks passed."
