import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from collections import defaultdict

import config

# All credentials and integration settings come from environment variables (Azure in production).
def _default(key: str) -> str:
    return config.get_env(key)


SUPABASE_URL = _default("SUPABASE_URL")
SUPABASE_KEY = _default("SUPABASE_SERVICE_KEY")

SENSITIVE_KEYS = config.SENSITIVE_KEYS
ENV_KEYS = config.ENV_KEYS
APP_SETTING_KEYS = config.APP_SETTING_KEYS

# Sync client is process-safe. Async client must be per-event-loop (LiveKit job
# subprocesses / runners each have their own loop — a shared httpx client crashes with
# "Event is bound to a different event loop").
_sdb_client = None
_adb_by_loop: dict[int, Any] = {}
_adb_lock_by_loop: dict[int, asyncio.Lock] = {}

# Short TTL caches for dashboard reads (stats/contacts scan large tables).
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = {"stats": 15.0, "contacts": 20.0, "settings": 30.0}
_SETTING_CACHE_TTL = 30.0
_setting_cache: dict[str, tuple[float, str]] = {}

# List endpoints skip multi-KB transcript blobs.
_CALL_LIST_COLUMNS = (
    "id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, status, "
    "recording_url, notes, organization_id, lead_id, property_data, project_id, "
    "gemini_key_slot, gemini_tokens_in, gemini_tokens_out, gemini_tokens_total"
)
_CALL_LIST_COLUMNS_LEGACY = (
    "id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, status, "
    "recording_url, notes, organization_id, lead_id, property_data, project_id"
)
# Active-call polling needs live transcript fields for the dashboard.
_CALL_ACTIVE_COLUMNS = (
    "id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, status, "
    "organization_id, lead_id, live_transcript, transcript_json, property_data, project_id, "
    "gemini_key_slot, gemini_tokens_in, gemini_tokens_out, gemini_tokens_total"
)
_CALL_ACTIVE_COLUMNS_LEGACY = (
    "id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, status, "
    "organization_id, lead_id, live_transcript, transcript_json, property_data, project_id"
)
_gemini_columns_ready: Optional[bool] = None


async def ensure_gemini_token_columns() -> bool:
    """Return True if call_logs has Gemini token columns (cached after first probe)."""
    global _gemini_columns_ready
    if _gemini_columns_ready is not None:
        return _gemini_columns_ready
    db = await _adb()
    try:
        await (
            db.table("call_logs")
            .select("gemini_tokens_total")
            .limit(1)
            .execute()
        )
        _gemini_columns_ready = True
    except Exception as exc:
        _gemini_columns_ready = False
        print(
            "WARN: call_logs Gemini token columns missing — run in Supabase SQL Editor:\n"
            "  ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_key_slot TEXT;\n"
            "  ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_in INTEGER DEFAULT 0;\n"
            "  ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_out INTEGER DEFAULT 0;\n"
            "  ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_total INTEGER DEFAULT 0;\n"
            f"  ({exc})"
        )
    return _gemini_columns_ready
_STATS_ROW_LIMIT = 5000
_CONTACTS_ROW_LIMIT = 3000


def _cache_get(key: str) -> Any:
    entry = _cache.get(key)
    if not entry:
        return None
    expires, value = entry
    if time.monotonic() >= expires:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl: float) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


def invalidate_read_cache(*keys: str) -> None:
    for key in keys:
        if key == "contacts":
            for ck in list(_cache):
                if ck == "contacts" or ck.startswith("contacts:"):
                    _cache.pop(ck, None)
        else:
            _cache.pop(key, None)


async def _cached(key: str, ttl: float, factory: Callable[[], Any]) -> Any:
    hit = _cache_get(key)
    if hit is not None:
        return hit
    value = await factory()
    _cache_set(key, value, ttl)
    return value


def _sdb():
    global _sdb_client
    if _sdb_client is None:
        from supabase import create_client
        _sdb_client = create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))
    return _sdb_client


async def _adb():
    """Return an async Supabase client bound to the current event loop."""
    loop_id = id(asyncio.get_running_loop())
    client = _adb_by_loop.get(loop_id)
    if client is not None:
        return client
    lock = _adb_lock_by_loop.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _adb_lock_by_loop[loop_id] = lock
    async with lock:
        client = _adb_by_loop.get(loop_id)
        if client is None:
            from supabase._async.client import create_client
            client = await create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))
            _adb_by_loop[loop_id] = client
        return client


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", SUPABASE_URL)
    key = os.getenv("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
    if not url or not key:
        print("WARN: SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("OK: Supabase connected")
    except Exception as exc:
        print(f"WARN: Supabase connection failed: {exc}")
        print("   Run supabase_schema.sql in your Supabase Dashboard SQL Editor")


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict:
    """Return settings for dashboard — env keys always read from process environment only."""
    async def _load() -> dict:
        KNOWN_KEYS = sorted(ENV_KEYS)
        out: dict = {"env_only": config.is_env_only_mode()}
        for k in KNOWN_KEYS:
            env_val = _default(k)
            if k in SENSITIVE_KEYS:
                out[k] = {"value": "", "configured": bool(env_val)}
            else:
                out[k] = {"value": env_val, "configured": bool(env_val)}
        return out

    return await _cached("settings", _CACHE_TTL["settings"], _load)


async def save_settings(data: dict) -> None:
    """Persist settings. Production: env keys must be set in Azure only."""
    if config.is_env_only_mode():
        blocked = [k for k in data if k in ENV_KEYS]
        if blocked:
            raise ValueError(
                f"Cannot save env keys via dashboard in production: {', '.join(blocked)}. "
                "Set them in Azure environment variables."
            )
    db = await _adb()
    updated_at = datetime.now().isoformat()
    allowed = APP_SETTING_KEYS if config.is_env_only_mode() else (APP_SETTING_KEYS | ENV_KEYS)
    rows = [
        {"key": k, "value": str(v), "updated_at": updated_at}
        for k, v in data.items()
        if k in allowed and v is not None and v != ""
    ]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()
    invalidate_read_cache("settings")
    _setting_cache.clear()
    if not config.is_env_only_mode():
        for k, v in data.items():
            if k in ENV_KEYS and v is not None and v != "":
                os.environ[k] = str(v)


async def get_setting(key: str, default: str = "") -> str:
    """Env keys → process environment. App settings (system_prompt) may use Supabase."""
    if key == "system_prompt":
        # Azure SYSTEM_PROMPT env locks the prompt; otherwise dashboard DB value wins.
        env_prompt = (config.get_env("SYSTEM_PROMPT") or "").strip()
        if env_prompt:
            return env_prompt
        cached = _setting_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        try:
            db = await _adb()
            result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
            if result and result.data:
                val = result.data.get("value") or ""
                _setting_cache[key] = (time.monotonic() + _SETTING_CACHE_TTL, val)
                return val
        except Exception:
            pass
        return default

    env_val = _default(key) if key in ENV_KEYS else ""
    if env_val:
        return env_val

    cached = _setting_cache.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    if not config.is_env_only_mode() and key in (ENV_KEYS | APP_SETTING_KEYS):
        db = await _adb()
        result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        if result and result.data:
            val = result.data["value"]
            _setting_cache[key] = (time.monotonic() + _SETTING_CACHE_TTL, val)
            return val
    return default


async def set_setting(key: str, value: str) -> None:
    """Only app settings (e.g. system_prompt) may be written to Supabase."""
    if key in ENV_KEYS:
        if config.is_env_only_mode():
            raise ValueError(f"{key} must be set via Azure environment variables.")
        return
    if key not in APP_SETTING_KEYS:
        return
    db = await _adb()
    await db.table("settings").upsert(
        {"key": key, "value": value, "updated_at": datetime.now().isoformat()},
        on_conflict="key",
    ).execute()
    invalidate_read_cache("settings")
    _setting_cache.pop(key, None)


async def get_enabled_tools() -> list:
    try:
        raw = await get_setting("ENABLED_TOOLS", "")
    except Exception:
        return []
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Error logs ────────────────────────────────────────────────────────────────

async def _log_error_impl(source: str, message: str, detail: str, level: str) -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()),
            "source": source,
            "level": level,
            "message": message[:500],
            "detail": detail[:2000],
            "timestamp": datetime.now().isoformat(),
        }).execute()
    except Exception:
        pass


async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        asyncio.get_running_loop().create_task(
            _log_error_impl(source, message, detail, level)
        )
    except RuntimeError:
        await _log_error_impl(source, message, detail, level)


async def get_errors(limit: int = 100) -> list:
    db = await _adb()
    result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    if source:
        query = query.eq("source", source)
    result = await query.execute()
    return result.data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


# ── Appointments ──────────────────────────────────────────────────────────────

class SlotUnavailableError(Exception):
    """Raised when another booking already holds this date/time slot."""

    def __init__(self, next_slot: str = ""):
        self.next_slot = next_slot or ""
        super().__init__(f"slot unavailable; next={self.next_slot}")


def _slot_lock_key(date: str, time: str) -> str:
    """Settings PK used as a distributed lock (no DDL required)."""
    return f"appt_slot:{date}:{time}"


def _is_unique_violation(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or ""
    if str(code) == "23505":
        return True
    # postgrest may put the payload in args[0]
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, dict) and str(arg.get("code") or "") == "23505":
            return True
    err = str(exc).lower()
    return "23505" in err or "duplicate" in err or "unique" in err


def _is_missing_booking_rpc(exc: BaseException) -> bool:
    """True when Supabase has not applied the atomic-booking migration yet."""
    text = str(exc).lower()
    return (
        "pgrst202" in text
        or "book_appointment_atomic" in text
        and ("could not find" in text or "schema cache" in text or "does not exist" in text)
    )


async def _insert_appointment_legacy(
    name: str,
    phone: str,
    date: str,
    time: str,
    service: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    project_id: Optional[str] = None,
    call_id: Optional[str] = None,
) -> str:
    """Compatibility path used only until the Supabase RPC migration is applied."""
    if not await check_slot(date, time):
        next_slot = await get_next_available(date, time)
        raise SlotUnavailableError(next_slot)
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    await _acquire_slot_lock(date, time, full_id)
    db = await _adb()
    try:
        row = {
            "id": full_id, "name": name, "phone": phone,
            "date": date, "time": time, "service": service,
            "status": "booked", "created_at": datetime.now().isoformat(),
        }
        if organization_id:
            row["organization_id"] = organization_id
        if lead_id:
            row["lead_id"] = lead_id
        if project_id:
            row["project_id"] = project_id
        if call_id:
            row["call_id"] = call_id
        await db.table("appointments").insert(row).execute()
    except Exception as exc:
        await _release_slot_lock(date, time)
        if _is_unique_violation(exc) or not await check_slot(date, time):
            next_slot = await get_next_available(date, time)
            raise SlotUnavailableError(next_slot) from exc
        raise
    return booking_id


async def _release_slot_lock(date: str, time: str) -> None:
    try:
        db = await _adb()
        await db.table("settings").delete().eq("key", _slot_lock_key(date, time)).execute()
    except Exception:
        pass


async def _acquire_slot_lock(date: str, time: str, appointment_id: str) -> None:
    """Atomically claim a slot via settings.key PRIMARY KEY. Raises SlotUnavailableError."""
    db = await _adb()
    lock_key = _slot_lock_key(date, time)
    try:
        await db.table("settings").insert({
            "key": lock_key,
            "value": appointment_id,
            "updated_at": datetime.now().isoformat(),
        }).execute()
        return
    except Exception as exc:
        if not _is_unique_violation(exc):
            raise
    # Stale lock: key exists but no booked appointment → clear and retry once.
    if await check_slot(date, time):
        await _release_slot_lock(date, time)
        try:
            await db.table("settings").insert({
                "key": lock_key,
                "value": appointment_id,
                "updated_at": datetime.now().isoformat(),
            }).execute()
            return
        except Exception as exc2:
            if _is_unique_violation(exc2):
                next_slot = await get_next_available(date, time)
                raise SlotUnavailableError(next_slot) from exc2
            raise
    next_slot = await get_next_available(date, time)
    raise SlotUnavailableError(next_slot)


async def insert_appointment(
    name: str,
    phone: str,
    date: str,
    time: str,
    service: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    project_id: Optional[str] = None,
    call_id: Optional[str] = None,
) -> str:
    """Book in one database transaction; fall back while the RPC migration rolls out."""
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    db = await _adb()
    try:
        result = await db.rpc("book_appointment_atomic", {
            "p_id": full_id,
            "p_name": name,
            "p_phone": phone,
            "p_date": date,
            "p_time": time,
            "p_service": service,
            "p_created_at": datetime.now().isoformat(),
        }).execute()
    except Exception as exc:
        if _is_missing_booking_rpc(exc):
            return await _insert_appointment_legacy(
                name,
                phone,
                date,
                time,
                service,
                organization_id=organization_id,
                lead_id=lead_id,
                project_id=project_id,
                call_id=call_id,
            )
        raise

    rows = result.data or []
    row = rows[0] if isinstance(rows, list) and rows else rows if isinstance(rows, dict) else {}
    status = str(row.get("result_status") or "").lower()
    if status == "booked":
        # The deployed legacy RPC creates the row atomically but predates tenant
        # columns. Patch ownership immediately after the successful insert. This
        # happens after the lead confirms a booking, so it does not affect normal
        # conversational response latency.
        scope = {
            key: value
            for key, value in {
                "organization_id": organization_id,
                "lead_id": lead_id,
                "project_id": project_id,
                "call_id": call_id,
            }.items()
            if value
        }
        if scope:
            await db.table("appointments").update(scope).eq("id", full_id).execute()
        return booking_id
    if status == "unavailable":
        next_date = str(row.get("next_date") or "").strip()
        next_time = str(row.get("next_time") or "").strip()
        next_slot = f"{next_date} at {next_time}" if next_date and next_time else "another time"
        raise SlotUnavailableError(next_slot)
    raise RuntimeError("Atomic booking returned an invalid response")


async def check_slot(date: str, time: str) -> bool:
    """Returns True if slot is available (no existing booking)."""
    db = await _adb()
    result = await (
        db.table("appointments").select("id")
        .eq("date", date).eq("time", time).eq("status", "booked")
        .maybe_single().execute()
    )
    if result is None:
        return True
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    existing = await (
        db.table("appointments").select("id, date, time")
        .eq("id", appointment_id).eq("status", "booked")
        .maybe_single().execute()
    )
    result = await (
        db.table("appointments").update({"status": "cancelled"})
        .eq("id", appointment_id).eq("status", "booked").execute()
    )
    ok = len(result.data or []) > 0
    if ok and existing and existing.data:
        await _release_slot_lock(
            existing.data.get("date") or "",
            existing.data.get("time") or "",
        )
    return ok


async def get_appointments_by_phone(
    phone: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").eq("phone", phone)
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    result = await query.order("date", desc=True).execute()
    return result.data or []


# ── Call logs ─────────────────────────────────────────────────────────────────

# Live / pre-answer statuses shown on the Active Calls UI.
_ACTIVE_CALL_STATUSES = ("ringing", "in_progress")
_ACTIVE_OR_QUEUED_STATUSES = ("ringing", "in_progress", "queued")


class ActiveCallConflictError(Exception):
    """Raised when a destination already has an active/queued call row."""

    def __init__(
        self,
        phone: str,
        *,
        existing_id: Optional[str] = None,
        existing_status: Optional[str] = None,
    ):
        self.phone = phone
        self.existing_id = existing_id
        self.existing_status = existing_status
        detail = f"Phone {phone} already has an active call"
        if existing_id:
            detail += f" (id={existing_id}"
            if existing_status:
                detail += f", status={existing_status}"
            detail += ")"
        super().__init__(detail)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()
_LIVE_CALL_STATUSES = frozenset({"ringing", "in_progress", "queued"})
# CRM outcomes set by the agent (end_call) — never downgrade these to stale failed.
_FINAL_CALL_OUTCOMES = frozenset({
    "booked", "interested", "not_interested", "wrong_number", "voicemail", "no_answer",
    "callback_requested", "completed", "busy", "transferred", "do_not_call",
    "disconnect_in_between",
})

# Outcome → sentiment score for CRM/webhook (not transcript NLP).
# 1 = positive, -1 = negative, 0 = neutral.
_SENTIMENT_POSITIVE = frozenset({"booked", "interested"})
_SENTIMENT_NEGATIVE = frozenset({"not_interested", "do_not_call"})


def sentiment_from_outcome(outcome: Optional[str]) -> int:
    """Map call outcome to sentiment score: 1 / 0 / -1."""
    o = (outcome or "").strip().lower()
    if o in _SENTIMENT_POSITIVE:
        return 1
    if o in _SENTIMENT_NEGATIVE:
        return -1
    return 0


def normalize_sentiment(stored: Any, outcome: Optional[str] = None) -> int:
    """Prefer stored DB value; otherwise derive from outcome."""
    if stored is not None and stored != "":
        try:
            v = int(stored)
            if v in (-1, 0, 1):
                return v
        except (TypeError, ValueError):
            pass
    return sentiment_from_outcome(outcome)


# Outcome base for lead AI score (0–100). Bonuses add engagement / qualification.
_AI_SCORE_OUTCOME_BASE: dict[str, int] = {
    "booked": 80,
    "interested": 68,
    "transferred": 65,
    "callback_requested": 55,
    "completed": 40,
    "disconnect_in_between": 35,
    "busy": 18,
    "no_answer": 15,
    "voicemail": 15,
    "wrong_number": 5,
    "not_interested": 12,
    "do_not_call": 0,
    "failed": 5,
}


def compute_ai_lead_score(record: dict) -> dict:
    """Score the lead from this call for CRM (0–100 + HOT/WARM/COLD).

    Uses outcome, collected property_data, and talk duration — not a separate LLM call.
    """
    outcome = (record.get("outcome") or "").strip().lower()
    duration = int(record.get("duration_seconds") or 0)
    props = parse_property_data(record.get("property_data"))

    score = int(_AI_SCORE_OUTCOME_BASE.get(outcome, 25))
    reasons: list[str] = [f"outcome:{outcome or 'unknown'}={score}"]

    if (props.get("budget") or "").strip():
        min_b, max_b = parse_budget_range(str(props.get("budget")))
        if min_b is not None or max_b is not None:
            score += 8
            reasons.append("budget+8")
        else:
            # "Yes" / vague budget answers still count lightly
            score += 3
            reasons.append("budget_soft+3")

    cityish = (
        (props.get("city") or "").strip()
        or (props.get("preferred_location") or "").strip()
        or (props.get("location_preference") or "").strip()
    )
    if cityish:
        score += 8
        reasons.append("location+8")
    elif (props.get("location_fit") or "").strip():
        score += 3
        reasons.append("location_fit+3")

    if (props.get("bhk") or "").strip():
        score += 5
        reasons.append("bhk+5")
    if (props.get("property_type") or "").strip():
        score += 3
        reasons.append("property_type+3")
    if (props.get("preferred_project") or "").strip():
        score += 4
        reasons.append("preferred_project+4")
    if (props.get("whatsapp_number") or "").strip():
        score += 2
        reasons.append("whatsapp+2")

    if duration >= 180:
        score += 6
        reasons.append("duration_3m+6")
    elif duration >= 60:
        score += 3
        reasons.append("duration_1m+3")

    # Conversation evidence (transcript) bumps weak no-talk outcomes slightly
    if _call_had_conversation(record) and outcome not in {"booked", "do_not_call"}:
        score += 2
        reasons.append("talked+2")

    score = max(0, min(100, score))
    if score >= 75:
        label = "HOT"
    elif score >= 45:
        label = "WARM"
    else:
        label = "COLD"

    return {
        "ai_score": score,
        "lead_score": label,
        "ai_score_label": label,
        "ai_score_reason": ", ".join(reasons),
    }


def _call_had_conversation(row: dict) -> bool:
    """True when transcript text suggests the callee answered and spoke."""
    if (row.get("live_transcript") or "").strip():
        return True
    try:
        lines = json.loads(row.get("transcript_json") or "[]")
        return isinstance(lines, list) and len(lines) > 0
    except (json.JSONDecodeError, TypeError):
        return False


_MIDCALL_CUT_OUTCOMES = frozenset({"disconnect_in_between"})
_RESUME_SKIP_STATUSES = frozenset({"ringing", "in_progress", "queued"})


def _resume_cut_max_hours() -> float:
    return max(1.0, float(os.getenv("RESUME_CUT_CALL_HOURS", "24") or "24"))


def _transcript_snippet_for_resume(row: dict, *, max_lines: int = 14, max_chars: int = 1400) -> str:
    """Last spoken lines from the cut call — same number resume only."""
    lines_out: list[str] = []
    try:
        raw = json.loads(row.get("transcript_json") or "[]")
        if isinstance(raw, list):
            for ln in raw[-max_lines:]:
                if not isinstance(ln, dict):
                    continue
                role = (ln.get("role") or ln.get("speaker") or "unknown").strip()
                text = (ln.get("text") or ln.get("transcript") or "").strip()
                if text:
                    lines_out.append(f"{role}: {text}")
    except (json.JSONDecodeError, TypeError):
        pass
    if not lines_out:
        live = (row.get("live_transcript") or "").strip()
        if live:
            return live[-max_chars:]
        return ""
    blob = "\n".join(lines_out)
    return blob[-max_chars:] if len(blob) > max_chars else blob


def pick_same_number_resume_call(
    calls: list,
    *,
    exclude_call_id: Optional[str] = None,
) -> Optional[dict]:
    """If this phone's latest finished call was cut mid-talk, return that row.

    ``calls`` must already be for one E.164 number (newest first). Never mix numbers.
    """
    max_age = timedelta(hours=_resume_cut_max_hours())
    for row in calls or []:
        cid = str(row.get("id") or "")
        if exclude_call_id and cid == str(exclude_call_id):
            continue
        status = (row.get("status") or "").strip().lower()
        if status in _RESUME_SKIP_STATUSES:
            continue
        started = _parse_call_timestamp(row.get("timestamp"))
        if started:
            now = datetime.now(started.tzinfo)
            if now - started > max_age:
                return None
        outcome = (row.get("outcome") or "").strip().lower()
        reason = (row.get("reason") or "").strip().lower()
        is_cut = outcome in _MIDCALL_CUT_OUTCOMES or (
            "disconnect" in reason and "mid" in reason
        )
        if not is_cut:
            return None
        if not _call_had_conversation(row):
            return None
        return row
    return None


def format_same_number_resume_block(row: dict) -> str:
    """Prompt block: continue THIS number's cut conversation (not any other lead)."""
    props = parse_property_data(row.get("property_data"))
    collected = []
    for key in (
        "budget", "city", "preferred_location", "bhk", "property_type",
        "location_preference", "preferred_project", "whatsapp_number",
    ):
        val = (props.get(key) or "").strip()
        if val:
            collected.append(f"  • {key}: {val}")
    snippet = _transcript_snippet_for_resume(row)
    ts = (row.get("timestamp") or "")[:19]
    lines = [
        "THIS CALL IS A RECONNECT — SAME PHONE NUMBER ONLY.",
        "The previous outbound call to THIS exact number was cut mid-conversation "
        f"(outcome={row.get('outcome')}, at {ts}).",
        "Do NOT use any other lead's history. Do NOT restart a full fresh pitch.",
        "Apologise for the cut by name, ask whether to continue, then continue from where "
        "this conversation was cut (see RECONNECT MODE).",
    ]
    if collected:
        lines.append("Already collected on THIS number (do not re-ask unless they correct it):")
        lines.extend(collected)
    if snippet:
        lines.append("Last exchange on THIS number before the cut:")
        lines.append(snippet)
    else:
        lines.append("Transcript snippet missing — continue from remembered details; do not invent facts.")
    return "\n".join(lines)


def _estimate_duration_from_row(row: dict) -> int:
    """Best-effort talk duration when stale cleanup runs (avoid 5h wall-clock artifacts)."""
    try:
        lines = json.loads(row.get("transcript_json") or "[]")
        if isinstance(lines, list) and lines:
            chars = sum(len((ln.get("text") or "")) for ln in lines if isinstance(ln, dict))
            # ~12 chars/sec spoken + turn gaps — clamp to sane call length
            return max(15, min(3600, chars // 10))
    except Exception:
        pass
    return 0


def _last_transcript_activity_at(row: dict) -> Optional[datetime]:
    """Latest transcript line timestamp, if present — used for idle stale reclaim."""
    raw = row.get("transcript_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("lines") or raw.get("transcript") or []
    if not isinstance(raw, list):
        return None
    latest: Optional[datetime] = None
    for ln in raw:
        if not isinstance(ln, dict):
            continue
        ts = _parse_call_timestamp(ln.get("ts") or ln.get("timestamp") or ln.get("time"))
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


async def _find_active_calls_for_phone(
    phone: str,
    *,
    organization_id: Optional[str] = None,
) -> list[dict]:
    """All ringing/in_progress/queued rows for a destination (newest first)."""
    phone = (phone or "").strip()
    if not phone:
        return []
    db = await _adb()
    query = (
        db.table("call_logs")
        .select("id, phone_number, lead_name, status, outcome, timestamp, organization_id")
        .eq("phone_number", phone)
        .in_("status", list(_ACTIVE_OR_QUEUED_STATUSES))
        .order("timestamp", desc=True)
        .limit(20)
    )
    if organization_id:
        query = query.eq("organization_id", organization_id.strip())
    result = await query.execute()
    return list(result.data or [])


async def create_active_call(
    phone_number: str,
    lead_name: Optional[str],
    *,
    room_name: Optional[str] = None,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    project_id: Optional[str] = None,
    status: str = "ringing",
    scheduled_at: Optional[str] = None,
    callback_urgency: Optional[str] = None,
    initial_property_data: Optional[dict] = None,
) -> str:
    """Create a call row only if the destination has no active/queued call.

    Uses pre-check + post-insert winner election so concurrent inserts cannot
    both stay active. Prefer also applying the partial unique index in
    ``supabase_schema.sql``.
    """
    phone = (phone_number or "").strip()
    org = (organization_id or "").strip() or None
    existing = await _find_active_calls_for_phone(phone, organization_id=org)
    if existing:
        row0 = existing[0]
        raise ActiveCallConflictError(
            phone,
            existing_id=row0.get("id"),
            existing_status=row0.get("status"),
        )

    db = await _adb()
    call_id = str(uuid.uuid4())
    initial = (status or "ringing").strip().lower()
    if initial not in ("ringing", "queued", "in_progress"):
        initial = "ringing"
    props: dict = dict(initial_property_data or {})
    if scheduled_at:
        props["scheduled_at"] = scheduled_at
    if callback_urgency:
        props["callback_urgency"] = callback_urgency

    row: dict = {
        "id": call_id,
        "phone_number": phone,
        "lead_name": lead_name,
        "outcome": initial,
        "reason": room_name or "",
        "duration_seconds": 0,
        "timestamp": _utc_now_iso(),
        "status": initial,
        "live_transcript": "",
        "transcript_json": "[]",
        "transcript_status": "pending",
        "transcript_source": "gemini_live",
        "last_transcript_error": None,
        "property_data": json.dumps(props, ensure_ascii=False) if props else "{}",
    }
    if org:
        row["organization_id"] = org
    if lead_id:
        row["lead_id"] = lead_id
    if project_id:
        row["project_id"] = project_id
    if scheduled_at:
        row["scheduled_at"] = scheduled_at
    if callback_urgency:
        row["callback_urgency"] = callback_urgency
    try:
        await db.table("call_logs").insert(row).execute()
    except Exception as exc:
        # Unique active-phone index (when deployed) surfaces as insert failure.
        err = str(exc).lower()
        if "uq_call_logs_one_active" in err or "duplicate" in err or "unique" in err:
            others = await _find_active_calls_for_phone(phone, organization_id=org)
            other = next((r for r in others if r.get("id") != call_id), others[0] if others else None)
            raise ActiveCallConflictError(
                phone,
                existing_id=(other or {}).get("id"),
                existing_status=(other or {}).get("status"),
            ) from exc
        for key in (
            "scheduled_at", "callback_urgency", "transcript_status",
            "transcript_source", "last_transcript_error",
        ):
            row.pop(key, None)
        if row.get("property_data") == "{}" or (
            scheduled_at or callback_urgency
        ):
            row["property_data"] = "{}"
        print(f"WARN: create_active_call optional columns failed ({exc}); retrying without them")
        await db.table("call_logs").insert(row).execute()

    # Post-insert winner election: oldest active row wins under a race.
    peers = await _find_active_calls_for_phone(phone, organization_id=org)
    if len(peers) > 1:
        def _peer_key(r: dict) -> tuple:
            ts = _parse_call_timestamp(r.get("timestamp")) or _utc_now()
            return (ts, str(r.get("id") or ""))

        winner = sorted(peers, key=_peer_key)[0]
        if winner.get("id") != call_id:
            try:
                await db.table("call_logs").delete().eq("id", call_id).execute()
            except Exception:
                pass
            raise ActiveCallConflictError(
                phone,
                existing_id=winner.get("id"),
                existing_status=winner.get("status"),
            )

    invalidate_read_cache("stats", "contacts")
    return call_id


async def mark_call_ringing(call_id: str) -> None:
    """Flip queued → ringing when the dial is actually dispatched."""
    if not call_id:
        return
    db = await _adb()
    # Reset the clock: queued rows can wait minutes in a FIFO campaign, and the
    # stale reconciler measures ringing age from this timestamp.
    await db.table("call_logs").update({
        "status": "ringing",
        "outcome": "ringing",
        "timestamp": _utc_now_iso(),
    }).eq("id", call_id).execute()
    invalidate_read_cache("stats", "contacts")


async def set_call_answered(call_id: str) -> None:
    """Flip ringing → in_progress when the callee answers (SIP connected)."""
    if not call_id:
        return
    db = await _adb()
    await db.table("call_logs").update({
        "status": "in_progress",
        "outcome": "in_progress",
    }).eq("id", call_id).execute()
    invalidate_read_cache("stats", "contacts")


async def mark_transcript_capturing(call_id: str) -> None:
    """Expose that live transcript handlers are attached for this call."""
    if not call_id:
        return
    db = await _adb()
    try:
        await db.table("call_logs").update({
            "transcript_status": "capturing",
            "transcript_source": "gemini_live",
            "last_transcript_error": None,
        }).eq("id", call_id).execute()
    except Exception:
        # Transcript capture itself must remain available if only the health
        # columns are absent or temporarily unwritable.
        pass


def estimate_gemini_tokens(
    *,
    transcript_json: Optional[str] = None,
    live_transcript: Optional[str] = None,
    duration_seconds: int = 0,
) -> tuple[int, int, int]:
    """Estimate Gemini tokens from call transcript text (~4 chars ≈ 1 token).

    Returns ``(tokens_in, tokens_out, tokens_total)``.
    Falls back to a duration-based floor when no transcript text exists
    (Realtime audio still consumes tokens even if transcript flush missed).
    """
    tin = tout = 0
    lines: list = []
    if transcript_json:
        try:
            parsed = json.loads(transcript_json)
            if isinstance(parsed, list):
                lines = parsed
        except (json.JSONDecodeError, TypeError):
            lines = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        text = (line.get("text") or "").strip()
        if not text:
            continue
        n = max(1, (len(text) + 3) // 4)
        role = (line.get("role") or "").lower()
        if role in ("user", "customer", "lead"):
            tin += n
        else:
            tout += n
    if tin == 0 and tout == 0 and live_transcript:
        text = str(live_transcript).strip()
        if text:
            # Split Lead:/Agent: lines when present; else split 50/50.
            lead_chars = agent_chars = 0
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                lower = line.lower()
                if lower.startswith("lead:"):
                    lead_chars += len(line.split(":", 1)[-1])
                elif lower.startswith("agent:"):
                    agent_chars += len(line.split(":", 1)[-1])
                else:
                    agent_chars += len(line)
            if lead_chars or agent_chars:
                tin = max(1, (lead_chars + 3) // 4) if lead_chars else 0
                tout = max(1, (agent_chars + 3) // 4) if agent_chars else 0
            else:
                total = max(1, (len(text) + 3) // 4)
                tin, tout = total // 2, total - total // 2
    # Duration floor: ~15 tokens/sec of talk (input+output) when transcript empty.
    if tin == 0 and tout == 0 and duration_seconds and int(duration_seconds) > 0:
        total = max(10, int(duration_seconds) * 15)
        tin, tout = total // 2, total - total // 2
    total = tin + tout
    return tin, tout, total


async def update_call_gemini_usage(
    call_id: str,
    *,
    key_slot: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    tokens_total: Optional[int] = None,
) -> None:
    """Persist opaque Gemini key slot + optional token counters (never store secrets)."""
    if not call_id:
        return
    row: dict = {}
    if key_slot:
        row["gemini_key_slot"] = str(key_slot)[:64]
    if tokens_in is not None:
        row["gemini_tokens_in"] = max(0, int(tokens_in))
    if tokens_out is not None:
        row["gemini_tokens_out"] = max(0, int(tokens_out))
    if tokens_total is not None:
        row["gemini_tokens_total"] = max(0, int(tokens_total))
    elif "gemini_tokens_in" in row or "gemini_tokens_out" in row:
        tin = row.get("gemini_tokens_in")
        tout = row.get("gemini_tokens_out")
        if tin is None or tout is None:
            existing = await get_call_by_id(call_id)
            if existing:
                if tin is None:
                    tin = int(existing.get("gemini_tokens_in") or 0)
                if tout is None:
                    tout = int(existing.get("gemini_tokens_out") or 0)
        row["gemini_tokens_total"] = int(tin or 0) + int(tout or 0)
    if not row:
        return
    if not await ensure_gemini_token_columns():
        return
    db = await _adb()
    try:
        await db.table("call_logs").update(row).eq("id", call_id).execute()
    except Exception as exc:
        global _gemini_columns_ready
        _gemini_columns_ready = False
        print(f"WARN: update_call_gemini_usage failed for {call_id}: {exc}")


def tokens_per_minute(tokens_total: int | float | None, duration_seconds: int | float | None) -> Optional[float]:
    """``total_tokens ÷ duration_minutes`` (None when duration is zero/missing)."""
    try:
        tokens = float(tokens_total or 0)
        secs = float(duration_seconds or 0)
    except (TypeError, ValueError):
        return None
    if tokens <= 0 or secs <= 0:
        return None
    return round(tokens / (secs / 60.0), 2)


def attach_tokens_per_minute(call: dict) -> dict:
    """Add ``gemini_tokens_per_minute`` onto a call_logs row (in place)."""
    total = call.get("gemini_tokens_total")
    if total is None:
        try:
            total = int(call.get("gemini_tokens_in") or 0) + int(call.get("gemini_tokens_out") or 0)
        except (TypeError, ValueError):
            total = 0
        call["gemini_tokens_total"] = total
    tpm = tokens_per_minute(total, call.get("duration_seconds"))
    call["gemini_tokens_per_minute"] = tpm
    return call


async def backfill_call_gemini_tokens(call: dict, *, persist: bool = True) -> dict:
    """If token fields are empty, estimate from stored transcript (and optionally save)."""
    if not call:
        return call
    existing_total = int(call.get("gemini_tokens_total") or 0)
    existing_in = int(call.get("gemini_tokens_in") or 0)
    existing_out = int(call.get("gemini_tokens_out") or 0)
    if existing_total > 0 or existing_in > 0 or existing_out > 0:
        return attach_tokens_per_minute(call)

    call_id = call.get("id")
    transcript_json = call.get("transcript_json")
    live_transcript = call.get("live_transcript")
    # List queries omit transcript blobs — fetch once when tokens are missing.
    if call_id and transcript_json is None and live_transcript is None:
        full = await get_call_by_id(call_id)
        if full:
            transcript_json = full.get("transcript_json")
            live_transcript = full.get("live_transcript")
            if full.get("gemini_key_slot") and not call.get("gemini_key_slot"):
                call["gemini_key_slot"] = full.get("gemini_key_slot")

    tin, tout, total = estimate_gemini_tokens(
        transcript_json=transcript_json,
        live_transcript=live_transcript,
        duration_seconds=int(call.get("duration_seconds") or 0),
    )
    if total <= 0:
        return attach_tokens_per_minute(call)

    call["gemini_tokens_in"] = tin
    call["gemini_tokens_out"] = tout
    call["gemini_tokens_total"] = total
    if persist and call_id and await ensure_gemini_token_columns():
        await update_call_gemini_usage(
            call_id,
            tokens_in=tin,
            tokens_out=tout,
            tokens_total=total,
        )
    return attach_tokens_per_minute(call)


async def get_gemini_token_summary(
    *,
    organization_id: Optional[str] = None,
    limit: int = 5000,
) -> dict:
    """Aggregate Gemini token counters from recent call_logs (for Call Logs KPI)."""
    db = await _adb()
    query = (
        db.table("call_logs")
        .select(
            "gemini_key_slot, gemini_tokens_in, gemini_tokens_out, "
            "gemini_tokens_total, duration_seconds"
        )
        .order("timestamp", desc=True)
        .limit(max(1, min(int(limit), 10000)))
    )
    if organization_id:
        query = query.eq("organization_id", organization_id)
    try:
        rows = (await query.execute()).data or []
    except Exception:
        return {
            "calls_with_key": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_total": 0,
            "duration_seconds": 0,
            "tokens_per_minute": None,
            "by_slot": {},
        }
    by_slot: dict = {}
    tin = tout = total = 0
    duration_sec = 0
    with_key = 0
    for r in rows:
        slot = (r.get("gemini_key_slot") or "").strip()
        if slot:
            with_key += 1
            by_slot[slot] = by_slot.get(slot, 0) + 1
        tin += int(r.get("gemini_tokens_in") or 0)
        tout += int(r.get("gemini_tokens_out") or 0)
        total += int(r.get("gemini_tokens_total") or 0)
        duration_sec += int(r.get("duration_seconds") or 0)
    if total <= 0:
        total = tin + tout
    return {
        "calls_scanned": len(rows),
        "calls_with_key": with_key,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_total": total,
        "duration_seconds": duration_sec,
        "tokens_per_minute": tokens_per_minute(total, duration_sec),
        "by_slot": by_slot,
    }


async def update_call_transcript(call_id: str, transcript_json: str, live_transcript: str) -> None:
    db = await _adb()
    row = {
        "transcript_json": transcript_json,
        "live_transcript": live_transcript,
        "transcript_status": "saved",
        "transcript_source": "gemini_live",
        "last_transcript_error": None,
    }
    try:
        await db.table("call_logs").update(row).eq("id", call_id).execute()
    except Exception as exc:
        # Compatibility for deployments that have not applied the transcript
        # health columns yet. Network failures will also fail this second write
        # and propagate to the retry/queue layer.
        try:
            await db.table("call_logs").update({
                "transcript_json": transcript_json,
                "live_transcript": live_transcript,
            }).eq("id", call_id).execute()
        except Exception:
            raise exc


async def enqueue_transcript_write(
    call_id: str,
    transcript_json: str,
    live_transcript: str,
    *,
    attempts: int,
    last_error: str,
) -> None:
    """Persist the latest failed transcript snapshot for later replay."""
    db = await _adb()
    now = datetime.now().isoformat()
    row = {
        # One row per call intentionally coalesces repeated failed snapshots.
        "id": call_id,
        "call_id": call_id,
        "transcript_json": transcript_json,
        "live_transcript": live_transcript,
        "attempts": max(0, int(attempts)),
        "last_error": str(last_error)[:2000],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    await db.table("transcript_write_queue").upsert(row, on_conflict="id").execute()
    try:
        await db.table("call_logs").update({
            "transcript_status": "failed",
            "last_transcript_error": str(last_error)[:2000],
        }).eq("id", call_id).execute()
    except Exception:
        pass


async def complete_queued_transcript_write(call_id: str) -> None:
    """Mark a coalesced queue item completed after the call row is saved."""
    db = await _adb()
    try:
        await db.table("transcript_write_queue").update({
            "status": "completed",
            "last_error": None,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", call_id).execute()
    except Exception:
        # Queue bookkeeping must never turn a successful transcript save into a
        # failed save.
        pass


async def process_transcript_write_queue(limit: int = 25) -> dict:
    """Replay pending transcript snapshots; safe to call when a worker starts."""
    db = await _adb()
    result = await (
        db.table("transcript_write_queue")
        .select("*")
        .eq("status", "pending")
        .order("updated_at")
        .limit(max(1, min(int(limit), 100)))
        .execute()
    )
    rows = result.data or []
    saved = failed = 0
    for row in rows:
        call_id = str(row.get("call_id") or "").strip()
        if not call_id:
            continue
        try:
            await update_call_transcript(
                call_id,
                row.get("transcript_json") or "[]",
                row.get("live_transcript") or "",
            )
            await complete_queued_transcript_write(call_id)
            saved += 1
        except Exception as exc:
            failed += 1
            attempts = int(row.get("attempts") or 0) + 1
            try:
                await db.table("transcript_write_queue").update({
                    "attempts": attempts,
                    "last_error": str(exc)[:2000],
                    "updated_at": datetime.now().isoformat(),
                }).eq("id", row.get("id")).execute()
            except Exception:
                pass
    return {"scanned": len(rows), "saved": saved, "failed": failed}


async def finalize_call(
    call_id: str,
    *,
    outcome: str,
    reason: str,
    duration_seconds: int,
    recording_url: Optional[str] = None,
    live_transcript: Optional[str] = None,
    transcript_json: Optional[str] = None,
    room_name: Optional[str] = None,
    notify_webhook: bool = True,
    release_caller_id: bool = True,
    scheduled_at: Optional[str] = None,
    callback_urgency: Optional[str] = None,
) -> None:
    """Persist final call outcome. Skips stale-failed downgrades after a real outcome exists.

    ``release_caller_id`` and ``notify_webhook`` are independent: stale/UI cleanup
    may mark a row completed without freeing the DID or notifying Node when the
    SIP line has not been confirmed clear.
    """
    existing = await get_call_by_id(call_id) if call_id else None
    if existing:
        old_status = (existing.get("status") or "").lower()
        old_outcome = (existing.get("outcome") or "").lower()
        is_stale = str(reason or "").lower().startswith("stale")
        if old_outcome in _FINAL_CALL_OUTCOMES and old_status in ("completed", "failed"):
            if is_stale or outcome == "failed":
                return
        if old_outcome in _FINAL_CALL_OUTCOMES and outcome in _LIVE_CALL_STATUSES:
            return

    from callback_schedule import resolve_callback_schedule

    cb = {"scheduled_at": None, "callback_urgency": None}
    if (outcome or "").strip().lower() == "callback_requested" or scheduled_at or callback_urgency:
        cb = resolve_callback_schedule(
            scheduled_at_raw=scheduled_at,
            callback_urgency=callback_urgency,
            reason=reason,
        )

    db = await _adb()
    sentiment = sentiment_from_outcome(outcome)
    row: dict = {
        "outcome": outcome,
        "reason": reason,
        "duration_seconds": duration_seconds,
        "status": "completed" if outcome != "failed" else "failed",
        "sentiment": sentiment,
    }
    if recording_url:
        row["recording_url"] = recording_url
    if live_transcript is not None:
        row["live_transcript"] = live_transcript
    if transcript_json is not None:
        row["transcript_json"] = transcript_json
    if (live_transcript or "").strip() or (transcript_json or "").strip() not in ("", "[]"):
        row["transcript_status"] = "saved"
        row["transcript_source"] = "gemini_live"
        row["last_transcript_error"] = None
    if cb.get("scheduled_at"):
        row["scheduled_at"] = cb["scheduled_at"]
    if cb.get("callback_urgency"):
        row["callback_urgency"] = cb["callback_urgency"]

    # Mirror into property_data so webhook / Node can also read from that object.
    if cb.get("scheduled_at") or cb.get("callback_urgency"):
        try:
            props = parse_property_data((existing or {}).get("property_data") if existing else None)
            if cb.get("scheduled_at"):
                props["scheduled_at"] = cb["scheduled_at"]
            if cb.get("callback_urgency"):
                props["callback_urgency"] = cb["callback_urgency"]
            row["property_data"] = json.dumps(props, ensure_ascii=False)
        except Exception:
            pass

    # Always attach token estimates at finalize so Call Logs never stays blank.
    tin, tout, total = estimate_gemini_tokens(
        transcript_json=transcript_json,
        live_transcript=live_transcript,
        duration_seconds=duration_seconds,
    )
    if total <= 0 and call_id:
        try:
            existing = await get_call_by_id(call_id)
            if existing:
                tin, tout, total = estimate_gemini_tokens(
                    transcript_json=transcript_json or existing.get("transcript_json"),
                    live_transcript=live_transcript or existing.get("live_transcript"),
                    duration_seconds=duration_seconds or int(existing.get("duration_seconds") or 0),
                )
        except Exception:
            pass
    if total > 0:
        row["gemini_tokens_in"] = tin
        row["gemini_tokens_out"] = tout
        row["gemini_tokens_total"] = total

    try:
        await db.table("call_logs").update(row).eq("id", call_id).execute()
    except Exception as exc:
        # Retry without optional columns if migration not applied yet.
        for key in (
            "gemini_tokens_in", "gemini_tokens_out", "gemini_tokens_total", "sentiment",
            "scheduled_at", "callback_urgency", "transcript_status",
            "transcript_source", "last_transcript_error",
        ):
            row.pop(key, None)
        print(f"WARN: finalize_call optional columns failed ({exc}); retrying without them")
        await db.table("call_logs").update(row).eq("id", call_id).execute()
    if row.get("transcript_json") or row.get("live_transcript"):
        try:
            await complete_queued_transcript_write(call_id)
        except Exception:
            pass
    invalidate_read_cache("stats", "contacts")
    if release_caller_id:
        try:
            await release_number(call_id=call_id)
        except Exception:
            pass
    if notify_webhook:
        from call_webhook import schedule_call_completed_webhook
        schedule_call_completed_webhook(call_id, room_name=room_name)
    from whatsapp import schedule_whatsapp_followup
    schedule_whatsapp_followup(call_id)
    from email_followup import schedule_email_followup
    schedule_email_followup(call_id)


async def update_final_transcript(call_id: str, final_transcript: str) -> None:
    db = await _adb()
    await db.table("call_logs").update({"final_transcript": final_transcript}).eq("id", call_id).execute()


async def get_call_by_id(call_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("id", call_id).maybe_single().execute()
    return result.data if result else None


def _parse_call_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse call timestamps. Naive values are treated as UTC (legacy rows)."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None


def _wall_duration_seconds(started: Optional[datetime]) -> int:
    """UTC-normalized wall duration, clamped to avoid TZ skew artifacts (e.g. 330:16)."""
    if not started:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    try:
        started_utc = started.astimezone(timezone.utc)
    except Exception:
        return 0
    return max(0, min(3600, int((now - started_utc).total_seconds())))


def _active_call_max_age_minutes() -> int:
    return max(5, int(os.getenv("ACTIVE_CALL_MAX_AGE_MINUTES", "75")))


def _active_call_idle_minutes() -> int:
    return max(2, int(os.getenv("ACTIVE_CALL_IDLE_MINUTES", "5")))


def _ringing_max_seconds() -> int:
    """How long a call may stay in ringing before we treat it as stale."""
    ring = max(15, int(os.getenv("RINGING_TIMEOUT_SEC", "30")))
    # Worker may accept a job up to DISPATCH_MAX_AGE_SEC late, then ring for `ring`.
    dispatch_max = max(30, int(float(os.getenv("DISPATCH_MAX_AGE_SEC", "45"))))
    return max(90, dispatch_max + ring + 15)


_active_reconcile_at = 0.0
_ACTIVE_RECONCILE_INTERVAL_SEC = 30.0


async def _expire_active_call(
    row: dict,
    reason: str,
    *,
    release_caller_id: bool = False,
    notify_webhook: bool = False,
) -> None:
    """Mark orphaned active rows completed for UI.

    Default: do NOT release DID or webhook. Releasing a number / notifying Node
    without LiveKit SIP confirmation can start the next dial while audio is live.
    """
    call_id = row.get("id")
    if not call_id:
        return
    started = _parse_call_timestamp(row.get("timestamp"))
    wall_duration = _wall_duration_seconds(started)
    est = _estimate_duration_from_row(row)

    had_talk = _call_had_conversation(row)
    status = (row.get("status") or "").lower()
    if had_talk or status == "in_progress":
        from client_requirements import infer_hangup_outcome

        intent_outcome = infer_hangup_outcome(
            live_transcript=row.get("live_transcript"),
            transcript_json=row.get("transcript_json"),
            property_data=row.get("property_data"),
        )
        if intent_outcome:
            outcome = intent_outcome
        else:
            outcome = "disconnect_in_between"
        est = _estimate_duration_from_row(row)
        duration = est if est > 0 else min(wall_duration, 3600)
    else:
        outcome = "failed"
        # Prefer transcript estimate; never send raw skewed wall clock to Node.
        duration = est if est > 0 else wall_duration

    try:
        await finalize_call(
            call_id,
            outcome=outcome,
            reason=(
                f"{reason} (intent={outcome})"
                if outcome in ("interested", "not_interested", "do_not_call")
                else reason
            ),
            duration_seconds=duration,
            live_transcript=row.get("live_transcript"),
            transcript_json=row.get("transcript_json"),
            notify_webhook=notify_webhook,
            release_caller_id=release_caller_id,
        )
    except Exception:
        pass


async def _livekit_confirm_room_clear(row: dict) -> Optional[bool]:
    """True if room/SIP clear, False if still live, None if cannot check."""
    # Active rows keep the LiveKit room name in ``reason`` (set by create_active_call).
    room_name = (row.get("reason") or "").strip()
    if not re.match(r"^(call|batch|camp)-[\w-]+$", room_name):
        return None
    phone = (row.get("phone_number") or "").strip()
    sip_identity = f"sip_{phone}" if phone else None
    try:
        from sip_hangup import force_delete_room_and_confirm, livekit_api_from_env

        lk = await livekit_api_from_env()
        if not lk:
            return None
        try:
            return await force_delete_room_and_confirm(
                lk,
                room_name=room_name,
                sip_identity=sip_identity,
                timeout_sec=5.0,
            )
        finally:
            try:
                await lk.aclose()
            except Exception:
                pass
    except Exception:
        return None


async def get_active_call_for_phone(
    phone: str,
    *,
    organization_id: Optional[str] = None,
) -> Optional[dict]:
    """Return the newest ringing/in_progress/queued row for this phone, if any."""
    phone = (phone or "").strip()
    if not phone:
        return None
    await _reconcile_stale_active_calls()
    rows = await _find_active_calls_for_phone(phone, organization_id=organization_id)
    return rows[0] if rows else None


async def _reconcile_stale_active_calls() -> None:
    """Close orphaned ringing / no-talk rows. Never unlock a live talking call
    without LiveKit SIP confirmation.
    """
    global _active_reconcile_at
    now_mono = time.time()
    if now_mono - _active_reconcile_at < _ACTIVE_RECONCILE_INTERVAL_SEC:
        return
    _active_reconcile_at = now_mono

    db = await _adb()
    result = (
        await db.table("call_logs")
        .select(
            "id, phone_number, timestamp, live_transcript, transcript_json, "
            "status, property_data, reason"
        )
        .in_("status", list(_ACTIVE_CALL_STATUSES))
        .order("timestamp", desc=True)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return

    max_age = timedelta(minutes=_active_call_max_age_minutes())
    idle_limit = timedelta(minutes=_active_call_idle_minutes())
    ringing_limit = timedelta(seconds=_ringing_max_seconds())

    keep_newest_by_phone: dict[str, dict] = {}
    for row in rows:
        phone = (row.get("phone_number") or "").strip()
        if phone and phone not in keep_newest_by_phone:
            keep_newest_by_phone[phone] = row

    for row in rows:
        call_id = row.get("id")
        started = _parse_call_timestamp(row.get("timestamp"))
        if not call_id or not started:
            continue

        now = datetime.now(timezone.utc)
        started_utc = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
        age = now - started_utc.astimezone(timezone.utc)
        status = (row.get("status") or "").strip().lower()
        phone = (row.get("phone_number") or "").strip()
        duplicate = phone and keep_newest_by_phone.get(phone, {}).get("id") != call_id
        had_talk = _call_had_conversation(row)
        last_activity = _last_transcript_activity_at(row) or started_utc
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)
        idle_for = now - last_activity.astimezone(timezone.utc)

        # Older duplicates: close UI row without releasing DID (lease belongs to newest).
        if duplicate:
            await _expire_active_call(
                row,
                "stale - superseded by newer call",
                release_caller_id=False,
                notify_webhook=False,
            )
            continue

        # Ringing orphans / no-conversation idle — safe to close without SIP audio.
        if status == "ringing" and age > ringing_limit:
            # Never answered and past dispatch + ring windows (the worker refuses
            # late jobs), so the line is free — tell Node instead of leaving it waiting.
            await _expire_active_call(
                row,
                "stale - ringing timeout",
                release_caller_id=True,
                notify_webhook=True,
            )
            continue
        if status == "in_progress" and not had_talk and idle_for > idle_limit:
            await _expire_active_call(
                row,
                "stale - no live activity",
                release_caller_id=True,
                notify_webhook=False,
            )
            continue

        # Talking / live in_progress: never expire on timer alone. Inside the max
        # age window a quiet transcript is not proof the line is down (transcript
        # writes can stall), so never force-delete a room here.
        if status == "in_progress" and (had_talk or age <= max_age):
            if age <= max_age:
                continue
            # Past safety window — only unlock after LiveKit confirms SIP gone.
            cleared = await _livekit_confirm_room_clear(row)
            if cleared is True:
                await _expire_active_call(
                    row,
                    "stale - exceeded active window (livekit confirmed clear)",
                    release_caller_id=True,
                    notify_webhook=False,
                )
            # cleared False/None → keep locked; do not release DID or mark completed.
            continue

        if age > max_age:
            cleared = await _livekit_confirm_room_clear(row)
            if cleared is True:
                await _expire_active_call(
                    row,
                    "stale - exceeded active window (livekit confirmed clear)",
                    release_caller_id=True,
                    notify_webhook=False,
                )


async def run_locked_active_call_reconcile(owner: str) -> bool:
    """Scheduled sweep (one replica at a time) so stuck rows close without API reads."""
    if not await try_acquire_job_lock("active_call_reconcile", owner, ttl_sec=50):
        return False
    try:
        await _reconcile_stale_active_calls()
        return True
    finally:
        await release_job_lock("active_call_reconcile", owner)


async def get_active_calls(
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> list:
    await _reconcile_stale_active_calls()

    db = await _adb()
    cols = _CALL_ACTIVE_COLUMNS if await ensure_gemini_token_columns() else _CALL_ACTIVE_COLUMNS_LEGACY
    query = (
        db.table("call_logs")
        .select(cols)
        .in_("status", list(_ACTIVE_CALL_STATUSES))
        .order("timestamp", desc=True)
    )
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    try:
        result = await query.execute()
    except Exception as exc:
        if "gemini_" in str(exc).lower():
            global _gemini_columns_ready
            _gemini_columns_ready = False
            query = (
                db.table("call_logs")
                .select(_CALL_ACTIVE_COLUMNS_LEGACY)
                .in_("status", list(_ACTIVE_CALL_STATUSES))
                .order("timestamp", desc=True)
            )
            if organization_id:
                query = query.eq("organization_id", organization_id)
            if lead_id:
                query = query.eq("lead_id", lead_id)
            result = await query.execute()
        else:
            raise
    rows = result.data or []

    max_age = timedelta(minutes=_active_call_max_age_minutes())
    idle_limit = timedelta(minutes=_active_call_idle_minutes())
    ringing_limit = timedelta(seconds=_ringing_max_seconds())

    live_rows: list[dict] = []
    seen_phones: set[str] = set()
    for row in rows:
        started = _parse_call_timestamp(row.get("timestamp"))
        if not started:
            continue
        now = datetime.now(started.tzinfo)
        age = now - started
        status = (row.get("status") or "").strip().lower()
        transcript = (row.get("live_transcript") or "").strip()
        had_talk = _call_had_conversation(row)
        # Keep connected calls visible until agent finalizes (even past max_age).
        if status == "in_progress" and had_talk:
            phone = (row.get("phone_number") or "").strip()
            if phone:
                if phone in seen_phones:
                    continue
                seen_phones.add(phone)
            live_rows.append(row)
            continue
        if age > max_age:
            continue
        # Ringing has no transcript yet — keep visible until ring window ends.
        if status == "ringing":
            if age > ringing_limit:
                continue
        elif not transcript and age > idle_limit:
            continue
        phone = (row.get("phone_number") or "").strip()
        if phone:
            if phone in seen_phones:
                continue
            seen_phones.add(phone)
        live_rows.append(row)
    return live_rows


async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    call_id: Optional[str] = None, live_transcript: Optional[str] = None,
    transcript_json: Optional[str] = None,
    room_name: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    callback_urgency: Optional[str] = None,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    if call_id:
        # Persist notes before finalize so the single webhook payload includes them.
        if notes:
            await update_call_notes(call_id, notes)
        await finalize_call(
            call_id,
            outcome=outcome,
            reason=reason,
            duration_seconds=duration_seconds,
            recording_url=recording_url,
            live_transcript=live_transcript,
            transcript_json=transcript_json,
            room_name=room_name,
            scheduled_at=scheduled_at,
            callback_urgency=callback_urgency,
        )
        return call_id

    db = await _adb()
    new_id = str(uuid.uuid4())
    row: dict = {
        "id": new_id, "phone_number": phone_number, "lead_name": lead_name,
        "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds,
        "timestamp": _utc_now_iso(), "status": "completed",
        "sentiment": sentiment_from_outcome(outcome),
    }
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    if live_transcript:
        row["live_transcript"] = live_transcript
    if transcript_json:
        row["transcript_json"] = transcript_json
    if organization_id:
        row["organization_id"] = organization_id
    if lead_id:
        row["lead_id"] = lead_id
    if project_id:
        row["project_id"] = project_id
    tin, tout, total = estimate_gemini_tokens(
        transcript_json=transcript_json,
        live_transcript=live_transcript,
        duration_seconds=duration_seconds,
    )
    if total > 0:
        row["gemini_tokens_in"] = tin
        row["gemini_tokens_out"] = tout
        row["gemini_tokens_total"] = total
    try:
        await db.table("call_logs").insert(row).execute()
    except Exception as exc:
        for key in ("gemini_tokens_in", "gemini_tokens_out", "gemini_tokens_total", "sentiment"):
            row.pop(key, None)
        print(f"WARN: log_call optional columns failed ({exc}); retrying without them")
        await db.table("call_logs").insert(row).execute()
    invalidate_read_cache("stats", "contacts")
    from call_webhook import schedule_call_completed_webhook
    schedule_call_completed_webhook(new_id, room_name=room_name)
    return new_id


async def get_all_calls(
    page: int = 1,
    limit: int = 20,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    cols = _CALL_LIST_COLUMNS if await ensure_gemini_token_columns() else _CALL_LIST_COLUMNS_LEGACY
    query = db.table("call_logs").select(cols)
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    try:
        result = (
            await query.order("timestamp", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        if "gemini_" in str(exc).lower():
            global _gemini_columns_ready
            _gemini_columns_ready = False
            query = db.table("call_logs").select(_CALL_LIST_COLUMNS_LEGACY)
            if organization_id:
                query = query.eq("organization_id", organization_id)
            if lead_id:
                query = query.eq("lead_id", lead_id)
            result = (
                await query.order("timestamp", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            return result.data or []
        raise


async def get_org_call_usage(
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    limit: int = 5000,
) -> list:
    """Duration rows for billing (org-scoped when organization_id is set)."""
    db = await _adb()
    query = (
        db.table("call_logs")
        .select("id, phone_number, duration_seconds, organization_id, outcome, timestamp")
        .order("timestamp", desc=True)
        .limit(max(1, min(int(limit), 10000)))
    )
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    result = await query.execute()
    return result.data or []


async def get_calls_by_phone(
    phone: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> list:
    db = await _adb()
    query = db.table("call_logs").select("*").eq("phone_number", phone)
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    result = await query.order("timestamp", desc=True).execute()
    return result.data or []


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts(
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    last_outcome: Optional[str] = None,
    phone: Optional[str] = None,
) -> list:
    cache_key = f"contacts:{organization_id or ''}:{lead_id or ''}:{last_outcome or ''}:{phone or ''}"

    async def _load() -> list:
        db = await _adb()
        query = db.table("call_logs").select(
            "id, phone_number, lead_name, outcome, reason, timestamp, "
            "organization_id, lead_id, duration_seconds"
        )
        if organization_id:
            query = query.eq("organization_id", organization_id)
        if lead_id:
            query = query.eq("lead_id", lead_id)
        if phone:
            query = query.eq("phone_number", phone)
        result = (
            await query.order("timestamp", desc=True)
            .limit(_CONTACTS_ROW_LIMIT)
            .execute()
        )
        rows = result.data or []
        contacts: dict = {}
        for row in rows:
            phone_number = row["phone_number"]
            if phone_number not in contacts:
                contacts[phone_number] = {
                    "phone_number": phone_number,
                    "lead_name": row.get("lead_name"),
                    "organization_id": row.get("organization_id"),
                    "lead_id": row.get("lead_id"),
                    "total_calls": 0,
                    "booked": 0,
                    "last_call": row["timestamp"],
                    "last_call_id": row.get("id"),
                    "last_outcome": row.get("outcome"),
                    "last_reason": row.get("reason"),
                    "last_duration_seconds": row.get("duration_seconds") or 0,
                }
            contacts[phone_number]["total_calls"] += 1
            if row.get("outcome") == "booked":
                contacts[phone_number]["booked"] += 1
        items = sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)
        if last_outcome:
            want = last_outcome.strip().lower()
            items = [c for c in items if (c.get("last_outcome") or "").lower() == want]
        return items

    return await _cached(cache_key, _CACHE_TTL["contacts"], _load)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    async def _load() -> dict:
        db = await _adb()
        rows = (
            await db.table("call_logs")
            .select("outcome, duration_seconds, timestamp")
            .order("timestamp", desc=True)
            .limit(_STATS_ROW_LIMIT)
            .execute()
        ).data or []
        total_calls    = len(rows)
        booked         = sum(1 for r in rows if r.get("outcome") == "booked")
        not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
        durations      = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
        avg_dur        = sum(durations) / len(durations) if durations else 0
        booking_rate   = round((booked / total_calls * 100) if total_calls else 0, 1)
        outcomes: dict = {}
        for r in rows:
            o = r.get("outcome") or "unknown"
            outcomes[o] = outcomes.get(o, 0) + 1
        daily: dict = defaultdict(int)
        for r in rows:
            ts = (r.get("timestamp") or "")[:10]
            if ts:
                daily[ts] += 1
        today = datetime.now().date()
        timeline = [{"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]
        dur_sum: dict = defaultdict(float)
        dur_cnt: dict = defaultdict(int)
        for r in rows:
            o = r.get("outcome") or "unknown"
            sec = r.get("duration_seconds")
            if sec:
                dur_sum[o] += sec
                dur_cnt[o] += 1
        duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}
        return {
            "total_calls": total_calls, "booked": booked, "not_interested": not_interested,
            "avg_duration_seconds": round(avg_dur, 1), "booking_rate_percent": booking_rate,
            "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
        }

    return await _cached("stats", _CACHE_TTL["stats"], _load)


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 0,
    max_parallel_calls: int = 5,
    queue_advance: bool = True,
    system_prompt: Optional[str] = None,
    additional_instructions: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row: dict = {
        "id": campaign_id, "name": name, "status": "active",
        "contacts_json": contacts_json, "schedule_type": schedule_type,
        "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
        "max_parallel_calls": max(1, min(20, max_parallel_calls)),
        "queue_advance": bool(queue_advance),
        "created_at": datetime.now().isoformat(), "total_dispatched": 0, "total_failed": 0,
    }
    if system_prompt:
        row["system_prompt"] = system_prompt
    if additional_instructions:
        row["additional_instructions"] = additional_instructions
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    try:
        await db.table("campaigns").insert(row).execute()
    except Exception as exc:
        # Older DBs without newer columns — retry without optional fields.
        err = str(exc).lower()
        if "column" in err or "additional_instructions" in err or "queue_advance" in err:
            row.pop("queue_advance", None)
            row.pop("additional_instructions", None)
            await db.table("campaigns").insert(row).execute()
        else:
            raise
    return campaign_id


async def get_all_campaigns() -> list:
    db = await _adb()
    result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return result.data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def get_running_campaign() -> Optional[dict]:
    """Return the campaign currently marked ``running``, if any."""
    db = await _adb()
    result = await (
        db.table("campaigns").select("*")
        .eq("status", "running")
        .order("last_run_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


async def get_campaign_run_lock_owner() -> Optional[str]:
    """Return campaign_id holding the global campaign-run job lock, if any."""
    try:
        db = await _adb()
        key = _job_lock_key("campaign_run")
        row = await db.table("settings").select("value, updated_at").eq("key", key).maybe_single().execute()
        if not row or not row.data:
            return None
        val = str(row.data.get("value") or "")
        owner = val.split("|", 1)[0].strip() if val else ""
        return owner or None
    except Exception:
        return None


async def clear_stale_running_campaigns() -> int:
    """Reset ``running`` campaigns whose job lock is gone (crash / restart)."""
    owner = await get_campaign_run_lock_owner()
    db = await _adb()
    result = await db.table("campaigns").select("id, schedule_type").eq("status", "running").execute()
    cleared = 0
    for row in result.data or []:
        if owner and owner == row.get("id"):
            continue
        await (
            db.table("campaigns").update({"status": "active"})
            .eq("id", row["id"])
            .eq("status", "running")
            .execute()
        )
        cleared += 1
    return cleared


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    """Increment run totals. Mark completed only for one-shot campaigns."""
    db = await _adb()
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    new_dispatched = int(campaign.get("total_dispatched") or 0) + max(0, int(dispatched or 0))
    new_failed = int(campaign.get("total_failed") or 0) + max(0, int(failed or 0))
    row: dict = {
        "last_run_at": datetime.now().isoformat(),
        "total_dispatched": new_dispatched,
        "total_failed": new_failed,
    }
    # Recurring schedules stay active so cron keeps firing.
    if (campaign.get("schedule_type") or "once") == "once":
        row["status"] = "completed"
    await db.table("campaigns").update(row).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


# ── Call property data (per call_id) ───────────────────────────────────────────

# Longer prefixes first — matching uses startswith.
_PROPERTY_FIELD_PREFIXES = (
    ("budget", "budget"),
    ("city/area", "city"),
    ("city", "city"),
    ("preferred location", "location_preference"),
    ("location preference", "location_preference"),
    ("location fit", "location_fit"),
    ("location suit", "location_fit"),
    ("property type", "property_type"),
    ("bhk", "bhk"),
    ("whatsapp number", "whatsapp_number"),
    ("whatsapp", "whatsapp_number"),
    ("preferred project", "preferred_project"),
    ("possession timeline", "possession_timeline"),
    ("parking spaces", "parking_spaces"),
    ("hometown", "hometown"),
    ("requirements", "requirements"),
    ("customer intent", "customer_intent"),
)

_PROPERTY_DATA_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_CONTACT_MEMORY_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def parse_property_data(raw) -> dict:
    """Parse call_logs.property_data from JSON string or dict."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _parse_money_token(token: str) -> Optional[int]:
    """Parse one amount token like '3', '3.5', '80L', '1Cr' into INR integer."""
    import re
    t = (token or "").strip().lower().replace(",", "")
    if not t:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(cr|crore|crs|lakh|lakhs|lacs|lac|l)?$", t)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit.startswith("cr"):
        return int(num * 10_000_000)
    if unit.startswith("l"):
        return int(num * 100_000)
    # Bare number next to a shared unit is handled by parse_budget_range.
    return int(num) if num >= 1000 else None


def parse_budget_range(budget_text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse '3-4 Lakhs' / '50-70 lakh' / '1 Cr' / '3 से 4 लाख' → (min_inr, max_inr)."""
    import re
    raw = (budget_text or "").strip()
    if not raw:
        return None, None
    # Ignore yes/no fit answers from project-mode Q2.
    if raw.lower() in {"yes", "no", "haan", "ha", "nahi", "na"}:
        return None, None

    # Normalize Hindi / casual separators before English regexes.
    lower = raw.lower().replace(",", "")
    for src, dst in (
        ("लाख", "lakh"),
        ("लाखों", "lakh"),
        ("करोड़", "cr"),
        ("करोड", "cr"),
        ("से", "-"),
        (" se ", "-"),
        (" tak ", "-"),
        ("–", "-"),
        ("—", "-"),
        (" to ", "-"),
    ):
        lower = lower.replace(src, dst)

    # Shared unit after a range: "3-4 lakhs", "50-70 lakh"
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(cr|crore|crs|lakh|lakhs|lacs|lac|l)\b",
        lower,
    )
    if m:
        a, b, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        mul = 10_000_000 if unit.startswith("cr") else 100_000
        lo, hi = int(a * mul), int(b * mul)
        return (min(lo, hi), max(lo, hi))

    # Two explicit amounts: "50L-70L", "1Cr-1.5Cr"
    m2 = re.search(
        r"(\d+(?:\.\d+)?\s*(?:cr|crore|crs|lakh|lakhs|lacs|lac|l)?)\s*-\s*"
        r"(\d+(?:\.\d+)?\s*(?:cr|crore|crs|lakh|lakhs|lacs|lac|l)?)",
        lower,
    )
    if m2:
        lo = _parse_money_token(m2.group(1))
        hi = _parse_money_token(m2.group(2))
        if lo is None and hi is not None and re.match(r"^\d", m2.group(1).strip()):
            unit_m = re.search(r"(cr|lakh|lac|l)\b", m2.group(2))
            if unit_m:
                lo = _parse_money_token(m2.group(1).strip() + unit_m.group(1))
        if hi is None and lo is not None and re.match(r"^\d", m2.group(2).strip()):
            unit_m = re.search(r"(cr|lakh|lac|l)\b", m2.group(1))
            if unit_m:
                hi = _parse_money_token(m2.group(2).strip() + unit_m.group(1))
        if lo is not None and hi is not None:
            return (min(lo, hi), max(lo, hi))
        if lo is not None:
            return lo, lo
        if hi is not None:
            return hi, hi

    single = _parse_money_token(lower)
    if single is None:
        m3 = re.search(r"(\d+(?:\.\d+)?)\s*(cr|crore|crs|lakh|lakhs|lacs|lac|l)\b", lower)
        if m3:
            single = _parse_money_token(m3.group(1) + m3.group(2))
    if single is not None:
        return single, single
    return None, None


def enrich_property_data_for_webhook(raw) -> dict:
    """Normalize property_data for Node lead UI (location + numeric budget)."""
    data = parse_property_data(raw)
    if not data:
        return {}
    out = dict(data)

    city = (out.get("city") or "").strip()
    loc_pref = (out.get("location_preference") or "").strip()
    loc_fit = (out.get("location_fit") or "").strip()
    if loc_fit and loc_fit.lower() in {"yes", "no", "haan", "ha", "nahi", "na"}:
        loc_fit = ""

    # preferred_location = best human-readable place string for Node lead cards.
    preferred = city or loc_pref or loc_fit or (out.get("preferred_location") or "").strip()
    if preferred:
        out["preferred_location"] = preferred
    # Do NOT invent city from soft preferences like "less pollution".

    min_b, max_b = parse_budget_range(str(out.get("budget") or ""))
    if min_b is not None:
        out["min_budget"] = min_b
    if max_b is not None:
        out["max_budget"] = max_b
    return out


def parse_property_insight(insight: str) -> Optional[tuple[str, str]]:
    """Map remember_details insight like 'Budget: 50L' → ('budget', '50L')."""
    if not insight or ":" not in insight:
        return None
    key, _, value = insight.partition(":")
    key_norm = key.strip().lower()
    value = value.strip()[:500]
    if not value:
        return None
    for prefix, field in _PROPERTY_FIELD_PREFIXES:
        if key_norm == prefix or key_norm.startswith(prefix):
            return field, value
    return None


async def update_call_property_fields(call_id: str, updates: dict[str, Any]) -> bool:
    """Atomically merge CRM fields into property_data within this worker process."""
    cleaned = {
        str(field).strip(): str(value).strip()[:500]
        for field, value in (updates or {}).items()
        if str(field).strip() and value is not None and str(value).strip()
    }
    if not call_id or not cleaned:
        return False
    lock_key = (id(asyncio.get_running_loop()), call_id)
    lock = _PROPERTY_DATA_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        db = await _adb()
        try:
            result = await (
                db.table("call_logs")
                .select("property_data")
                .eq("id", call_id)
                .maybe_single()
                .execute()
            )
            if not result or not result.data:
                return False
            data = parse_property_data(result.data.get("property_data"))
            if "requirements" in cleaned and data.get("requirements"):
                prior = [x.strip() for x in str(data["requirements"]).split(",") if x.strip()]
                newer = [x.strip() for x in cleaned["requirements"].split(",") if x.strip()]
                cleaned["requirements"] = ", ".join(dict.fromkeys(prior + newer))
            data.update(cleaned)
            await (
                db.table("call_logs")
                .update({"property_data": json.dumps(data, ensure_ascii=False)})
                .eq("id", call_id)
                .execute()
            )
            invalidate_read_cache("contacts")
            return True
        except Exception as exc:
            print(f"WARN: update_call_property_fields failed for call_id={call_id}: {exc}")
            return False


async def update_call_property_data(call_id: str, field: str, value: str) -> bool:
    """Backward-compatible single-field property_data merge."""
    return await update_call_property_fields(call_id, {field: value})


# ── Contact Memory ────────────────────────────────────────────────────────────

async def add_contact_memory(
    phone: str,
    insight: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> None:
    parsed = parse_property_insight(insight)
    if parsed and organization_id and lead_id:
        field, value = parsed
        await upsert_contact_requirements(
            phone,
            {field: value},
            organization_id=organization_id,
            lead_id=lead_id,
        )
        return

    db = await _adb()
    row = {
        "id": str(uuid.uuid4()),
        "phone_number": phone,
        "insight": insight[:1000],
        "created_at": datetime.now().isoformat(),
    }
    if organization_id:
        row["organization_id"] = organization_id
    if lead_id:
        row["lead_id"] = lead_id
    if parsed:
        row["memory_key"] = parsed[0]
    await db.table("contact_memory").insert(row).execute()


_CONTACT_MEMORY_LABELS = {
    "budget": "Budget",
    "budget_flexibility": "Budget flexibility",
    "city": "City/Area",
    "location_preference": "Location preference",
    "bhk": "BHK",
    "property_type": "Property type",
    "possession_timeline": "Possession timeline",
    "parking_spaces": "Parking spaces",
    "hometown": "Hometown",
    "requirements": "Requirements",
    "email": "Email",
    "whatsapp_number": "WhatsApp number",
    "preferred_project": "Preferred project",
    "customer_intent": "Customer intent",
}


async def upsert_contact_requirements(
    phone: str,
    updates: dict[str, Any],
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> None:
    """Replace older structured facts while preserving unrelated memory notes."""
    if not phone or not updates:
        return
    normalized = {
        field: str(value).strip()[:500]
        for field, value in updates.items()
        if field in _CONTACT_MEMORY_LABELS and value is not None and str(value).strip()
    }
    if not normalized:
        return
    scope_key = f"{organization_id or ''}:{lead_id or ''}:{phone}"
    lock_key = (id(asyncio.get_running_loop()), scope_key)
    lock = _CONTACT_MEMORY_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        db = await _adb()
        query = db.table("contact_memory").select("id, insight, memory_key").eq(
            "phone_number", phone
        )
        if organization_id:
            query = query.eq("organization_id", organization_id)
        if lead_id:
            query = query.eq("lead_id", lead_id)
        existing = await query.execute()
        replace_labels = {_CONTACT_MEMORY_LABELS[field].casefold() for field in normalized}
        delete_ids = []
        for row in existing.data or []:
            label = str(row.get("insight") or "").partition(":")[0].strip().casefold()
            memory_key = str(row.get("memory_key") or "").strip()
            if (label in replace_labels or memory_key in normalized) and row.get("id"):
                delete_ids.append(row["id"])
        if delete_ids:
            await db.table("contact_memory").delete().in_("id", delete_ids).execute()
        now = datetime.now().isoformat()
        rows = [
            {
                "id": str(uuid.uuid4()),
                "phone_number": phone,
                "insight": f"{_CONTACT_MEMORY_LABELS[field]}: {value}",
                "memory_key": field,
                "created_at": now,
                **({"organization_id": organization_id} if organization_id else {}),
                **({"lead_id": lead_id} if lead_id else {}),
            }
            for field, value in normalized.items()
        ]
        if rows:
            await db.table("contact_memory").insert(rows).execute()


async def get_contact_memory(
    phone: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> list:
    db = await _adb()
    query = db.table("contact_memory").select(
        "insight, created_at, memory_key"
    ).eq("phone_number", phone)
    if organization_id:
        query = query.eq("organization_id", organization_id)
    if lead_id:
        query = query.eq("lead_id", lead_id)
    result = await query.order("created_at", desc=True).limit(20).execute()
    return result.data or []


async def compress_contact_memory(
    phone: str,
    compressed: str,
    *,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> None:
    db = await _adb()
    delete_query = db.table("contact_memory").delete().eq("phone_number", phone)
    if organization_id:
        delete_query = delete_query.eq("organization_id", organization_id)
    if lead_id:
        delete_query = delete_query.eq("lead_id", lead_id)
    await delete_query.execute()
    row = {
        "id": str(uuid.uuid4()),
        "phone_number": phone,
        "insight": compressed[:2000],
        "created_at": datetime.now().isoformat(),
    }
    if organization_id:
        row["organization_id"] = organization_id
    if lead_id:
        row["lead_id"] = lead_id
    await db.table("contact_memory").insert(row).execute()


# ── Organization project scope ────────────────────────────────────────────────

class ProjectScopeError(ValueError):
    """Raised when a call requests a project outside its organization scope."""


def _project_details_for_storage(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return {"items": value}
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    except (json.JSONDecodeError, TypeError):
        pass
    return {"source_text": text}


def _project_details_from_storage(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value.strip()
        value = parsed
    if isinstance(value, dict) and set(value) == {"source_text"}:
        return str(value.get("source_text") or "").strip()
    if isinstance(value, dict):
        if not value:
            return ""
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if isinstance(value, list):
        if not value:
            return ""
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value or "").strip()


def _canonical_project_details(value: Any) -> str:
    """Comparable form of project details: whitespace and JSON key order ignored."""
    stored = _project_details_for_storage(value)
    if not stored:
        return ""
    if set(stored) == {"source_text"}:
        return " ".join(str(stored.get("source_text") or "").split())
    return json.dumps(stored, ensure_ascii=False, sort_keys=True, default=str)


def _project_name_from_details(project_id: str, details: str) -> str:
    import re

    match = re.search(r"(?im)^Project Name:\s*(.+)$", details or "")
    if match:
        return match.group(1).strip()[:300]
    try:
        payload = json.loads(details or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if isinstance(payload, dict):
        name = payload.get("projectName") or payload.get("project_name") or payload.get("name")
        if name:
            return str(name).strip()[:300]
    return project_id[:300]


async def resolve_organization_projects(
    organization_id: str,
    projects: list[dict],
    *,
    register_missing: bool = False,
    payload_is_fresh: bool = True,
) -> list[dict]:
    """Return project details for this call, in request order, for one organization.

    ``payload_is_fresh=False`` is for saved copies (campaign contacts stored at
    campaign creation): Supabase wins when it has details, the saved copy only fills
    gaps, and existing rows are never updated from it — so a recurring campaign
    cannot roll the catalog back to its creation-time project details.

    The call payload is the source of truth: when it carries details (or per-project
    notes) those are used for the call. Supabase is the fallback when the payload
    sends only a ``project_id``.

    API dispatch (``register_missing=True``) also keeps ``organization_projects`` in
    sync: it compares the payload with the stored row and writes only real changes
    (whitespace / JSON key order ignored), creating unseen projects. An empty payload
    never wipes stored data. The LiveKit runtime uses ``register_missing=False``: it
    never writes, and cannot introduce a project the API did not register.
    """
    org = str(organization_id or "").strip()
    if not org:
        raise ProjectScopeError("organization_id is required for project validation")
    if not projects:
        return []

    requested: list[dict] = []
    seen: set[str] = set()
    for idx, project in enumerate(projects):
        project_id = str(
            (project or {}).get("project_id")
            or (project or {}).get("projectId")
            or (project or {}).get("id")
            or ""
        ).strip()
        details = str(
            (project or {}).get("project_details")
            or (project or {}).get("projectDetails")
            or ""
        ).strip()
        if not project_id:
            name = (
                (project or {}).get("project_name")
                or (project or {}).get("projectName")
                or (project or {}).get("name")
            )
            if name:
                project_id = re.sub(r"[^a-zA-Z0-9_-]", "-", str(name).strip().lower()).strip("-")
            elif details:
                import hashlib
                project_id = f"proj-{hashlib.sha256(details.encode('utf-8')).hexdigest()[:10]}"
            else:
                project_id = f"proj-{idx + 1}"
        if project_id in seen:
            continue
        seen.add(project_id)
        requested.append({
            "project_id": project_id,
            "project_details": details,
            "additional_instructions": str(
                (project or {}).get("additional_instructions") or ""
            ).strip(),
        })

    db = await _adb()
    ids = [project["project_id"] for project in requested]
    result = await (
        db.table("organization_projects")
        .select("organization_id, project_id, project_name, project_details, additional_instructions, is_active")
        .eq("organization_id", org)
        .in_("project_id", ids)
        .execute()
    )
    by_id = {str(row.get("project_id")): row for row in result.data or []}

    inactive = [pid for pid, row in by_id.items() if row.get("is_active") is False]
    if inactive:
        raise ProjectScopeError(f"Inactive project(s) for organization: {', '.join(inactive)}")

    missing = [project for project in requested if project["project_id"] not in by_id]
    if missing and not register_missing:
        raise ProjectScopeError(
            "Project(s) are not registered for this organization: "
            + ", ".join(project["project_id"] for project in missing)
        )

    if missing:
        missing_ids = [project["project_id"] for project in missing]
        owners = await (
            db.table("organization_projects")
            .select("organization_id, project_id")
            .in_("project_id", missing_ids)
            .execute()
        )
        conflicts = [
            str(row.get("project_id"))
            for row in owners.data or []
            if str(row.get("organization_id") or "").strip() != org
        ]
        if conflicts:
            raise ProjectScopeError(
                "Project ID already belongs to another organization: "
                + ", ".join(sorted(set(conflicts)))
            )

        now = datetime.now().isoformat()
        rows = []
        for project in missing:
            if not project["project_details"]:
                raise ProjectScopeError(
                    f"Project {project['project_id']} has no verified details to register"
                )
            rows.append({
                "organization_id": org,
                "project_id": project["project_id"],
                "project_name": _project_name_from_details(
                    project["project_id"], project["project_details"]
                ),
                "project_details": _project_details_for_storage(project["project_details"]),
                "additional_instructions": project["additional_instructions"] or None,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            })
        inserted = await db.table("organization_projects").insert(rows).execute()
        for row in inserted.data or rows:
            by_id[str(row.get("project_id"))] = row

    created_ids = {project["project_id"] for project in missing}
    resolved: list[dict] = []
    for requested_project in requested:
        project_id = requested_project["project_id"]
        row = by_id.get(project_id)
        if not row or row.get("is_active") is False:
            raise ProjectScopeError(
                f"Project {project_id} is unavailable for organization {org}"
            )
        db_details = _project_details_from_storage(row.get("project_details"))
        req_details = (requested_project.get("project_details") or "").strip()
        db_notes = str(row.get("additional_instructions") or "").strip()
        req_notes = (requested_project.get("additional_instructions") or "").strip()

        if payload_is_fresh:
            # Payload wins for this call; Supabase fills in only what the payload omits.
            details_out = req_details or db_details
            notes = req_notes or db_notes
        else:
            # Saved copy may be stale: latest Supabase wins, copy only fills gaps.
            details_out = db_details or req_details
            notes = db_notes or req_notes

        if register_missing and payload_is_fresh and project_id not in created_ids:
            changes: dict[str, Any] = {}
            if req_details and _canonical_project_details(req_details) != _canonical_project_details(
                row.get("project_details")
            ):
                changes["project_details"] = _project_details_for_storage(req_details)
                changes["project_name"] = _project_name_from_details(project_id, req_details)
            if req_notes and " ".join(req_notes.split()) != " ".join(db_notes.split()):
                changes["additional_instructions"] = req_notes
            if changes:
                changes["updated_at"] = datetime.now().isoformat()
                try:
                    await (
                        db.table("organization_projects")
                        .update(changes)
                        .eq("organization_id", org)
                        .eq("project_id", project_id)
                        .execute()
                    )
                    fields = sorted(k for k in changes if k not in ("updated_at", "project_name"))
                    await log_error(
                        "projects",
                        f"PROJECT_UPDATED org={org} project={project_id} fields={','.join(fields)}",
                        f"details {len(db_details)}→{len(details_out)} chars; "
                        f"notes {len(db_notes)}→{len(notes)} chars",
                        "info",
                    )
                except Exception as exc:
                    # The call still uses the payload; the catalog catches up next time.
                    await log_error(
                        "projects",
                        f"PROJECT_UPDATE_FAILED org={org} project={project_id}",
                        str(exc)[:500],
                        "warning",
                    )

        resolved.append({
            "project_id": requested_project["project_id"],
            "project_details": details_out,
            "additional_instructions": notes,
        })
    return resolved


async def bind_call_projects(
    call_id: str,
    organization_id: str,
    projects: list[dict],
) -> None:
    """Persist the exact organization-verified catalog allowed for this call."""
    if not call_id or not organization_id or not projects:
        return
    rows = [
        {
            "call_id": call_id,
            "organization_id": organization_id,
            "project_id": str(project.get("project_id") or "").strip(),
        }
        for project in projects
        if str(project.get("project_id") or "").strip()
    ]
    if rows:
        db = await _adb()
        await db.table("call_projects").upsert(
            rows, on_conflict="call_id,project_id"
        ).execute()


# ── Agent Profiles ────────────────────────────────────────────────────────────

async def get_all_agent_profiles() -> list:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").order("created_at").execute()
    return result.data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def create_agent_profile(
    name: str, voice: str = "Leda", model: str = "gemini-3.8-live",
    system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False,
) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({
        "id": profile_id, "name": name, "voice": voice, "model": model,
        "system_prompt": system_prompt, "enabled_tools": enabled_tools,
        "is_default": 1 if is_default else 0, "created_at": datetime.now().isoformat(),
    }).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()


# ── Phone number pool (outbound caller ID) ────────────────────────────────────

class NoNumbersAvailable(Exception):
    """Raised when every pool number is busy or disabled."""

    def __init__(self, message: str = "No outbound phone numbers available"):
        super().__init__(message)


def _phone_pool_busy_max_minutes() -> int:
    return max(5, int(os.getenv("PHONE_NUMBER_BUSY_MAX_MINUTES", "75")))


def _job_lock_key(name: str) -> str:
    return f"job_lock:{name}"


async def try_acquire_job_lock(name: str, owner: str, ttl_sec: int = 120) -> bool:
    """DB-backed lease so only one ACA replica runs a scheduled job."""
    db = await _adb()
    key = _job_lock_key(name)
    now = datetime.now()
    now_iso = now.isoformat()
    value = f"{owner}|{now_iso}"
    existing = await db.table("settings").select("key, value, updated_at").eq("key", key).maybe_single().execute()
    if existing and existing.data:
        try:
            raw_ts = str(existing.data.get("updated_at") or "").replace("Z", "+00:00")
            updated = datetime.fromisoformat(raw_ts)
            if updated.tzinfo is not None:
                updated = updated.replace(tzinfo=None)
            age = (now - updated).total_seconds()
            if age < max(15, int(ttl_sec)):
                return False
        except Exception:
            pass
        await db.table("settings").update({"value": value, "updated_at": now_iso}).eq("key", key).execute()
        return True
    try:
        await db.table("settings").insert({
            "key": key,
            "value": value,
            "updated_at": now_iso,
        }).execute()
        return True
    except Exception as exc:
        if _is_unique_violation(exc):
            return False
        raise


async def release_job_lock(name: str, owner: str) -> None:
    try:
        db = await _adb()
        key = _job_lock_key(name)
        row = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        if row and row.data:
            val = str(row.data.get("value") or "")
            if val.startswith(f"{owner}|") or not val:
                await db.table("settings").delete().eq("key", key).execute()
    except Exception:
        pass


async def list_phone_numbers(organization_id: Optional[str] = None) -> list[dict]:
    db = await _adb()
    query = db.table("phone_numbers").select("*").order("status").order("e164")
    org = (organization_id or "").strip() or None
    result = await query.execute()
    rows = result.data or []
    if not org:
        return rows
    return [
        r for r in rows
        if (r.get("organization_id") or "").strip() in ("", org)
    ]


async def phone_pool_status(organization_id: Optional[str] = None) -> dict:
    rows = await list_phone_numbers(organization_id=organization_id)
    free = sum(1 for r in rows if r.get("status") == "free")
    busy = sum(1 for r in rows if r.get("status") == "busy")
    disabled = sum(1 for r in rows if r.get("status") == "disabled")
    return {
        "total": len(rows),
        "free": free,
        "busy": busy,
        "disabled": disabled,
        "organization_id": (organization_id or "").strip() or None,
        "numbers": rows,
    }


def _is_shared_org(org_val: Optional[str]) -> bool:
    return not (org_val or "").strip()


async def count_pool_capacity(organization_id: Optional[str] = None) -> int:
    """Numbers that can be leased now or are currently leased (excludes disabled)."""
    db = await _adb()
    result = await (
        db.table("phone_numbers").select("e164, status, organization_id")
        .in_("status", ["free", "busy"])
        .execute()
    )
    rows = result.data or []
    org = (organization_id or "").strip() or None
    if not org:
        return len([r for r in rows if _is_shared_org(r.get("organization_id"))])
    return len([
        r for r in rows
        if (r.get("organization_id") or "").strip() in ("", org)
    ])


async def list_active_pool_e164() -> list[str]:
    """E.164 values that should be registered on the LiveKit outbound trunk."""
    db = await _adb()
    result = await (
        db.table("phone_numbers").select("e164")
        .neq("status", "disabled")
        .execute()
    )
    return [str(r["e164"]) for r in (result.data or []) if r.get("e164")]


async def set_phone_number_organization(
    e164: str,
    organization_id: Optional[str],
) -> Optional[dict]:
    """Map a pool number to an org (null/empty = shared across orgs)."""
    e164 = (e164 or "").strip()
    if not e164:
        raise ValueError("e164 is required")
    org = (organization_id or "").strip() or None
    db = await _adb()
    existing = await (
        db.table("phone_numbers").select("e164")
        .eq("e164", e164).maybe_single().execute()
    )
    if not existing or not existing.data:
        return None
    upd = await (
        db.table("phone_numbers")
        .update({"organization_id": org})
        .eq("e164", e164)
        .execute()
    )
    rows = upd.data or []
    return rows[0] if rows else {"e164": e164, "organization_id": org}


async def acquire_specific_number(
    call_id: str,
    e164: str,
    *,
    room_name: Optional[str] = None,
    assigned_agent: Optional[str] = None,
) -> str:
    """Lease a specific pool number for this call (user-assigned caller ID)."""
    if not call_id:
        raise ValueError("call_id is required to acquire a phone number")
    target = (e164 or "").strip()
    if not target:
        raise ValueError("e164 is required")
    db = await _adb()
    now_iso = datetime.now().isoformat()

    existing = await (
        db.table("phone_numbers").select("e164, status, current_call_id")
        .eq("e164", target)
        .maybe_single()
        .execute()
    )
    if not existing or not existing.data:
        raise NoNumbersAvailable(f"Calling number {target} is not in the phone pool")

    row = existing.data
    if row.get("status") == "disabled":
        raise NoNumbersAvailable(f"Calling number {target} is disabled")
    if row.get("status") == "busy" and (row.get("current_call_id") or "") == call_id:
        return target
    if row.get("status") == "busy":
        raise NoNumbersAvailable(f"Calling number {target} is currently busy on another call")

    upd = await (
        db.table("phone_numbers").update({
            "status": "busy",
            "current_call_id": call_id,
            "current_room_name": room_name,
            "assigned_agent": assigned_agent,
            "last_used_at": now_iso,
        })
        .eq("e164", target)
        .eq("status", "free")
        .execute()
    )
    if upd.data:
        try:
            from number_status import publish_number_status
            await publish_number_status(
                e164=target,
                status="busy",
                previous_status="free",
                call_id=call_id,
                room_name=room_name,
                reason="leased",
            )
        except Exception:
            pass
        return target
    raise NoNumbersAvailable(f"Could not lease calling number {target} (race — try another)")


async def get_available_number(
    call_id: str,
    *,
    room_name: Optional[str] = None,
    assigned_agent: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> str:
    """
    Atomically lease one free pool number for this call.
    Prefer org-owned DIDs, then shared (NULL org). Never lease another org's number.
    """
    if not call_id:
        raise ValueError("call_id is required to acquire a phone number")
    db = await _adb()
    now_iso = datetime.now().isoformat()
    org = (organization_id or "").strip() or None

    try:
        rpc = await db.rpc(
            "acquire_phone_number",
            {
                "p_call_id": call_id,
                "p_room_name": room_name,
                "p_assigned_agent": assigned_agent,
                "p_organization_id": org,
            },
        ).execute()
        rows = rpc.data if isinstance(rpc.data, list) else ([rpc.data] if rpc.data else [])
        if rows and rows[0].get("e164"):
            leased = str(rows[0]["e164"])
            try:
                from number_status import publish_number_status
                await publish_number_status(
                    e164=leased,
                    status="busy",
                    previous_status="free",
                    call_id=call_id,
                    room_name=room_name,
                    reason="leased",
                )
            except Exception:
                pass
            return leased
        if rows == [] or rows is not None:
            raise NoNumbersAvailable(
                "No outbound phone numbers available for this organization "
                "(all busy/disabled, or none mapped to this org / shared)"
            )
    except NoNumbersAvailable:
        raise
    except Exception:
        pass

    candidates = await (
        db.table("phone_numbers").select("e164, organization_id, last_used_at")
        .eq("status", "free")
        .order("last_used_at")
        .limit(50)
        .execute()
    )
    ranked: list[dict] = []
    for row in candidates.data or []:
        row_org = (row.get("organization_id") or "").strip() or None
        if org:
            if row_org and row_org != org:
                continue
            priority = 0 if row_org == org else 1
        else:
            if row_org:
                continue
            priority = 0
        ranked.append({**row, "_p": priority})
    ranked.sort(key=lambda r: (r["_p"], r.get("last_used_at") or ""))

    for row in ranked:
        e164 = row.get("e164")
        if not e164:
            continue
        upd = await (
            db.table("phone_numbers").update({
                "status": "busy",
                "current_call_id": call_id,
                "current_room_name": room_name,
                "assigned_agent": assigned_agent,
                "last_used_at": now_iso,
            })
            .eq("e164", e164)
            .eq("status", "free")
            .execute()
        )
        if upd.data:
            leased = str(e164)
            try:
                from number_status import publish_number_status
                await publish_number_status(
                    e164=leased,
                    status="busy",
                    previous_status="free",
                    call_id=call_id,
                    room_name=room_name,
                    reason="leased",
                )
            except Exception:
                pass
            return leased
    raise NoNumbersAvailable(
        "No outbound phone numbers available for this organization "
        "(all busy/disabled, or none mapped to this org / shared)"
    )


async def release_number(
    *,
    call_id: Optional[str] = None,
    e164: Optional[str] = None,
) -> bool:
    """Release a leased number back to free (or disabled if Vobiz no longer has it)."""
    if not call_id and not e164:
        return False
    db = await _adb()
    query = db.table("phone_numbers").select("e164, status, vobiz_status, current_call_id, current_room_name")
    if call_id:
        query = query.eq("current_call_id", call_id)
    else:
        query = query.eq("e164", e164)
    existing = await query.maybe_single().execute()
    if not existing or not existing.data:
        # Fallback: match by e164 when call_id lease was lost
        if call_id and e164:
            existing = await (
                db.table("phone_numbers").select("e164, status, vobiz_status, current_call_id, current_room_name")
                .eq("e164", e164).maybe_single().execute()
            )
        if not existing or not existing.data:
            return False

    row = existing.data
    prev_status = (row.get("status") or "").lower()
    vobiz_status = (row.get("vobiz_status") or "").lower()
    new_status = "free" if vobiz_status in ("", "active") else "disabled"
    upd = await (
        db.table("phone_numbers").update({
            "status": new_status,
            "current_call_id": None,
            "current_room_name": None,
        })
        .eq("e164", row["e164"])
        .eq("status", "busy")
        .execute()
    )
    freed = len(upd.data or []) > 0
    if freed:
        try:
            from number_status import publish_number_status
            await publish_number_status(
                e164=str(row["e164"]),
                status=new_status,
                previous_status=prev_status or "busy",
                call_id=call_id or row.get("current_call_id"),
                room_name=row.get("current_room_name"),
                reason="call_ended",
            )
        except Exception:
            pass
    return freed


async def reclaim_stale_busy_numbers(max_age_minutes: Optional[int] = None) -> int:
    """Safety net: free numbers stuck busy longer than max call duration."""
    max_age = max_age_minutes if max_age_minutes is not None else _phone_pool_busy_max_minutes()
    db = await _adb()
    result = await db.table("phone_numbers").select("*").eq("status", "busy").execute()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age)
    freed = 0
    for row in result.data or []:
        used = _parse_call_timestamp(row.get("last_used_at"))
        if used is None:
            used = _parse_call_timestamp(row.get("synced_at")) or _parse_call_timestamp(row.get("created_at"))
        if used is None:
            continue
        used_utc = used if used.tzinfo else used.replace(tzinfo=timezone.utc)
        used_utc = used_utc.astimezone(timezone.utc)
        if used_utc > cutoff:
            continue
        vobiz_status = (row.get("vobiz_status") or "").lower()
        new_status = "free" if vobiz_status in ("", "active") else "disabled"
        upd = await (
            db.table("phone_numbers").update({
                "status": new_status,
                "current_call_id": None,
                "current_room_name": None,
            })
            .eq("e164", row["e164"])
            .eq("status", "busy")
            .execute()
        )
        if upd.data:
            freed += 1
            try:
                from number_status import publish_number_status
                await publish_number_status(
                    e164=str(row["e164"]),
                    status=new_status,
                    previous_status="busy",
                    call_id=row.get("current_call_id"),
                    reason="stale_reclaim",
                )
            except Exception:
                pass
    return freed


async def upsert_phone_numbers_from_vobiz(items: list[dict]) -> dict:
    """
    Upsert Vobiz list into phone_numbers.
    New usable → free; missing / unusable → disabled (never hard-delete).
    Never flips busy → free.
    """
    db = await _adb()
    now_iso = datetime.now().isoformat()
    seen: set[str] = set()
    inserted = 0
    updated = 0
    disabled = 0

    existing_rows = await db.table("phone_numbers").select("*").execute()
    by_e164 = {str(r["e164"]): r for r in (existing_rows.data or []) if r.get("e164")}

    for item in items:
        e164 = str(item.get("e164") or "").strip()
        if not e164:
            continue
        seen.add(e164)
        vobiz_status = str(item.get("status") or "").strip().lower()
        voice_ok = bool(item.get("voice_enabled", True))
        blocked = bool(item.get("is_blocked"))
        usable = vobiz_status == "active" and voice_ok and not blocked

        meta = {
            "vobiz_id": item.get("id"),
            "vobiz_status": vobiz_status or None,
            "country": item.get("country"),
            "region": item.get("region"),
            "synced_at": now_iso,
        }

        prev = by_e164.get(e164)
        if not prev:
            await db.table("phone_numbers").insert({
                "e164": e164,
                "status": "free" if usable else "disabled",
                "created_at": now_iso,
                **meta,
            }).execute()
            inserted += 1
            if not usable:
                disabled += 1
            continue

        patch = dict(meta)
        if prev.get("status") == "busy":
            # Keep lease; only refresh Vobiz metadata.
            pass
        elif usable:
            if prev.get("status") == "disabled":
                patch["status"] = "free"
        else:
            if prev.get("status") != "disabled":
                patch["status"] = "disabled"
                disabled += 1

        await db.table("phone_numbers").update(patch).eq("e164", e164).execute()
        updated += 1

    # Numbers we own locally but Vobiz no longer returns
    for e164, prev in by_e164.items():
        if e164 in seen:
            continue
        if prev.get("status") == "busy":
            await (
                db.table("phone_numbers").update({
                    "vobiz_status": "missing",
                    "synced_at": now_iso,
                }).eq("e164", e164).execute()
            )
            updated += 1
            continue
        if prev.get("status") != "disabled":
            await (
                db.table("phone_numbers").update({
                    "status": "disabled",
                    "vobiz_status": "missing",
                    "synced_at": now_iso,
                    "current_call_id": None,
                    "current_room_name": None,
                }).eq("e164", e164).execute()
            )
            disabled += 1
            updated += 1
        else:
            await (
                db.table("phone_numbers").update({
                    "vobiz_status": "missing",
                    "synced_at": now_iso,
                }).eq("e164", e164).execute()
            )

    return {
        "seen": len(seen),
        "inserted": inserted,
        "updated": updated,
        "disabled": disabled,
        "synced_at": now_iso,
    }
