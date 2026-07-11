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
K6_DURATION_SECONDS = 180
METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$"
)
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


def sum_metric(url: str, metric_name: str) -> float:
    with urllib.request.urlopen(url, timeout=15) as response:
        lines = response.read().decode("utf-8").splitlines()
    total = 0.0
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        match = METRIC_LINE.match(line.strip())
        if match and match.group("name") == metric_name:
            total += float(match.group("value"))
    return total


def parse_k6_summary(path: Path) -> dict[str, float]:
    metrics = json.loads(path.read_text(encoding="utf-8")).get("metrics", {})

    def value(metric: str, key: str) -> float:
        return float(metrics.get(metric, {}).get("values", {}).get(key, 0.0))

    return {
        "tickets_created": value("tickets_created", "count"),
        "tickets_rejected": value("tickets_rejected", "count"),
        "p95_ms": value("ticket_create_latency_ms", "p(95)"),
    }


def write_results_file(
    rows: list[BenchmarkResult], path: Path, *, zone: str, project_id: str, access_mode: str,
) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "# GKE scale-out benchmark results", "", f"**Generated:** {timestamp}  ",
        f"**Cluster:** GKE zonal, `{zone}`, project `{project_id}`  ",
        "**Machine type:** e2-standard-4  ",
        "**Load script:** `loadtests/scale_out.js` (multi-tenant)  ",
        f"**Run duration:** {K6_DURATION_SECONDS}s per node count  ", f"**Access:** {access_mode}", "",
        "| Nodes | API / Worker / Dispatcher | Tickets created | Tickets rejected | matches_created/s | Tickets:matches ratio | p95 ticket-create latency |",
        "|-------|---------------------------|-----------------|------------------|-------------------|----------------------|---------------------------|",
    ]
    lines += [
        f"| {r.nodes} | {r.replicas} | {r.tickets_created} ({r.tickets_per_sec}/s) | "
        f"{r.tickets_rejected} ({r.rejected_per_sec}/s) | "
        f"{r.matches_per_sec} ({r.matches_delta} matches / {K6_DURATION_SECONDS}s) | "
        f"{r.pair_ratio_pct}% | {r.p95_latency_ms}ms |" for r in rows
    ]
    lines += ["", "## Raw worker metrics", ""]
    lines += [
        f"- **{r.nodes} node(s):** matches before={r.matches_before}, "
        f"after={r.matches_after}, delta={r.matches_delta}" for r in rows
    ]
    lines += [
        "", "## Notes", "",
        "- Worker deployment restarted before each run so partition leases redistribute.",
        "- Node pool scaled to 0 automatically after the script finished (unless `--skip-teardown`).",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Results written to {path}", flush=True)


class BenchmarkRunner:
    DATA_STORES = ("app=postgres", "app=redis")
    APP_STACK = ("app=api", "app=worker")

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.node_counts = [1, 3, 5] if args.all else [args.node_count]
        self.results: list[BenchmarkResult] = []
        self.steps = StepTracker(7 + len(self.node_counts) * 3)

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

    def apply_ingress(self, timeout: int = 600) -> str:
        self.delete_ingress()
        run(kubectl("apply", "-f", "infra/k8s/ingress.yaml"))

        def check() -> tuple[bool, str, str | None]:
            ip = run(kubectl(
                "get", "ingress", "pms-api", "-o",
                "jsonpath={.status.loadBalancer.ingress[0].ip}", namespace=self.ns,
            ), capture=True, quiet=True).stdout.strip()
            return bool(ip), "provisioning load balancer", ip or None

        ip = wait_until(
            "Waiting for ingress external IP", timeout, 15, check,
            lambda value: f"Ingress IP {value}", "Timed out waiting for ingress IP on pms-api",
        )
        assert ip
        return ip

    def start_port_forward(self, resource: str, local: int, remote: int) -> subprocess.Popen:
        return subprocess.Popen([
            resolve_executable("kubectl"), "-n", self.ns, "port-forward", resource,
            f"{local}:{remote}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        if a.skip_deploy:
            self.steps.begin("Skip deploy (workloads already running)")
            print("  Using existing deployment.", flush=True)
            return
        password = a.postgres_password or os.environ.get("PMS_POSTGRES_PASSWORD")
        if not password:
            raise RuntimeError("Set PMS_POSTGRES_PASSWORD before deploying")

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
        self.wait_resource("ready", ("pod", "-l", "app=postgres"), 180, "Waiting for Postgres pod", 180)

        migrated = subprocess.run(prepare_cmd(kubectl(
            "get", "job", "pms-migrate", "-o", "jsonpath={.status.succeeded}",
            namespace=self.ns,
        )), cwd=ROOT, text=True, capture_output=True).stdout.strip()
        if migrated != "1":
            subprocess.run(prepare_cmd(kubectl(
                "delete", "job", "pms-migrate", "--ignore-not-found", namespace=self.ns,
            )), cwd=ROOT, check=False)
            run(kubectl("apply", "-f", "infra/k8s/migrate-job.yaml"))
            self.wait_resource("complete", ("job/pms-migrate",), 180, "Running database migration", 120)

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
            if "empty reply" in lowered or "connection reset" in lowered:
                detail = "load balancer not ready yet"
            elif "pool not initialized" in lowered and time.time() - started_at >= 30:
                raise RuntimeError(
                    "API PostgreSQL initialization did not run within 30 seconds. "
                    "The API process must call init_db() from its startup/lifespan hook."
                )
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
        time.sleep(3)
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
            time.sleep(3)
            base_url = "http://localhost:8080"
        else:
            self.wait_health_in_cluster()
            base_url = f"http://{self.apply_ingress()}"
            print(f"  API URL: {base_url}", flush=True)

        with TaskProgress("Starting worker metrics port-forward", 5):
            metrics_process = self.start_port_forward("svc/worker-metrics", 9090, 9090)
            time.sleep(3)
        try:
            self.wait_health(f"{base_url}/health", "Waiting for API /health via ingress", 600)
            try:
                before = int(sum_metric(
                    "http://localhost:9090/metrics", "pms_worker_matches_created_total"
                ))
            except Exception as exc:
                raise RuntimeError(f"Worker metrics not reachable on localhost:9090: {exc}") from exc
            print(f"  Worker matches before: {before}", flush=True)

            summary = Path(os.environ.get("TEMP", "/tmp")) / f"pms-k6-summary-{nodes}.json"
            if summary.exists():
                summary.unlink()
            run_with_progress([
                "k6", "run", "-e", f"BASE_URL={base_url}",
                f"--summary-export={summary}", "loadtests/scale_out.js",
            ], "k6 load test", expected_seconds=K6_DURATION_SECONDS + 30)
            after = int(sum_metric(
                "http://localhost:9090/metrics", "pms_worker_matches_created_total"
            ))
            k6 = parse_k6_summary(summary)
            print(f"  Worker matches after: {after} (delta {after - before})", flush=True)
        finally:
            self.stop_process(metrics_process)
            self.stop_process(api_process)

        delta = after - before
        ratio = round(100 * k6["tickets_created"] / (2 * delta), 1) if delta > 0 else 0.0
        return BenchmarkResult(
            nodes, f"{nodes} / {nodes} / {self.dispatcher_replicas(nodes)}",
            int(k6["tickets_created"]), int(k6["tickets_rejected"]),
            round(k6["tickets_created"] / K6_DURATION_SECONDS, 2),
            round(k6["tickets_rejected"] / K6_DURATION_SECONDS, 2),
            delta, round(delta / K6_DURATION_SECONDS, 2), ratio,
            round(k6["p95_ms"], 2), before, after,
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
            self.steps.begin(f"Connect kubectl to {self.args.cluster_name}")
            run(self.cluster_cmd("get-credentials"))
            self.wait_for_cluster()
            self.build_image()
            for nodes in self.node_counts:
                self.steps.begin(f"Scale node pool to {nodes}")
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
            self.teardown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GKE scale-out benchmark end-to-end.")
    parser.add_argument("--node-count", type=int, choices=(1, 3, 5), default=1)
    parser.add_argument("--all", action="store_true", help="Run 1, 3, and 5 node benchmarks")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--skip-teardown", action="store_true")
    parser.add_argument("--use-port-forward", action="store_true")
    parser.add_argument("--postgres-password", default=os.environ.get("PMS_POSTGRES_PASSWORD"))
    parser.add_argument("--project-id", default="se-proto")
    parser.add_argument("--zone", default="europe-west1-b")
    parser.add_argument("--cluster-name", default="pms-cluster")
    parser.add_argument("--node-pool", default="pms-node-pool")
    parser.add_argument(
        "--registry-image", default="europe-west1-docker.pkg.dev/se-proto/pms/pms:local"
    )
    parser.add_argument("--namespace", default="pms")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    augment_path()
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