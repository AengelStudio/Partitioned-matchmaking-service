"""End-to-end smoke test: ticket creation -> match -> callback delivery."""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

API_BASE = "http://localhost:8080"
MOCK_CALLBACK_BASE = "http://localhost:9000"
TENANT_ID = "studio_a"
TIMEOUT_SECONDS = 60


def wait_for_health(client: httpx.Client, url: str) -> None:
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            response = client.get(url)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Service not healthy at {url}")


def create_ticket(client: httpx.Client, player_id: str) -> dict:
    response = client.post(
        f"{API_BASE}/v1/tickets",
        headers={
            "X-Tenant-Id": TENANT_ID,
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={
            "player_id": player_id,
            "region": "eu-west",
            "queue_name": "ranked_1v1",
            "skill": 1500,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def wait_for_match(client: httpx.Client, ticket_id: str) -> str:
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        response = client.get(
            f"{API_BASE}/v1/tickets/{ticket_id}",
            headers={"X-Tenant-Id": TENANT_ID},
            timeout=10.0,
        )
        response.raise_for_status()
        body = response.json()
        match_id = body.get("match_id")
        if match_id:
            return match_id
        time.sleep(0.5)
    raise RuntimeError(f"Ticket {ticket_id} was not matched in time")


def wait_for_callback_delivered(client: httpx.Client, match_id: str) -> None:
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        response = client.get(
            f"{API_BASE}/v1/matches/{match_id}",
            headers={"X-Tenant-Id": TENANT_ID},
            timeout=10.0,
        )
        response.raise_for_status()
        if response.json().get("status") == "callback_delivered":
            return
        time.sleep(0.5)
    raise RuntimeError(f"Match {match_id} callback was not delivered in time")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run API -> worker -> callback E2E test")
    parser.parse_args()

    with httpx.Client() as client:
        wait_for_health(client, f"{API_BASE}/health")
        wait_for_health(client, f"{MOCK_CALLBACK_BASE}/health")

        client.delete(f"{MOCK_CALLBACK_BASE}/callbacks")

        suffix = uuid.uuid4().hex[:8]
        first = create_ticket(client, f"e2e-player-a-{suffix}")
        second = create_ticket(client, f"e2e-player-b-{suffix}")

        match_id = wait_for_match(client, first["ticket_id"])
        if second.get("match_id") and second["match_id"] != match_id:
            raise RuntimeError("Players did not land in the same match")

        wait_for_callback_delivered(client, match_id)

        callbacks = client.get(f"{MOCK_CALLBACK_BASE}/callbacks", timeout=10.0)
        callbacks.raise_for_status()
        count = callbacks.json().get("count", 0)
        if count < 1:
            raise RuntimeError("Mock callback receiver recorded no deliveries")

    print("E2E test passed: match created and callback delivered.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"E2E test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
