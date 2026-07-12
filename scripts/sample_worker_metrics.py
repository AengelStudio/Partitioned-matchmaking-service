"""Sample Prometheus counters from worker /metrics, optionally on an interval."""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$")


def sum_metric(text: str, metric_name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = LINE.match(line.strip())
        if match and match.group("name") == metric_name:
            total += float(match.group("value"))
    return total


def fetch_metric(url: str, metric_name: str) -> float:
    with urllib.request.urlopen(url, timeout=15) as response:
        text = response.read().decode("utf-8")
    return sum_metric(text, metric_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sum a Prometheus metric from /metrics.")
    parser.add_argument("--url", default="http://localhost:9090/metrics")
    parser.add_argument(
        "--metric",
        default="pms_worker_matches_created_total",
        help="Exact metric name to sum across all label sets",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        help="If > 0, poll every N seconds and print rate (delta / interval)",
    )
    args = parser.parse_args()

    previous = None
    previous_at = None

    while True:
        try:
            value = fetch_metric(args.url, args.metric)
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        now = time.time()
        stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        if args.interval <= 0:
            print(f"{value:.0f}")
            return 0

        if previous is None:
            print(f"{stamp}  {args.metric}={value:.0f}")
        else:
            elapsed = max(now - previous_at, 0.001)
            rate = (value - previous) / elapsed
            print(f"{stamp}  {args.metric}={value:.0f}  rate={rate:.2f}/s")

        previous = value
        previous_at = now
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
