#!/usr/bin/env python3
"""Automated GKE scale-out benchmark: deploy, run k6 via ingress, write results, scale to 0."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
K6_DURATION_SECONDS = 180
K6_SMOKE_DURATION_SECONDS = 120
INGRESS_ERROR_CHECK_GRACE_SECONDS = 90
PLACEHOLDER_PASSWORDS = frozenset(
    {"", "replace_me", "changeme", "your-postgres-password", "your-password"}
)
INGRESS_FATAL_PATTERNS = (
    "error syncing load balancer",
    "quota exceeded",
    "failed to create",
    "does not have any active node",
    "does not exist",
    "no healthy upstream",
    "backendconfig",
    "invalid resource",
    "permission denied",
    "insufficient",
)
METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$"
)
LABELED_METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>-?[0-9.eE+]+)$"
)
METRIC_LABEL_KV = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
REJECTION_REASON_ORDER = ("rate_limit", "tenant_quota", "partition_overload", "load_shedding")
REJECTION_REASON_HTTP = {
    "rate_limit": "429",
    "tenant_quota": "429",
    "partition_overload": "503",
    "load_shedding": "503",
}
WINDOWS_PATH_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Cloud SDK/google-cloud-sdk/bin",
    Path(os.environ.get("ProgramFiles", "")) / "Google/Cloud SDK/google-cloud-sdk/bin",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links",
    Path(os.environ.get("ProgramFiles", "")) / "k6",
)
T = TypeVar("T")


@dataclass
class BenchmarkResult:
    nodes: int
    replicas: str
    tickets_created: int
    tickets_rejected: int
    tickets_per_sec: float
    rejected_per_sec: float
    matches_delta: int
    matches_per_sec: float
    pair_ratio_pct: float
    p95_latency_ms: float
    matches_before: int
    matches_after: int
    waiting_tickets_before: int
    waiting_tickets_after: int
    run_duration_seconds: int
    rejections_by_reason: dict[str, int]
    rejections_by_tenant: dict[str, dict[str, int]]
    fresh_state: bool


class StepTracker:
    def __init__(self, total: int) -> None:
        self.total, self.current = total, 0

    def begin(self, message: str) -> None:
        self.current += 1
        print(f"\n[Step {self.current}/{self.total}] {message}", flush=True)


class TaskProgress:
    BAR_WIDTH = 28

    def __init__(self, label: str, total_seconds: float | None = None) -> None:
        self.label, self.total_seconds = label, total_seconds
        self.started_at = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_width = 0
        self._write_lock = threading.Lock()

    @staticmethod
    def _fmt(seconds: float) -> str:
        minutes, seconds = divmod(max(0, int(seconds)), 60)
        return f"{minutes}m{seconds:02d}s"

    def _render(self, detail: str = "") -> str:
        elapsed = time.time() - self.started_at
        if self.total_seconds and self.total_seconds > 0:
            ratio = min(1.0, elapsed / self.total_seconds)
            filled = int(self.BAR_WIDTH * ratio)
            bar = "=" * filled + ">" + " " * max(0, self.BAR_WIDTH - filled - 1)
            pct = f"{ratio * 100:5.1f}%"
            timing = f"{self._fmt(elapsed)} / {self._fmt(self.total_seconds)}"
        else:
            spin = "|/-\\"[int(elapsed * 4) % 4]
            bar, pct, timing = spin * 2 + "." * (self.BAR_WIDTH - 2), "  ... ", self._fmt(elapsed)
        return f"\r  [{bar}] {pct} {timing}  {self.label}" + (f" ({detail})" if detail else "")

    def _write(self, line: str, *, newline: bool = False) -> None:
        line = line.lstrip("\r")
        with self._write_lock:
            width = max(self._last_width, len(line))
            sys.stdout.write("\r" + line.ljust(width) + ("\n" if newline else ""))
            sys.stdout.flush()
            self._last_width = 0 if newline else len(line)

    def update(self, detail: str = "") -> None:
        self._write(self._render(detail))

    def finish(self, message: str = "done") -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._write(
            f"  [{self.label}] {message} ({self._fmt(time.time() - self.started_at)})",
            newline=True,
        )

    def _spin(self) -> None:
        while not self._stop.wait(0.25):
            self.update()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def __enter__(self) -> TaskProgress:
        self.start_background()
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is KeyboardInterrupt:
            self.finish("interrupted")
        elif exc_type is not None:
            self.finish("failed")
        else:
            self.finish()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def augment_path() -> None:
    additions = [str(path) for path in WINDOWS_PATH_CANDIDATES if path.is_dir()]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, os.environ.get("PATH", "")])


def load_project_env() -> None:
    if not ENV_FILE.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except ImportError:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_postgres_password(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    for key in ("PG_PASS", "PMS_POSTGRES_PASSWORD"):
        if value := os.environ.get(key):
            return value
    return None


def validate_postgres_password(password: str | None) -> str:
    if not password:
        raise RuntimeError(
            "Set PG_PASS in .env (or PMS_POSTGRES_PASSWORD / --postgres-password) before deploying"
        )
    if password.strip().lower() in PLACEHOLDER_PASSWORDS:
        raise RuntimeError(
            f"Postgres password looks like a placeholder ({password!r}). "
            "Set a real PG_PASS in .env."
        )
    return password


def resolve_executable(name: str) -> str:
    candidates = [name]
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        candidates += [name + suffix for suffix in (".cmd", ".exe", ".bat")]
    for candidate in candidates:
        if found := shutil.which(candidate):
            return found
    raise FileNotFoundError(
        f"Cannot find '{name}' on PATH. Install it or open a shell where '{name}' works, "
        "then re-run this script."
    )


def prepare_cmd(cmd: list[str]) -> list[str]:
    return [resolve_executable(cmd[0]), *cmd[1:]] if cmd else cmd


def run(
    cmd: list[str], *, cwd: Path = ROOT, check: bool = True,
    capture: bool = False, quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = prepare_cmd(cmd)
    if not quiet:
        print("+", " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=capture)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Failed to run: {cmd[0]} ({exc})") from exc


def run_with_progress(
    cmd: list[str], label: str, *, expected_seconds: float | None = None, cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    cmd = prepare_cmd(cmd)
    print("+", " ".join(cmd), flush=True)
    process = subprocess.Popen(cmd, cwd=cwd)
    progress = TaskProgress(label, expected_seconds)
    progress.start_background()
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        progress.finish("interrupted")
        if process.poll() is None:
            process.terminate()
        raise
    except BaseException:
        progress.finish("failed")
        if process.poll() is None:
            process.terminate()
        raise
    if return_code:
        progress.finish(f"failed: exit {return_code}")
        raise subprocess.CalledProcessError(return_code, cmd)
    progress.finish()
    return subprocess.CompletedProcess(cmd, return_code, "", "")


def kubectl(*parts: object, namespace: str | None = None) -> list[str]:
    return ["kubectl", *(["-n", namespace] if namespace else []), *map(str, parts)]


def wait_until(
    label: str, timeout: int, interval: int,
    check: Callable[[], tuple[bool, str, T | None]],
    success: str | Callable[[T | None], str], timeout_error: str,
) -> T | None:
    progress = TaskProgress(label, timeout)
    progress.start_background()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            done, detail, value = check()
            if done:
                progress.finish(success(value) if callable(success) else success)
                return value
            progress.update(detail)
            time.sleep(interval)
    except KeyboardInterrupt:
        progress.finish("interrupted")
        raise
    except BaseException:
        progress.finish("failed")
        raise
    progress.finish("timed out")
    raise RuntimeError(timeout_error)


def fetch_metrics_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read().decode("utf-8")


def sum_metric(url: str, metric_name: str) -> float:
    total = 0.0
    for line in fetch_metrics_text(url).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = METRIC_LINE.match(line.strip())
        if match and match.group("name") == metric_name:
            total += float(match.group("value"))
    return total


def parse_metric_labels(raw: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in METRIC_LABEL_KV.finditer(raw)}


def parse_rejection_metrics_text(text: str) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    by_reason: dict[str, int] = {}
    by_tenant: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = LABELED_METRIC_LINE.match(line.strip())
        if not match or match.group("name") != "tickets_rejected_total":
            continue
        labels = parse_metric_labels(match.group("labels"))
        reason = labels.get("reason", "unknown")
        tenant_id = labels.get("tenant_id", "unknown")
        count = int(float(match.group("value")))
        by_reason[reason] = by_reason.get(reason, 0) + count
        by_tenant.setdefault(tenant_id, {})[reason] = (
            by_tenant.setdefault(tenant_id, {}).get(reason, 0) + count
        )
    return by_reason, by_tenant


def parse_rejection_metrics(url: str) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    return parse_rejection_metrics_text(fetch_metrics_text(url))


def sum_metric_text(text: str, metric_name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = METRIC_LINE.match(line.strip())
        if match and match.group("name") == metric_name:
            total += float(match.group("value"))
    return total


def format_rejection_share(count: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{100 * count / total:.1f}%"


def format_rejection_tenant_summary(by_tenant: dict[str, dict[str, int]], reason: str) -> str:
    counts = [tenant.get(reason, 0) for tenant in by_tenant.values() if tenant.get(reason, 0) > 0]
    if not counts:
        return f"No `{reason}` rejections recorded."
    tenant_count = len(counts)
    low, high = min(counts), max(counts)
    if tenant_count == 1:
        return f"All `{reason}` rejections came from one tenant ({low:,})."
    if high - low <= max(5, high * 0.05):
        return (
            f"Rejections are spread evenly across {tenant_count} tenants "
            f"({low:,}–{high:,} each for `{reason}`). "
            "That pattern means per-tenant rate limiting, not one hot partition or tenant."
        )
    return (
        f"`{reason}` rejections across {tenant_count} tenants ranged from {low:,} to {high:,} "
        f"(uneven spread — check for hot tenants or partitions)."
    )


def parse_k6_summary(path: Path) -> dict[str, float]:
    metrics = json.loads(path.read_text(encoding="utf-8")).get("metrics", {})

    def value(metric: str, key: str) -> float:
        data = metrics.get(metric, {})
        if key in data:
            return float(data[key])
        return float(data.get("values", {}).get(key, 0.0))

    return {
        "tickets_created": value("tickets_created", "count"),
        "tickets_rejected": value("tickets_rejected", "count"),
        "p95_ms": value("ticket_create_latency_ms", "p(95)"),
    }


def write_results_file(
    rows: list[BenchmarkResult], path: Path, *, zone: str, project_id: str, access_mode: str,
    machine_type: str,
) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    duration = rows[0].run_duration_seconds if rows else K6_DURATION_SECONDS
    lines = [
        "# GKE scale-out benchmark results", "", f"**Generated:** {timestamp}  ",
        f"**Cluster:** GKE zonal, `{zone}`, project `{project_id}`  ",
        f"**Machine type:** {machine_type}  ",
        "**Load script:** `loadtests/scale_out.js` (multi-tenant)  ",
        f"**Run duration:** {duration}s per node count  ", f"**Access:** {access_mode}", "",
        "| Nodes | API / Worker / Dispatcher | Tickets created | Tickets rejected | matches_created/s | Tickets:matches ratio | p95 ticket-create latency |",
        "|-------|---------------------------|-----------------|------------------|-------------------|----------------------|---------------------------|",
    ]
    lines += [
        f"| {r.nodes} | {r.replicas} | {r.tickets_created} ({r.tickets_per_sec}/s) | "
        f"{r.tickets_rejected} ({r.rejected_per_sec}/s) | "
        f"{r.matches_per_sec} ({r.matches_delta} matches / {r.run_duration_seconds}s) | "
        f"{r.pair_ratio_pct}% | {r.p95_latency_ms}ms |" for r in rows
    ]
    lines += ["", "## Raw worker metrics", ""]
    lines += [
        f"- **{r.nodes} node(s):** matches before={r.matches_before}, "
        f"after={r.matches_after}, delta={r.matches_delta}; "
        f"waiting tickets before={r.waiting_tickets_before}, after={r.waiting_tickets_after}"
        for r in rows
    ]
    lines += [
        "", "## Admission control rejections", "",
        "From API `/metrics` after each run (`tickets_rejected_total`), summed across all API pods.", "",
    ]
    for row in rows:
        total = sum(row.rejections_by_reason.values())
        lines.append(f"### {row.nodes} node(s)")
        lines += [
            "",
            "| Reason | HTTP | Count | Share |",
            "|--------|------|------:|------:|",
        ]
        for reason in REJECTION_REASON_ORDER:
            count = row.rejections_by_reason.get(reason, 0)
            http = REJECTION_REASON_HTTP.get(reason, "—")
            lines.append(
                f"| `{reason}` | {http} | {count:,} | {format_rejection_share(count, total)} |"
            )
        extra_reasons = sorted(
            reason for reason in row.rejections_by_reason if reason not in REJECTION_REASON_ORDER
        )
        for reason in extra_reasons:
            count = row.rejections_by_reason[reason]
            lines.append(
                f"| `{reason}` | — | {count:,} | {format_rejection_share(count, total)} |"
            )
        lines.append("")
        if total:
            dominant = max(row.rejections_by_reason, key=row.rejections_by_reason.get)
            lines.append(format_rejection_tenant_summary(row.rejections_by_tenant, dominant))
        else:
            lines.append("No admission-control rejections recorded in API metrics.")
        lines.append("")
    if any(r.fresh_state for r in rows):
        lines.append("- Postgres PVC reset before run (`--reset-postgres`) for clean ticket/match state.")
    lines += [
        "## Notes", "",
        "- Worker deployment restarted before each run so partition leases redistribute.",
        "- Worker and API metrics are summed across all pods (not a single service/LB hop).",
        "- Node pool scaled to 0 automatically after the script finished (unless `--skip-teardown`).",
        "- High `rate_limit` (429) counts are expected: `scale_out.js` ramps to 100 VUs while each "
        "tenant is capped at 300 ticket creates/minute (`DEFAULT_TICKET_RATE_LIMIT_PER_MINUTE`).",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Results written to {path}", flush=True)


class BenchmarkRunner:
    DATA_STORES = ("app=postgres", "app=redis")
    APP_STACK = ("app=api", "app=worker")
    STATEFUL_STORES = (
        ("postgres", "postgres-data-postgres-0", "infra/k8s/postgres.yaml"),
        ("redis", "redis-data-redis-0", "infra/k8s/redis.yaml"),
    )

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.node_counts = [1, 3, 5] if args.all else [args.node_count]
        self.results: list[BenchmarkResult] = []
        self.postgres_password = validate_postgres_password(
            resolve_postgres_password(args.postgres_password)
        )
        self.k6_duration = K6_SMOKE_DURATION_SECONDS if args.smoke else K6_DURATION_SECONDS
        if args.smoke and args.ingress_timeout == 480:
            args.ingress_timeout = 300
        step_total = 2 if args.preflight else 8 + len(self.node_counts) * 3
        self.steps = StepTracker(step_total)

    @property
    def ns(self) -> str:
        return self.args.namespace

    def cluster_cmd(self, action: str, *middle: object, tail: tuple[object, ...] = ()) -> list[str]:
        a = self.args
        return [
            "gcloud", "container", "clusters", action, a.cluster_name, *map(str, middle),
            "--zone", a.zone, "--project", a.project_id, *map(str, tail),
        ]

    def resize_cmd(self, nodes: int) -> list[str]:
        return self.cluster_cmd(
            "resize", "--node-pool", self.args.node_pool, "--num-nodes", nodes,
            tail=("--quiet",),
        )

    def verify_tools(self) -> None:
        missing = []
        for tool in ("gcloud", "kubectl", "k6", "docker"):
            try:
                resolve_executable(tool)
            except FileNotFoundError:
                missing.append(tool)
        if missing:
            raise RuntimeError(
                f"Missing required tools: {', '.join(missing)}. "
                "Add them to PATH (Google Cloud SDK bin, k6, Docker Desktop)."
            )
        print("Tools OK: gcloud, kubectl, k6, docker", flush=True)

    def preflight(self) -> None:
        self.steps.begin("Preflight checks")
        print(f"  Postgres password: loaded ({len(self.postgres_password)} chars)", flush=True)
        run(self.cluster_cmd("get-credentials"))
        self.wait_for_cluster(timeout=120)
        print("  Cluster credentials OK.", flush=True)

    def wait_for_cluster(self, timeout: int = 300) -> None:
        a = self.args

        def check() -> tuple[bool, str, None]:
            cluster = run(
                self.cluster_cmd("describe", tail=("--format=value(status)",)),
                capture=True, quiet=True, check=False,
            ).stdout.strip()
            pool = run([
                "gcloud", "container", "node-pools", "describe", a.node_pool,
                "--cluster", a.cluster_name, "--zone", a.zone, "--project", a.project_id,
                "--format=value(status)",
            ], capture=True, quiet=True, check=False).stdout.strip()
            return cluster == "RUNNING" and pool == "RUNNING", f"cluster={cluster} pool={pool}", None

        wait_until(
            "Waiting for cluster and node pool", timeout, 10, check, "cluster ready",
            "Timed out waiting for GKE cluster/node pool to reach RUNNING",
        )

    @staticmethod
    def schedulable(line: str) -> bool:
        line = line.strip()
        return bool(line) and " Ready " in f" {line} " and "SchedulingDisabled" not in line

    def current_node_count(self) -> int:
        output = run(
            kubectl("get", "nodes", "--no-headers"),
            capture=True, quiet=True, check=False,
        ).stdout
        return sum(self.schedulable(line) for line in output.splitlines())

    def uncordon_nodes(self) -> None:
        output = run(
            kubectl("get", "nodes", "-o", 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'),
            capture=True, quiet=True,
        ).stdout
        for node in map(str.strip, output.splitlines()):
            if node:
                run(kubectl("uncordon", node), quiet=True)

    def wait_for_nodes(self, expected: int, timeout: int = 420) -> None:
        def check() -> tuple[bool, str, int]:
            self.uncordon_nodes()
            output = run(kubectl("get", "nodes", "--no-headers"), capture=True, quiet=True).stdout
            ready = sum(self.schedulable(line) for line in output.splitlines())
            return ready >= expected, f"{ready}/{expected} schedulable", ready

        try:
            wait_until(
                f"Waiting for {expected} schedulable node(s)", timeout, 10, check,
                lambda ready: f"{ready} node(s) schedulable",
                f"Timed out waiting for {expected} schedulable node(s)",
            )
        except RuntimeError:
            run(kubectl("get", "nodes", "-o", "wide"), check=False)
            raise
        run(kubectl("get", "nodes"))

    @staticmethod
    def pod_ready(line: str) -> bool:
        parts = line.split()
        if len(parts) < 2 or "/" not in parts[1]:
            return False
        try:
            ready, total = map(int, parts[1].split("/", 1))
            return ready == total and total > 0
        except ValueError:
            return False

    def pod_diagnostics(self, selector: str) -> None:
        print("\n  --- Pod status ---", flush=True)
        run(kubectl("get", "pods", "-l", selector, "-o", "wide", namespace=self.ns), check=False)
        output = run(kubectl(
            "get", "pods", "-l", selector, "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}', namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout
        for pod in map(str.strip, output.splitlines()):
            if pod:
                print(f"\n  --- Recent events for {pod} ---", flush=True)
                run(kubectl(
                    "get", "events", "--field-selector", f"involvedObject.name={pod}",
                    "--sort-by=.lastTimestamp", namespace=self.ns,
                ), check=False)

    def wait_for_pods(self, selector: str, timeout: int = 420) -> None:
        last_error = ""

        def check() -> tuple[bool, str, None]:
            nonlocal last_error
            result = run(kubectl(
                "wait", "--for=condition=ready", "pod", "-l", selector,
                "--timeout=15s", namespace=self.ns,
            ), check=False, capture=True, quiet=True)
            if result.returncode == 0:
                return True, "", None
            combined = (result.stderr or result.stdout or "").strip()
            last_error = combined.splitlines()[-1][:120] if combined else "pods not ready"
            lines = [line for line in run(kubectl(
                "get", "pods", "-l", selector, "--no-headers", namespace=self.ns,
            ), capture=True, quiet=True, check=False).stdout.splitlines() if line.strip()]
            detail = "no pods yet" if not lines else f"{sum(map(self.pod_ready, lines))}/{len(lines)} ready"
            return False, detail, None

        try:
            wait_until(f"Waiting for pods ({selector})", timeout, 10, check, "ready", "")
        except RuntimeError:
            self.pod_diagnostics(selector)
            raise RuntimeError(
                f"Timed out waiting for pods with selector {selector}. Last error: {last_error}"
            ) from None

    def wait_for_stack(self, selectors: tuple[str, ...]) -> None:
        for selector in selectors:
            self.wait_for_pods(selector)

    def delete_ingress(self) -> None:
        subprocess.run(prepare_cmd([
            "kubectl", "delete", "-f", "infra/k8s/ingress.yaml",
            "--ignore-not-found", "--wait=true",
        ]), cwd=ROOT, check=False)
        deadline = time.time() + 120
        while time.time() < deadline:
            output = run(kubectl(
                "get", "ingress", "pms-api", "--ignore-not-found", "--no-headers",
                namespace=self.ns,
            ), capture=True, quiet=True).stdout
            if not output.strip():
                return
            time.sleep(5)
        raise RuntimeError("Timed out waiting for ingress deletion to finish")

    def verify_service_endpoints(self, service: str) -> None:
        addresses = run(kubectl(
            "get", "endpoints", service, "-o",
            "jsonpath={.subsets[*].addresses[*].ip}", namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout.strip()
        if addresses:
            return
        self.pod_diagnostics(f"app={service}" if service == "api" else f"app={service}")
        raise RuntimeError(
            f"Service '{service}' has no ready endpoints. Fix the workload before creating ingress."
        )

    def current_node_names(self) -> set[str]:
        output = run(
            kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"),
            capture=True, quiet=True, check=False,
        ).stdout.strip()
        return {name for name in output.split() if name}

    def pvc_selected_node(self, pvc_name: str) -> str | None:
        output = run(kubectl(
            "get", "pvc", pvc_name,
            "-o", "jsonpath={.metadata.annotations.volume\\.kubernetes\\.io/selected-node}",
            namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout.strip()
        return output or None

    def pod_scheduling_issue(self, pod_name: str) -> str | None:
        phase = run(kubectl(
            "get", "pod", pod_name, "-o", "jsonpath={.status.phase}", namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout.strip()
        if phase != "Pending":
            return None
        events = run(kubectl(
            "get", "events", "--field-selector", f"involvedObject.name={pod_name}",
            "--sort-by=.lastTimestamp", namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout
        for line in reversed(events.splitlines()):
            lowered = line.lower()
            if "persistentvolume's node affinity" in lowered:
                return line.strip()
            if "didn't match persistentvolume's node affinity" in lowered:
                return line.strip()
        return None

    def stateful_store_needs_reset(self, app: str, pvc_name: str) -> bool:
        nodes = self.current_node_names()
        if not nodes:
            return False
        selected = self.pvc_selected_node(pvc_name)
        if selected and selected not in nodes:
            return True
        return self.pod_scheduling_issue(f"{app}-0") is not None

    def wait_stateful_pod_deleted(self, app: str, timeout: int = 120) -> None:
        wait_until(
            f"Waiting for {app}-0 deletion", timeout, 5,
            lambda: (
                run(kubectl(
                    "get", "pod", f"{app}-0", "--ignore-not-found", "--no-headers",
                    namespace=self.ns,
                ), capture=True, quiet=True, check=False).stdout.strip() == "",
                "",
                None,
            ),
            "deleted",
            f"Timed out waiting for {app}-0 to be deleted",
        )

    def reset_stateful_store(self, app: str, pvc_name: str, manifest: str) -> None:
        print(f"  Resetting {app} (delete PVC {pvc_name})", flush=True)
        run(kubectl("scale", f"statefulset/{app}", "--replicas=0", namespace=self.ns), quiet=True)
        self.wait_stateful_pod_deleted(app)
        run(kubectl("delete", "pvc", pvc_name, "--ignore-not-found", "--wait=true", namespace=self.ns))
        run(kubectl("apply", "-f", manifest))
        run(kubectl("scale", f"statefulset/{app}", "--replicas=1", namespace=self.ns), quiet=True)

    def force_reset_postgres(self) -> None:
        print("  Forcing Postgres reset for clean benchmark state", flush=True)
        app, pvc_name, manifest = self.STATEFUL_STORES[0]
        self.reset_stateful_store(app, pvc_name, manifest)
        self.wait_for_pods("app=postgres")
        self.verify_postgres_auth()
        self.ensure_migration()
        self.restart_app_deployments()
        for deployment in ("api", "worker", "callback-dispatcher"):
            with TaskProgress(f"Rollout {deployment} after postgres reset", 180):
                run(kubectl(
                    "rollout", "status", f"deployment/{deployment}", "--timeout=180s",
                    namespace=self.ns,
                ), quiet=True)

    def list_ready_pod_names(self, selector: str) -> list[str]:
        output = run(kubectl(
            "get", "pods", "-l", selector,
            "--field-selector=status.phase=Running",
            "-o", "json",
            namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout
        if not output.strip():
            return []
        data = json.loads(output)
        return [
            item["metadata"]["name"]
            for item in data.get("items", [])
            if not item.get("metadata", {}).get("deletionTimestamp")
        ]

    def fetch_pod_metrics_text(self, pod: str, port: int, path: str = "/metrics") -> str:
        url = f"http://127.0.0.1:{port}{path}"
        result = run(kubectl(
            "exec", pod, "--", "python", "-c",
            "import urllib.request; "
            f"print(urllib.request.urlopen({url!r}, timeout=15).read().decode())",
            namespace=self.ns,
        ), capture=True, quiet=True, check=False)
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".strip()
            raise RuntimeError(f"Failed to fetch metrics from {pod}: {combined[:240]}")
        return result.stdout

    def sum_metric_across_pods(self, selector: str, port: int, metric_name: str) -> int:
        pods = self.list_ready_pod_names(selector)
        if not pods:
            raise RuntimeError(f"No running pods found for selector {selector!r}")
        total = 0.0
        for pod in pods:
            total += sum_metric_text(self.fetch_pod_metrics_text(pod, port), metric_name)
        print(
            f"  {metric_name} across {len(pods)} pod(s) [{selector}]: {int(total)}",
            flush=True,
        )
        return int(total)

    def aggregate_rejection_metrics_across_pods(self) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        by_reason: dict[str, int] = {}
        by_tenant: dict[str, dict[str, int]] = {}
        pods = self.list_ready_pod_names("app=api")
        if not pods:
            raise RuntimeError("No running API pods found for rejection metrics")
        for pod in pods:
            pod_reason, pod_tenant = parse_rejection_metrics_text(
                self.fetch_pod_metrics_text(pod, 8080)
            )
            for reason, count in pod_reason.items():
                by_reason[reason] = by_reason.get(reason, 0) + count
            for tenant_id, reasons in pod_tenant.items():
                tenant = by_tenant.setdefault(tenant_id, {})
                for reason, count in reasons.items():
                    tenant[reason] = tenant.get(reason, 0) + count
        print(f"  tickets_rejected_total across {len(pods)} API pod(s)", flush=True)
        return by_reason, by_tenant

    def query_waiting_tickets(self) -> int:
        password = self.postgres_password
        result = run(kubectl(
            "exec", "statefulset/postgres", "--",
            "env", f"PGPASSWORD={password}", "psql", "-U", "pms", "-d", "pms", "-tAc",
            "SELECT COUNT(*) FROM tickets WHERE status = 'waiting';",
            namespace=self.ns,
        ), capture=True, quiet=True, check=False)
        combined = f"{result.stdout}\n{result.stderr}".strip()
        if result.returncode != 0:
            raise RuntimeError(f"Waiting-ticket query failed: {combined[:240]}")
        try:
            return int(combined.splitlines()[-1].strip())
        except ValueError as exc:
            raise RuntimeError(f"Unexpected waiting-ticket query output: {combined[:240]}") from exc

    def restart_app_deployments(self) -> None:
        for deployment in ("api", "worker", "callback-dispatcher"):
            run(kubectl("rollout", "restart", f"deployment/{deployment}", namespace=self.ns), quiet=True)

    def ensure_migration(self) -> None:
        subprocess.run(prepare_cmd(kubectl(
            "delete", "job", "pms-migrate", "--ignore-not-found", namespace=self.ns,
        )), cwd=ROOT, check=False)
        run(kubectl("apply", "-f", "infra/k8s/migrate-job.yaml"))
        self.wait_resource("complete", ("job/pms-migrate",), 180, "Running database migration", 120)

    def ensure_stateful_stores(self) -> bool:
        reset_postgres = False
        for app, pvc_name, manifest in self.STATEFUL_STORES:
            if self.stateful_store_needs_reset(app, pvc_name):
                self.reset_stateful_store(app, pvc_name, manifest)
                if app == "postgres":
                    reset_postgres = True
        if reset_postgres:
            self.wait_for_pods("app=postgres")
            self.verify_postgres_auth()
            self.ensure_migration()
            self.restart_app_deployments()
            for deployment in ("api", "worker", "callback-dispatcher"):
                with TaskProgress(f"Rollout {deployment} after postgres reset", 180):
                    run(kubectl(
                        "rollout", "status", f"deployment/{deployment}", "--timeout=180s",
                        namespace=self.ns,
                    ), quiet=True)
        return reset_postgres

    def verify_postgres_auth(self) -> None:
        password = self.postgres_password
        result = run(kubectl(
            "exec", "statefulset/postgres", "--",
            "env", f"PGPASSWORD={password}", "psql", "-U", "pms", "-d", "pms", "-tAc", "SELECT 1",
            namespace=self.ns,
        ), check=False, capture=True, quiet=True)
        combined = f"{result.stdout}\n{result.stderr}".strip()
        if result.returncode == 0 and "1" in combined:
            print("  Postgres auth OK.", flush=True)
            return
        if "password authentication failed" in combined.lower():
            raise RuntimeError(
                "Postgres password mismatch: PG_PASS does not match the password stored in the "
                "postgres PVC. Reset Postgres, then re-run:\n"
                "  kubectl -n pms scale statefulset/postgres --replicas=0\n"
                "  kubectl -n pms wait --for=delete pod/postgres-0 --timeout=120s\n"
                "  kubectl -n pms delete pvc postgres-data-postgres-0"
            )
        raise RuntimeError(f"Postgres auth check failed: {combined[:240]}")

    def get_ingress_ip(self) -> str | None:
        ip = run(kubectl(
            "get", "ingress", "pms-api", "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip}", namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout.strip()
        return ip or None

    def ingress_failure_reason(self) -> str | None:
        events = run(kubectl(
            "get", "events", "--field-selector", "involvedObject.name=pms-api",
            "--sort-by=.lastTimestamp", namespace=self.ns,
        ), capture=True, quiet=True, check=False).stdout
        describe = run(
            kubectl("describe", "ingress", "pms-api", namespace=self.ns),
            capture=True, quiet=True, check=False,
        ).stdout
        combined = f"{events}\n{describe}".lower()
        for pattern in INGRESS_FATAL_PATTERNS:
            if pattern in combined:
                for line in f"{events}\n{describe}".splitlines():
                    if pattern in line.lower():
                        return line.strip()[:240]
                return f"Ingress/LB error matched: {pattern}"
        if "unhealthy" in combined:
            return "GCE ingress backend is UNHEALTHY (check API readiness and /health)"
        return None

    def ingress_diagnostics(self) -> None:
        print("\n  --- Ingress diagnostics ---", flush=True)
        run(kubectl("get", "ingress", "pms-api", "-o", "wide", namespace=self.ns), check=False)
        run(kubectl("describe", "ingress", "pms-api", namespace=self.ns), check=False)
        run(kubectl("get", "endpoints", "api", namespace=self.ns), check=False)
        run(kubectl(
            "get", "events", "--field-selector", "involvedObject.name=pms-api",
            "--sort-by=.lastTimestamp", namespace=self.ns,
        ), check=False)

    def wait_for_ingress_ip(self, timeout: int) -> str:
        started_at = time.time()

        def check() -> tuple[bool, str, str | None]:
            ip = self.get_ingress_ip()
            if ip:
                return True, f"Ingress IP {ip}", ip
            elapsed = time.time() - started_at
            if elapsed >= INGRESS_ERROR_CHECK_GRACE_SECONDS:
                if reason := self.ingress_failure_reason():
                    raise RuntimeError(f"Ingress failed after {int(elapsed)}s: {reason}")
                if not run(kubectl(
                    "get", "endpoints", "api", "-o",
                    "jsonpath={.subsets[*].addresses[*].ip}", namespace=self.ns,
                ), capture=True, quiet=True, check=False).stdout.strip():
                    raise RuntimeError(
                        f"Ingress has no IP after {int(elapsed)}s and service 'api' has no endpoints"
                    )
            detail = "provisioning load balancer"
            if elapsed >= INGRESS_ERROR_CHECK_GRACE_SECONDS:
                detail = f"still no IP after {int(elapsed)}s (checking events)"
            return False, detail, None

        try:
            ip = wait_until(
                "Waiting for ingress external IP", timeout, 10, check,
                lambda value: f"Ingress IP {value}",
                f"Timed out waiting for ingress IP on pms-api after {timeout}s",
            )
        except RuntimeError:
            self.ingress_diagnostics()
            raise
        assert ip
        return ip

    def apply_ingress(self, timeout: int | None = None) -> str:
        timeout = timeout or self.args.ingress_timeout
        existing_ip = self.get_ingress_ip()
        if existing_ip and not self.args.recreate_ingress:
            print(f"  Reusing existing ingress IP {existing_ip}", flush=True)
            return existing_ip

        if self.args.recreate_ingress:
            self.delete_ingress()
        run(kubectl("apply", "-f", "infra/k8s/ingress.yaml"))
        self.verify_service_endpoints("api")
        return self.wait_for_ingress_ip(timeout)

    def start_port_forward(
        self, resource: str, local: int, remote: int, *, health_path: str = "/",
    ) -> subprocess.Popen:
        process = subprocess.Popen([
            resolve_executable("kubectl"), "-n", self.ns, "port-forward", resource,
            f"{local}:{remote}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        url = f"http://127.0.0.1:{local}{health_path}"
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"kubectl port-forward {resource} {local}:{remote} exited early "
                    f"(code {process.returncode})"
                )
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        return process
            except Exception:
                time.sleep(0.5)
        self.stop_process(process)
        raise RuntimeError(
            f"kubectl port-forward {resource} {local}:{remote} did not become reachable within 15s"
        )

    @staticmethod
    def stop_process(process: subprocess.Popen | None) -> None:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    def apply_manifests(self, *paths: str) -> None:
        for path in paths:
            run(kubectl("apply", "-f", path))

    def wait_resource(
        self, condition: str, resource: tuple[object, ...], command_timeout: int,
        label: str, expected_seconds: int,
    ) -> None:
        with TaskProgress(label, expected_seconds):
            run(kubectl(
                "wait", f"--for=condition={condition}", *resource,
                f"--timeout={command_timeout}s", namespace=self.ns,
            ), quiet=True)

    def ensure_deploy(self) -> None:
        a = self.args
        if a.reset_postgres:
            self.steps.begin("Reset Postgres for clean benchmark state")
            self.force_reset_postgres()
        if a.skip_deploy:
            self.steps.begin("Skip deploy (workloads already running)")
            print("  Using existing deployment.", flush=True)
            if not a.reset_postgres:
                self.ensure_stateful_stores()
            return
        password = self.postgres_password

        self.steps.begin("Apply Kubernetes manifests")
        run(kubectl("apply", "-f", "infra/k8s/namespace.yaml"))
        secret = run(kubectl(
            "create", "secret", "generic", "pms-secrets", "--namespace", self.ns,
            f"--from-literal=DATABASE_URL=postgresql://pms:{password}@postgres:5432/pms",
            f"--from-literal=POSTGRES_PASSWORD={password}", "--dry-run=client", "-o", "yaml",
        ), capture=True)
        subprocess.run(
            prepare_cmd(["kubectl", "apply", "-f", "-"]), cwd=ROOT,
            input=secret.stdout, text=True, check=True,
        )
        self.apply_manifests(
            "infra/k8s/configmap.yaml", "infra/k8s/postgres.yaml", "infra/k8s/redis.yaml"
        )
        postgres_reset = self.ensure_stateful_stores()
        self.wait_resource("ready", ("pod", "-l", "app=postgres"), 180, "Waiting for Postgres pod", 180)
        if not postgres_reset:
            self.verify_postgres_auth()

        migrated = subprocess.run(prepare_cmd(kubectl(
            "get", "job", "pms-migrate", "-o", "jsonpath={.status.succeeded}",
            namespace=self.ns,
        )), cwd=ROOT, text=True, capture_output=True).stdout.strip()
        if migrated != "1":
            self.ensure_migration()

        self.apply_manifests(
            "infra/k8s/mock-callback.yaml", "infra/k8s/api.yaml",
            "infra/k8s/worker.yaml", "infra/k8s/callback-dispatcher.yaml",
        )

    @staticmethod
    def dispatcher_replicas(nodes: int) -> int:
        return max(1, nodes - 1) if nodes > 1 else 1

    def scale_replicas(self, nodes: int) -> None:
        replicas = {
            "api": nodes, "worker": nodes,
            "callback-dispatcher": self.dispatcher_replicas(nodes),
        }
        self.steps.begin(
            f"Scale replicas (api={nodes}, worker={nodes}, dispatcher={replicas['callback-dispatcher']})"
        )
        for deployment, count in replicas.items():
            run(kubectl("scale", f"deployment/{deployment}", f"--replicas={count}", namespace=self.ns))

        # Reapplying a Secret/ConfigMap does not restart existing pods. Restart all
        # application deployments so API startup reinitializes its database pool and
        # every component receives the current configuration.
        for deployment in replicas:
            run(kubectl("rollout", "restart", f"deployment/{deployment}", namespace=self.ns))
        for deployment in replicas:
            with TaskProgress(f"Rollout {deployment}", 180):
                run(kubectl(
                    "rollout", "status", f"deployment/{deployment}", "--timeout=180s",
                    namespace=self.ns,
                ), quiet=True)
        self.wait_for_old_pods_gone(tuple(replicas.keys()))

    def wait_for_old_pods_gone(self, apps: tuple[str, ...], timeout: int = 120) -> None:
        label_selector = f"app in ({','.join(apps)})"

        def check() -> tuple[bool, str, None]:
            output = run(kubectl(
                "get", "pods", "-l", label_selector,
                "-o", 'jsonpath={range .items[*]}{.metadata.deletionTimestamp}{"\\n"}{end}',
                namespace=self.ns,
            ), capture=True, quiet=True, check=False).stdout
            terminating = sum(1 for line in output.splitlines() if line.strip())
            return terminating == 0, f"{terminating} pod(s) still terminating", None

        wait_until(
            "Waiting for old pods to finish terminating", timeout, 5, check,
            "terminating pods cleared", "Timed out waiting for old pods to terminate",
        )

    def build_image(self) -> None:
        a = self.args
        if a.skip_build:
            self.steps.begin("Skip build (using existing image)")
            print(f"  Image: {a.registry_image}", flush=True)
            return
        self.steps.begin("Build and push Docker image")
        run(["gcloud", "auth", "configure-docker", "europe-west1-docker.pkg.dev", "--quiet"])
        run_with_progress(["docker", "build", "-t", a.registry_image, "."], "Docker build")
        run_with_progress(["docker", "push", a.registry_image], "Docker push")

    def api_diagnostics(self) -> None:
        print("\n  --- API diagnostics ---", flush=True)
        run(kubectl("get", "pods", "-l", "app=api", "-o", "wide", namespace=self.ns), check=False)
        run(kubectl("logs", "deployment/api", "--tail=200", namespace=self.ns), check=False)
        run(kubectl("describe", "deployment/api", namespace=self.ns), check=False)

    @staticmethod
    def health_status(url: str, timeout: int = 15) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return False, str(exc)[:80]
        if data.get("status") == "ok":
            return True, "ok"
        return False, (
            f"status={data.get('status')} postgres={data.get('postgres')} redis={data.get('redis')}"
        )

    def wait_health(self, url: str, label: str, timeout: int) -> None:
        detail = "no response yet"
        started_at = time.time()

        def check() -> tuple[bool, str, None]:
            nonlocal detail
            healthy, detail = self.health_status(url)
            lowered = detail.lower()
            if "password authentication failed" in lowered:
                raise RuntimeError(
                    "API cannot connect to Postgres (password authentication failed). "
                    "PG_PASS must match the postgres PVC password — delete the PVC and re-run."
                )
            if "empty reply" in lowered or "connection reset" in lowered:
                detail = "load balancer not ready yet"
            elif "pool not initialized" in lowered:
                if time.time() - started_at >= 300:
                    raise RuntimeError(
                        "API PostgreSQL pool not initialized after 300 seconds. "
                        f"Last health response: {detail}"
                    )
                detail = "API starting up (DB pool not ready yet)"
            elif "postgres=error" in lowered or "postgres=failed" in lowered:
                raise RuntimeError(f"API Postgres check failed: {detail}")
            return healthy, detail[:60], None

        try:
            wait_until(label, timeout, 15, check, "API healthy", "")
        except RuntimeError as exc:
            self.api_diagnostics()
            if str(exc):
                raise
            raise RuntimeError(f"API not healthy at {url} after {timeout}s (last: {detail})") from None

    def wait_health_in_cluster(self, timeout: int = 180) -> None:
        process = self.start_port_forward("svc/api", 18080, 8080)
        try:
            self.wait_health(
                "http://127.0.0.1:18080/health", "Waiting for API /health (in-cluster)", timeout
            )
        finally:
            self.stop_process(process)

    def run_benchmark(self, nodes: int) -> BenchmarkResult:
        self.steps.begin(f"Run k6 scale_out benchmark ({nodes} node(s))")
        api_process = None
        if self.args.use_port_forward:
            print("  WARNING: port-forward cannot sustain 100 VUs.", flush=True)
            api_process = self.start_port_forward("svc/api", 8080, 8080)
            base_url = "http://localhost:8080"
        else:
            if not self.get_ingress_ip():
                self.wait_health_in_cluster()
            base_url = f"http://{self.apply_ingress()}"
            print(f"  API URL: {base_url}", flush=True)

        metrics_process = None
        try:
            self.wait_health(f"{base_url}/health", "Waiting for API /health via ingress", 600)
            waiting_before = self.query_waiting_tickets()
            print(f"  Waiting tickets before: {waiting_before}", flush=True)
            try:
                before = self.sum_metric_across_pods(
                    "app=worker", 9090, "pms_worker_matches_created_total"
                )
            except Exception as exc:
                raise RuntimeError(f"Worker metrics not reachable: {exc}") from exc
            print(f"  Worker matches before: {before}", flush=True)

            summary = Path(os.environ.get("TEMP", "/tmp")) / f"pms-k6-summary-{nodes}.json"
            if summary.exists():
                summary.unlink()
            k6_cmd = ["k6", "run"]
            if self.args.smoke:
                k6_cmd.extend(["--stage", "30s:10,60s:20,30s:0"])
            k6_cmd.extend([
                "-e", f"BASE_URL={base_url}",
                f"--summary-export={summary}", "loadtests/scale_out.js",
            ])
            run_with_progress(
                k6_cmd, "k6 load test", expected_seconds=self.k6_duration + 30,
            )
            after = self.sum_metric_across_pods(
                "app=worker", 9090, "pms_worker_matches_created_total"
            )
            waiting_after = self.query_waiting_tickets()
            k6 = parse_k6_summary(summary)
            print(f"  Worker matches after: {after} (delta {after - before})", flush=True)
            print(f"  Waiting tickets after: {waiting_after}", flush=True)
            rejections_by_reason, rejections_by_tenant = (
                self.aggregate_rejection_metrics_across_pods()
            )
            if rejections_by_reason:
                total_rejected = sum(rejections_by_reason.values())
                print(
                    f"  API rejections: {total_rejected} total "
                    f"({', '.join(f'{k}={v}' for k, v in sorted(rejections_by_reason.items()))})",
                    flush=True,
                )
        finally:
            self.stop_process(metrics_process)
            self.stop_process(api_process)

        delta = after - before
        ratio = round(100 * k6["tickets_created"] / (2 * delta), 1) if delta > 0 else 0.0
        return BenchmarkResult(
            nodes, f"{nodes} / {nodes} / {self.dispatcher_replicas(nodes)}",
            int(k6["tickets_created"]), int(k6["tickets_rejected"]),
            round(k6["tickets_created"] / self.k6_duration, 2),
            round(k6["tickets_rejected"] / self.k6_duration, 2),
            delta, round(delta / self.k6_duration, 2), ratio,
            round(k6["p95_ms"], 2), before, after,
            waiting_before, waiting_after,
            self.k6_duration, rejections_by_reason, rejections_by_tenant,
            self.args.reset_postgres,
        )

    def write_results(self) -> None:
        self.steps.begin("Write results file")
        path = ROOT / "loadtests" / f"results-gke-{datetime.now().strftime('%Y-%m-%d-%H%M')}.md"
        write_results_file(
            self.results, path, zone=self.args.zone, project_id=self.args.project_id,
            access_mode=(
                "kubectl port-forward (smoke test only)"
                if self.args.use_port_forward else "GCE ingress load balancer"
            ),
            machine_type=self.args.machine_type,
        )
        print("\nBenchmark summary:", flush=True)
        for row in self.results:
            print(
                f"  nodes={row.nodes} tickets={row.tickets_created} "
                f"matches/s={row.matches_per_sec} p95={row.p95_latency_ms}ms",
                flush=True,
            )

    def teardown(self) -> None:
        if self.args.skip_teardown:
            print("\n[Teardown] Skipped - cluster left running.", flush=True)
            return
        self.steps.begin("Tear down (delete ingress, scale nodes to 0)")
        self.delete_ingress()
        try:
            with TaskProgress("Scaling node pool to 0", 180):
                run(self.resize_cmd(0), quiet=True)
        except KeyboardInterrupt:
            print("  Teardown interrupted; the node pool may still be running.", file=sys.stderr)
            raise
        print("  VM billing stopped.", flush=True)

    def execute(self) -> int:
        try:
            self.steps.begin("Verify required tools")
            self.verify_tools()
            self.preflight()
            if self.args.preflight:
                print("\nPreflight passed.", flush=True)
                return 0
            self.build_image()
            for nodes in self.node_counts:
                self.steps.begin(f"Scale node pool to {nodes}")
                current_nodes = self.current_node_count()
                if nodes > current_nodes and current_nodes > 0:
                    # Avoid CPUS_ALL_REGIONS quota spikes when growing the pool on
                    # larger machine types (e.g. e2-standard-8 needs 40 vCPU at 5 nodes).
                    with TaskProgress("Scaling node pool to 0 (quota-safe resize)", 180):
                        run(self.resize_cmd(0), quiet=True)
                    self.wait_for_cluster()
                    self.wait_for_nodes(0, timeout=420)
                with TaskProgress(f"Resizing node pool to {nodes}", 180):
                    run(self.resize_cmd(nodes), quiet=True)
                self.wait_for_cluster()
                self.wait_for_nodes(nodes)
                self.ensure_deploy()
                self.wait_for_stack(self.DATA_STORES)
                self.scale_replicas(nodes)
                self.wait_for_stack(self.APP_STACK)
                run(kubectl("get", "pods", namespace=self.ns))
                self.results.append(self.run_benchmark(nodes))
            self.write_results()
            return 0
        except Exception as exc:
            print(f"\nFAILED: {exc}", file=sys.stderr, flush=True)
            raise
        finally:
            if not self.args.preflight:
                self.teardown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GKE scale-out benchmark end-to-end.")
    parser.add_argument("--node-count", type=int, choices=(1, 3, 5), default=1)
    parser.add_argument("--all", action="store_true", help="Run 1, 3, and 5 node benchmarks")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument(
        "--reset-postgres",
        action="store_true",
        help="Delete and recreate the Postgres PVC before the benchmark for clean ticket/match state",
    )
    parser.add_argument("--skip-teardown", action="store_true")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Quick 2-minute k6 run and 5-minute ingress timeout (for validation)",
    )
    parser.add_argument("--preflight", action="store_true", help="Run preflight checks only, then exit")
    parser.add_argument("--recreate-ingress", action="store_true", help="Delete and recreate GCE ingress")
    parser.add_argument(
        "--ingress-timeout",
        type=int,
        default=480,
        help="Max seconds to wait for ingress IP (default: 480)",
    )
    parser.add_argument("--use-port-forward", action="store_true")
    parser.add_argument(
        "--postgres-password",
        default=resolve_postgres_password(),
        help="Postgres password (default: PG_PASS from .env, then PMS_POSTGRES_PASSWORD)",
    )
    parser.add_argument("--project-id", default="se-proto")
    parser.add_argument("--zone", default="europe-west1-b")
    parser.add_argument("--cluster-name", default="pms-cluster")
    parser.add_argument("--node-pool", default="pms-node-pool")
    parser.add_argument(
        "--registry-image", default="europe-west1-docker.pkg.dev/se-proto/pms/pms:local"
    )
    parser.add_argument("--namespace", default="pms")
    parser.add_argument(
        "--machine-type",
        default=os.environ.get("PMS_MACHINE_TYPE", "e2-standard-4"),
        help="Node machine type (for results file; set via terraform for actual nodes)",
    )
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    augment_path()
    load_project_env()
    args = parse_args()
    os.environ["USE_GKE_GCLOUD_AUTH_PLUGIN"] = "True"
    return BenchmarkRunner(args).execute()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
        print(f"FAILED: command error ({cmd}), exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception:
        raise SystemExit(1)