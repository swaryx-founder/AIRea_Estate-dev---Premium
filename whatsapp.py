"""
Post-call WhatsApp message trigger via backend API.

Endpoint: http://192.168.31.177:5000/api/calls/whatsapp/send
Payload:
{
  "organizationId": "<ORGANIZATION_ID>",
  "leadId": "<LEAD_ID>",
  "templateName": "welcome_template",
  "variables": {
    "name": "<LEAD_NAME>"
  },
  "campaignId": "<CAMPAIGN_ID>"
}

Triggered from finalize_call() after call completes if lead confirmed WhatsApp message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import config

logger = logging.getLogger("whatsapp")

# Strong references for fire-and-forget background tasks
_background_tasks: set[asyncio.Task] = set()

DEFAULT_BACKEND_SEND_URL = "http://192.168.31.177:5000/api/calls/whatsapp/send"
DEFAULT_TEMPLATE_NAME = "property_inquiry_followup"

SKIP_OUTCOMES = frozenset({
    "no_answer", "voicemail", "wrong_number", "failed", "not_interested",
    "disconnect_in_between",
})


def whatsapp_enabled() -> bool:
    """Check if WhatsApp backend sending is enabled."""
    if config.get_env("WHATSAPP_BACKEND_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    url = config.get_env("WHATSAPP_BACKEND_SEND_URL", DEFAULT_BACKEND_SEND_URL).strip()
    return bool(url)


def should_send_whatsapp(outcome: Optional[str], property_data: Optional[dict] = None) -> bool:
    """Check if the user confirmed/wants a WhatsApp message.
    Strictly returns False if the user declined, said no, or had an unserviceable/negative outcome.
    """
    pdata = property_data or {}

    # 1. Explicit negative consent / opt-out
    if pdata.get("wants_whatsapp") is False or pdata.get("whatsapp_confirmed") is False:
        return False

    wa_val = str(pdata.get("whatsapp_number") or pdata.get("whatsapp") or "").strip().lower()
    if any(neg in wa_val for neg in ("no", "none", "decline", "not interested", "dont send", "don't send", "false", "n/a", "nahi", "mat bhejo", "mat bhejna")):
        return False

    # 2. Negative or incomplete call outcomes (skip sending)
    if not outcome or outcome.strip().lower() in SKIP_OUTCOMES:
        return False

    # 3. Explicit positive consent
    if pdata.get("wants_whatsapp") is True or pdata.get("whatsapp_confirmed") is True:
        return True
    if pdata.get("whatsapp_number"):
        return True

    return True


async def send_whatsapp_to_backend(
    record: dict,
    property_data: Optional[dict] = None,
    *,
    backend_url: Optional[str] = None,
    template_name: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Send the WhatsApp message payload to the backend endpoint.
    """
    import httpx

    url = (
        backend_url
        or config.get_env("WHATSAPP_BACKEND_SEND_URL", DEFAULT_BACKEND_SEND_URL)
    ).strip()
    if not url:
        return False, "WHATSAPP_BACKEND_SEND_URL not set"

    tmpl = (
        template_name
        or config.get_env("WHATSAPP_TEMPLATE_NAME", DEFAULT_TEMPLATE_NAME)
    ).strip() or DEFAULT_TEMPLATE_NAME

    lead_name = (record.get("lead_name") or "there").strip()
    org_id = (
        record.get("organization_id")
        or (property_data or {}).get("organization_id")
        or (property_data or {}).get("organizationId")
        or ""
    )
    lead_id = (
        record.get("lead_id")
        or (property_data or {}).get("lead_id")
        or (property_data or {}).get("leadId")
        or ""
    )
    campaign_id = (
        record.get("campaign_id")
        or (property_data or {}).get("campaign_id")
        or (property_data or {}).get("campaignId")
        or ""
    )

    phone_num = (record.get("phone_number") or record.get("phone") or "").strip()
    payload: dict[str, Any] = {
        "organizationId": org_id,
        "leadId": lead_id,
        "templateName": tmpl,
        "variables": {
            "name": lead_name,
        },
        "campaignId": campaign_id,
        "phone": phone_num,
        "phoneNumber": phone_num,
    }

    timeout = float(config.get_env("WHATSAPP_BACKEND_TIMEOUT", "15") or "15")
    # WhatsApp authentication is owned by the backend. The AI service sends
    # call context only and never accepts, stores, or forwards an auth token.
    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            preview = (resp.text or "")[:400]
            return False, f"HTTP {resp.status_code}: {preview}"
        return True, (resp.text or "ok")[:200]
    except Exception as exc:
        return False, str(exc)


async def notify_whatsapp_followup(call_id: str) -> bool:
    """Invoked after call finalization to trigger WhatsApp backend API."""
    from db import get_call_by_id, log_error, parse_property_data

    if not whatsapp_enabled():
        logger.debug("WhatsApp backend send skipped (not enabled) — call_id=%s", call_id)
        return False

    record = await get_call_by_id(call_id)
    if not record:
        logger.warning("WhatsApp skipped — call %s not found", call_id)
        return False

    outcome = record.get("outcome")
    property_data = parse_property_data(record.get("property_data"))

    if not should_send_whatsapp(outcome, property_data):
        await log_error(
            "whatsapp",
            f"WhatsApp skipped for outcome={outcome}",
            f"call_id={call_id}",
            "info",
        )
        return False

    ok, detail = await send_whatsapp_to_backend(record, property_data)
    if ok:
        await log_error(
            "whatsapp",
            f"WhatsApp API sent for lead_id={record.get('lead_id')}",
            f"call_id={call_id} response={detail}",
            "info",
        )
        logger.info("WhatsApp API sent call_id=%s lead_id=%s", call_id, record.get("lead_id"))
        return True

    await log_error(
        "whatsapp",
        f"WhatsApp API failed for lead_id={record.get('lead_id')}",
        f"call_id={call_id} error={detail}",
        "warning",
    )
    logger.warning("WhatsApp API failed call_id=%s: %s", call_id, detail)
    return False


def schedule_whatsapp_followup(call_id: str) -> None:
    """Fire-and-forget WhatsApp dispatch after call is finalized."""
    if not whatsapp_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("WhatsApp: no event loop — call_id=%s", call_id)
        return

    task = loop.create_task(notify_whatsapp_followup(call_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
