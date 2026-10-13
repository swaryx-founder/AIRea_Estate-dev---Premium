"""Call billing — prefer live Vobiz CDR cost; fall back to local estimate.

Vobiz India SIP typically bills whole minutes (ceil) × rate_per_minute.
Example: 209s → 4 min × ₹0.45 = ₹1.80 (matches Vobiz console).
"""

from __future__ import annotations

import math
from typing import Any, Optional

import config
from vobiz_api import (
    apply_vobiz_cdr,
    fetch_account_rate_hint,
    get_cached_cdrs_for_calls,
    match_cdr_for_call,
    vobiz_api_configured,
)

# Fallback when CDR not matched — must match Vobiz portal per-minute rate.
_DEFAULT_RATE = 0.45
_DEFAULT_CURRENCY = "INR"
# Ignore corrupt duration values in summaries (e.g. bad DB rows).
_MAX_CALL_SECONDS = 6 * 3600


def vobiz_rate_per_minute() -> float:
    raw = (config.get_env("VOBIZ_RATE_PER_MINUTE", "") or "").strip()
    if not raw:
        return _DEFAULT_RATE
    try:
        rate = float(raw)
    except ValueError:
        return _DEFAULT_RATE
    return rate if rate >= 0 else _DEFAULT_RATE


def vobiz_billing_currency() -> str:
    cur = (config.get_env("VOBIZ_BILLING_CURRENCY", _DEFAULT_CURRENCY) or _DEFAULT_CURRENCY).strip().upper()
    # Guard unresolved ADO macros like $(VOBIZ_BILLING_CURRENCY) / ${...}
    if len(cur) != 3 or not cur.isalpha():
        return _DEFAULT_CURRENCY
    return cur


def billable_minutes(duration_seconds: Optional[int]) -> float:
    """Vobiz-style whole minutes (ceil). 1–60s → 1.0, 61–120s → 2.0."""
    sec = max(0, int(duration_seconds or 0))
    if sec <= 0:
        return 0.0
    if sec > _MAX_CALL_SECONDS:
        return 0.0
    return float(math.ceil(sec / 60.0))


def estimated_cost(duration_seconds: Optional[int], *, rate: Optional[float] = None) -> float:
    r = vobiz_rate_per_minute() if rate is None else float(rate)
    return round(billable_minutes(duration_seconds) * r, 2)


def rate_payload() -> dict[str, Any]:
    return {
        "rate_per_minute": vobiz_rate_per_minute(),
        "currency": vobiz_billing_currency(),
        "rate_source": "env",
        "billing_increment": "per_minute_ceil",
        "provider": "vobiz",
        "vobiz_api_configured": vobiz_api_configured(),
    }


def enrich_call_billing(call: dict, *, rate: Optional[float] = None) -> dict:
    """Local estimate only (no network). Used as fallback."""
    r = vobiz_rate_per_minute() if rate is None else float(rate)
    dur = call.get("duration_seconds")
    mins = billable_minutes(dur)
    call["billable_minutes"] = mins
    call["estimated_cost"] = round(mins * r, 2)
    call["rate_per_minute"] = r
    call["currency"] = vobiz_billing_currency()
    call["rate_source"] = "estimate"
    return call


async def enrich_calls_billing(calls: list[dict]) -> list[dict]:
    """Attach Vobiz CDR total_cost per call when Auth ID/Token are set."""
    if not calls:
        return calls

    cdrs: list[dict] = []
    fetch_meta: dict[str, Any] = {}
    if vobiz_api_configured():
        cdrs, fetch_meta = await get_cached_cdrs_for_calls(calls)

    used_uuids: set[str] = set()
    for call in calls:
        cdr = match_cdr_for_call(call, cdrs) if cdrs else None
        uuid = (cdr or {}).get("uuid")
        if cdr and uuid and uuid in used_uuids:
            cdr = None
        if cdr:
            if uuid:
                used_uuids.add(str(uuid))
            apply_vobiz_cdr(call, cdr)
        else:
            enrich_call_billing(call)
        if fetch_meta:
            call["_vobiz_fetch"] = fetch_meta
    return calls


async def enrich_call_billing_async(call: dict) -> dict:
    await enrich_calls_billing([call])
    return call


def summarize_usage(
    rows: list[dict],
    *,
    organization_id: Optional[str] = None,
    rate: Optional[float] = None,
) -> dict[str, Any]:
    """Sum live Vobiz CDR costs when matched; env rate only for unmatched leftovers."""
    fallback_rate = vobiz_rate_per_minute() if rate is None else float(rate)
    fetch_meta: dict[str, Any] = {}
    cdr_seconds = 0
    cdr_billable = 0.0
    cdr_cost = 0.0
    vobiz_hits = 0
    skipped = 0
    estimate_rows: list[tuple[int, float]] = []  # (seconds, mins)

    for row in rows:
        if row.get("_vobiz_fetch") and not fetch_meta:
            fetch_meta = dict(row.get("_vobiz_fetch") or {})
        if row.get("rate_source") == "vobiz_cdr":
            vobiz_hits += 1
            billsec = int(row.get("vobiz_billsec") or 0)
            if billsec > _MAX_CALL_SECONDS:
                skipped += 1
                continue
            cdr_seconds += max(0, billsec)
            cdr_billable += float(row.get("billable_minutes") or 0)
            cdr_cost += float(row.get("estimated_cost") or 0)
            continue

        sec = int(row.get("duration_seconds") or 0)
        if sec <= 0:
            continue
        if sec > _MAX_CALL_SECONDS:
            skipped += 1
            continue
        estimate_rows.append((sec, billable_minutes(sec)))

    # Prefer effective CDR rate over env when we already have live matches.
    if cdr_billable > 0:
        unit_rate = cdr_cost / cdr_billable
        display_rate = round(unit_rate, 4)
        rate_source = "vobiz_cdr"
    elif rate is not None:
        unit_rate = float(rate)
        display_rate = unit_rate
        rate_source = "estimate"
    elif not vobiz_api_configured():
        unit_rate = fallback_rate
        display_rate = unit_rate
        rate_source = "estimate"
    else:
        unit_rate = None
        display_rate = None
        rate_source = "estimate"

    est_seconds = 0
    est_billable = 0.0
    est_cost = 0.0
    for sec, mins in estimate_rows:
        est_seconds += sec
        est_billable += mins
        if unit_rate is not None:
            est_cost += round(mins * unit_rate, 2)

    out = rate_payload()
    out.update(
        {
            "organization_id": organization_id,
            "rate_per_minute": display_rate,
            "fallback_rate_per_minute": fallback_rate,
            "total_calls": len(rows),
            "total_duration_seconds": cdr_seconds + est_seconds,
            "billable_minutes": round(cdr_billable + est_billable, 4),
            "estimated_cost": round(cdr_cost + est_cost, 2),
            "vobiz_matched_calls": vobiz_hits,
            "vobiz_unmatched_calls": len(estimate_rows),
            "skipped_corrupt_rows": skipped,
            "rate_source": rate_source,
            "vobiz_cdrs_fetched": fetch_meta.get("cdr_count"),
            "vobiz_fetch_error": fetch_meta.get("error"),
        }
    )
    return out


async def billing_rate_payload_live() -> dict[str, Any]:
    """Prefer live Vobiz account rate; env value kept only as fallback metadata."""
    out = rate_payload()
    out["fallback_rate_per_minute"] = out["rate_per_minute"]
    hint = await fetch_account_rate_hint()
    if hint is not None:
        out["vobiz_account_rate_hint"] = hint
        out["rate_per_minute"] = hint
        out["rate_source"] = "vobiz_account"
    elif vobiz_api_configured():
        # Do not advertise env rate as live when Auth is set but /auth/me has no tier.
        out["rate_per_minute"] = None
        out["rate_source"] = "vobiz_pending"
    return out
