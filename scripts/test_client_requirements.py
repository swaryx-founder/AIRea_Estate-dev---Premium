"""Unit tests for deterministic CRM requirement capture."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client_requirements import (
    detect_customer_intent,
    extract_client_requirements,
    infer_hangup_outcome,
)


def test_core_requirements():
    data = extract_client_requirements(
        "Mujhe Noida mein 4 BHK apartment chahiye, budget 2 crore hai aur three car parking honi chahiye."
    )
    assert data["city"] == "Noida"
    assert data["bhk"] == "4 BHK"
    assert data["property_type"] == "Apartment"
    assert data["budget"] == "2 crore"
    assert data["parking_spaces"] == "3"


def test_correction_is_extractable():
    first = extract_client_requirements("Delhi mein 3 BHK dekh raha hoon")
    correction = extract_client_requirements("Nahi, actually Noida mein 4 BHK chahiye")
    merged = {**first, **correction}
    assert merged["city"] == "Noida"
    assert merged["bhk"] == "4 BHK"


def test_intent_and_amenities():
    data = extract_client_requirements(
        "I am not interested. Mujhe low pollution, school aur hospital paas mein chahiye."
    )
    assert data["customer_intent"] == "not_interested"
    assert "Low pollution" in data["requirements"]
    assert "Nearby schools" in data["requirements"]
    assert "Nearby hospitals" in data["requirements"]


def test_detect_customer_intent():
    assert detect_customer_intent("I am interested") == "interested"
    assert detect_customer_intent("Haan interested hoon") == "interested"
    assert detect_customer_intent("Mujhe interest nahi hai") == "not_interested"
    assert detect_customer_intent("Not interested") == "not_interested"
    assert detect_customer_intent("Interested nahi") == "not_interested"
    assert detect_customer_intent("Dubara call mat karna") == "do_not_call"
    assert detect_customer_intent("Budget 2 crore hai") is None


def test_infer_hangup_prefers_intent_over_cut():
    transcript = json.dumps(
        [
            {"role": "agent", "text": "Kya aap interested hain?"},
            {"role": "user", "text": "Haan interested hoon"},
        ]
    )
    assert (
        infer_hangup_outcome(transcript_json=transcript) == "interested"
    )
    transcript_ni = json.dumps(
        [
            {"role": "user", "text": "Not interested, thank you"},
        ]
    )
    assert (
        infer_hangup_outcome(transcript_json=transcript_ni) == "not_interested"
    )
    # Last user signal wins
    both = json.dumps(
        [
            {"role": "user", "text": "Interested hoon"},
            {"role": "agent", "text": "Great"},
            {"role": "user", "text": "Actually interested nahi"},
        ]
    )
    assert infer_hangup_outcome(transcript_json=both) == "not_interested"
    # No intent → None (caller keeps disconnect_in_between)
    none = json.dumps([{"role": "user", "text": "Budget kitna hai?"}])
    assert infer_hangup_outcome(transcript_json=none) is None
    # property_data fallback
    assert (
        infer_hangup_outcome(property_data={"customer_intent": "interested"})
        == "interested"
    )


if __name__ == "__main__":
    test_core_requirements()
    test_correction_is_extractable()
    test_intent_and_amenities()
    test_detect_customer_intent()
    test_infer_hangup_prefers_intent_over_cut()
    print("OK client requirement extraction")
