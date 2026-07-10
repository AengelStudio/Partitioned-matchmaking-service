from fastapi.testclient import TestClient

from app.mock_callback.main import app


def test_mock_callback_records_and_clears_callbacks() -> None:
    client = TestClient(app)

    client.delete("/callbacks")
    response = client.post(
        "/tenant-matchmaking-callback",
        headers={
            "X-PMS-Event-Id": "event-1",
            "X-PMS-Timestamp": "2026-06-24T14:32:45Z",
            "X-PMS-Signature": "sha256=test",
        },
        json={"event_id": "event-1", "event_type": "match.created"},
    )
    assert response.status_code == 204

    response = client.get("/callbacks")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["callbacks"][0]["headers"]["x-pms-event-id"] == "event-1"
    assert body["callbacks"][0]["payload"]["event_type"] == "match.created"

    response = client.delete("/callbacks")
    assert response.status_code == 204
    assert client.get("/callbacks").json()["count"] == 0
