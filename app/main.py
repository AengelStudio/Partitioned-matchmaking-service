from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.db.postgres import init_db, close_db
from app.db.redis import init_redis, close_redis
from app.api.routes import tickets, matches

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_redis()
    yield
    await close_db()
    await close_redis()

app = FastAPI(title="Matchmaking API", lifespan=lifespan)

app.include_router(tickets.router, prefix="/v1")
app.include_router(matches.router, prefix="/v1")