"""Regression checks for transcript capture and failed-write recovery."""

import asyncio
import inspect
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")

import agent
import db
from transcripts import CallTranscriptTracker


def test_tracker_retries_before_queueing() -> None:
    attempts = 0
    queued = []
    completed = []

    async def flaky_update(call_id, transcript_json, live_transcript):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary write failure")

    async def fake_enqueue(*args, **kwargs):
        queued.append((args, kwargs))

    async def fake_complete(call_id):
        completed.append(call_id)

    originals = (
        db.update_call_transcript,
        db.enqueue_transcript_write,
        db.complete_queued_transcript_write,
    )
    db.update_call_transcript = flaky_update
    db.enqueue_transcript_write = fake_enqueue
    db.complete_queued_transcript_write = fake_complete
    try:
        tracker = CallTranscriptTracker("call-1")
        tracker.add("agent", "Hello")
        assert asyncio.run(tracker.flush()) is True
    finally:
        (
            db.update_call_transcript,
            db.enqueue_transcript_write,
            db.complete_queued_transcript_write,
        ) = originals

    assert attempts == 3
    assert queued == []
    assert completed == []


def test_tracker_queues_after_three_failures() -> None:
    attempts = 0
    queued = []

    async def failed_update(call_id, transcript_json, live_transcript):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database unavailable")

    async def fake_enqueue(*args, **kwargs):
        queued.append((args, kwargs))

    async def fake_complete(_call_id):
        raise AssertionError("failed transcript must not be completed")

    originals = (
        db.update_call_transcript,
        db.enqueue_transcript_write,
        db.complete_queued_transcript_write,
    )
    db.update_call_transcript = failed_update
    db.enqueue_transcript_write = fake_enqueue
    db.complete_queued_transcript_write = fake_complete
    try:
        tracker = CallTranscriptTracker("call-2")
        tracker.add("user", "Can you hear me?")
        assert asyncio.run(tracker.flush()) is False
    finally:
        (
            db.update_call_transcript,
            db.enqueue_transcript_write,
            db.complete_queued_transcript_write,
        ) = originals

    assert attempts == 3
    assert len(queued) == 1
    assert queued[0][0][0] == "call-2"
    assert queued[0][1]["attempts"] == 3


def test_handlers_are_attached_before_session_start() -> None:
    source = inspect.getsource(agent._run_outbound_job)
    attach_at = source.index("attach_transcript_handlers(session, tool_ctx.transcript_tracker)")
    start_at = source.index("await session.start(**_session_kwargs)")
    greeting_at = source.index("await _speak_greeting(")
    assert attach_at < start_at < greeting_at


if __name__ == "__main__":
    test_tracker_retries_before_queueing()
    test_tracker_queues_after_three_failures()
    test_handlers_are_attached_before_session_start()
    print("Transcript reliability checks passed.")
