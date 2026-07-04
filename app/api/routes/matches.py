from fastapi import APIRouter, Depends, HTTPException

from app.core.tenants import require_tenant
from app.db.postgres import get_pool
from app.models.tickets import MatchPlayer, MatchResponse

router = APIRouter()


@router.get("/matches/{match_id}", response_model=MatchResponse)
async def get_match(match_id: str, tenant: dict = Depends(require_tenant)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM matches WHERE match_id = $1::uuid AND tenant_id = $2",
            match_id,
            tenant["tenant_id"],
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Match not found")
        players = await conn.fetch(
            """SELECT player_id, ticket_id
               FROM match_players
               WHERE match_id = $1::uuid""",
            match_id,
        )
    return MatchResponse(
        match_id=row["match_id"],
        tenant_id=row["tenant_id"],
        region=row["region"],
        queue_name=row["queue_name"],
        partition_id=row["partition_id"],
        status=row["status"],
        created_at=row["created_at"],
        players=[
            MatchPlayer(player_id=p["player_id"], ticket_id=p["ticket_id"])
            for p in players
        ],
    )
