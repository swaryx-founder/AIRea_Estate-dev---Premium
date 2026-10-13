"""Read-only, sanitized dump of recent calls for QA review."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("LOAD_DOTENV", "true")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _mask_phone(value: object) -> str:
    phone = str(value or "")
    return f"{phone[:3]}***{phone[-3:]}" if len(phone) > 7 else "***"


async def main() -> int:
    from db import _adb, init_db

    init_db()
    client = await _adb()
    result = await (
        client.table("call_logs")
        .select(
            "id,phone_number,lead_name,status,outcome,reason,timestamp,"
            "duration_seconds,live_transcript,transcript_json,property_data,"
            "project_id,organization_id,lead_id,recording_url"
        )
        .order("timestamp", desc=True)
        .limit(3)
        .execute()
    )
    rows = result.data or []
    for index, source in enumerate(rows, 1):
        row = dict(source)
        row["phone_number"] = _mask_phone(row.get("phone_number"))
        print(f"CALL_ROW_{index} {json.dumps(row, ensure_ascii=False, default=str)}")

    if rows:
        organization_id = rows[0].get("organization_id")
        if organization_id:
            projects = await (
                client.table("organization_projects")
                .select(
                    "project_id,project_name,project_details,additional_instructions,is_active"
                )
                .eq("organization_id", organization_id)
                .execute()
            )
            print(
                "VERIFIED_PROJECTS "
                + json.dumps(projects.data or [], ensure_ascii=False, default=str)
            )

        recent_errors = await (
            client.table("error_logs")
            .select("source,message,detail,level,timestamp")
            .order("timestamp", desc=True)
            .limit(50)
            .execute()
        )
        call_ids = {str(row.get("id") or "") for row in rows}
        relevant = []
        for item in recent_errors.data or []:
            text = f"{item.get('message') or ''} {item.get('detail') or ''}"
            if any(call_id and call_id in text for call_id in call_ids) or any(
                marker in text.upper()
                for marker in ("SILENCE_FAILSAFE", "GREETING", "RECONNECT")
            ):
                relevant.append(item)
        print("RELEVANT_LOGS " + json.dumps(relevant, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
