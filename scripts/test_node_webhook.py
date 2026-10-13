"""Test Node webhook endpoint directly (Node must be running on :5000).

  python scripts/test_node_webhook.py
  python scripts/test_node_webhook.py <dispatch-call-id-from-post-api-call>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

try:
    import httpx
except ImportError:
    print("pip install httpx")
    sys.exit(1)


def main() -> None:
    config.load_env()
    url = config.get_env("CALL_COMPLETED_WEBHOOK_URL", "").strip()
    if not url:
        base = config.get_env("CALLING_SERVICE_BASE_URL", "").strip().rstrip("/")
        url = f"{base}/api/calls/webhook/call-completed" if base else ""
    if not url:
        print("Set CALL_COMPLETED_WEBHOOK_URL in .env (and LOAD_DOTENV=true for local)")
        sys.exit(1)

    call_id = sys.argv[1] if len(sys.argv) > 1 else "00000000-0000-0000-0000-000000000000"
    payload = {
        "id": call_id,
        "status": "COMPLETED",
        "outcome": "booked",
        "outcome_reason": "test script",
        "duration": 60,
        "transcript": [
            {"speaker": "agent", "text": "Test", "timestamp": 0},
            {"speaker": "customer", "text": "OK", "timestamp": 2},
        ],
    }
    print(f"POST {url}")
    print(json.dumps(payload, indent=2))
    r = httpx.post(url, json=payload, timeout=15)
    print(f"HTTP {r.status_code}")
    print(r.text[:1000])
    if r.status_code == 404:
        print("\n404 = Node has no Call with thirdPartyCallId =", call_id)
        print("Use real id from POST /api/call response after saving thirdPartyCallId in Node DB.")


if __name__ == "__main__":
    main()
