from datetime import UTC, datetime
from threading import Lock
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="mock-tenant-callback")

_callbacks: list[dict[str, Any]] = []
_callbacks_lock = Lock()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "mock-callback"}


@app.post("/tenant-matchmaking-callback")
async def receive_callback(request: Request) -> Response:
    payload = await request.json()
    record = {
        "received_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "headers": {
            "x-pms-event-id": request.headers.get("x-pms-event-id"),
            "x-pms-timestamp": request.headers.get("x-pms-timestamp"),
            "x-pms-signature": request.headers.get("x-pms-signature"),
        },
        "payload": payload,
    }
    with _callbacks_lock:
        _callbacks.append(record)
    return Response(status_code=204)


@app.get("/callbacks")
def list_callbacks() -> dict:
    with _callbacks_lock:
        callbacks = list(_callbacks)
    return {"count": len(callbacks), "callbacks": callbacks}


@app.delete("/callbacks")
def clear_callbacks() -> Response:
    with _callbacks_lock:
        _callbacks.clear()
    return Response(status_code=204)
