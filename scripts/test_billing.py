"""Unit checks for billing + Vobiz CDR matching (run: python scripts/test_billing.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from billing import billable_minutes, estimated_cost, summarize_usage
from vobiz_api import apply_vobiz_cdr, match_cdr_for_call, normalize_phone


def test_billable_minutes_ceil():
    assert billable_minutes(0) == 0.0
    assert billable_minutes(1) == 1.0
    assert billable_minutes(60) == 1.0
    assert billable_minutes(61) == 2.0
    assert billable_minutes(209) == 4.0


def test_matches_vobiz_console_pulse():
    # Vobiz: 209s → ₹1.80 at ₹0.45/min (ceil 4 minutes)
    assert estimated_cost(209, rate=0.45) == 1.80
    # Earlier console: 133s → ₹1.35 = 3 × 0.45
    assert estimated_cost(133, rate=0.45) == 1.35


def test_normalize_phone():
    assert normalize_phone("+919990274564") == "9990274564"
    assert normalize_phone("919990274564") == "9990274564"


def test_match_cdr_ist_naive_timestamp():
    # call_logs store naive IST; CDR uses UTC — must still match.
    call = {
        "phone_number": "+919876543210",
        "timestamp": "2026-07-18T16:44:00",  # IST naive
        "duration_seconds": 209,
    }
    cdrs = [
        {
            "uuid": "a",
            "destination_number": "+919876543210",
            "billsec": 209,
            "total_cost": 1.8,
            "currency": "INR",
            "end_time": "2026-07-18T11:14:00Z",  # same moment in UTC
        },
    ]
    hit = match_cdr_for_call(call, cdrs)
    assert hit and hit["uuid"] == "a"
    out = apply_vobiz_cdr({}, hit)
    assert out["estimated_cost"] == 1.8
    assert out["rate_source"] == "vobiz_cdr"
    assert out["billable_minutes"] == 4.0


def test_match_prefers_billed_cdr_over_empty():
    # Real case: 81s call was ₹0.90 on Vobiz but dashboard latched onto empty CDR.
    call = {
        "phone_number": "+919811122233",
        "timestamp": "2026-07-20T08:30:00",
        "duration_seconds": 81,
    }
    cdrs = [
        {
            "uuid": "empty",
            "destination_number": "+919811122233",
            "billsec": 0,
            "duration": 0,
            "total_cost": 0,
            "end_time": "2026-07-20T02:59:50Z",
        },
        {
            "uuid": "billed",
            "destination_number": "+919811122233",
            "billsec": 81,
            "duration": 85,
            "total_cost": 0.9,
            "currency": "INR",
            "end_time": "2026-07-20T03:00:10Z",
        },
    ]
    hit = match_cdr_for_call(call, cdrs)
    assert hit and hit["uuid"] == "billed"
    out = apply_vobiz_cdr({}, hit)
    assert out["estimated_cost"] == 0.9
    assert out["billable_minutes"] == 2.0
    assert out["currency"] == "INR"


def test_match_allows_nonconnected_fee():
    call = {
        "phone_number": "+919811122233",
        "timestamp": "2026-07-20T08:29:00",
        "duration_seconds": 31,
    }
    cdrs = [
        {
            "uuid": "fee",
            "destination_number": "+919811122233",
            "billsec": 0,
            "duration": 31,
            "total_cost": 0.02,
            "currency": "INR",
            "end_time": "2026-07-20T02:59:00Z",
        },
    ]
    hit = match_cdr_for_call(call, cdrs)
    assert hit and hit["uuid"] == "fee"
    out = apply_vobiz_cdr({}, hit)
    assert out["estimated_cost"] == 0.02


def test_summarize_skips_corrupt_duration():
    rows = [
        {"duration_seconds": 60, "rate_source": "estimate"},
        {"duration_seconds": 99_999_999, "rate_source": "estimate"},
        {
            "duration_seconds": 200,
            "billable_minutes": 4.0,
            "estimated_cost": 1.8,
            "rate_source": "vobiz_cdr",
            "vobiz_billsec": 209,
        },
    ]
    s = summarize_usage(rows, organization_id="org_1", rate=0.45)
    assert s["skipped_corrupt_rows"] == 1
    assert s["vobiz_matched_calls"] == 1
    assert s["rate_source"] == "vobiz_cdr"
    assert s["rate_per_minute"] == 0.45  # 1.8 / 4 mins from CDR
    assert s["estimated_cost"] == round(0.45 + 1.8, 2)


def test_summarize_hides_env_rate_when_auth_no_match():
    s = summarize_usage(
        [
            {
                "duration_seconds": 120,
                "billable_minutes": 2.0,
                "estimated_cost": 0.9,
                "rate_source": "vobiz_cdr",
                "vobiz_billsec": 120,
            }
        ],
        organization_id=None,
    )
    assert s["rate_source"] == "vobiz_cdr"
    assert s["rate_per_minute"] == 0.45
    assert s["estimated_cost"] == 0.9


if __name__ == "__main__":
    test_billable_minutes_ceil()
    test_matches_vobiz_console_pulse()
    test_normalize_phone()
    test_match_cdr_ist_naive_timestamp()
    test_match_prefers_billed_cdr_over_empty()
    test_match_allows_nonconnected_fee()
    test_summarize_skips_corrupt_duration()
    test_summarize_hides_env_rate_when_auth_no_match()
    print("OK: billing tests passed")
