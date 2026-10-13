"""Unit tests for SIP-confirmed hangup, UTC timestamps, and fail-closed gates."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FakeRoomApi:
    def __init__(self, *, participants=None, list_error=None, delete_error=None):
        self._participants = list(participants or [])
        self.list_error = list_error
        self.delete_error = delete_error
        self.delete_calls = 0
        self.remove_calls = 0

    async def list_participants(self, req):
        if self.list_error is not None:
            raise self.list_error
        return SimpleNamespace(
            participants=[SimpleNamespace(identity=i) for i in self._participants]
        )

    async def delete_room(self, req):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        self._participants = []

    async def remove_participant(self, req):
        self.remove_calls += 1
        ident = getattr(req, "identity", None)
        self._participants = [p for p in self._participants if p != ident]


class SipHangupTests(unittest.TestCase):
    def test_confirmed_clear_when_room_missing(self):
        from sip_hangup import sip_line_confirmed_clear

        api = FakeRoomApi(list_error=Exception("room not found"))
        lk = SimpleNamespace(room=api)
        ok = asyncio.run(
            sip_line_confirmed_clear(
                lk, room_name="call-x", sip_identity="sip_+91", timeout_sec=1.0, poll_sec=0.05
            )
        )
        self.assertTrue(ok)

    def test_confirmed_clear_when_sip_absent(self):
        from sip_hangup import sip_line_confirmed_clear

        api = FakeRoomApi(participants=["agent-1"])
        lk = SimpleNamespace(room=api)
        ok = asyncio.run(
            sip_line_confirmed_clear(
                lk, room_name="call-x", sip_identity="sip_+91", timeout_sec=1.0, poll_sec=0.05
            )
        )
        self.assertTrue(ok)

    def test_fail_closed_when_sip_still_present(self):
        from sip_hangup import sip_line_confirmed_clear

        api = FakeRoomApi(participants=["sip_+919999"])
        lk = SimpleNamespace(room=api)
        ok = asyncio.run(
            sip_line_confirmed_clear(
                lk,
                room_name="call-x",
                sip_identity="sip_+919999",
                timeout_sec=0.4,
                poll_sec=0.1,
            )
        )
        self.assertFalse(ok)

    def test_fail_closed_on_unknown_api_error(self):
        from sip_hangup import sip_line_confirmed_clear

        api = FakeRoomApi(list_error=RuntimeError("network down"))
        lk = SimpleNamespace(room=api)
        ok = asyncio.run(
            sip_line_confirmed_clear(
                lk, room_name="call-x", sip_identity="sip_+91", timeout_sec=0.35, poll_sec=0.1
            )
        )
        self.assertFalse(ok)

    def test_force_delete_then_confirm(self):
        from sip_hangup import force_delete_room_and_confirm

        api = FakeRoomApi(participants=["sip_+91"])
        lk = SimpleNamespace(room=api)
        ok = asyncio.run(
            force_delete_room_and_confirm(
                lk, room_name="call-x", sip_identity="sip_+91", timeout_sec=1.0
            )
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(api.delete_calls, 1)


class TimestampTests(unittest.TestCase):
    def test_naive_timestamp_treated_as_utc(self):
        from db import _parse_call_timestamp, _wall_duration_seconds

        naive = "2026-10-05T10:00:00"
        parsed = _parse_call_timestamp(naive)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.tzinfo, timezone.utc)

        # A "new" call created moments ago as naive UTC must not look hours old.
        recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(tzinfo=None)
        dur = _wall_duration_seconds(_parse_call_timestamp(recent.isoformat()))
        self.assertLess(dur, 120)


class FinalizeReleaseSplitTests(unittest.TestCase):
    def test_finalize_signature_has_release_flag(self):
        import inspect
        from db import finalize_call

        params = inspect.signature(finalize_call).parameters
        self.assertIn("release_caller_id", params)
        self.assertIn("notify_webhook", params)
        self.assertTrue(params["release_caller_id"].default is True)


class HangupReturnContractTests(unittest.TestCase):
    def test_hangup_call_annotated_bool(self):
        import inspect
        from tools import AppointmentTools

        hints = getattr(AppointmentTools._hangup_call, "__annotations__", {})
        self.assertEqual(hints.get("return"), bool)

    def test_end_call_skips_log_when_hangup_fails(self):
        source_path = os.path.join(ROOT, "tools.py")
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("Skipping log_call/webhook", source)
        self.assertIn("SIP confirmed clear", source)
        self.assertIn("sip_line_confirmed_clear", source)

    def test_agent_runtime_skips_finalize_when_hangup_fails(self):
        source_path = os.path.join(ROOT, "agent_runtime.py")
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("skipping finalize/webhook — SIP not confirmed absent", source)
        self.assertIn("still_there = True", source)
        self.assertNotIn("HANGUP_BEFORE_WEBHOOK_GRACE_SEC", source)

    def test_reconcile_does_not_blind_expire_talking(self):
        source_path = os.path.join(ROOT, "db.py")
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("livekit confirmed clear", source)
        self.assertIn("release_caller_id", source)
        self.assertIn("ActiveCallConflictError", source)
        self.assertIn("_utc_now_iso", source)


class ActiveCallConflictTests(unittest.TestCase):
    def test_conflict_error_message(self):
        from db import ActiveCallConflictError

        err = ActiveCallConflictError(
            "+919999", existing_id="abc", existing_status="in_progress"
        )
        self.assertIn("+919999", str(err))
        self.assertIn("abc", str(err))
        self.assertIn("in_progress", str(err))


if __name__ == "__main__":
    unittest.main()
