#!/usr/bin/env python3
"""Run elasticity load test with HPA/node/metrics sampling."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NS = "pms"
METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$"
)


def run(cmd: list[str], *, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=capture, text=True, check=check)


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "-n", NS, *args]


def stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def sum_metric(text: str, metric_name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = METRIC_LINE.match(line.strip())
        if match and match.group("name") == metric_name:
            total += float(match.group("value"))
    return total


def fetch_worker_matches() -> float:
    pods = run(kubectl("get", "pods", "-l", "app=worker", "-o", "jsonpath={.items[*].metadata.name}")).stdout.split()
    total = 0.0
    for pod in pods:
        if not pod:
            continue
        text = run(
            kubectl(
                "exec", pod, "--", "python", "-c",
                "import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=15).read().decode())",
            )
        ).stdout
        total += sum_metric(text, "pms_worker_matches_created_total")
    return total


def get_hpa_status() -> dict[str, dict[str, int | str]]:
    result = run(["kubectl", "-n", NS, "get", "hpa", "-o", "json"], check=False)
    if result.returncode != 0:
        return {}
    data = json.loads(result.stdout)
    out: dict[str, dict[str, int | str]] = {}
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        status = item.get("status", {})
        out[name] = {
            "min": item["spec"].get("minReplicas", 1),
            "max": item["spec"].get("maxReplicas", 1),
            "current": status.get("currentReplicas", 0),
            "desired": status.get("desiredReplicas", 0),
        }
    return out


def get_node_count() -> int:
    result = run(["kubectl", "get", "nodes", "-o", "json"], check=False)
    if result.returncode != 0:
        return 0
    data = json.loads(result.stdout)
    ready = 0
    for item in data.get("items", []):
        for cond in item.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                ready += 1
                break
    return ready


def get_ingress_ip() -> str:
    return run(kubectl("get", "ingress", "pms-api", "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}")).stdout.strip()


def wait_for_ready(timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        nodes = get_node_count()
        pods = run(kubectl("get", "pods", "-o", "jsonpath={.items[?(@.status.phase=='Running')].metadata.name}"), check=False).stdout
        running = len([p for p in pods.split() if p])
        api_ready = run(
            kubectl("get", "pods", "-l", "app=api", "-o", "jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}"),
            check=False,
        ).stdout
        api_count = api_ready.count("True")
        ip = get_ingress_ip()
        print(f"  [{stamp()}] nodes={nodes} running_pods={running} api_ready={api_count} ingress={ip or 'pending'}", flush=True)
        if nodes >= 1 and api_count >= 1 and ip:
            try:
                with urllib.request.urlopen(f"http://{ip}/health", timeout=5) as response:
                    if response.status < 500:
                        return
            except OSError:
                pass
        time.sleep(15)
    raise RuntimeError("Cluster not ready within timeout")


class Sampler:
    def __init__(self, path: Path, interval: float = 15.0) -> None:
        self.path = path
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev_matches: float | None = None
        self._prev_at: float | None = None

    def _sample_once(self) -> dict:
        now = time.time()
        matches = fetch_worker_matches()
        rate = None
        if self._prev_matches is not None and self._prev_at is not None:
            elapsed = max(now - self._prev_at, 0.001)
            rate = (matches - self._prev_matches) / elapsed
        self._prev_matches = matches
        self._prev_at = now
        row = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "nodes": get_node_count(),
            "hpa": get_hpa_status(),
            "matches_total": matches,
            "matches_per_sec": rate,
        }
        self.samples.append(row)
        hpa_str = ", ".join(
            f"{k}={v['current']}/{v['desired']}" for k, v in row["hpa"].items()
        ) if row["hpa"] else "no-hpa"
        rate_str = f"{rate:.2f}/s" if rate is not None else "n/a"
        line = f"{row['timestamp']}  nodes={row['nodes']}  {hpa_str}  matches={int(matches)}  rate={rate_str}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return row

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_once()
            except Exception as exc:
                err = f"{stamp()}  SAMPLE ERROR: {exc}"
                print(err, flush=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(err + "\n")
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"# Elasticity sampling started {stamp()}\n", encoding="utf-8")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.samples


def main() -> int:
    out_dir = ROOT / "loadtests"
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    sample_log = out_dir / f"elasticity-samples-{ts}.log"
    k6_log = out_dir / f"elasticity-k6-{ts}.json"

    print(f"Waiting for cluster readiness...", flush=True)
    wait_for_ready()

    ip = get_ingress_ip()
    base_url = f"http://{ip}"
    print(f"BASE_URL={base_url}", flush=True)

    sampler = Sampler(sample_log, interval=15.0)
    sampler.start()
    time.sleep(2)

    print(f"Starting k6 elasticity test (~5.5 min)...", flush=True)
    k6 = subprocess.run(
        [
            "k6", "run",
            "-e", f"BASE_URL={base_url}",
            "--summary-export", str(k6_log.with_suffix(".summary.json")),
            "loadtests/elasticity.js",
        ],
        cwd=ROOT,
        text=True,
    )
    samples = sampler.stop()

    summary_path = k6_log.with_suffix(".summary.json")
    summary = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    results_path = out_dir / f"results-gke-elasticity-{ts}.md"
    write_results(results_path, base_url, samples, summary, k6.returncode)
    print(f"\nWrote {results_path}", flush=True)
    print(f"Sample log: {sample_log}", flush=True)
    return k6.returncode


def write_results(
    path: Path,
    base_url: str,
    samples: list[dict],
    summary: dict,
    k6_exit: int,
) -> None:
    def first_scale(field: str, threshold: int) -> str | None:
        for row in samples:
            if field == "nodes" and row["nodes"] >= threshold:
                return row["timestamp"]
            if field.startswith("hpa:"):
                _, hpa_name, key = field.split(":")
                hpa = row.get("hpa", {}).get(hpa_name, {})
                if hpa.get(key, 0) >= threshold:
                    return row["timestamp"]
        return None

    peak_rate = max((r["matches_per_sec"] or 0) for r in samples) if samples else 0
    stable_rate = None
    stable_at = None
    if samples:
        window = [r for r in samples if (r["matches_per_sec"] or 0) > 0]
        if window:
            target = peak_rate * 0.85 if peak_rate else 0
            for row in window:
                rate = row["matches_per_sec"] or 0
                if rate >= target and target > 0:
                    stable_at = row["timestamp"]
                    stable_rate = rate
                    break

    metrics = summary.get("metrics", {})
    tickets_created = metrics.get("tickets_created", {}).get("count", 0)
    tickets_rejected = metrics.get("tickets_rejected", {}).get("count", 0)
    p95 = metrics.get("ticket_create_latency_ms", {}).get("p(95)", 0)

    lines = [
        "# GKE elasticity load test results",
        "",
        f"**Generated:** {stamp()}",
        "**Cluster:** GKE zonal, `europe-west1-b`, project `se-proto`",
        "**Machine type:** e2-standard-4 (autoscaling 1–5 nodes)",
        "**Load script:** `loadtests/elasticity.js` (30 → 80 → 30 VUs, ~5.5 min)",
        f"**BASE_URL:** {base_url}",
        f"**k6 exit code:** {k6_exit}",
        "",
        "## k6 summary",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Tickets created | {tickets_created:,} |",
        f"| Tickets rejected | {tickets_rejected:,} |",
        f"| p95 ticket-create latency | {p95:.2f}ms |",
        "",
        "## Elasticity timeline",
        "",
        "| Event | Timestamp |",
        "|-------|-----------|",
    ]

    test_start = samples[0]["timestamp"] if samples else "n/a"
    lines.append(f"| Test sampling start | {test_start} |")

    api_scale_2 = first_scale("hpa:api:current", 2)
    worker_scale_2 = first_scale("hpa:worker:current", 2)
    node_scale_2 = first_scale("nodes", 2)

    lines.append(f"| HPA api → 2+ replicas | {api_scale_2 or 'not observed'} |")
    lines.append(f"| HPA worker → 2+ replicas | {worker_scale_2 or 'not observed'} |")
    lines.append(f"| Cluster → 2+ nodes | {node_scale_2 or 'not observed'} |")
    lines.append(f"| Peak matches/s (sampled) | {peak_rate:.2f} |")
    lines.append(f"| Throughput stabilizes (~85% peak) | {stable_at or 'not observed'} ({stable_rate:.2f}/s)" if stable_rate else f"| Throughput stabilizes (~85% peak) | {stable_at or 'not observed'} |")

    lines.extend([
        "",
        "## Sample log (every 15s)",
        "",
        "```",
    ])
    for row in samples:
        hpa_str = ", ".join(f"{k}={v['current']}/{v['desired']}" for k, v in row["hpa"].items()) if row["hpa"] else "no-hpa"
        rate = row["matches_per_sec"]
        rate_str = f"{rate:.2f}/s" if rate is not None else "n/a"
        lines.append(f"{row['timestamp']}  nodes={row['nodes']}  {hpa_str}  matches={int(row['matches_total'])}  rate={rate_str}")
    lines.extend(["```", ""])

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
