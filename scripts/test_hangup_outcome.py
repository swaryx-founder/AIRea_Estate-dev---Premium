"""Tests for abrupt hangup outcome inference."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hangup_outcome import infer_hangup_outcome


def test_not_interested_hangup():
    live = (
        "Agent: Kya abhi baat karne ka sahi samay hai?\n"
        "Lead: Not interested.\n"
        "Agent: Thik hai.\n"
    )
    outcome, reason = infer_hangup_outcome(live_transcript=live)
    assert outcome == "not_interested", outcome
    assert "not_interested" in reason


def test_interested_hangup():
    live = (
        "Agent: Project ke baare mein bataun?\n"
        "Lead: Haan, I am interested.\n"
        "Agent: Budget kya hai?\n"
    )
    outcome, reason = infer_hangup_outcome(live_transcript=live)
    assert outcome == "interested", outcome
    assert "interest" in reason


def test_investment_view_counts_as_interest():
    live = (
        "Agent: Investment ya business?\n"
        "Lead: investment point of view se dekh rahe hain.\n"
    )
    outcome, reason = infer_hangup_outcome(live_transcript=live)
    assert outcome == "interested", outcome


def test_unclear_stays_disconnect():
    live = (
        "Agent: Namaste?\n"
        "Lead: Haan.\n"
        "Agent: Main Riya bol rahi hoon.\n"
    )
    outcome, reason = infer_hangup_outcome(live_transcript=live)
    assert outcome == "disconnect_in_between", outcome


def test_property_data_intent_wins():
    live = "Lead: hello\n"
    outcome, _ = infer_hangup_outcome(
        live_transcript=live,
        property_data={"customer_intent": "not_interested"},
    )
    assert outcome == "not_interested"


if __name__ == "__main__":
    test_not_interested_hangup()
    test_interested_hangup()
    test_investment_view_counts_as_interest()
    test_unclear_stays_disconnect()
    test_property_data_intent_wins()
    print("OK hangup_outcome")
