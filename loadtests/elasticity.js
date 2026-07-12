// Sustained load for elasticity / autoscaling tests. Ramp up, hold, then ramp down
// so HPA and the cluster autoscaler have time to react.
//
// Watch scaling while this runs:
//   kubectl -n pms get hpa -w
//   kubectl get nodes -w
//
// Sample worker throughput every 15s:
//   python scripts/sample_worker_metrics.py --interval 15
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";

const TENANTS = [
  "studio_a",
  "studio_01",
  "studio_02",
  "studio_03",
  "studio_04",
  "studio_05",
  "studio_06",
  "studio_07",
  "studio_08",
  "studio_09",
  "studio_10",
];

const REGIONS = ["eu-west", "eu-central", "us-east", "us-west", "ap-southeast"];
const QUEUES = ["ranked_1v1", "ranked_2v2", "casual_1v1"];

const ticketsCreated = new Counter("tickets_created");
const ticketsRejected = new Counter("tickets_rejected");
const ticketCreateLatency = new Trend("ticket_create_latency_ms", true);

export const options = {
  scenarios: {
    elasticity: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 30 },
        { duration: "3m", target: 80 },
        { duration: "1m", target: 30 },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: {
    ticket_create_latency_ms: ["p(95)<2000"],
  },
};

export default function () {
  const tenantId = TENANTS[(__VU + __ITER) % TENANTS.length];
  const playerId = `elastic-${tenantId}-${__VU}-${__ITER}`;
  const region = REGIONS[(__VU + __ITER) % REGIONS.length];
  const queueName = QUEUES[__VU % QUEUES.length];
  const payload = JSON.stringify({
    player_id: playerId,
    region: region,
    queue_name: queueName,
    skill: 1000 + Math.floor(Math.random() * 800),
  });

  const params = {
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": tenantId,
      "Idempotency-Key": `${tenantId}-${__VU}-${__ITER}-${Date.now()}`,
    },
  };

  const res = http.post(`${BASE_URL}/v1/tickets`, payload, params);
  ticketCreateLatency.add(res.timings.duration);

  if (res.status === 201) {
    ticketsCreated.add(1);
  } else if (res.status === 429 || res.status === 503) {
    ticketsRejected.add(1);
  }

  check(res, {
    "status is 201, 429, or 503": (r) => [201, 429, 503].includes(r.status),
  });

  sleep(0.05);
}
