"""Notify apps when outbound pool DIDs become busy or free.

Two channels:
  1. HTTP webhook — set NUMBER_STATUS_WEBHOOK_URL (POST JSON on each change)
  2. In-process pub/sub — used by GET /api/numbers/stream (SSE) for live popups
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import config

logger = logging.getLogger("number-status")

# Recent events for SSE catch-up (last ~2 minutes).
_RECENT: list[dict[str, Any]] = []
_RECENT_MAX = 200
_SUBSCRIBERS: list[asyncio.Queue] = []
_LOCK = asyncio.Lock()


def _webhook_url() -> str:
    return (
        config.get_env("NUMBER_STATUS_WEBHOOK_URL")
        or config.get_env("PHONE_NUMBER_STATUS_WEBHOOK_URL")
        or ""
    ).strip()


def _webhook_timeout() -> float:
    raw = (config.get_env("NUMBER_STATUS_WEBHOOK_TIMEOUT") or "5").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 5.0


async def publish_number_status(
    *,
    e164: str,
    status: str,
    previous_status: Optional[str] = None,
    call_id: Optional[str] = None,
    room_name: Optional[str] = None,
    reason: str = "",
) -> dict[str, Any]:
    """Broadcast a DID status change (busy/free/disabled) to webhook + SSE subscribers."""
    e164 = (e164 or "").strip()
    status = (status or "").strip().lower()
    if not e164 or not status:
        return {}

    event = {
        "type": "number_status",
        "e164": e164,
        "status": status,
        "previous_status": (previous_status or "").strip().lower() or None,
        "call_id": call_id,
        "room_name": room_name,
        "reason": reason or None,
        "message": (
            f"{e164} is now free"
            if status == "free"
            else f"{e164} is now busy"
            if status == "busy"
            else f"{e164} is now {status}"
        ),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    async with _LOCK:
        _RECENT.append(event)
        if len(_RECENT) > _RECENT_MAX:
            del _RECENT[: len(_RECENT) - _RECENT_MAX]
        dead: list[asyncio.Queue] = []
        for q in _SUBSCRIBERS:
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _SUBSCRIBERS.remove(q)
            except ValueError:
                pass

    asyncio.create_task(_post_webhook(event))
    return event


async def _post_webhook(event: dict[str, Any]) -> None:
    url = _webhook_url()
    if not url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_webhook_timeout()) as client:
            r = await client.post(url, json=event)
            if r.status_code >= 400:
                logger.warning(
                    "number-status webhook HTTP %s — %s",
                    r.status_code,
                    (r.text or "")[:200],
                )
            else:
                logger.info("number-status webhook ok e164=%s status=%s", event.get("e164"), event.get("status"))
    except Exception as exc:
        logger.warning("number-status webhook failed: %s", exc)


async def subscribe(maxsize: int = 64) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    async with _LOCK:
        _SUBSCRIBERS.append(q)
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _LOCK:
        try:
            _SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    return list(_RECENT[-max(1, min(limit, _RECENT_MAX)):])


def status_map_from_pool(numbers: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in numbers or []:
        e164 = (row.get("e164") or "").strip()
        if e164:
            out[e164] = (row.get("status") or "").strip().lower() or "unknown"
    return out


def diff_status_maps(
    before: dict[str, str],
    after: dict[str, str],
) -> list[dict[str, Any]]:
    """Build number_status events for status transitions (busy↔free etc.)."""
    events: list[dict[str, Any]] = []
    keys = set(before) | set(after)
    for e164 in sorted(keys):
        prev = before.get(e164)
        cur = after.get(e164)
        if not cur or prev == cur:
            continue
        events.append({
            "type": "number_status",
            "e164": e164,
            "status": cur,
            "previous_status": prev,
            "call_id": None,
            "room_name": None,
            "reason": "poll_diff",
            "message": (
                f"{e164} is now free"
                if cur == "free"
                else f"{e164} is now busy"
                if cur == "busy"
                else f"{e164} is now {cur}"
            ),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    return events
