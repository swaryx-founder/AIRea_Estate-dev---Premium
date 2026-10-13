"""
Post-call email follow-up via Azure Communication Services.

Triggered from finalize_call() after Supabase is updated (same pattern as
call_webhook + whatsapp). There is no separate services/notification_service
package in this repo — this module is the notification email path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from typing import Optional

import config

logger = logging.getLogger("email-followup")

# Keep strong refs to fire-and-forget tasks so they are not GC'd mid-run.
_background_tasks: set[asyncio.Task] = set()

# Missed / failed style outcomes — do not email (status may still be "completed").
SKIP_OUTCOMES = frozenset({
    "no_answer", "voicemail", "wrong_number", "failed", "busy", "cancelled",
    "disconnect_in_between",
})
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def email_followup_enabled() -> bool:
    if config.get_env("ACS_EMAIL_ENABLED", "").lower() in ("0", "false", "no"):
        return False
    conn = (config.get_env("ACS_CONNECTION_STRING", "") or "").strip()
    sender = (config.get_env("ACS_SENDER_EMAIL", "") or "").strip()
    return bool(conn and sender)


def is_valid_email(value: Optional[str]) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


def should_send_email_followup(status: Optional[str], outcome: Optional[str]) -> bool:
    if (status or "").strip().lower() != "completed":
        return False
    if not outcome:
        return True
    return outcome.strip().lower() not in SKIP_OUTCOMES


def resolve_recipient_email(record: dict, property_data: dict) -> Optional[str]:
    """Prefer lead-assigned agent email from property_data, then env default."""
    candidates = [
        property_data.get("assigned_to_email"),
        property_data.get("assignedToEmail"),
        property_data.get("agent_email"),
        property_data.get("recipient_email"),
        record.get("assigned_to_email"),
        config.get_env("ACS_FOLLOWUP_TO", ""),
    ]
    for raw in candidates:
        email = (raw or "").strip()
        if is_valid_email(email):
            return email
    return None


def build_followup_template(
    *,
    lead_name: str = "",
    lead_phone: str = "",
    lead_email: str = "",
    assigned_to_email: str = "",
    lead_score: str = "",
    call_id: str = "",
    duration: int = 0,
    outcome: str = "",
    summary: str = "",
    agent_name: str = "",
) -> str:
    """Clean HTML email: call details table + summary section."""
    duration_label = f"{max(0, int(duration or 0))} seconds"

    def cell(label: str, value: str) -> str:
        return (
            "<tr>"
            f'<td style="padding:10px 12px;border:1px solid #e5e7eb;background:#f9fafb;width:36%">'
            f"<strong>{escape(label)}</strong></td>"
            f'<td style="padding:10px 12px;border:1px solid #e5e7eb">{escape(value or "—")}</td>'
            "</tr>"
        )

    summary_html = escape(summary or "No summary available.").replace("\n", "<br>")
    greeting = f"Hi {escape(agent_name)}," if agent_name else "Hi,"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8" /></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Segoe UI,Arial,sans-serif;color:#1f2937">
  <div style="max-width:640px;margin:24px auto;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb">
    <div style="background:#0f766e;color:#fff;padding:20px 24px">
      <h1 style="margin:0;font-size:20px;font-weight:600">Call follow-up</h1>
      <p style="margin:6px 0 0;font-size:13px;opacity:.9">Swaryx voice agent · post-call summary</p>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 16px;font-size:15px">{greeting}</p>
      <p style="margin:0 0 20px;font-size:14px;line-height:1.5;color:#4b5563">
        A completed outbound call needs your review. Details below.
      </p>

      <h2 style="margin:0 0 10px;font-size:15px;color:#111827">Call details</h2>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:22px">
        {cell("Lead", lead_name)}
        {cell("Phone", lead_phone)}
        {cell("Lead email", lead_email)}
        {cell("Assigned to", assigned_to_email)}
        {cell("Lead score", lead_score)}
        {cell("Call ID", call_id)}
        {cell("Outcome", outcome or "completed")}
        {cell("Duration", duration_label)}
      </table>

      <h2 style="margin:0 0 10px;font-size:15px;color:#111827">Call summary</h2>
      <div style="padding:14px 16px;background:#f0fdfa;border:1px solid #99f6e4;border-radius:8px;font-size:14px;line-height:1.55">
        {summary_html}
      </div>
    </div>
    <div style="padding:14px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280">
      Sent automatically by Swaryx via Azure Communication Services.
    </div>
  </div>
</body>
</html>""".strip()


def send_followup_email(to_email: str, subject: str, html_content: str) -> str:
    """
    Send email via Azure Communication Services EmailClient.

    Retries once on failure. Raises RuntimeError if both attempts fail.
    Returns ACS operation / message id on success.
    """
    connection_string = (config.get_env("ACS_CONNECTION_STRING", "") or "").strip()
    sender = (config.get_env("ACS_SENDER_EMAIL", "") or "").strip()
    if not connection_string:
        raise RuntimeError("ACS_CONNECTION_STRING is not configured")
    if not sender:
        raise RuntimeError("ACS_SENDER_EMAIL is not configured")
    if not is_valid_email(to_email):
        raise RuntimeError(f"Invalid recipient email: {to_email!r}")

    from azure.communication.email import EmailClient

    message = {
        "senderAddress": sender,
        "content": {
            "subject": subject,
            "html": html_content,
            "plainText": re.sub(r"<[^>]+>", " ", html_content),
        },
        "recipients": {
            "to": [{"address": to_email.strip()}],
        },
    }

    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            client = EmailClient.from_connection_string(connection_string)
            poller = client.begin_send(message)
            result = poller.result()
            message_id = ""
            if isinstance(result, dict):
                message_id = str(result.get("id") or result.get("messageId") or "")
            else:
                message_id = str(getattr(result, "id", "") or getattr(result, "message_id", "") or "")
            logger.info(
                "Follow-up email sent to=%s attempt=%s id=%s",
                to_email, attempt, message_id or "ok",
            )
            return message_id or "ok"
        except Exception as exc:
            last_error = exc
            logger.error(
                "Follow-up email failed to=%s attempt=%s error=%s",
                to_email, attempt, exc,
            )
            if attempt == 1:
                time.sleep(1.5)
                continue

    raise RuntimeError(
        f"Follow-up email failed permanently for {to_email}: {last_error}"
    )


async def on_call_ended(call_id: str) -> bool:
    """
    Call-ended hook: send ACS follow-up only for completed (non-missed) calls.
    """
    from db import get_call_by_id, log_error, parse_property_data

    if not email_followup_enabled():
        logger.debug("Email follow-up skipped (ACS not configured) — call_id=%s", call_id)
        return False

    record = await get_call_by_id(call_id)
    if not record:
        logger.warning("Email follow-up skipped — call %s not found", call_id)
        return False

    status = record.get("status")
    outcome = record.get("outcome")
    if not should_send_email_followup(status, outcome):
        logger.info(
            "Email follow-up skipped — status=%s outcome=%s call_id=%s",
            status, outcome, call_id,
        )
        return False

    property_data = parse_property_data(record.get("property_data"))
    to_email = resolve_recipient_email(record, property_data)
    if not to_email:
        msg = f"Email follow-up skipped — recipient missing/invalid (call_id={call_id})"
        logger.warning(msg)
        await log_error("email-followup", msg, f"call_id={call_id}", "warning")
        return False

    lead_name = record.get("lead_name") or property_data.get("lead_name") or ""
    lead_phone = record.get("phone_number") or ""
    lead_email = (
        property_data.get("lead_email")
        or property_data.get("email")
        or ""
    )
    lead_score = (
        property_data.get("lead_score")
        or property_data.get("score")
        or ""
    )
    assigned = (
        property_data.get("assigned_to_email")
        or property_data.get("assignedToEmail")
        or to_email
    )
    summary = (
        record.get("final_transcript")
        or record.get("reason")
        or record.get("live_transcript")
        or ""
    )
    if isinstance(summary, str) and len(summary) > 4000:
        summary = summary[:3999] + "…"

    agent_name = (
        property_data.get("agent_name")
        or config.get_env("EMAIL_FOLLOWUP_AGENT_NAME", "Agent")
        or "Agent"
    )

    html = build_followup_template(
        lead_name=str(lead_name),
        lead_phone=str(lead_phone),
        lead_email=str(lead_email),
        assigned_to_email=str(assigned),
        lead_score=str(lead_score),
        call_id=str(call_id),
        duration=int(record.get("duration_seconds") or 0),
        outcome=str(outcome or "completed"),
        summary=str(summary),
        agent_name=str(agent_name),
    )
    subject = f"Call completed — {lead_name or lead_phone or call_id}"

    try:
        # ACS SDK is sync; run off the event loop.
        message_id = await asyncio.to_thread(send_followup_email, to_email, subject, html)
        await log_error(
            "email-followup",
            f"Follow-up email sent to {to_email}",
            f"call_id={call_id} message_id={message_id}",
            "info",
        )
        return True
    except Exception as exc:
        await log_error(
            "email-followup",
            f"Follow-up email failed for {to_email}",
            f"call_id={call_id} error={exc}",
            "error",
        )
        logger.error("Email follow-up failed call_id=%s: %s", call_id, exc)
        return False


def schedule_email_followup(call_id: str) -> None:
    """Fire-and-forget email after call is finalized."""
    if not email_followup_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("Email follow-up: no event loop — call_id=%s", call_id)
        return

    task = loop.create_task(on_call_ended(call_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
