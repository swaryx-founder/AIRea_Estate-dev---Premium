"""Build a local HTML preview of the ACS follow-up email (no send).

Writes: /tmp/email_preview.html
On Windows that resolves to C:\\tmp\\email_preview.html

Usage:
  python scripts/preview_email_template.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_followup import build_followup_template


def main() -> int:
    html = build_followup_template(
        lead_name="Rahul Sharma",
        lead_phone="+919876543210",
        lead_email="rahul.sharma@example.com",
        assigned_to_email="agent.priya@swaryx.com",
        lead_score="HOT",
        call_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        duration=245,
        outcome="callback_requested",
        summary=(
            "Lead inquired about Green Valley (Noida Sector 150). "
            "Budget is around 80–90 lakh for a 3BHK. "
            "Interested in a site visit this weekend; preferred callback tomorrow evening after 6 PM. "
            "Mentioned comparing with Sky Towers as well."
        ),
        agent_name="Priya",
    )

    out = Path("/tmp/email_preview.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"OK wrote {out.resolve()} ({len(html)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
