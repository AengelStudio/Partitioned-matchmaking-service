// Noisy-tenant fairness test: one tenant hammers the API far harder
// than another. If tenant quotas, queue-depth limits, and per-tenant
// callback concurrency are working, the quiet tenant's success rate
// and latency should stay roughly stable regardless of how noisy the
// other tenant gets.
import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const NOISY_TENANT = __ENV.NOISY_TENANT || "studio_noisy";
const QUIET_TENANT = __ENV.QUIET_TENANT || "studio_quiet";

const quietSuccessRate = new Rate("quiet_tenant_success_rate");
const quietLatency = new Trend("quiet_tenant_latency_ms", true);
const noisySuccessRate = new Rate("noisy_tenant_success_rate");

function createTicket(tenantId) {
  const payload = JSON.stringify({
    player_id: `player-${tenantId}-${__VU}-${__ITER}`,
    region: "eu-west",
    queue_name: "ranked_1v1",
    skill: 1200,
  });
  const params = {
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": tenantId,
      "Idempotency-Key": `${tenantId}-${__VU}-${__ITER}-${Date.now()}`,
    },
  };
  return http.post(`${BASE_URL}/v1/tickets`, payload, params);
}

export const options = {
  scenarios: {
    noisy_tenant: {
      executor: "constant-vus",
      vus: 100,
      duration: "2m",
      exec: "noisyTraffic",
    },
    quiet_tenant: {
      executor: "constant-arrival-rate",
      rate: 5,
      timeUnit: "1s",
      duration: "2m",
      preAllocatedVUs: 10,
      maxVUs: 20,
      exec: "quietTraffic",
    },
  },
  thresholds: {
    // The quiet tenant should keep succeeding even while the noisy
    // one is hammering the API.
    "quiet_tenant_success_rate": ["rate>0.9"],
    "quiet_tenant_latency_ms": ["p(95)<1000"],
  },
};

export function noisyTraffic() {
  const res = createTicket(NOISY_TENANT);
  noisySuccessRate.add(res.status === 201);
}

export function quietTraffic() {
  const res = createTicket(QUIET_TENANT);
  quietLatency.add(res.timings.duration);
  const ok = res.status === 201;
  quietSuccessRate.add(ok);
  check(res, { "quiet tenant ticket created": () => ok });
  sleep(0.1);
}
