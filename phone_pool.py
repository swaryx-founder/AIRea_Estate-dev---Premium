"""Outbound Vobiz number pool — sync, trunk refresh, locked scheduled jobs.

Dynamic by design: every sync pulls *all* owned Vobiz DIDs, upserts them into
``phone_numbers``, and registers the full active set on the LiveKit outbound
trunk. No hardcoded caller IDs — new numbers appear after sync automatically.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from typing import Any, Optional

import config
from db import (
    list_active_pool_e164,
    reclaim_stale_busy_numbers,
    release_job_lock,
    try_acquire_job_lock,
    upsert_phone_numbers_from_vobiz,
)
from phone_utils import normalize_e164_phone
from vobiz_api import list_account_numbers, vobiz_api_configured

logger = logging.getLogger("phone-pool")

SYNC_JOB = "phone_number_sync"
RECLAIM_JOB = "phone_number_reclaim"


def sync_interval_minutes() -> int:
    return max(1, int(os.getenv("PHONE_NUMBER_SYNC_INTERVAL_MINUTES", "15")))


def job_lock_ttl_sec() -> int:
    # Slightly longer than a slow Vobiz+LiveKit sync so the other replica stays out.
    return max(60, int(os.getenv("PHONE_NUMBER_JOB_LOCK_TTL_SEC", "180")))


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _normalize_vobiz_number_item(item: dict) -> Optional[dict]:
    """Map raw Vobiz PhoneNumber JSON → internal shape with E.164 ``e164``."""
    if not isinstance(item, dict):
        return None
    raw = (
        item.get("e164")
        or item.get("number")
        or item.get("phone_number")
        or item.get("phone")
        or item.get("did")
        or ""
    )
    e164 = normalize_e164_phone(str(raw).strip()) or str(raw).strip()
    if not e164:
        return None
    out = dict(item)
    out["e164"] = e164
    return out


async def refresh_outbound_trunk_numbers(numbers: list[str]) -> dict[str, Any]:
    """
    Ensure LiveKit outbound trunk ``numbers`` matches the full dynamic pool.
    Required so any newly synced DID can be used as per-call ``sip_number`` (FROM).
    """
    trunk_id = (os.getenv("OUTBOUND_TRUNK_ID") or "").strip()
    if not trunk_id:
        return {"updated": False, "reason": "OUTBOUND_TRUNK_ID not set"}

    merged: list[str] = []
    for n in list(numbers):
        n = (n or "").strip()
        if n and n not in merged:
            merged.append(n)
    if not merged:
        return {"updated": False, "reason": "no numbers to register"}

    try:
        from livekit import api as lk_api
        import aiohttp
        import ssl
        import certifi

        url = config.get_env("LIVEKIT_URL", "")
        key = config.get_env("LIVEKIT_API_KEY", "")
        secret = config.get_env("LIVEKIT_API_SECRET", "")
        if not all([url, key, secret]):
            return {"updated": False, "reason": "LiveKit credentials missing"}

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
        lk = None
        try:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            # LiveKit SDK 1.x: update_outbound_trunk_fields(trunk_id, numbers=[...])
            await lk.sip.update_outbound_trunk_fields(trunk_id, numbers=merged)
        finally:
            if lk is not None:
                try:
                    await lk.aclose()
                except Exception:
                    pass
            await session.close()
        logger.info("LiveKit trunk %s numbers refreshed (dynamic pool count=%d)", trunk_id, len(merged))
        return {"updated": True, "count": len(merged), "numbers": merged}
    except Exception as exc:
        logger.warning("LiveKit trunk number refresh failed: %s", exc)
        return {"updated": False, "reason": str(exc)[:200]}


async def sync_phone_numbers(*, refresh_trunk: bool = True) -> dict[str, Any]:
    """Pull *all* owned numbers from Vobiz and upsert into phone_numbers (dynamic)."""
    if not vobiz_api_configured():
        return {"ok": False, "error": "VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN not configured"}

    raw_items = await list_account_numbers()
    items = []
    for row in raw_items:
        normalized = _normalize_vobiz_number_item(row)
        if normalized:
            items.append(normalized)

    result = await upsert_phone_numbers_from_vobiz(items)
    trunk: dict[str, Any] = {"updated": False, "skipped": True}
    if refresh_trunk:
        active = await list_active_pool_e164()
        trunk = await refresh_outbound_trunk_numbers(active)
    return {
        "ok": True,
        "dynamic": True,
        "vobiz_count": len(items),
        **result,
        "trunk": trunk,
        "message": (
            "Pool refreshed from Vobiz. New numbers are free to lease; "
            "removed/disabled numbers are marked disabled. "
            "GET /api/numbers always reflects the current pool."
        ),
    }


async def run_locked_sync() -> Optional[dict[str, Any]]:
    owner = _owner_id()
    if not await try_acquire_job_lock(SYNC_JOB, owner, ttl_sec=job_lock_ttl_sec()):
        logger.info("phone_number_sync skipped — another replica holds the lock")
        return None
    try:
        result = await sync_phone_numbers(refresh_trunk=True)
        logger.info("phone_number_sync done: %s", result)
        return result
    except Exception as exc:
        logger.exception("phone_number_sync failed: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        await release_job_lock(SYNC_JOB, owner)


async def run_locked_reclaim() -> Optional[dict[str, Any]]:
    owner = _owner_id()
    if not await try_acquire_job_lock(RECLAIM_JOB, owner, ttl_sec=60):
        return None
    try:
        freed = await reclaim_stale_busy_numbers()
        if freed:
            logger.info("phone_number_reclaim freed %d stuck busy number(s)", freed)
        return {"freed": freed}
    except Exception as exc:
        logger.warning("phone_number_reclaim failed: %s", exc)
        return {"freed": 0, "error": str(exc)[:200]}
    finally:
        await release_job_lock(RECLAIM_JOB, owner)
