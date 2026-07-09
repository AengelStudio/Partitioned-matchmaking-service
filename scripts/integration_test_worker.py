import argparse
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LINE_PATTERN = re.compile(r"^(?P<name>\w+)(\{[^}]*\})?\s+(?P<value>-?[0-9.]+)$")

REQUIRED_METRICS = [
    "pms_worker_matches_created_total",
    "pms_worker_leases_claimed_total",
    "pms_worker_leases_renewed_total",
    "pms_worker_tickets_fetched_total",
    "pms_worker_pairs_found_total",
    "pms_worker_pairs_skipped_total",
    "pms_worker_loop_budget_exceeded_total",
    "pms_worker_max_ticket_wait_seconds",
    "pms_worker_jittered_sleep_ms",
    "pms_worker_lease_ops_ms",
    "pms_worker_ticket_fetch_ms",
    "pms_worker_pair_search_ms",
    "pms_worker_match_creation_ms",
    "pms_worker_owned_partitions_count",
]


def fetch_metrics(url: str) -> dict[str, float]:
    with urllib.request.urlopen(url, timeout=10) as response:
        text = response.read().decode("utf-8")
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = LINE_PATTERN.match(line.strip())
        if match:
            values[match.group("name")] = float(match.group("value"))
    return values


def post_json(url: str, body: str, headers: dict[str, str]) -> int:
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def run_psql(sql: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "pms",
            "-d",
            "pms",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def seed_backlog(partition_id: int, old_count: int, fresh_count: int) -> None:
    run_psql(
        f"""
        INSERT INTO tickets
            (tenant_id, player_id, region, queue_name, skill, partition_id, status, created_at)
        SELECT
            'studio_a',
            'backlog_old_' || g,
            'eu-west',
            'ranked_1v1',
            1400 + (g % 50),
            {partition_id},
            'waiting',
            now() - interval '2 hours' - (g || ' seconds')::interval
        FROM generate_series(1, {old_count}) AS g;
        """
    )
    run_psql(
        f"""
        INSERT INTO tickets
            (tenant_id, player_id, region, queue_name, skill, partition_id, status, created_at)
        SELECT
            'studio_a',
            'backlog_fresh_' || g,
            'eu-west',
            'ranked_1v1',
            1400 + (g % 10),
            {partition_id},
            'waiting',
            now() - (g || ' seconds')::interval
        FROM generate_series(1, {fresh_count}) AS g;
        """
    )


def force_partition_lease(partition_id: int, worker_host: str) -> None:
    run_psql(
        f"""
        UPDATE partition_leases
        SET owned_by = '{worker_host}',
            lease_until = now() + interval '60 seconds',
            updated_at = now()
        WHERE partition_id = {partition_id};
        """
    )


def get_worker_containers() -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "ps",
            "--format",
            "{{.Name}}",
            "worker",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def metrics_url_for_container(container: str) -> str:
    return f"http://127.0.0.1:9090/metrics"


def exec_metrics(container: str) -> dict[str, float]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            (
                "import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=5).read().decode())"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    values: dict[str, float] = {}
    for line in result.stdout.splitlines():
        match = LINE_PATTERN.match(line.strip())
        if match:
            values[match.group("name")] = float(match.group("value"))
    return values


def check_health(container: str) -> dict:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            (
                "import json, urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:9090/health', timeout=5).read().decode())"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    return json.loads(result.stdout.strip())


def release_other_partitions(partition_id: int, worker_host: str) -> None:
    run_psql(
        f"""
        UPDATE partition_leases
        SET owned_by = NULL, lease_until = NULL, updated_at = now()
        WHERE partition_id <> {partition_id};
        """
    )
    force_partition_lease(partition_id, worker_host)


def main() -> int:
    parser = argparse.ArgumentParser(description="Integration tests for Section B worker")
    parser.add_argument("--partition-id", type=int, default=42)
    parser.add_argument("--old-tickets", type=int, default=400)
    parser.add_argument("--fresh-tickets", type=int, default=40)
    parser.add_argument("--observe-seconds", type=float, default=8.0)
    args = parser.parse_args()

    failures: list[str] = []

    print("=== 1. Worker health ===")
    workers = get_worker_containers()
    if not workers:
        print("FAIL: no worker containers found; run docker compose up --scale worker=3")
        return 1
    print(f"Found {len(workers)} worker container(s): {workers}")

    health_ids: list[str] = []
    for container in workers:
        health = check_health(container)
        health_ids.append(health["worker_id"])
        print(f"  {container}: worker_id={health['worker_id']} status={health['status']}")
    if len(set(health_ids)) != len(health_ids):
        failures.append("worker_id values are not unique across replicas")

    print("\n=== 2. Metrics surface ===")
    before = exec_metrics(workers[0])
    missing = [name for name in REQUIRED_METRICS if name not in before]
    if missing:
        failures.append(f"missing metrics on {workers[0]}: {missing}")
    else:
        print(f"OK: all {len(REQUIRED_METRICS)} required metrics present on {workers[0]}")

    print("\n=== 3. Seed artificial backlog ===")
    target_worker = health_ids[0]
    release_other_partitions(args.partition_id, target_worker)
    seed_backlog(args.partition_id, args.old_tickets, args.fresh_tickets)
    print(
        f"Seeded {args.old_tickets} old + {args.fresh_tickets} fresh tickets "
        f"on partition {args.partition_id}, exclusive lease to {target_worker}"
    )

    before = exec_metrics(workers[0])
    print(f"\n=== 4. Observe worker for {args.observe_seconds:.0f}s (from seed) ===")
    peak_wait = before.get("pms_worker_max_ticket_wait_seconds", 0)
    sample_interval = 1.0
    elapsed = 0.0
    while elapsed < args.observe_seconds:
        time.sleep(sample_interval)
        elapsed += sample_interval
        sample = exec_metrics(workers[0])
        peak_wait = max(peak_wait, sample.get("pms_worker_max_ticket_wait_seconds", 0))
    after = exec_metrics(workers[0])

    matches_delta = after.get("pms_worker_matches_created_total", 0) - before.get(
        "pms_worker_matches_created_total", 0
    )
    loops_delta = after.get("pms_worker_loops_completed_total", 0) - before.get(
        "pms_worker_loops_completed_total", 0
    )
    skipped_delta = after.get("pms_worker_pairs_skipped_total", 0) - before.get(
        "pms_worker_pairs_skipped_total", 0
    )
    budget_delta = after.get("pms_worker_loop_budget_exceeded_total", 0) - before.get(
        "pms_worker_loop_budget_exceeded_total", 0
    )
    max_wait = after.get("pms_worker_max_ticket_wait_seconds", 0)

    print(f"  loops_completed: +{loops_delta:.0f}")
    print(f"  matches_created: +{matches_delta:.0f}")
    print(f"  pairs_skipped: +{skipped_delta:.0f}")
    print(f"  loop_budget_exceeded: +{budget_delta:.0f}")
    print(f"  max_ticket_wait_seconds (peak sampled): {peak_wait:.1f}")
    print(f"  max_ticket_wait_seconds (last loop): {max_wait:.1f}")

    if loops_delta <= 0:
        failures.append("worker loops did not advance during observation window")
    if matches_delta <= 0:
        failures.append("no matches created during backlog observation")
    if skipped_delta <= 0 and budget_delta <= 0:
        cumulative_skipped = after.get("pms_worker_pairs_skipped_total", 0)
        if cumulative_skipped <= 0:
            failures.append(
                "expected pairs_skipped or loop_budget_exceeded to increase under backlog"
            )
        else:
            print(
                f"  note: delta skipped=0 but cumulative pairs_skipped_total={cumulative_skipped:.0f}"
            )

    peak_wait = max(
        peak_wait,
        before.get("pms_worker_max_ticket_wait_seconds", 0),
        after.get("pms_worker_max_ticket_wait_seconds", 0),
    )
    if peak_wait < 300:
        failures.append(
            f"expected max_ticket_wait_seconds to reflect old backlog (peak {peak_wait:.1f})"
        )

    print("\n=== 5. De-correlation across replicas ===")
    sleep_values: dict[str, float] = {}
    for container in workers:
        metrics = exec_metrics(container)
        sleep_ms = metrics.get("pms_worker_jittered_sleep_ms", 0)
        sleep_values[container] = sleep_ms
        print(f"  {container}: jittered_sleep_ms={sleep_ms:.0f}")

    unique_sleeps = len(set(sleep_values.values()))
    if len(workers) >= 2 and unique_sleeps < 2:
        failures.append(
            f"expected different jittered_sleep_ms across workers, got {sleep_values}"
        )
    else:
        print(f"OK: {unique_sleeps} distinct jittered_sleep_ms value(s) across replicas")

    print("\n=== 6. Stage timing present ===")
    for name in (
        "pms_worker_lease_ops_ms_count",
        "pms_worker_ticket_fetch_ms_count",
        "pms_worker_pair_search_ms_count",
    ):
        count = after.get(name, 0)
        print(f"  {name}: {count:.0f}")
        if count <= 0:
            failures.append(f"{name} never incremented")

    print(f"\n=== Result at {datetime.now(timezone.utc).isoformat()} ===")
    if failures:
        print("FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("PASS: backlog resistance, metrics, and de-correlation checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
