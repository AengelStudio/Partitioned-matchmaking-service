from unittest.mock import MagicMock
from uuid import UUID

from app.callback_dispatcher.main import (
    CallbackEvent,
    build_headers,
    encode_payload,
    mark_delivered,
    mark_failed_attempt,
    next_backoff_seconds,
    signature,
)
from app.config import Settings


def test_encode_payload_is_stable_json() -> None:
    assert encode_payload({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_build_headers_include_contract_headers() -> None:
    event = CallbackEvent(
        event_id=UUID("4897f958-1302-4186-b76b-f40b95df1404"),
        tenant_id="studio_a",
        match_id=UUID("09b61c8f-c4f5-45e8-a8b7-ff40debb6b44"),
        event_type="match.created",
        callback_url="http://example.test/callback",
        payload={"event_id": "4897f958-1302-4186-b76b-f40b95df1404"},
        attempts=0,
        callback_secret="secret",
    )

    body = encode_payload(event.payload)
    headers = build_headers(event, body)

    assert headers["Content-Type"] == "application/json"
    assert headers["X-PMS-Event-Id"] == str(event.event_id)
    assert headers["X-PMS-Timestamp"].endswith("Z")
    assert headers["X-PMS-Signature"].startswith("sha256=")


def test_signature_uses_timestamp_and_raw_body() -> None:
    assert signature("secret", "2026-06-24T14:32:45Z", '{"a":1}') == (
        "9d8fe6be0aa335239e18b2ae096c17c0"
        "a79655e54e901e483df371eea45dcdbd"
    )


def test_next_backoff_is_capped_before_jitter(monkeypatch) -> None:
    monkeypatch.setattr("app.callback_dispatcher.main.random.randint", lambda _a, _b: 3)
    settings = Settings(
        callback_base_backoff_seconds=2,
        callback_max_backoff_seconds=10,
        callback_jitter_seconds=3,
    )

    assert next_backoff_seconds(10, settings) == 13


def _sample_event() -> CallbackEvent:
    return CallbackEvent(
        event_id=UUID("4897f958-1302-4186-b76b-f40b95df1404"),
        tenant_id="studio_a",
        match_id=UUID("09b61c8f-c4f5-45e8-a8b7-ff40debb6b44"),
        event_type="match.created",
        callback_url="http://example.test/callback",
        payload={"event_id": "4897f958-1302-4186-b76b-f40b95df1404"},
        attempts=0,
        callback_secret="secret",
    )


def test_mark_delivered_updates_matches_before_callback_events() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
    conn.transaction.return_value.__exit__ = MagicMock(return_value=False)

    mark_delivered(conn, _sample_event())

    sql_calls = [call.args[0].strip() for call in cursor.execute.call_args_list]
    assert sql_calls[0].startswith("UPDATE matches")
    assert sql_calls[1].startswith("UPDATE callback_events")


def test_mark_failed_final_updates_matches_before_callback_events() -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
    conn.transaction.return_value.__exit__ = MagicMock(return_value=False)

    settings = Settings(callback_max_attempts=1)
    failed = mark_failed_attempt(conn, _sample_event(), settings, "timeout")

    assert failed is True
    sql_calls = [call.args[0].strip() for call in cursor.execute.call_args_list]
    assert sql_calls[0].startswith("UPDATE matches")
    assert sql_calls[1].startswith("UPDATE callback_events")
