"""Read-only validation of the Supabase columns required by production code."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("LOAD_DOTENV", "true")


REQUIRED_COLUMNS = {
    "call_logs": (
        "id,organization_id,lead_id,project_id,phone_number,status,outcome,"
        "live_transcript,transcript_json,property_data,duration_seconds,"
        "recording_url,gemini_key_slot,gemini_tokens_in,gemini_tokens_out,"
        "gemini_tokens_total"
    ),
    "appointments": "id,organization_id,lead_id,project_id,phone,date,time,status",
    "campaigns": (
        "id,contacts_json,status,system_prompt,additional_instructions,"
        "agent_profile_id,max_parallel_calls,queue_advance"
    ),
    "organization_projects": (
        "organization_id,project_id,project_name,project_details,"
        "additional_instructions,is_active,updated_at"
    ),
    "call_projects": "call_id,organization_id,project_id",
    "contact_memory": "id,organization_id,lead_id,phone_number,memory_key,insight,created_at",
    "transcript_write_queue": "call_id,transcript_json,live_transcript,attempts,last_error",
    "phone_numbers": (
        "e164,organization_id,status,current_call_id,current_room_name,"
        "assigned_agent,last_used_at,synced_at,vobiz_id,vobiz_status"
    ),
    "agent_profiles": "id,name,voice,model,system_prompt,enabled_tools,is_default",
    "settings": "key,value,updated_at",
    "error_logs": "id,source,message,detail,level,timestamp",
}


async def main() -> int:
    from db import _adb, init_db

    init_db()
    client = await _adb()
    failures = 0
    for table, columns in REQUIRED_COLUMNS.items():
        try:
            await client.table(table).select(columns).limit(1).execute()
            print(f"PASS {table}")
        except Exception as exc:
            failures += 1
            # Column/table errors are useful; never print request headers or credentials.
            print(f"FAIL {table}: {type(exc).__name__}: {str(exc)[:240]}")
    print(f"SUMMARY passed={len(REQUIRED_COLUMNS) - failures} failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
