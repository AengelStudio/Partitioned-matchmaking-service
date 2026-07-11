"""Extract scale_out.js counters from a k6 --summary-export JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def metric_values(summary: dict, name: str) -> dict:
    metrics = summary.get("metrics", {})
    entry = metrics.get(name, {})
    return entry.get("values", {})


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse k6 summary export JSON.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    summary = json.loads(args.path.read_text(encoding="utf-8"))
    created = metric_values(summary, "tickets_created").get("count", 0.0)
    rejected = metric_values(summary, "tickets_rejected").get("count", 0.0)
    p95 = metric_values(summary, "ticket_create_latency_ms").get("p(95)", 0.0)

    print(f"tickets_created={created:.0f}")
    print(f"tickets_rejected={rejected:.0f}")
    print(f"p95_ms={p95:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
