import asyncio
import json
import os
import sys

os.environ["LOAD_DOTENV"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_all_calls
from whatsapp import send_whatsapp_to_backend

async def main():
    calls = await get_all_calls(limit=5)
    if not calls:
        print("No calls found in database.")
        return

    last = calls[0]
    print(f"Latest Call ID: {last.get('id')}")
    print(f"Phone: {last.get('phone_number')}")
    print(f"Lead Name: {last.get('lead_name')}")
    print(f"Lead ID: {last.get('lead_id')}")
    print(f"Org ID: {last.get('organization_id')}")
    print(f"Outcome: {last.get('outcome')}")
    
    print("\nTriggering WhatsApp send to backend...")
    ok, msg = await send_whatsapp_to_backend(last)
    print(f"Success: {ok}")
    print(f"Backend Response: {msg}")

if __name__ == "__main__":
    asyncio.run(main())
