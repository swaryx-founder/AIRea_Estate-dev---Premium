"""Demo webhook line-by-line output. Run: python scripts/demo_webhook_verbose.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

config.load_env()
os.environ.setdefault("LOAD_DOTENV", "true")

from call_webhook import notify_call_completed
from db import get_all_calls


async def main() -> None:
    calls = await get_all_calls(page=1, limit=1)
    if not calls:
        print("[WEBHOOK] No calls in DB — start one from http://localhost:8000 then run again")
        return
    call = calls[0]
    print(f"[WEBHOOK] Testing with call id={call['id']} status={call.get('status')}")
    await notify_call_completed(call["id"], room_name=call.get("reason") or "demo-room")


if __name__ == "__main__":
    asyncio.run(main())
