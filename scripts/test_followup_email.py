"""Simulate a completed-call email follow-up without a real LiveKit call.

Usage:
  set LOAD_DOTENV=true
  set ACS_CONNECTION_STRING=...
  set ACS_SENDER_EMAIL=DoNotReply@....azurecomm.net
  set ACS_FOLLOWUP_TO=you@example.com
  python scripts/test_followup_email.py

Dry-run (no ACS send — only builds HTML):
  python scripts/test_followup_email.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("LOAD_DOTENV", "true")

from email_followup import (  # noqa: E402
    build_followup_template,
    is_valid_email,
    send_followup_email,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test ACS call follow-up email")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only build HTML; do not call Azure",
    )
    parser.add_argument(
        "--to",
        default=os.getenv("ACS_FOLLOWUP_TO", ""),
        help="Recipient override (default ACS_FOLLOWUP_TO)",
    )
    args = parser.parse_args()

    html = build_followup_template(
        lead_name="Test Lead",
        lead_phone="+919876543210",
        lead_email="lead@example.com",
        assigned_to_email=args.to or "agent@example.com",
        lead_score="HOT",
        call_id="test-call-0001",
        duration=81,
        outcome="callback_requested",
        summary=(
            "Lead showed interest in Green Valley (Noida). "
            "Budget around 80 lakh. Requested a callback tomorrow evening."
        ),
        agent_name="Riya",
    )
    subject = "Call completed — Test Lead (test)"

    print("--- HTML preview (first 500 chars) ---")
    print(html[:500], "...\n")

    if args.dry_run:
        print("OK dry-run: template built, ACS not called")
        return 0

    to_email = (args.to or "").strip()
    if not is_valid_email(to_email):
        print("ERROR: pass --to you@example.com or set ACS_FOLLOWUP_TO")
        return 1
    if not (os.getenv("ACS_CONNECTION_STRING") or "").strip():
        print("ERROR: ACS_CONNECTION_STRING is not set")
        return 1
    if not (os.getenv("ACS_SENDER_EMAIL") or "").strip():
        print("ERROR: ACS_SENDER_EMAIL is not set")
        return 1

    message_id = send_followup_email(to_email, subject, html)
    print(f"OK email sent to {to_email} message_id={message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
