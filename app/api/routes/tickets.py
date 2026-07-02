from fastapi import APIRouter

router = APIRouter()

@router.post("/tickets")
async def create_ticket():
    return {"status": "stub"}

@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):
    return {"ticket_id": ticket_id, "status": "stub"}

@router.delete("/tickets/{ticket_id}")
async def cancel_ticket(ticket_id: str):
    return {"status": "stub"}