// Idempotency test: many virtual users concurrently retry the exact
// same request (same Idempotency-Key + same body), simulating a
// client that times out and retries. Exactly one should create the
// ticket; every other concurrent attempt should replay it safely
// (never a 409, never a duplicate ticket). After the storm, a replay
// and a conflicting-body request confirm the persisted behavior
// documented in Contracts.md.
import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const TENANT_ID = __ENV.TENANT_ID || "studio_a";

// RUN_ID must come from __ENV (resolved once by the shell before k6
// starts), not Date.now() computed in script init code — k6 runs
// each VU's init code independently, so per-VU Date.now() calls can
// disagree by the time all VUs actually fire, silently turning a
// "same key" concurrency test into a bunch of unrelated requests.
const RUN_ID = __ENV.RUN_ID || "fallback-run-id";

const IDEMPOTENCY_KEY = `idem-test-${RUN_ID}`;
const BODY = JSON.stringify({
  player_id: `idem-player-${RUN_ID}`,
  region: "eu-west",
  queue_name: "ranked_1v1",
  skill: 1500,
});
const CONFLICTING_BODY = JSON.stringify({
  player_id: `idem-player-conflict-${RUN_ID}`,
  region: "eu-west",
  queue_name: "ranked_1v1",
  skill: 1500,
});

export const options = {
  scenarios: {
    concurrent_retries: {
      executor: "shared-iterations",
      vus: 20,
      iterations: 20,
      maxDuration: "30s",
      exec: "retrySameRequest",
    },
  },
};

function postTicket(body, key) {
  return http.post(`${BASE_URL}/v1/tickets`, body, {
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": TENANT_ID,
      "Idempotency-Key": key,
    },
  });
}

export function retrySameRequest() {
  const res = postTicket(BODY, IDEMPOTENCY_KEY);
  check(res, {
    "concurrent retry never conflicts (201 created or 200 replay)": (r) =>
      r.status === 201 || r.status === 200,
  });
}

// Runs once, after every VU above has finished.
export function teardown() {
  const replay = postTicket(BODY, IDEMPOTENCY_KEY);
  check(replay, {
    "replay after the storm returns idempotent_replay=true": (r) => {
      if (r.status !== 200) return false;
      const parsed = JSON.parse(r.body);
      return parsed.idempotent_replay === true;
    },
  });

  const conflict = postTicket(CONFLICTING_BODY, IDEMPOTENCY_KEY);
  check(conflict, {
    "same key with a different body returns 409": (r) => r.status === 409,
  });
}
