from fastapi import APIRouter, HTTPException
from app.db.postgres import get_pool
from app.models.schemas import MatchResponse

router = APIRouter()


@router.get("/matches/{match_id}", response_model=MatchResponse)
async def get_match(match_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM matches WHERE match_id = $1::uuid", match_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Match not found")
        players = await conn.fetch(
            "SELECT player_id FROM match_players WHERE match_id = $1::uuid", match_id
        )
    return MatchResponse(
        **{**dict(row), "match_id": str(row["match_id"])},
        players=[p["player_id"] for p in players],
    )
