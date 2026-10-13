"""SIP / LiveKit hangup verification — unlock CRM only when the line is confirmed clear."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("sip-hangup")


def _confirm_timeout_sec() -> float:
    return max(0.5, float(os.getenv("SIP_ABSENT_CONFIRM_TIMEOUT_SEC", "8") or "8"))


def _confirm_poll_sec() -> float:
    return max(0.15, float(os.getenv("SIP_ABSENT_CONFIRM_POLL_SEC", "0.4") or "0.4"))


def _is_room_missing_error(exc: BaseException) -> bool:
    err = str(exc).lower()
    return (
        "not found" in err
        or "404" in err
        or "does not exist" in err
        or "unknown room" in err
        or "room does not exist" in err
    )


def _participant_identities(response: Any) -> list[str]:
    participants = getattr(response, "participants", None) or []
    out: list[str] = []
    for p in participants:
        ident = getattr(p, "identity", None)
        if ident:
            out.append(str(ident))
    return out


async def sip_line_confirmed_clear(
    lk_api: Any,
    *,
    room_name: str,
    sip_identity: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    poll_sec: Optional[float] = None,
) -> bool:
    """Return True only when the room is gone or the SIP identity is absent.

    Fail-closed: API failures / unknown state return False after the timeout.
    """
    if not lk_api or not room_name:
        return False
    from livekit import api

    deadline = time.monotonic() + (timeout_sec if timeout_sec is not None else _confirm_timeout_sec())
    interval = poll_sec if poll_sec is not None else _confirm_poll_sec()
    saw_present = False

    while time.monotonic() < deadline:
        try:
            resp = await lk_api.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            identities = _participant_identities(resp)
            if sip_identity:
                still = sip_identity in identities
            else:
                still = any(ident.startswith("sip_") for ident in identities)
            if not still:
                return True
            saw_present = True
        except Exception as exc:
            if _is_room_missing_error(exc):
                return True
            logger.warning(
                "SIP absent check failed for room=%s identity=%s: %s",
                room_name,
                sip_identity,
                exc,
            )
        await asyncio.sleep(interval)

    if saw_present:
        logger.warning(
            "SIP still present after confirm timeout room=%s identity=%s",
            room_name,
            sip_identity,
        )
    else:
        logger.warning(
            "SIP absent check unknown after timeout room=%s identity=%s",
            room_name,
            sip_identity,
        )
    return False


async def force_delete_room_and_confirm(
    lk_api: Any,
    *,
    room_name: str,
    sip_identity: Optional[str] = None,
    timeout_sec: Optional[float] = None,
) -> bool:
    """Best-effort DeleteRoom then confirm SIP/room is gone."""
    if not lk_api or not room_name:
        return False
    from livekit import api

    try:
        await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception as exc:
        if not _is_room_missing_error(exc):
            logger.warning("force delete_room failed room=%s: %s", room_name, exc)
            # Still try confirm — room may already be gone.
    return await sip_line_confirmed_clear(
        lk_api,
        room_name=room_name,
        sip_identity=sip_identity,
        timeout_sec=timeout_sec,
    )


async def livekit_api_from_env() -> Optional[Any]:
    """Build a short-lived LiveKitAPI from process env (reconciler / safety nets)."""
    url = (os.getenv("LIVEKIT_URL") or "").strip()
    key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not (url and key and secret):
        return None
    try:
        from livekit import api as lk_api

        return lk_api.LiveKitAPI(url, key, secret)
    except Exception as exc:
        logger.warning("Could not create LiveKitAPI from env: %s", exc)
        return None
