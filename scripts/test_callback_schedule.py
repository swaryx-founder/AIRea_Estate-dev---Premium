"""Tests for callback scheduled_at / callback_urgency contract."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callback_schedule import (  # noqa: E402
    format_callback_for_agent,
    resolve_callback_schedule,
    parse_relative_callback_when,
    normalize_callback_urgency,
)
from call_webhook import build_call_completed_payload  # noqa: E402


def test_urgency_aliases():
    assert normalize_callback_urgency("NOW") == "now"
    assert normalize_callback_urgency("specific") == "specific_time"
    assert normalize_callback_urgency("later") == "later"
    assert normalize_callback_urgency("no_schedule") == "none"
    assert normalize_callback_urgency("off_topic") == "none"
    print("OK urgency aliases")


def test_none_urgency_skips_auto_dial():
    fixed = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    out = resolve_callback_schedule(
        callback_urgency="none",
        reason="off-topic after two redirects",
        now=fixed,
    )
    assert out["callback_urgency"] == "none"
    assert out["scheduled_at"] is None
    print("OK none urgency → no scheduled_at")


def test_off_topic_reason_skips_auto_dial_without_urgency():
    fixed = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    out = resolve_callback_schedule(
        reason="scope lock off-topic close",
        now=fixed,
    )
    assert out["callback_urgency"] == "none"
    assert out["scheduled_at"] is None
    print("OK off-topic reason → no scheduled_at")


def test_now_default_offset():
    fixed = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    out = resolve_callback_schedule(
        callback_urgency="now",
        now=fixed,
    )
    assert out["callback_urgency"] == "now"
    assert out["scheduled_at"] == "2026-08-07T10:02:00.000Z"
    print("OK now → +2 minutes UTC")


def test_later_defaults_to_ten_minutes():
    fixed = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    out = resolve_callback_schedule(callback_urgency="later", scheduled_at_raw="", now=fixed)
    assert out["callback_urgency"] == "later"
    assert out["scheduled_at"] == "2026-08-07T10:10:00.000Z"
    print("OK later → +10 minutes UTC")


def test_specific_tomorrow_5pm_ist():
    fixed = datetime(2026, 8, 6, 6, 0, 0, tzinfo=timezone.utc)  # afternoon IST
    out = resolve_callback_schedule(
        scheduled_at_raw="tomorrow 5pm",
        callback_urgency="specific_time",
        now=fixed,
    )
    assert out["callback_urgency"] == "specific_time"
    # 5pm IST = 11:30 UTC
    assert out["scheduled_at"] == "2026-08-07T11:30:00.000Z"
    print("OK tomorrow 5pm IST → UTC")


def test_after_two_minutes():
    fixed = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
    dt = parse_relative_callback_when("after 2 minutes", now=fixed)
    assert dt is not None
    assert dt.astimezone(timezone.utc).isoformat().startswith("2026-08-07T12:02")
    five = parse_relative_callback_when("in 5 minutes", now=fixed)
    assert five is not None
    assert five.astimezone(timezone.utc).isoformat().startswith("2026-08-07T12:05")
    print("OK after 2 minutes / in 5 minutes")


def test_reason_lead_requested_call_in_two_minutes():
    fixed = datetime(2026, 8, 17, 10, 9, 0, tzinfo=timezone.utc)
    out = resolve_callback_schedule(
        reason="lead requested call in 2 minutes",
        now=fixed,
    )
    assert out["callback_urgency"] == "now"
    assert out["scheduled_at"] == "2026-08-17T10:11:00.000Z"
    print("OK reason: lead requested call in 2 minutes")


def test_webhook_payload_includes_callback_fields():
    payload = build_call_completed_payload({
        "id": "cb-test-1",
        "status": "completed",
        "outcome": "callback_requested",
        "reason": "agreed callback: tomorrow 5pm",
        "duration_seconds": 90,
        "timestamp": "2026-08-06T10:00:00",
        "property_data": {
            "scheduled_at": "2026-08-07T11:30:00.000Z",
            "callback_urgency": "specific_time",
        },
    })
    assert payload["outcome"] == "callback_requested"
    assert payload["scheduled_at"] == "2026-08-07T11:30:00.000Z"
    assert payload["callback_urgency"] == "specific_time"
    assert payload["property_data"]["scheduled_at"] == payload["scheduled_at"]
    print("OK webhook includes scheduled_at + callback_urgency")


def test_agent_callback_context_uses_persisted_time():
    formatted = format_callback_for_agent({
        "outcome": "callback_requested",
        "scheduled_at": "2026-09-17T11:30:00.000Z",
        "callback_urgency": "specific_time",
    })
    assert formatted is not None
    assert "Thursday, 17 September 2026 at 05:00 PM IST" in formatted
    assert "2026-09-17T11:30:00.000Z" in formatted

    # Old records without a persisted schedule must not use a made-up current time.
    assert format_callback_for_agent({
        "outcome": "callback_requested",
        "reason": "call me later",
    }) is None
    print("OK agent callback context uses exact persisted schedule")


def test_contact_prefetch_injects_callback_schedule():
    import asyncio
    import agent

    async def fake_calls(_phone):
        return [{
            "id": "previous-call",
            "status": "completed",
            "outcome": "callback_requested",
            "timestamp": "2026-09-16T10:00:00.000Z",
            "scheduled_at": "2026-09-17T11:30:00.000Z",
            "callback_urgency": "specific_time",
        }]

    async def fake_empty(_phone):
        return []

    originals = (
        agent.get_calls_by_phone,
        agent.get_appointments_by_phone,
        agent.get_contact_memory,
    )
    agent.get_calls_by_phone = fake_calls
    agent.get_appointments_by_phone = fake_empty
    agent.get_contact_memory = fake_empty
    try:
        context, reconnect = asyncio.run(agent._fetch_contact_context("+919876543210"))
    finally:
        (
            agent.get_calls_by_phone,
            agent.get_appointments_by_phone,
            agent.get_contact_memory,
        ) = originals

    assert reconnect is False
    assert "CALLBACK SCHEDULE" in context
    assert "Thursday, 17 September 2026 at 05:00 PM IST" in context
    assert "answer with this exact date and time" in context
    print("OK contact prefetch injects callback schedule into Gemini prompt")


if __name__ == "__main__":
    test_urgency_aliases()
    test_none_urgency_skips_auto_dial()
    test_off_topic_reason_skips_auto_dial_without_urgency()
    test_now_default_offset()
    test_later_defaults_to_ten_minutes()
    test_specific_tomorrow_5pm_ist()
    test_after_two_minutes()
    test_reason_lead_requested_call_in_two_minutes()
    test_webhook_payload_includes_callback_fields()
    test_agent_callback_context_uses_persisted_time()
    test_contact_prefetch_injects_callback_schedule()
    print("\nAll callback schedule checks passed.")
