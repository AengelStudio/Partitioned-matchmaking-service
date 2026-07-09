from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from app.db.postgres import init_db, close_db
from app.db.redis import init_redis, close_redis
from app.db.migrate import migrate
from app.api.routes import tickets, matches, health, metrics

DASHBOARD_HTML = (Path(__file__).parent / "static" / "index.html").read_text()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        await migrate()
    except Exception as e:
        print(f"PostgreSQL startup failed (will report degraded): {e}")
    try:
        await init_redis()
    except Exception as e:
        print(f"Redis startup failed (will report degraded): {e}")
    yield
    await close_db()
    await close_redis()

app = FastAPI(title="Matchmaking API", lifespan=lifespan)

app.include_router(tickets.router, prefix="/v1")
app.include_router(matches.router, prefix="/v1")
app.include_router(health.router)
app.include_router(metrics.router)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML