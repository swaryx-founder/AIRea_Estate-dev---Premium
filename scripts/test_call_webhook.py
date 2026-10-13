"""Quick sanity checks for call_webhook (run: python scripts/test_call_webhook.py)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from call_webhook import (
    build_call_completed_payload,
    webhook_enabled,
    webhook_url,
    _transcript_lines,
    _http_status,
)


def test_payload():
    record = {
        "id": "call-uuid-123",
        "status": "completed",
        "outcome": "booked",
        "reason": "wants site visit",
        "duration_seconds": 245,
        "timestamp": "2026-06-03T10:00:00",
        "recording_url": "https://storage.example.com/rec.ogg",
        "transcript_json": json.dumps([
            {"role": "user", "text": "Hi", "ts": "2026-06-03T10:00:05"},
            {"role": "agent", "text": "Hello", "ts": "2026-06-03T10:00:08"},
        ]),
        "property_data": json.dumps({
            "budget": "3-4 Lakhs",
            "location_preference": "green area",
            "bhk": "3BHK",
        }),
    }
    payload = build_call_completed_payload(record, room_name="call-919876543210-1234")
    assert payload["id"] == "call-uuid-123"
    assert payload["status"] == "COMPLETED"
    assert payload["duration"] == 245
    assert payload["sentiment"] == 1
    assert payload["ai_score"] >= 75
    assert payload["lead_score"] == "HOT"
    assert payload["property_data"]["ai_score"] == payload["ai_score"]
    assert payload["room_name"] == "call-919876543210-1234"
    assert payload["property_data"]["min_budget"] == 300_000
    assert payload["property_data"]["max_budget"] == 400_000
    assert payload["min_budget"] == 300_000
    assert payload["max_budget"] == 400_000
    assert payload["budget"] == "3-4 Lakhs"
    assert payload["property_data"]["preferred_location"] == "green area"
    print("OK build_call_completed_payload")


def test_ai_lead_score():
    from db import compute_ai_lead_score
    hot = compute_ai_lead_score({
        "outcome": "booked",
        "duration_seconds": 200,
        "property_data": {
            "budget": "50-70 lakh",
            "city": "Pune",
            "bhk": "3BHK",
            "property_type": "Apartment",
        },
        "live_transcript": "Lead: hi\nAgent: hello",
    })
    assert hot["ai_score"] >= 75
    assert hot["lead_score"] == "HOT"

    cold = compute_ai_lead_score({
        "outcome": "do_not_call",
        "duration_seconds": 10,
        "property_data": {},
    })
    assert cold["ai_score"] <= 20
    assert cold["lead_score"] == "COLD"

    warm = compute_ai_lead_score({
        "outcome": "callback_requested",
        "duration_seconds": 70,
        "property_data": {"city": "Noida"},
        "live_transcript": "hi",
    })
    assert warm["lead_score"] in {"WARM", "HOT"}
    print("OK compute_ai_lead_score")


def test_sentiment_mapping():
    from db import sentiment_from_outcome, normalize_sentiment

    assert sentiment_from_outcome("booked") == 1
    assert sentiment_from_outcome("interested") == 1
    assert sentiment_from_outcome("not_interested") == -1
    assert sentiment_from_outcome("do_not_call") == -1
    assert sentiment_from_outcome("callback_requested") == 0
    assert sentiment_from_outcome("voicemail") == 0
    assert sentiment_from_outcome("no_answer") == 0
    assert sentiment_from_outcome("wrong_number") == 0
    assert sentiment_from_outcome("failed") == 0
    assert sentiment_from_outcome("completed") == 0
    assert sentiment_from_outcome("disconnect_in_between") == 0
    assert normalize_sentiment(None, "booked") == 1
    assert normalize_sentiment(-1, "booked") == -1  # stored wins
    # Missing DB column still yields score from outcome on webhook
    payload = build_call_completed_payload({
        "id": "x",
        "status": "completed",
        "outcome": "not_interested",
        "duration_seconds": 10,
        "timestamp": "2026-06-03T10:00:00",
    })
    assert payload["sentiment"] == -1
    print("OK sentiment mapping")


def test_failed_status():
    assert _http_status({"outcome": "failed", "status": "failed"}) == "FAILED"
    assert _http_status({"outcome": "booked", "status": "completed"}) == "COMPLETED"
    print("OK _http_status")


def test_transcript_empty():
    assert _transcript_lines(None, None) == []
    assert _transcript_lines("not-json", None) == []
    print("OK _transcript_lines edge cases")


def test_webhook_disabled_without_env():
    os.environ.pop("CALL_COMPLETED_WEBHOOK_URL", None)
    os.environ.pop("CALLING_SERVICE_BASE_URL", None)
    # Simulate Azure/production so local localhost default does not apply
    os.environ["CONFIG_FROM_ENV_ONLY"] = "true"
    try:
        assert webhook_url() == ""
        assert webhook_enabled() is False
    finally:
        os.environ.pop("CONFIG_FROM_ENV_ONLY", None)
    print("OK webhook disabled when env unset")


def test_webhook_local_defaults_to_localhost_5000():
    os.environ.pop("CONFIG_FROM_ENV_ONLY", None)
    os.environ.pop("WEBSITE_SITE_NAME", None)
    os.environ.pop("CONTAINER_APP_NAME", None)
    os.environ.pop("WEBSITE_INSTANCE_ID", None)
    url = webhook_url()
    assert url == "http://localhost:5000/api/calls/webhook/call-completed"
    assert webhook_enabled() is True
    print("OK local webhook URL is localhost:5000")


def test_explicit_webhook_url_wins_locally():
    old = os.environ.get("CALL_COMPLETED_WEBHOOK_URL")
    os.environ.pop("CONFIG_FROM_ENV_ONLY", None)
    os.environ["CALL_COMPLETED_WEBHOOK_URL"] = "https://backend.example/webhook/"
    try:
        assert webhook_url() == "https://backend.example/webhook"
    finally:
        if old is None:
            os.environ.pop("CALL_COMPLETED_WEBHOOK_URL", None)
        else:
            os.environ["CALL_COMPLETED_WEBHOOK_URL"] = old
    print("OK explicit webhook URL wins locally")


def test_requirements_always_sent_for_terminal_outcomes():
    for outcome in (
        "booked", "interested", "not_interested", "callback_requested", "wrong_number",
        "voicemail", "no_answer", "busy", "transferred", "do_not_call",
        "completed", "disconnect_in_between", "failed",
    ):
        payload = build_call_completed_payload({
            "id": f"call-{outcome}",
            "status": "completed",
            "outcome": outcome,
            "duration_seconds": 0,
            "timestamp": "2026-06-03T10:00:00",
            "organization_id": "org-1",
            "lead_id": "lead-1",
            "phone_number": "+919999999999",
            "lead_name": "Test Lead",
        })
        assert "property_data" in payload
        assert "client_requirements" in payload
        assert payload["organization_id"] == "org-1"
        assert payload["lead_id"] == "lead-1"
    print("OK requirements payload sent for all terminal outcomes")


async def _check_notify_skips_without_url():
    os.environ.pop("CALL_COMPLETED_WEBHOOK_URL", None)
    os.environ["CONFIG_FROM_ENV_ONLY"] = "true"
    try:
        from call_webhook import notify_call_completed
        ok = await notify_call_completed("fake-id")
        assert ok is False
    finally:
        os.environ.pop("CONFIG_FROM_ENV_ONLY", None)
    print("OK notify_call_completed no-op without URL")


def test_notify_skips_without_url():
    asyncio.run(_check_notify_skips_without_url())


if __name__ == "__main__":
    test_payload()
    test_ai_lead_score()
    test_sentiment_mapping()
    test_failed_status()
    test_transcript_empty()
    test_webhook_disabled_without_env()
    test_webhook_local_defaults_to_localhost_5000()
    test_explicit_webhook_url_wins_locally()
    test_requirements_always_sent_for_terminal_outcomes()
    test_notify_skips_without_url()
    print("\nAll checks passed.")
