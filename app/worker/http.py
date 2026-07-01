import threading

import uvicorn
from fastapi import FastAPI, Response

from app.worker.metrics import WorkerMetrics


def create_app(metrics: WorkerMetrics, worker_id: str, metrics_enabled: bool) -> FastAPI:
    app = FastAPI(title="pms-worker")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "worker", "worker_id": worker_id}

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        if not metrics_enabled:
            return Response(content="# metrics disabled\n", media_type="text/plain")
        return Response(
            content=metrics.format_prometheus(worker_id),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


def start_metrics_server(
    host: str, port: int, metrics: WorkerMetrics, worker_id: str, metrics_enabled: bool
) -> threading.Thread:
    app = create_app(metrics, worker_id, metrics_enabled)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def run_server() -> None:
        server.run()

    thread = threading.Thread(target=run_server, daemon=True, name="worker-metrics")
    thread.start()
    return thread
