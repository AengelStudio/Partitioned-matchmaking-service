from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.worker.matching import WaitingTicket, find_pairs, wait_seconds
from app.config import Settings


def _ticket(
    skill: int,
    age_seconds: float = 0.0,
    player_id: str | None = None,
) -> WaitingTicket:
    created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return WaitingTicket(
        ticket_id=uuid4(),
        tenant_id="studio_a",
        player_id=player_id or f"player_{uuid4().hex[:8]}",
        region="eu-west",
        queue_name="ranked_1v1",
        skill=skill,
        partition_id=42,
        created_at=created_at,
    )


def test_wait_seconds_reflects_ticket_age() -> None:
    ticket = _ticket(1500, age_seconds=45.0)
    assert 44.0 <= wait_seconds(ticket.created_at) <= 46.0


def test_find_pairs_respects_skill_and_degradation() -> None:
    settings = Settings()
    close = _ticket(1500, player_id="a")
    close2 = _ticket(1520, player_id="b")
    far = _ticket(1800, player_id="c")
    pairs = find_pairs([close, close2, far], settings)
    assert len(pairs) == 1
    ids = {pairs[0][0].player_id, pairs[0][1].player_id}
    assert ids == {"a", "b"}


def test_find_pairs_allows_wider_delta_for_old_tickets() -> None:
    settings = Settings()
    old = _ticket(1500, age_seconds=65.0, player_id="old")
    old2 = _ticket(1850, age_seconds=65.0, player_id="old2")
    pairs = find_pairs([old, old2], settings)
    assert len(pairs) == 1
