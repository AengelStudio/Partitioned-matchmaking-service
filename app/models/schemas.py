from datetime import datetime
from pydantic import BaseModel


class TicketRequest(BaseModel):
    player_id: str
    tenant_id: str
    region: str
    queue_name: str
    skill_rating: float


class TicketResponse(BaseModel):
    ticket_id: str
    player_id: str
    tenant_id: str
    region: str
    queue_name: str
    skill_rating: float
    status: str
    partition_id: int
    created_at: datetime


class MatchResponse(BaseModel):
    match_id: str
    tenant_id: str
    region: str
    queue_name: str
    status: str
    created_at: datetime
    players: list[str]
