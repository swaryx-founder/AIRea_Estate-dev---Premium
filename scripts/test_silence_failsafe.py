"""Unit tests for SilenceFailsafe loop guards (no LiveKit network)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
os.environ["SILENCE_FAILSAFE_ENABLED"] = "true"
os.environ["SILENCE_FAILSAFE_MAX"] = "2"
os.environ["USER_SILENCE_ACTIVE_CHAT_SEC"] = "12"
os.environ["SILENCE_FAILSAFE_SEC"] = "7"

import agent_runtime


class _FakeSession:
    def __init__(self):
        self.agent_state = "listening"
        self.user_state = "listening"
        self._closing = False
        self._activity = None
        self.replies = []

    def generate_reply(self, *, instructions, allow_interruptions=True):
        self.replies.append(instructions)
        return SimpleNamespace(interrupted=False)


async def _test_active_chat_defers_failsafe_instead_of_dropping() -> None:
    session = _FakeSession()
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    fs._last_user_speech_at = time.time()
    fs.on_agent_state_changed(
        SimpleNamespace(old_state="speaking", new_state="listening")
    )
    assert fs._task is not None, "active chat must schedule a deferred rearm"
    assert fs._task.get_name() == "silence_failsafe_deferred"
    fs.stop()


def test_active_chat_defers_failsafe_instead_of_dropping() -> None:
    asyncio.run(_test_active_chat_defers_failsafe_instead_of_dropping())


async def _test_deferred_active_chat_rearms_silence_timer() -> None:
    session = _FakeSession()
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    # Grace already expired — deferred wait should immediately arm the silence timer.
    fs._last_user_speech_at = time.time() - 20.0
    await fs._run_after_active_chat(0.01)
    assert fs._task is not None
    assert fs._task.get_name() == "silence_failsafe"
    fs.stop()


def test_deferred_active_chat_rearms_silence_timer() -> None:
    asyncio.run(_test_deferred_active_chat_rearms_silence_timer())


async def _test_failsafe_does_not_stack_schedule_while_nudge_in_flight() -> None:
    session = _FakeSession()
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    fs._nudge_in_flight = True
    fs._count = 1
    fs._schedule(force=True)
    assert fs._task is None, "must not schedule while hear-check is still speaking"


def test_failsafe_does_not_stack_schedule_while_nudge_in_flight() -> None:
    asyncio.run(_test_failsafe_does_not_stack_schedule_while_nudge_in_flight())


def test_user_speech_resets_failsafe_budget() -> None:
    session = _FakeSession()
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    fs._count = 2
    fs._awaiting_reply_to_nudge = True
    fs.on_user_state_changed(SimpleNamespace(new_state="speaking"))
    assert fs._count == 0
    assert fs._awaiting_reply_to_nudge is False


def test_max_default_is_two() -> None:
    assert agent_runtime._SILENCE_FAILSAFE_MAX == 2


async def _test_false_vad_rearms_when_user_stops() -> None:
    session = _FakeSession()
    session.agent_state = "listening"
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    fs.on_user_state_changed(SimpleNamespace(old_state="listening", new_state="speaking"))
    assert fs._task is None
    fs.on_user_state_changed(SimpleNamespace(old_state="speaking", new_state="listening"))
    assert fs._task is not None, "watchdog must rearm after false VAD ends"
    fs.stop()


def test_false_vad_rearms_when_user_stops() -> None:
    asyncio.run(_test_false_vad_rearms_when_user_stops())


async def _test_should_fire_false_does_not_kill_watchdog() -> None:
    session = _FakeSession()
    session._activity = None  # _session_can_speak → False
    fs = agent_runtime.SilenceFailsafe(session, model="gemini-3.8-live", lead_name="Rahul")
    fs.arm()
    fs._task = asyncio.create_task(asyncio.sleep(0), name="placeholder")
    await fs._task
    # Simulate end of a silence wait that cannot fire yet.
    fs._task = asyncio.current_task()
    # Detach and ensure retry path works from a finished wait.
    fs._task = None
    fs._ensure_armed_timer(delay=0.05)
    assert fs._task is not None
    assert fs._task.get_name() == "silence_failsafe_retry"
    await asyncio.sleep(0.08)
    # After delay, schedule() should create the main silence timer (or deferred).
    assert fs._task is not None
    fs.stop()


def test_should_fire_false_does_not_kill_watchdog() -> None:
    asyncio.run(_test_should_fire_false_does_not_kill_watchdog())


if __name__ == "__main__":
    test_active_chat_defers_failsafe_instead_of_dropping()
    print("OK active chat defers failsafe")
    test_deferred_active_chat_rearms_silence_timer()
    print("OK deferred active chat rearms silence timer")
    test_failsafe_does_not_stack_schedule_while_nudge_in_flight()
    print("OK no stacked schedule during nudge")
    test_user_speech_resets_failsafe_budget()
    print("OK user speech resets budget")
    test_max_default_is_two()
    print("OK silence failsafe max is 2")
    test_false_vad_rearms_when_user_stops()
    print("OK false VAD rearms watchdog")
    test_should_fire_false_does_not_kill_watchdog()
    print("OK should_fire false keeps watchdog alive")
    print("All silence failsafe checks passed.")
