"""
Notify the main Calling Service (Node / Prisma) when a call completes.

See webhook.md for the contract: POST /api/calls/webhook/call-completed
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger("call-webhook")

# Windows consoles default to cp1252 and crash on Hindi (Devanagari) transcript text.
# Force UTF-8 so webhook prints never raise UnicodeEncodeError mid-send.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _safe_print(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _verbose() -> bool:
    flag = config.get_env("CALL_WEBHOOK_VERBOSE", "").lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return not config.is_env_only_mode()


def _step(step: str, detail: str = "", **fields: Any) -> None:
    """Line-by-line webhook trace — visible in local terminal."""
    msg = f"[WEBHOOK] {step}"
    if detail:
        msg += f" - {detail}"
    _safe_print(msg)
    for key, value in fields.items():
        _safe_print(f"[WEBHOOK]   {key}: {value}")


_LOCAL_WEBHOOK_URL = "http://localhost:5000/api/calls/webhook/call-completed"


def webhook_url() -> str:
    """Full webhook URL from env, or base URL + default path.

    An explicit completion URL always wins. Local development falls back to
    localhost:5000 only when no URL or service base is configured.
    """
    explicit = config.get_env("CALL_COMPLETED_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = config.get_env("CALLING_SERVICE_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/api/calls/webhook/call-completed"
    return "" if config.is_env_only_mode() else _LOCAL_WEBHOOK_URL


def webhook_enabled() -> bool:
    if config.get_env("CALL_COMPLETED_WEBHOOK_ENABLED", "").lower() in ("0", "false", "no"):
        return False
    return bool(webhook_url())


def webhook_status() -> dict[str, Any]:
    """Diagnostics for /api/health — no secrets."""
    url = webhook_url()
    enabled = webhook_enabled()
    host = ""
    localhost_target = False
    if url:
        try:
            parsed = urlparse(url)
            host = parsed.netloc or ""
            localhost_target = host.split(":")[0].lower() in (
                "localhost", "127.0.0.1", "0.0.0.0",
            )
        except Exception:
            host = "invalid-url"
    return {
        "enabled": enabled,
        "configured": bool(url),
        "host": host,
        "localhost_target": localhost_target,
        "azure_warning": (
            "localhost will not work from Azure Container Apps — use public Node HTTPS URL"
            if localhost_target and config.is_env_only_mode()
            else None
        ),
    }


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _transcript_lines(transcript_json: Optional[str], call_started: Optional[str]) -> list[dict]:
    if not transcript_json:
        return []
    try:
        lines = json.loads(transcript_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(lines, list):
        return []

    start = _parse_dt(call_started or "")
    out: list[dict] = []
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        role = (line.get("role") or "").lower()
        speaker = "customer" if role in ("user", "customer", "lead") else "agent"
        text = (line.get("text") or "").strip()
        ts_raw = line.get("ts") or line.get("timestamp")
        offset = i
        if ts_raw is not None and start:
            if isinstance(ts_raw, (int, float)):
                offset = int(ts_raw)
            else:
                ts_dt = _parse_dt(str(ts_raw))
                if ts_dt:
                    try:
                        offset = max(0, int((ts_dt - start).total_seconds()))
                    except TypeError:
                        offset = i
        out.append({"speaker": speaker, "text": text, "timestamp": offset})
    return out


def _http_status(record: dict) -> str:
    outcome = (record.get("outcome") or "").lower()
    status = (record.get("status") or "").lower()
    if outcome == "failed" or status == "failed":
        return "FAILED"
    return "COMPLETED"


def _summary(record: dict) -> Optional[str]:
    notes = (record.get("notes") or "").strip()
    if notes:
        return notes
    final = (record.get("final_transcript") or "").strip()
    if final:
        return final[:2000]
    outcome = record.get("outcome") or ""
    reason = (record.get("reason") or "").strip()
    if outcome and reason:
        return f"{outcome}: {reason}"
    return outcome or reason or None


def _sentiment_score(record: dict) -> int:
    """Outcome-mapped sentiment for Node (1 / 0 / -1). Always included in payload."""
    from db import normalize_sentiment
    return normalize_sentiment(record.get("sentiment"), record.get("outcome"))


def build_call_completed_payload(
    record: dict,
    *,
    room_name: Optional[str] = None,
    ended_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Map Supabase call_logs row → Node webhook body."""
    call_id = record.get("id")
    started = record.get("timestamp") or ""
    end = ended_at or datetime.now(timezone.utc)
    started_dt = _parse_dt(started)

    payload: dict[str, Any] = {
        "id": str(call_id),
        "status": _http_status(record),
        "outcome": record.get("outcome"),
        "outcome_reason": record.get("reason"),
        "duration": int(record.get("duration_seconds") or 0),
        "recording_url": record.get("recording_url"),
        "summary": _summary(record),
        "sentiment": _sentiment_score(record),
        "transcript": _transcript_lines(record.get("transcript_json"), started),
        "started_at": started_dt.isoformat() if started_dt else started or None,
        "ended_at": end.isoformat(),
        "room_name": room_name or None,
        "organization_id": record.get("organization_id"),
        "lead_id": record.get("lead_id"),
        "phone_number": record.get("phone_number"),
        "lead_name": record.get("lead_name"),
        "project_id": record.get("project_id"),
    }
    from callback_schedule import callback_fields_from_record
    from db import compute_ai_lead_score, enrich_property_data_for_webhook
    score_info = compute_ai_lead_score(record)
    payload["ai_score"] = score_info["ai_score"]
    payload["lead_score"] = score_info["lead_score"]
    payload["ai_score_label"] = score_info["ai_score_label"]
    cb = callback_fields_from_record(record)
    if cb.get("scheduled_at"):
        payload["scheduled_at"] = cb["scheduled_at"]
    if cb.get("callback_urgency"):
        payload["callback_urgency"] = cb["callback_urgency"]
    property_data = {
        **enrich_property_data_for_webhook(record.get("property_data")),
        "ai_score": score_info["ai_score"],
        "lead_score": score_info["lead_score"],
    }
    if cb.get("scheduled_at"):
        property_data["scheduled_at"] = cb["scheduled_at"]
    if cb.get("callback_urgency"):
        property_data["callback_urgency"] = cb["callback_urgency"]
    payload["property_data"] = property_data
    payload["client_requirements"] = dict(property_data)
    for field in (
        "preferred_location", "city", "bhk", "property_type",
        "parking_spaces", "hometown", "requirements", "customer_intent",
    ):
        value = property_data.get(field)
        if value not in (None, ""):
            payload[field] = value
    budget_text = (property_data.get("budget") or "").strip()
    if budget_text:
        payload["budget"] = budget_text
    if property_data.get("min_budget") is not None:
        payload["min_budget"] = int(property_data["min_budget"])
    if property_data.get("max_budget") is not None:
        payload["max_budget"] = int(property_data["max_budget"])
    if record.get("project_id"):
        payload["project_id"] = record.get("project_id")
    return {k: v for k, v in payload.items() if v is not None}


def schedule_call_completed_webhook(
    call_id: str,
    *,
    room_name: Optional[str] = None,
) -> None:
    """Fire-and-forget webhook after call data is persisted."""
    if _verbose():
        _step(
            "1/8 TRIGGER",
            "Call ended — scheduling webhook",
            call_id=call_id,
            room_name=room_name or "(none)",
        )
    if not webhook_enabled():
        if _verbose():
            _step("SKIP", "CALL_COMPLETED_WEBHOOK_URL not set — webhook disabled", call_id=call_id)
        logger.warning(
            "call-completed webhook skipped (CALL_COMPLETED_WEBHOOK_URL not set) — call_id=%s",
            call_id,
        )
        return
    if _verbose():
        _step("2/8 CONFIG", "Webhook URL loaded", url=webhook_url())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if _verbose():
            _step("ERROR", "No asyncio event loop — cannot send webhook", call_id=call_id)
        logger.error("call-completed webhook: no event loop — call_id=%s", call_id)
        return
    if _verbose():
        _step("3/8 QUEUED", "Background task created — will POST to Node", call_id=call_id)
    loop.create_task(notify_call_completed(call_id, room_name=room_name))


async def notify_call_completed(
    call_id: str,
    *,
    room_name: Optional[str] = None,
) -> bool:
    if _verbose():
        _step("4/8 START", "Loading call from Supabase", call_id=call_id)

    url = webhook_url()
    if not url:
        if _verbose():
            _step("FAIL", "No webhook URL configured")
        return False

    from db import get_call_by_id

    record = await get_call_by_id(call_id)
    if not record:
        if _verbose():
            _step("FAIL", "Call not found in Supabase call_logs", call_id=call_id)
        logger.warning("call-completed webhook: call %s not found in DB", call_id)
        return False

    db_status = (record.get("status") or "").lower()
    if _verbose():
        _step(
            "5/8 DB",
            "Call record loaded",
            status=db_status,
            outcome=record.get("outcome"),
            duration_seconds=record.get("duration_seconds"),
            phone=record.get("phone_number"),
        )

    if db_status in ("in_progress", "ringing"):
        if _verbose():
            _step("SKIP", f"Call still {db_status} — webhook not sent yet", call_id=call_id)
        logger.debug("call-completed webhook: skip %s call %s", db_status, call_id)
        return False

    payload = build_call_completed_payload(record, room_name=room_name)
    timeout = float(config.get_env("CALL_COMPLETED_WEBHOOK_TIMEOUT", "10") or "10")

    import httpx

    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = config.get_env("CALL_COMPLETED_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["Authorization"] = "Bearer ***"

    if _verbose():
        _step(
            "6/8 PAYLOAD",
            "Built JSON for Node (thirdPartyCallId = dispatch id)",
            id=payload.get("id"),
            status=payload.get("status"),
            outcome=payload.get("outcome"),
            duration=payload.get("duration"),
            transcript_lines=len(payload.get("transcript") or []),
            has_recording=bool(payload.get("recording_url")),
        )
        _safe_print("[WEBHOOK]   full_payload:")
        _safe_print(json.dumps(payload, indent=2, ensure_ascii=True))

    logger.info(
        "call-completed webhook POST id=%s status=%s -> %s",
        payload.get("id"),
        payload.get("status"),
        url,
    )

    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(3):
            attempt_no = attempt + 1
            if _verbose():
                _step("7/8 POST", f"Sending to Node (attempt {attempt_no}/3)", url=url)
            try:
                resp = await client.post(url, json=payload, headers=headers)
                body_preview = (resp.text or "")[:300]
                if resp.status_code >= 400:
                    if _verbose():
                        _step(
                            "8/8 FAIL",
                            f"Node returned HTTP {resp.status_code}",
                            response=body_preview,
                            hint=(
                                "404 = Node has no Call with thirdPartyCallId = dispatch id"
                                if resp.status_code == 404
                                else f"Check Node server is reachable at {url}"
                            ),
                        )
                    logger.error(
                        "call-completed webhook failed: HTTP %s — %s (id=%s)",
                        resp.status_code,
                        body_preview,
                        call_id,
                    )
                    return False
                if _verbose():
                    _step(
                        "8/8 OK",
                        "Node accepted webhook — call synced",
                        http_status=resp.status_code,
                        response=body_preview,
                    )
                logger.info("call-completed webhook OK for call %s", call_id)
                return True
            except Exception as exc:
                last_exc = exc
                if _verbose():
                    _step(
                        "RETRY",
                        f"Connection error on attempt {attempt_no}/3",
                        error=str(exc),
                    )
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if _verbose():
                    _step(
                        "8/8 FAIL",
                        "Could not reach Node webhook",
                        error=str(exc),
                        hint=f"Node not reachable at {url} — check it is running and port open",
                    )
                logger.error("call-completed webhook error for %s: %s", call_id, exc)
                return False
    if last_exc:
        logger.error("call-completed webhook error for %s: %s", call_id, last_exc)
    return False
