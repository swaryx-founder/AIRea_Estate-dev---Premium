"""Parse and normalize callback schedule fields for Node auto-dial.

Contract (call-completed webhook):
  - outcome: "callback_requested"
  - scheduled_at: ISO-8601 UTC when lead gave a usable time (omit for off-topic / no-schedule)
  - callback_urgency: "now" | "later" | "specific_time" | "none"

Timezone default for relative phrases ("kal 5 baje", "tomorrow 5pm"): Asia/Kolkata (IST).
Vague "later" / "baad mein" with no clock time → scheduled_at = now + 10 minutes (today).
urgency "none" (off-topic scope-lock) → no scheduled_at; Node must not auto-dial.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

DEFAULT_TZ_NAME = "Asia/Kolkata"
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)
# "Call me now" → dial shortly after hangup (Node cron / near-term queue).
NOW_OFFSET_MINUTES = 2
# "Baad mein / later" with no number → dial after 10 minutes on today's date.
LATER_OFFSET_MINUTES = 10

VALID_URGENCIES = frozenset({"now", "later", "specific_time", "none"})


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(utc.microsecond / 1000):03d}Z"


def normalize_callback_urgency(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    v = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "immediate": "now",
        "asap": "now",
        "right_now": "now",
        "call_now": "now",
        "sometime": "later",
        "vague": "later",
        "no_time": "later",
        "specific": "specific_time",
        "scheduled": "specific_time",
        "datetime": "specific_time",
        "no_schedule": "none",
        "skip": "none",
        "skip_schedule": "none",
        "off_topic": "none",
        "noschedule": "none",
    }
    v = aliases.get(v, v)
    return v if v in VALID_URGENCIES else None


_OFF_TOPIC_CLOSE_RE = re.compile(
    r"\b(?:off[\s_-]?topic|scope\s*lock|not\s*real[\s_-]?estate|unrelated\s+topic)\b",
    re.IGNORECASE,
)


def is_off_topic_close_reason(reason: Optional[str]) -> bool:
    """True when the close reason is an off-topic / scope-lock hangup (no auto-dial)."""
    return bool(_OFF_TOPIC_CLOSE_RE.search(str(reason or "")))


def _parse_iso_datetime(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    # Allow trailing Z
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=DEFAULT_TZ)
        return dt
    except ValueError:
        return None


def _parse_hhmm(token: str) -> Optional[tuple[int, int]]:
    t = token.strip().lower()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", t)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _hindi_or_english_day_offset(text: str, now_local: datetime) -> Optional[int]:
    t = text.lower()
    if re.search(r"\b(aaj|today|abhi ke baad)\b", t) or "आज" in text:
        return 0
    if re.search(r"\b(kal|tomorrow)\b", t) or "कल" in text:
        return 1
    if re.search(r"\b(parso|day after tomorrow)\b", t) or "परसों" in text:
        return 2
    # weekday names (next occurrence)
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "somwar": 0, "mangal": 1, "budh": 2, "guru": 3, "shukra": 4, "shani": 5, "ravivar": 6,
    }
    for name, wd in weekdays.items():
        if re.search(rf"\b{name}\b", t):
            delta = (wd - now_local.weekday()) % 7
            return delta if delta else 7
    return None


def parse_relative_callback_when(
    raw: str,
    *,
    now: Optional[datetime] = None,
    urgency: Optional[str] = None,
) -> Optional[datetime]:
    """Parse lead-provided callback time into a timezone-aware datetime (IST default)."""
    text = (raw or "").strip()
    if not text:
        return None

    iso_dt = _parse_iso_datetime(text)
    if iso_dt is not None:
        return iso_dt

    now_utc = now or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(DEFAULT_TZ)
    lower = text.lower()

    # in/after N minutes, N min baad, "2-5 min" → use the larger number
    m = re.search(
        r"(?:in|after|baad)?\s*(\d+)\s*(?:-\s*(\d+))?\s*(?:minutes?|mins?|min)\b",
        lower,
    )
    if not m:
        m = re.search(
            r"(\d+)\s*(?:-\s*(\d+))?\s*(?:minute|min)\s*(?:mein|me|baad)?",
            lower,
        )
    if m:
        mins = int(m.group(2) or m.group(1))
        return now_utc + timedelta(minutes=max(1, mins))

    # in N hours
    m = re.search(r"(?:in\s+)?(\d+)\s*(?:hours?|hrs?)\b", lower)
    if m:
        return now_utc + timedelta(hours=int(m.group(1)))

    # evening / morning / afternoon without clock → soft defaults (still lead-usable)
    day_offset = _hindi_or_english_day_offset(text, now_local)
    hour_min: Optional[tuple[int, int]] = None

    # Explicit clock: 5pm, 17:30, 5 baje, shaam 5
    m = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", lower)
    if m:
        hour_min = _parse_hhmm(m.group(1))
    if hour_min is None:
        m = re.search(r"\b(\d{1,2})\s*(?:baje|bajе)\b", lower) or re.search(r"(\d{1,2})\s*बजे", text)
        if m:
            hour_min = (int(m.group(1)), 0)
            if hour_min[0] < 7:  # "5 baje" evening sales context → 17:00 if ambiguous
                # Prefer PM for callback hours 1–6 when "shaam/evening" or day word present
                if re.search(r"\b(shaam|evening|pm|raat)\b", lower) or "शाम" in text or day_offset is not None:
                    hour_min = (hour_min[0] + 12, 0) if hour_min[0] < 12 else hour_min

    if hour_min is None:
        if re.search(r"\b(subah|morning)\b", lower) or "सुबह" in text:
            hour_min = (10, 0)
        elif re.search(r"\b(dopahar|afternoon)\b", lower) or "दोपहर" in text:
            hour_min = (14, 0)
        elif re.search(r"\b(shaam|evening)\b", lower) or "शाम" in text:
            hour_min = (17, 30)
        elif re.search(r"\b(raat|night)\b", lower) or "रात" in text:
            hour_min = (20, 0)

    if day_offset is None and hour_min is not None:
        # Time only → today if still upcoming, else tomorrow
        candidate = now_local.replace(
            hour=hour_min[0], minute=hour_min[1], second=0, microsecond=0
        )
        if candidate <= now_local + timedelta(minutes=2):
            candidate += timedelta(days=1)
        return candidate

    if day_offset is not None and hour_min is not None:
        base = (now_local + timedelta(days=day_offset)).replace(
            hour=hour_min[0], minute=hour_min[1], second=0, microsecond=0
        )
        return base

    if day_offset is not None and hour_min is None:
        # Day without clock — not a usable slot here; later/now defaults apply upstream
        return None

    return None


def _extract_callback_time_from_reason(reason: Optional[str]) -> str:
    """Pull a dial-time phrase from CRM reason when end_call omitted scheduled_at."""
    text = (reason or "").strip()
    if not text:
        return ""

    # "callback: tomorrow 5pm", "call back: in 5 minutes"
    m = re.search(
        r"(?:callback|call back|will call back|agreed callback)\s*:?\s*(.+)$",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    # "requested call in 2 minutes", "lead requested call in 2 minutes"
    m = re.search(
        r"requested\s+call\s+(?:in|after)\s+(.+)$",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    # "call in 2 minutes", "call back in 5 min", "call after 10 minutes"
    m = re.search(
        r"call\s+(?:back\s+)?(?:in|after)\s+(.+)$",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    # "2 minute baad", "5 min mein"
    m = re.search(
        r"(\d+\s*(?:-\s*\d+\s*)?(?:minutes?|mins?|min)\s*(?:mein|me|baad)?)",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    return ""


def resolve_callback_schedule(
    *,
    scheduled_at_raw: Optional[str] = None,
    callback_urgency: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Optional[str]]:
    """Return {scheduled_at, callback_urgency} for webhook / DB.

    Rules:
    - urgency none → no scheduled_at (outcome may still be callback_requested; Node must not auto-dial)
    - urgency now → scheduled_at = now+2m if no usable time
    - urgency later / vague, no time → scheduled_at = now+10m (today)
    - "after 2/5 minutes" → now + N minutes on today's date
    - specific clock/day → parse absolute/relative; urgency defaults to specific_time
    - off-topic / scope-lock close reason with no real time → urgency none, no schedule
    """
    urgency = normalize_callback_urgency(callback_urgency)
    raw = (scheduled_at_raw or "").strip()
    now_utc = now or datetime.now(timezone.utc)

    # Off-topic close: keep callback_requested label upstream, but never invent a dial time.
    if urgency == "none" or (
        urgency is None
        and not raw
        and is_off_topic_close_reason(reason)
    ):
        return {"scheduled_at": None, "callback_urgency": "none"}

    # Pull free-text time from reason if model only filled reason
    if not raw and reason:
        raw = _extract_callback_time_from_reason(reason)
        if not raw and parse_relative_callback_when(reason.strip(), now=now_utc):
            raw = reason.strip()

    parsed = parse_relative_callback_when(raw, now=now_utc, urgency=urgency) if raw else None

    if parsed is None and urgency == "now":
        parsed = now_utc + timedelta(minutes=NOW_OFFSET_MINUTES)

    if urgency is None:
        reason_lower = (reason or "").lower()
        blob = f"{raw} {reason_lower}".lower()
        if parsed is not None and raw:
            # Explicit clock / relative minutes → specific or now
            if re.search(r"\b(now|abhi|turant)\b", raw.lower()) or "अभी" in raw:
                urgency = "now"
            elif re.search(r"\b(\d+)\s*(min|minute)", raw.lower()):
                urgency = "now"
            elif re.search(r"\b(\d+)\s*(min|minute)", reason_lower):
                urgency = "now"
            else:
                urgency = "specific_time"
        elif re.search(r"\b(later|baad|baad mein)\b", blob) or "बाद" in (raw + (reason or "")):
            urgency = "later"

    # Vague later / callback with no parseable time → today + 10 minutes
    if parsed is None and urgency == "later":
        parsed = now_utc + timedelta(minutes=LATER_OFFSET_MINUTES)
    if parsed is None and urgency is None:
        urgency = "later"
        parsed = now_utc + timedelta(minutes=LATER_OFFSET_MINUTES)

    scheduled_iso = to_utc_iso(parsed) if parsed is not None else None

    return {
        "scheduled_at": scheduled_iso,
        "callback_urgency": urgency,
    }


def callback_fields_from_record(record: dict[str, Any]) -> dict[str, Optional[str]]:
    """Read scheduled_at / callback_urgency from call_logs row + property_data."""
    from db import parse_property_data

    props = parse_property_data(record.get("property_data"))
    raw_at = (
        record.get("scheduled_at")
        or props.get("scheduled_at")
        or props.get("scheduledAt")
        or props.get("callback_scheduled_at")
    )
    raw_urgency = (
        record.get("callback_urgency")
        or props.get("callback_urgency")
        or props.get("callbackUrgency")
    )
    outcome = (record.get("outcome") or "").strip().lower()
    reason = record.get("reason") or ""

    if outcome != "callback_requested" and not raw_at and not raw_urgency:
        return {"scheduled_at": None, "callback_urgency": None}

    resolved = resolve_callback_schedule(
        scheduled_at_raw=str(raw_at) if raw_at else None,
        callback_urgency=str(raw_urgency) if raw_urgency else None,
        reason=reason if outcome == "callback_requested" else None,
    )
    return resolved


def format_callback_for_agent(
    record: dict[str, Any],
    *,
    timezone_name: str = DEFAULT_TZ_NAME,
) -> Optional[str]:
    """Return a precise, prompt-ready callback schedule from stored call data.

    Unlike ``callback_fields_from_record``, this helper never invents a fallback
    time from the current clock. It only reports a date/time that was already
    persisted on the call, so the voice agent cannot give a fabricated answer.
    """
    from db import parse_property_data

    props = parse_property_data(record.get("property_data"))
    raw_at = (
        record.get("scheduled_at")
        or props.get("scheduled_at")
        or props.get("scheduledAt")
        or props.get("callback_scheduled_at")
    )
    if not raw_at:
        return None

    raw_text = str(raw_at).strip()
    if not raw_text:
        return None

    urgency = normalize_callback_urgency(
        record.get("callback_urgency")
        or props.get("callback_urgency")
        or props.get("callbackUrgency")
    )

    try:
        parsed = datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            display_tz = ZoneInfo(timezone_name)
        except Exception:
            display_tz = DEFAULT_TZ
        local = parsed.astimezone(display_tz)
        human = local.strftime("%A, %d %B %Y at %I:%M %p %Z")
    except (TypeError, ValueError):
        # Preserve an unusual legacy value verbatim rather than guessing.
        human = raw_text

    suffix = f"; urgency={urgency}" if urgency else ""
    return f"{human} (stored schedule: {raw_text}{suffix})"
