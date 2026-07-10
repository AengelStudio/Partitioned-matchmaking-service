// Scale-out benchmark: ramps concurrent virtual users creating tickets
// against POST /v1/tickets and tracks throughput, rejection rate, and
// p95 latency. Run the same script unmodified against the 1-node,
// 3-node, and 5-node deployments to compare.
//
// matches_created_per_second (the primary scalability metric) is not
// computed here — read pms_worker_matches_created_total from the
// worker's /metrics endpoint before and after the run and divide by
// the run duration.
//
// partition_id = hash(tenant_id + region + queue_name) % MATCHMAKING_PARTITIONS
// (see app/shared/partition.py) — a single (tenant, region, queue) tuple
// always lands on exactly one partition, so it can only ever be worked by
// whichever single worker holds that one lease, no matter how many worker
// replicas exist. Spread requests across multiple (region, queue) pairs so
// the run actually touches enough partitions to exercise more than one
// worker — otherwise this benchmark cannot show worker-side scale-out at
// all, regardless of node count.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const TENANT_ID = __ENV.TENANT_ID || "studio_a";

const REGIONS = ["eu-west", "eu-central", "us-east", "us-west", "ap-southeast"];
const QUEUES = ["ranked_1v1", "ranked_2v2", "casual_1v1"];

const ticketsCreated = new Counter("tickets_created");
const ticketsRejected = new Counter("tickets_rejected");
const ticketCreateLatency = new Trend("ticket_create_latency_ms", true);

export const options = {
  scenarios: {
    ramping_load: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 20 },
        { duration: "1m", target: 50 },
        { duration: "1m", target: 100 },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: {
    "ticket_create_latency_ms": ["p(95)<1000"],
  },
};

export default function () {
  const playerId = `player-${__VU}-${__ITER}`;
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
      "X-Tenant-Id": TENANT_ID,
      "Idempotency-Key": `${TENANT_ID}-${__VU}-${__ITER}-${Date.now()}`,
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
    "status is 201 (created), 429 (quota), or 503 (shed)": (r) =>
      [201, 429, 503].includes(r.status),
  });

  sleep(0.1);
}
