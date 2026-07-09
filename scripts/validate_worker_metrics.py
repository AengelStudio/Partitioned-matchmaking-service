import argparse
import re
import sys
import time
import urllib.request

EXPECTED_METRICS = [
    "pms_worker_info",
    "pms_worker_matches_created_total",
    "pms_worker_matches_failed_total",
    "pms_worker_rollbacks_total",
    "pms_worker_leases_claimed_total",
    "pms_worker_leases_renewed_total",
    "pms_worker_lease_claim_failures_total",
    "pms_worker_reservations_expired_total",
    "pms_worker_reservations_cleaned_total",
    "pms_worker_pair_search_runs_total",
    "pms_worker_loop_duration_ms",
    "pms_worker_loops_completed_total",
    "pms_worker_tickets_fetched_total",
    "pms_worker_pairs_found_total",
    "pms_worker_pairs_skipped_total",
    "pms_worker_loop_budget_exceeded_total",
    "pms_worker_owned_partitions_count",
    "pms_worker_tickets_fetched_last_loop",
    "pms_worker_matches_created_last_loop",
    "pms_worker_max_ticket_wait_seconds",
    "pms_worker_avg_ticket_wait_seconds",
    "pms_worker_jittered_sleep_ms",
    "pms_worker_loop_budget_exceeded_total",
    "pms_worker_lease_ops_ms",
    "pms_worker_lease_ops_ms_sum",
    "pms_worker_lease_ops_ms_count",
    "pms_worker_ticket_fetch_ms",
    "pms_worker_ticket_fetch_ms_sum",
    "pms_worker_ticket_fetch_ms_count",
    "pms_worker_pair_search_ms",
    "pms_worker_pair_search_ms_sum",
    "pms_worker_pair_search_ms_count",
    "pms_worker_match_creation_ms",
    "pms_worker_match_creation_ms_sum",
    "pms_worker_match_creation_ms_count",
]

LINE_PATTERN = re.compile(r"^(?P<name>\w+)(\{[^}]*\})?\s+(?P<value>-?[0-9.]+)$")


def fetch_metrics(url: str) -> dict[str, float]:
    with urllib.request.urlopen(url, timeout=5) as response:
        text = response.read().decode("utf-8")

    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = LINE_PATTERN.match(line.strip())
        if match:
            values[match.group("name")] = float(match.group("value"))
    return values


def check_expected_present(values: dict[str, float]) -> list[str]:
    return [name for name in EXPECTED_METRICS if name not in values]


def report_deltas(before: dict[str, float], after: dict[str, float]) -> None:
    counters = [
        "pms_worker_loops_completed_total",
        "pms_worker_matches_created_total",
        "pms_worker_matches_failed_total",
        "pms_worker_leases_claimed_total",
        "pms_worker_leases_renewed_total",
        "pms_worker_reservations_cleaned_total",
        "pms_worker_tickets_fetched_total",
        "pms_worker_pairs_found_total",
        "pms_worker_pair_search_runs_total",
    ]
    print("\nDeltas over the sample interval:")
    for name in counters:
        delta = after.get(name, 0.0) - before.get(name, 0.0)
        print(f"  {name}: +{delta:.0f}")

    print("\nLast-loop timing snapshot (ms):")
    for name in (
        "pms_worker_lease_ops_ms",
        "pms_worker_ticket_fetch_ms",
        "pms_worker_pair_search_ms",
        "pms_worker_match_creation_ms",
        "pms_worker_loop_duration_ms",
    ):
        print(f"  {name}: {after.get(name, 0.0):.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the matchmaking worker /metrics endpoint under load."
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds to wait between the two samples used for delta reporting",
    )
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/metrics"

    print(f"Fetching {url} ...")
    before = fetch_metrics(url)

    missing = check_expected_present(before)
    if missing:
        print("FAIL: missing expected metrics:")
        for name in missing:
            print(f"  - {name}")
        return 1
    print(f"OK: all {len(EXPECTED_METRICS)} expected metrics are present")

    print(f"Waiting {args.interval:.1f}s for another loop iteration ...")
    time.sleep(args.interval)
    after = fetch_metrics(url)

    if after.get("pms_worker_loops_completed_total", 0.0) <= before.get(
        "pms_worker_loops_completed_total", 0.0
    ):
        print("WARNING: loops_completed_total did not advance; worker may be stuck")

    report_deltas(before, after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
