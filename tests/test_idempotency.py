from app.core.idempotency import compute_request_hash_from_model


def test_request_hash_is_stable_for_same_payload() -> None:
    body = {
        "player_id": "player_123",
        "region": "eu-west",
        "queue_name": "ranked_1v1",
        "skill": 1470,
    }
    assert compute_request_hash_from_model(body) == compute_request_hash_from_model(body)


def test_request_hash_ignores_key_order() -> None:
    first = {
        "player_id": "player_123",
        "region": "eu-west",
        "queue_name": "ranked_1v1",
        "skill": 1470,
    }
    second = {
        "skill": 1470,
        "queue_name": "ranked_1v1",
        "region": "eu-west",
        "player_id": "player_123",
    }
    assert compute_request_hash_from_model(first) == compute_request_hash_from_model(second)


def test_request_hash_changes_when_body_changes() -> None:
    base = {
        "player_id": "player_123",
        "region": "eu-west",
        "queue_name": "ranked_1v1",
        "skill": 1470,
    }
    changed = {**base, "skill": 1500}
    assert compute_request_hash_from_model(base) != compute_request_hash_from_model(changed)
