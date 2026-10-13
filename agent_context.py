"""CRM history and reconnect context used to build the immutable call prompt."""

import asyncio
import logging
from typing import Optional

from callback_schedule import format_callback_for_agent
from db import (
    format_same_number_resume_block,
    get_appointments_by_phone,
    get_calls_by_phone,
    get_contact_memory,
    pick_same_number_resume_call,
)


logger = logging.getLogger("outbound-agent")


class CallHistoryLookupError(RuntimeError):
    """Raised when reconnect state cannot be determined safely before dialing."""


async def fetch_contact_context(
    phone: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    exclude_call_id: Optional[str] = None,
) -> tuple[str, bool]:
    """Return prompt-ready CRM context and same-number reconnect status."""
    try:
        calls_lookup = (
            get_calls_by_phone(phone, organization_id=organization_id, lead_id=lead_id)
            if organization_id
            else get_calls_by_phone(phone)
        )
        appointments_lookup = (
            get_appointments_by_phone(phone, organization_id=organization_id, lead_id=lead_id)
            if organization_id
            else get_appointments_by_phone(phone)
        )
        memory_lookup = (
            get_contact_memory(phone, organization_id=organization_id, lead_id=lead_id)
            if organization_id
            else get_contact_memory(phone)
        )
        calls_result, appointments_result, memories_result = await asyncio.gather(
            calls_lookup,
            appointments_lookup,
            memory_lookup,
            return_exceptions=True,
        )
        if isinstance(calls_result, BaseException):
            raise CallHistoryLookupError("call history lookup failed") from calls_result
        calls = calls_result or []

        if isinstance(appointments_result, BaseException):
            logger.warning("Appointment prefetch failed (non-fatal): %s", appointments_result)
            appointments = []
        else:
            appointments = appointments_result or []

        if isinstance(memories_result, BaseException):
            logger.warning("Contact memory prefetch failed (non-fatal): %s", memories_result)
            memories = []
        else:
            memories = memories_result or []

        resume_row = pick_same_number_resume_call(calls, exclude_call_id=exclude_call_id)
        parts: list[str] = []
        if resume_row:
            parts.append(format_same_number_resume_block(resume_row))
        if not calls and not appointments and not memories and not resume_row:
            return (
                "PRIOR CONTACT DATA: First-time contact. Do NOT call lookup_contact before greeting.",
                False,
            )

        lines = [
            f"PRIOR CONTACT DATA for {phone} only (already loaded — greet FIRST, "
            "do NOT call lookup_contact now; do NOT mix other phone numbers):"
        ]
        if memories:
            lines.append(f"Remembered ({len(memories)} notes) for this number:")
            for memory in memories[:5]:
                lines.append(f"  • {memory['insight']}")
        if calls:
            lines.append(f"Call history for this number ({len(calls)} calls):")
            for call in calls[:3]:
                if exclude_call_id and str(call.get("id") or "") == str(exclude_call_id):
                    continue
                timestamp = (call.get("timestamp") or "")[:16]
                lines.append(f"  • {timestamp} — {call.get('outcome', '?')}")
                callback_schedule = format_callback_for_agent(call)
                if callback_schedule:
                    lines.append(f"    CALLBACK SCHEDULE: {callback_schedule}")
                    lines.append(
                        "    If the lead asks when they requested/scheduled this callback, "
                        "answer with this exact date and time. Say 'our records show' and "
                        "do not guess or substitute the current date."
                    )
                elif (call.get("outcome") or "").strip().lower() == "callback_requested":
                    lines.append(
                        "    CALLBACK SCHEDULE: exact date/time was not saved. If asked, "
                        "say the records do not show an exact time; do not guess."
                    )
        if appointments:
            lines.append(f"Appointments ({len(appointments)}):")
            for appointment in appointments[:2]:
                lines.append(
                    f"  • {appointment.get('date')} {appointment.get('time')} — "
                    f"{appointment.get('service')}"
                )
        parts.append("\n".join(lines))
        return "\n\n".join(parts), bool(resume_row)
    except CallHistoryLookupError:
        raise
    except Exception as exc:
        logger.warning("Contact prefetch failed (non-fatal): %s", exc)
        return (
            "PRIOR CONTACT DATA: unavailable. Greet first; skip lookup_contact unless needed later.",
            False,
        )


async def await_prefetched_contact_context(
    prefetch_task: "asyncio.Task[tuple[str, bool]]",
) -> tuple[str, bool]:
    """Resolve context before building the immutable Gemini session prompt."""
    fetched = await prefetch_task
    if isinstance(fetched, tuple):
        return fetched[0] or "", bool(fetched[1])
    return fetched or "", False


# Compatibility aliases for existing imports and tests.
_CallHistoryLookupError = CallHistoryLookupError
_fetch_contact_context = fetch_contact_context
_await_prefetched_contact_context = await_prefetched_contact_context
