"""Vobiz Account CDR client — realtime per-call cost from console billing."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

import config

logger = logging.getLogger("vobiz-api")

_BASE = "https://api.vobiz.ai/api/v1"
_CDR_CACHE: dict[str, Any] = {"at": 0.0, "cdrs": [], "meta": {}}
_CDR_CACHE_TTL = 45.0
_CACHE_LOCK = asyncio.Lock()
_IST = ZoneInfo("Asia/Kolkata")


def vobiz_api_configured() -> bool:
    return bool(_auth_id() and _auth_token())


def _auth_id() -> str:
    return (config.get_env("VOBIZ_AUTH_ID", "") or "").strip()


def _auth_token() -> str:
    return (config.get_env("VOBIZ_AUTH_TOKEN", "") or "").strip()


def _headers(auth_id: Optional[str] = None, auth_token: Optional[str] = None) -> dict[str, str]:
    aid = (auth_id or _auth_id()).strip()
    atoken = (auth_token or _auth_token()).strip()
    return {
        "X-Auth-ID": aid,
        "X-Auth-Token": atoken,
        "Accept": "application/json",
    }


def normalize_phone(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    elif len(digits) > 10:
        digits = digits[-10:]
    return digits


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse timestamps. Naive values are treated as Asia/Kolkata (our call_logs)."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_IST).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _cdr_numeric_cost(cdr: dict) -> float:
    raw = cdr.get("total_cost")
    if raw is None:
        raw = cdr.get("cost")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _cdr_match_score(call: dict, cdr: dict, call_ts: Optional[datetime]) -> Optional[float]:
    """Lower score is better. None = reject this CDR for this call."""
    call_dur = max(0, int(call.get("duration_seconds") or 0))
    billsec = max(0, int(cdr.get("billsec") or 0))
    cdr_dur = max(0, int(cdr.get("duration") or 0))
    cost = _cdr_numeric_cost(cdr)
    talk_ref = billsec if billsec > 0 else cdr_dur

    # Connected dashboard call must not latch onto an empty / zero-cost CDR
    # (that produced ₹0.00 Vobiz while console showed ₹0.90 for 81s).
    if call_dur >= 15 and talk_ref == 0 and cost <= 0:
        return None

    cdr_ts = (
        _parse_ts(cdr.get("end_time"))
        or _parse_ts(cdr.get("answer_time"))
        or _parse_ts(cdr.get("start_time"))
        or _parse_ts(cdr.get("created_at"))
    )
    if call_ts and cdr_ts:
        time_delta = abs((call_ts - cdr_ts).total_seconds())
        if time_delta > 12 * 3600:
            return None
    elif call_ts or cdr_ts:
        time_delta = 6 * 3600  # weak match if only one side has a timestamp
    else:
        time_delta = 12 * 3600

    dur_delta = abs(call_dur - talk_ref) if (call_dur or talk_ref) else 0
    # Prefer billed/connected legs over zero-cost stubs when durations are close.
    empty_penalty = 5000.0 if (call_dur >= 15 and cost <= 0) else 0.0
    return time_delta + (dur_delta * 3.0) + empty_penalty


def match_cdr_for_call(call: dict, cdrs: list[dict]) -> Optional[dict]:
    """Best-effort match: same phone, closest time + duration to the call."""
    phone = normalize_phone(call.get("phone_number"))
    if not phone or not cdrs:
        return None
    call_ts = _parse_ts(call.get("timestamp"))
    best: Optional[dict] = None
    best_score: Optional[float] = None
    for cdr in cdrs:
        dest = normalize_phone(cdr.get("destination_number") or cdr.get("to_number"))
        if dest != phone:
            continue
        score = _cdr_match_score(call, cdr, call_ts)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best = cdr
            best_score = score
    return best


def apply_vobiz_cdr(call: dict, cdr: dict) -> dict:
    """Overwrite billing fields from a live Vobiz CDR row."""
    billsec = int(cdr.get("billsec") or 0)
    cost = round(_cdr_numeric_cost(cdr), 2)
    # Display minutes as ceil(billsec/60) to match Vobiz pulse; cost is authoritative from CDR.
    # Non-connected fees often have billsec=0 but a small total_cost (e.g. ₹0.02).
    if billsec > 0:
        mins = float(math.ceil(billsec / 60.0))
    elif cost > 0:
        mins = 0.0
    else:
        mins = 0.0
    rate = round(cost / mins, 4) if mins > 0 else None
    cur = str(cdr.get("currency") or "INR").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", cur):
        cur = "INR"
    call["billable_minutes"] = mins
    call["estimated_cost"] = cost
    call["currency"] = cur
    call["rate_source"] = "vobiz_cdr"
    call["vobiz_billsec"] = billsec
    call["vobiz_duration"] = int(cdr.get("duration") or 0)
    call["vobiz_cdr_uuid"] = cdr.get("uuid")
    if rate is not None:
        call["rate_per_minute"] = rate
    return call


async def _get_json(
    url: str,
    params: Optional[dict] = None,
    *,
    auth_id: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> dict:
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.get(
            url,
            headers=_headers(auth_id=auth_id, auth_token=auth_token),
            params=params or {},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}


async def fetch_recent_cdrs(
    limit: int = 50,
    *,
    auth_id: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> list[dict]:
    auth = (auth_id or _auth_id()).strip()
    tok = (auth_token or _auth_token()).strip()
    if not auth or not tok:
        return []
    url = f"{_BASE}/Account/{quote(auth)}/cdr/recent"
    data = await _get_json(url, {"limit": max(1, min(int(limit), 100))}, auth_id=auth, auth_token=tok)
    rows = data.get("data") or []
    return rows if isinstance(rows, list) else []


async def fetch_cdrs(
    *,
    start_date: str,
    end_date: str,
    to_number: Optional[str] = None,
    per_page: int = 100,
    max_pages: int = 5,
    auth_id: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> list[dict]:
    auth = (auth_id or _auth_id()).strip()
    tok = (auth_token or _auth_token()).strip()
    if not auth or not tok:
        return []
    url = f"{_BASE}/Account/{quote(auth)}/cdr"
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        params: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "per_page": max(1, min(int(per_page), 100)),
        }
        # Do not force call_direction — SIP trunk legs are not always tagged "outbound".
        if to_number:
            params["to_number"] = to_number
        data = await _get_json(url, params)
        rows = data.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        pagination = data.get("pagination") or {}
        if not pagination.get("has_next"):
            break
        page += 1
    return out


async def get_cached_cdrs_for_calls(calls: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Fetch/cache CDRs covering the call list. Returns (cdrs, meta)."""
    meta: dict[str, Any] = {"cdr_count": 0, "error": None}
    if not calls or not vobiz_api_configured():
        return [], meta

    async with _CACHE_LOCK:
        now = time.monotonic()
        if _CDR_CACHE["cdrs"] and (now - float(_CDR_CACHE["at"])) < _CDR_CACHE_TTL:
            meta = dict(_CDR_CACHE.get("meta") or meta)
            return list(_CDR_CACHE["cdrs"]), meta

        timestamps = [_parse_ts(c.get("timestamp")) for c in calls]
        timestamps = [t for t in timestamps if t]
        try:
            recent = await fetch_recent_cdrs(100)
            dated: list[dict] = []
            if timestamps:
                start = min(timestamps) - timedelta(hours=12)
                end = max(timestamps) + timedelta(hours=12)
                start_s = start.astimezone(timezone.utc).date().isoformat()
                end_s = end.astimezone(timezone.utc).date().isoformat()
                dated = await fetch_cdrs(
                    start_date=start_s, end_date=end_s, per_page=100, max_pages=5
                )
            # Merge unique by uuid
            by_uuid: dict[str, dict] = {}
            for row in dated + recent:
                uid = str(row.get("uuid") or row.get("id") or "")
                if uid:
                    by_uuid[uid] = row
                else:
                    by_uuid[f"anon-{len(by_uuid)}"] = row
            cdrs = list(by_uuid.values())
            meta = {"cdr_count": len(cdrs), "error": None}
            logger.info("Vobiz CDR fetch ok — %d rows (recent+dated)", len(cdrs))
        except Exception as exc:
            logger.warning("Vobiz CDR fetch failed: %s", exc)
            meta = {"cdr_count": 0, "error": str(exc)[:200]}
            if _CDR_CACHE["cdrs"]:
                return list(_CDR_CACHE["cdrs"]), dict(_CDR_CACHE.get("meta") or meta)
            return [], meta

        _CDR_CACHE["at"] = now
        _CDR_CACHE["cdrs"] = cdrs
        _CDR_CACHE["meta"] = meta
        return list(cdrs), meta


async def fetch_account_rate_hint() -> Optional[float]:
    """Optional: read pricing_tier.rate_per_minute from /auth/me if present."""
    if not vobiz_api_configured():
        return None
    try:
        data = await _get_json(f"{_BASE}/auth/me")
        acct = data.get("data") or data.get("account") or data
        if not isinstance(acct, dict):
            return None
        tier = acct.get("pricing_tier") or {}
        for key in ("rate_per_minute", "rate_per_min", "per_minute_rate", "call_rate"):
            if key in tier and tier[key] is not None:
                return float(tier[key])
            if key in acct and acct[key] is not None:
                return float(acct[key])
    except Exception as exc:
        logger.debug("Vobiz account rate hint unavailable: %s", exc)
    return None


async def list_account_numbers(*, per_page: int = 100, max_pages: int = 20) -> list[dict]:
    """
    Paginate GET /Account/{auth_id}/numbers — owned DIDs for the phone pool sync.
    Returns the raw PhoneNumber objects from `items`.
    """
    auth = _auth_id()
    if not auth or not _auth_token():
        return []
    url = f"{_BASE}/Account/{quote(auth)}/numbers"
    out: list[dict] = []
    page = 1
    per = max(1, min(int(per_page), 100))
    while page <= max_pages:
        data = await _get_json(url, {"page": page, "per_page": per})
        items = data.get("items") or data.get("data") or []
        if not isinstance(items, list) or not items:
            break
        out.extend(row for row in items if isinstance(row, dict))
        total = data.get("total")
        if isinstance(total, int) and len(out) >= total:
            break
        if len(items) < per:
            break
        page += 1
    return out


async def get_number_health(e164: str, *, days: int = 30, granularity: str = "daily") -> dict:
    """GET /account/{auth_id}/numbers/{e164}/health — optional dashboard enrichment."""
    auth = _auth_id()
    if not auth or not _auth_token():
        return {}
    encoded = quote(e164, safe="")
    url = f"{_BASE}/account/{quote(auth)}/numbers/{encoded}/health"
    return await _get_json(url, {"days": max(1, int(days)), "granularity": granularity})
