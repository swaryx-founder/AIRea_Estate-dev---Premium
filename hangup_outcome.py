"""Infer CRM call outcome when the lead hangs up without end_call."""

from __future__ import annotations

import json
import re
from typing import Any, Optional


_NOT_INTERESTED = (
    "not interested",
    "not intrested",
    "no interest",
    "interested nahi",
    "interest nahi",
    "interest nai",
    "dilchaspi nahi",
    "dilchasp nahi",
    "रुचि नहीं",
    "रुचि नही",
    "nahi chahiye",
    "nahi chahie",
    "mat batao",
    "mat bataana",
    "don't want",
    "do not want",
    "no need",
    "already bought",
    "already purchased",
    "paise nahi",
)

_DO_NOT_CALL = (
    "do not call",
    "don't call",
    "dont call",
    "never call",
    "dubara call mat",
    "call mat kar",
    "call mat karo",
    "number delete",
    "remove my number",
    "stop calling",
)

_INTERESTED = (
    "i am interested",
    "i'm interested",
    "im interested",
    "interested hoon",
    "interested hun",
    "interested hu",
    "interested hai",
    "mujhe interest",
    "interest hai",
    "dilchaspi hai",
    "dilchasp hai",
    "मुझे दिलचस्पी",
    "दिलचस्पी है",
    "haan interested",
    "yes interested",
    "interested in",
    "dekhna chahta",
    "dekhna chahti",
    "dekhna hai",
    "sunna chahta",
    "sunna chahti",
    "batao details",
    "details chahiye",
    "visit karna",
    "site visit",
    "investment point of view",
    "investment ke liye",
)


def _clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _user_texts_from_transcript(
    live_transcript: Optional[str],
    transcript_json: Optional[str],
) -> list[str]:
    texts: list[str] = []
    raw = transcript_json
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parsed = parsed.get("lines") or parsed.get("transcript") or []
        if isinstance(parsed, list):
            for ln in parsed:
                if not isinstance(ln, dict):
                    continue
                role = (ln.get("role") or ln.get("speaker") or "").strip().lower()
                if role in ("user", "customer", "lead"):
                    t = _clean(str(ln.get("text") or ""))
                    if t:
                        texts.append(t)
    live = live_transcript or ""
    for line in live.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("lead:") or lower.startswith("user:") or lower.startswith("customer:"):
            texts.append(_clean(line.split(":", 1)[-1]))
    return texts


def _intent_from_property_data(property_data: Any) -> Optional[str]:
    props = property_data
    if isinstance(props, str) and props.strip():
        try:
            props = json.loads(props)
        except Exception:
            props = None
    if not isinstance(props, dict):
        return None
    intent = _clean(str(props.get("customer_intent") or "")).lower()
    if intent in ("do_not_call", "not_interested", "interested"):
        return intent
    return None


def _match_any(text: str, phrases: tuple[str, ...]) -> bool:
    lower = text.casefold()
    return any(p.casefold() in lower for p in phrases)


def infer_hangup_outcome(
    *,
    live_transcript: Optional[str] = None,
    transcript_json: Optional[str] = None,
    property_data: Any = None,
) -> tuple[str, str]:
    """Choose CRM outcome when the lead hangs up mid-call.

    Prefer clear interest / refusal so Node can skip useless redials:
    - interested → lead showed interest then dropped (follow-up, don't spam)
    - not_interested / do_not_call → stop callbacks
    - disconnect_in_between → only when intent is unclear
    """
    intent = _intent_from_property_data(property_data)
    if intent == "do_not_call":
        return "do_not_call", "hangup_after_do_not_call"
    if intent == "not_interested":
        return "not_interested", "hangup_after_not_interested"
    if intent == "interested":
        return "interested", "hangup_after_interest"

    user_texts = _user_texts_from_transcript(live_transcript, transcript_json)
    # Newest user lines first — last stated intent wins.
    for text in reversed(user_texts):
        if _match_any(text, _DO_NOT_CALL):
            return "do_not_call", "hangup_after_do_not_call"
        if _match_any(text, _NOT_INTERESTED):
            return "not_interested", "hangup_after_not_interested"
        if _match_any(text, _INTERESTED):
            return "interested", "hangup_after_interest"
        # Standalone "not interested" variants with spacing/noise
        if re.search(r"\bnot[\s\-]*interested\b", text, re.I):
            return "not_interested", "hangup_after_not_interested"
        if re.search(r"\binterested\b", text, re.I) and not re.search(
            r"\bnot[\s\-]*interested\b", text, re.I
        ):
            # Avoid false positive on agent-mirrored lines; only user texts here.
            return "interested", "hangup_after_interest"

    return "disconnect_in_between", "call disconnected mid-conversation"
