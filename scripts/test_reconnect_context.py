"""Regression checks for pre-dial reconnect context loading."""

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")

import agent


def test_slow_history_still_marks_reconnect() -> None:
    async def fake_calls(_phone):
        # Longer than the old 0.5-second prompt deadline.
        await asyncio.sleep(0.55)
        return [{
            "id": "previous-call",
            "status": "completed",
            "outcome": "disconnect_in_between",
            "reason": "call disconnected mid-conversation",
            "timestamp": "2099-01-01T10:00:00+00:00",
            "live_transcript": "Lead: Under one crore.\nAgent: Which location?",
            "transcript_json": "[]",
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
        async def resolve_prefetch():
            task = asyncio.create_task(
                agent._fetch_contact_context(
                    "+919876543210", exclude_call_id="current-call"
                )
            )
            return await agent._await_prefetched_contact_context(task)

        context, reconnect = asyncio.run(resolve_prefetch())
    finally:
        (
            agent.get_calls_by_phone,
            agent.get_appointments_by_phone,
            agent.get_contact_memory,
        ) = originals

    assert reconnect is True
    assert "THIS CALL IS A RECONNECT" in context
    assert "Under one crore" in context


def test_optional_crm_failure_does_not_erase_reconnect() -> None:
    async def fake_calls(_phone):
        return [{
            "id": "previous-call",
            "status": "completed",
            "outcome": "disconnect_in_between",
            "reason": "call disconnected mid-conversation",
            "timestamp": "2099-01-01T10:00:00+00:00",
            "live_transcript": "Lead: Ghaziabad.\nAgent: What is your budget?",
            "transcript_json": "[]",
        }]

    async def fake_failure(_phone):
        raise RuntimeError("optional table unavailable")

    originals = (
        agent.get_calls_by_phone,
        agent.get_appointments_by_phone,
        agent.get_contact_memory,
    )
    agent.get_calls_by_phone = fake_calls
    agent.get_appointments_by_phone = fake_failure
    agent.get_contact_memory = fake_failure
    try:
        context, reconnect = asyncio.run(agent._fetch_contact_context("+919876543210"))
    finally:
        (
            agent.get_calls_by_phone,
            agent.get_appointments_by_phone,
            agent.get_contact_memory,
        ) = originals

    assert reconnect is True
    assert "THIS CALL IS A RECONNECT" in context
    assert "Ghaziabad" in context


def test_history_failure_does_not_become_fresh_call() -> None:
    async def fake_history_failure(_phone):
        raise RuntimeError("call_logs unavailable")

    async def fake_empty(_phone):
        return []

    originals = (
        agent.get_calls_by_phone,
        agent.get_appointments_by_phone,
        agent.get_contact_memory,
    )
    agent.get_calls_by_phone = fake_history_failure
    agent.get_appointments_by_phone = fake_empty
    agent.get_contact_memory = fake_empty
    try:
        try:
            asyncio.run(agent._fetch_contact_context("+919876543210"))
        except agent._CallHistoryLookupError:
            pass
        else:
            raise AssertionError("history failure was incorrectly classified as a fresh call")
    finally:
        (
            agent.get_calls_by_phone,
            agent.get_appointments_by_phone,
            agent.get_contact_memory,
        ) = originals


if __name__ == "__main__":
    test_slow_history_still_marks_reconnect()
    test_optional_crm_failure_does_not_erase_reconnect()
    test_history_failure_does_not_become_fresh_call()
    print("Reconnect context checks passed.")
