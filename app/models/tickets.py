from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TicketCreate(BaseModel):
    player_id: str
    region: str
    queue_name: str
    skill: int


class TicketResponse(BaseModel):
    ticket_id: UUID
    tenant_id: str
    player_id: str
    region: str
    queue_name: str
    skill: int
    partition_id: int
    status: str
    created_at: datetime
    match_id: UUID | None = None
    matched_at: datetime | None = None
    cancelled_at: datetime | None = None
    idempotent_replay: bool | None = None


class MatchPlayer(BaseModel):
    player_id: str
    ticket_id: UUID


class TicketCancelResponse(BaseModel):
    ticket_id: UUID
    status: str
    cancelled_at: datetime


class MatchResponse(BaseModel):
    match_id: UUID
    tenant_id: str
    region: str
    queue_name: str
    partition_id: int
    status: str
    created_at: datetime
    players: list[MatchPlayer]
