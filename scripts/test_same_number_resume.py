"""Same-number mid-call resume picker."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import pick_same_number_resume_call, format_same_number_resume_block  # noqa: E402


def _row(**kwargs):
    now = datetime.now(timezone.utc)
    base = {
        "id": "prev",
        "phone_number": "+919876543210",
        "status": "completed",
        "outcome": "disconnect_in_between",
        "reason": "call disconnected mid-conversation",
        "timestamp": now.isoformat(),
        "live_transcript": "Agent: Budget?\nLead: 50 lakh",
        "transcript_json": json.dumps([
            {"role": "agent", "text": "Budget?"},
            {"role": "user", "text": "50 lakh"},
        ]),
        "property_data": json.dumps({"budget": "50 lakh", "city": "Noida"}),
    }
    base.update(kwargs)
    return base


def test_same_number_cut_resumes():
    current = _row(id="current", outcome="ringing", status="ringing", live_transcript="")
    prev = _row()
    got = pick_same_number_resume_call([current, prev], exclude_call_id="current")
    assert got and got["id"] == "prev"
    block = format_same_number_resume_block(got)
    assert "SAME PHONE NUMBER" in block
    assert "50 lakh" in block
    print("OK same number cut -> resume")


def test_other_outcome_does_not_resume():
    booked = _row(outcome="booked", reason="site visit")
    assert pick_same_number_resume_call([booked], exclude_call_id=None) is None
    print("OK booked last call -> no reconnect")


def test_stale_cut_does_not_resume():
    old = _row(timestamp=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
    assert pick_same_number_resume_call([old], exclude_call_id=None) is None
    print("OK stale cut -> no reconnect")


if __name__ == "__main__":
    test_same_number_cut_resumes()
    test_other_outcome_does_not_resume()
    test_stale_cut_does_not_resume()
    print("\nAll same-number resume checks passed.")
