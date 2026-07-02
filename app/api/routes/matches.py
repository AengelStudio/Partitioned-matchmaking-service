from fastapi import APIRouter

router = APIRouter()

@router.get("/matches/{match_id}")
async def get_match(match_id: str):
    return {"match_id": match_id, "status": "stub"}