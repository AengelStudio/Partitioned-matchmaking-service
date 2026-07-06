from fastapi import FastAPI, Response

app = FastAPI(title="mock-tenant-callback")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "mock-callback"}


@app.post("/tenant-matchmaking-callback")
async def receive_callback() -> Response:
    return Response(status_code=204)
