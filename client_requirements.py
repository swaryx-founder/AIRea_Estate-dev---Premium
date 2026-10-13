"""Deterministic, non-LLM capture of lead requirements during a live call.

The realtime model can still call ``remember_details`` for nuanced facts. This
module covers CRM-critical fields independently so a model/tool-call miss does
not leave the completed-call webhook empty.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger("client-requirements")

_MONEY_UNIT = r"(?:crores?|crs?|cr|करोड़|करोड़|lakhs?|lacs?|lac|लाख)"
_NUMBER_WORDS = {
    "one": 1, "ek": 1, "एक": 1,
    "two": 2, "do": 2, "दो": 2,
    "three": 3, "teen": 3, "तीन": 3,
    "four": 4, "char": 4, "chaar": 4, "चार": 4,
    "five": 5, "paanch": 5, "पांच": 5, "पाँच": 5,
}
_PLACE_ALIASES = (
    ("greater noida", "Greater Noida"),
    ("noida extension", "Noida Extension"),
    ("yamuna expressway", "Yamuna Expressway"),
    ("new delhi", "New Delhi"),
    ("moti nagar", "Moti Nagar"),
    ("muzaffarnagar", "Muzaffarnagar"),
    ("ghaziabad", "Ghaziabad"),
    ("gurugram", "Gurugram"),
    ("gurgaon", "Gurugram"),
    ("faridabad", "Faridabad"),
    ("delhi ncr", "Delhi NCR"),
    ("delhi", "Delhi"),
    ("noida", "Noida"),
    ("नोएडा", "Noida"),
    ("दिल्ली", "Delhi"),
    ("गाज़ियाबाद", "Ghaziabad"),
    ("गाजियाबाद", "Ghaziabad"),
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip(" ,.;:-"))


# Negatives first — "interested nahi" must not match as interested.
_NOT_INTERESTED_PHRASES = (
    "not interested",
    "no interested",
    "interested nahi",
    "interest nahi",
    "interested nahi hai",
    "interest nahi hai",
    "koi interest nahi",
    "mujhe interest nahi",
    "mujhe interested nahi",
    "nahi chahiye",
    "nahi lena",
    "mat batao",
    "mat batana",
    "don't want",
    "dont want",
    "do not want",
    "no thanks",
    "no thank you",
    "रुचि नहीं",
    "दिलचस्पी नहीं",
    "नहीं चाहिए",
)
_INTERESTED_PHRASES = (
    "i am interested",
    "i'm interested",
    "im interested",
    "interested hoon",
    "interested hun",
    "interested hai",
    "interested ho",
    "interest hai",
    "mujhe interest",
    "haan interested",
    "yes interested",
    "interested in",
    "dilchaspi",
    "dilchasp",
    "मुझे दिलचस्पी",
    "दिलचस्पी है",
    "रुचि है",
)
_DO_NOT_CALL_PHRASES = (
    "do not call",
    "don't call",
    "dont call",
    "dubara call mat",
    "call mat kar",
    "call mat karna",
    "number remove",
    "delete my number",
)


def detect_customer_intent(text: str) -> Optional[str]:
    """Return do_not_call | not_interested | interested from one utterance, else None."""
    lower = _clean(text).casefold()
    if not lower:
        return None
    if any(phrase.casefold() in lower for phrase in _DO_NOT_CALL_PHRASES):
        return "do_not_call"
    if any(phrase.casefold() in lower for phrase in _NOT_INTERESTED_PHRASES):
        return "not_interested"
    if any(phrase.casefold() in lower for phrase in _INTERESTED_PHRASES):
        return "interested"
    # Bare "interested" only when not negated (negatives already handled above).
    if re.search(r"(?<!\w)interested(?!\w)", lower) and "nahi" not in lower and "not " not in lower:
        return "interested"
    return None


def _user_transcript_texts(
    live_transcript: Optional[str] = None,
    transcript_json: Optional[str] = None,
) -> list[str]:
    """Collect lead/user spoken lines in order for hangup classification."""
    import json

    texts: list[str] = []
    raw = transcript_json
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            parsed = parsed.get("lines") or parsed.get("transcript") or []
        if isinstance(parsed, list):
            for ln in parsed:
                if not isinstance(ln, dict):
                    continue
                role = str(ln.get("role") or ln.get("speaker") or "").strip().lower()
                if role in ("user", "customer", "lead", "human"):
                    text = (ln.get("text") or ln.get("transcript") or "").strip()
                    if text:
                        texts.append(text)
    if texts:
        return texts
    # Fallback: live blob may be "User: …\nAgent: …"
    live = (live_transcript or "").strip()
    if not live:
        return []
    for line in live.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith(("user:", "customer:", "lead:", "human:")):
            texts.append(stripped.split(":", 1)[1].strip())
        elif lower.startswith(("agent:", "assistant:", "bot:")):
            continue
    return texts


def infer_hangup_outcome(
    *,
    live_transcript: Optional[str] = None,
    transcript_json: Optional[str] = None,
    property_data=None,
) -> Optional[str]:
    """If the lead already said interested / not interested, use that instead of cut-mid-call.

    Returns ``interested`` | ``not_interested`` | ``do_not_call`` | None.
    Last clear user signal wins; property_data.customer_intent is a fallback.
    """
    last: Optional[str] = None
    for text in _user_transcript_texts(live_transcript, transcript_json):
        intent = detect_customer_intent(text)
        if intent:
            last = intent
    if last:
        return last

    props = property_data
    if isinstance(props, str):
        import json
        try:
            props = json.loads(props)
        except (json.JSONDecodeError, TypeError):
            props = None
    if isinstance(props, dict):
        intent = str(props.get("customer_intent") or "").strip().lower()
        if intent in ("interested", "not_interested", "do_not_call"):
            return intent
    return None


def _extract_budget(text: str) -> Optional[str]:
    match = re.search(
        rf"\b\d+(?:\.\d+)?\s*(?:-|–|to|se|से)?\s*"
        rf"(?:\d+(?:\.\d+)?\s*)?{_MONEY_UNIT}\b",
        text,
        flags=re.IGNORECASE,
    )
    return _clean(match.group(0)) if match else None


def _extract_generic_location(original: str) -> Optional[str]:
    patterns = (
        r"(?:preferred\s+location|location|area|city)\s*(?:is|hai|h|:|-)?\s*"
        r"([A-Za-z][A-Za-z .'-]{2,35})",
        r"(?:property|flat|ghar)\s+([A-Za-z][A-Za-z .'-]{2,35}?)\s+"
        r"(?:mein|me|में)\s+(?:chahiye|dekh|looking|want)",
        r"([A-Za-z][A-Za-z .'-]{2,35}?)\s+(?:mein|me)\s+"
        r"(?:property|flat|ghar)\s+(?:chahiye|dekh|looking|want)",
    )
    stop = re.compile(
        r"\b(?:and|aur|but|lekin|with|budget|bhk|property|flat|ghar|chahiye|hai|h)\b",
        re.IGNORECASE,
    )
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE)
        if not match:
            continue
        value = stop.split(_clean(match.group(1)))[0].strip()
        if 2 < len(value) <= 40:
            return value.title()
    return None


def extract_client_requirements(text: str) -> dict[str, str]:
    """Extract CRM-safe facts stated by the customer in one utterance."""
    original = _clean(text)
    lower = original.lower()
    if not original:
        return {}

    updates: dict[str, str] = {}
    budget = _extract_budget(original)
    if budget:
        updates["budget"] = budget
    if "budget" in lower and any(word in lower for word in ("flexible", "adjust", "open")):
        updates["budget_flexibility"] = "Flexible"

    bhk = re.search(r"\b([1-9])\s*[- ]?\s*bhk\b", lower, flags=re.IGNORECASE)
    if bhk:
        updates["bhk"] = f"{bhk.group(1)} BHK"

    if any(word in lower for word in ("villa", "विला")):
        updates["property_type"] = "Villa"
    elif any(word in lower for word in ("plot", "प्लॉट")):
        updates["property_type"] = "Plot"
    elif any(word in lower for word in ("apartment", "अपार्टमेंट", " flat ")) or lower.startswith("flat "):
        updates["property_type"] = "Apartment"

    if any(phrase in lower for phrase in ("ready to move", "ready-to-move", "रेडी टू मूव")):
        updates["possession_timeline"] = "Ready to move"
    elif any(phrase in lower for phrase in ("under construction", "अंडर कंस्ट्रक्शन")):
        updates["possession_timeline"] = "Under construction"

    for alias, canonical in _PLACE_ALIASES:
        if alias in lower:
            updates["city"] = canonical
            break
    if "city" not in updates:
        generic_location = _extract_generic_location(original)
        if generic_location:
            updates["city"] = generic_location

    parking_word = re.search(r"(?:parking|gaadi|gadi|car|गाड़ी|गाड़ी)", lower, flags=re.IGNORECASE)
    if parking_word:
        context = lower[max(0, parking_word.start() - 28):parking_word.end() + 28]
        keyword = re.search(r"(?:parking|gaadi|gadi|car|गाड़ी|गाड़ी)", context, re.IGNORECASE)
        candidates: list[tuple[int, int]] = []
        for digit in re.finditer(r"\b([1-9])\b", context):
            candidates.append((abs(digit.start() - (keyword.start() if keyword else 0)), int(digit.group(1))))
        for word, number in _NUMBER_WORDS.items():
            for match in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", context):
                candidates.append((abs(match.start() - (keyword.start() if keyword else 0)), number))
        count: Optional[int] = min(candidates)[1] if candidates else None
        updates["parking_spaces"] = str(count) if count else "Required"

    hometown = re.search(
        r"(?:home\s*town|hometown|native\s+place|gaon|गाँव|गांव)\s*"
        r"(?:is|hai|h|:|-|mera|मेरी|मेरा)*\s*([A-Za-z][A-Za-z .'-]{2,40})",
        original,
        flags=re.IGNORECASE,
    )
    if hometown:
        updates["hometown"] = _clean(hometown.group(1)).title()

    needs: list[str] = []
    for needles, label in (
        (("less pollution", "low pollution", "pollution nahi", "कम प्रदूषण"), "Low pollution"),
        (("school", "स्कूल"), "Nearby schools"),
        (("hospital", "अस्पताल"), "Nearby hospitals"),
        (("metro", "मेट्रो"), "Metro connectivity"),
        (("connectivity", "कनेक्टिविटी"), "Good connectivity"),
        (("green area", "greenery", "हरियाली"), "Green surroundings"),
    ):
        if any(needle in lower for needle in needles):
            needs.append(label)
    if needs:
        updates["requirements"] = ", ".join(needs)

    email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", original, re.IGNORECASE)
    if email:
        updates["email"] = email.group(0).lower()

    intent = detect_customer_intent(original)
    if intent:
        updates["customer_intent"] = intent

    return updates


class ClientRequirementsCollector:
    """Serialize requirement writes and expose a final flush barrier."""

    def __init__(
        self,
        call_id: str,
        phone_number: str,
        *,
        organization_id: Optional[str] = None,
        lead_id: Optional[str] = None,
    ) -> None:
        self.call_id = call_id
        self.phone_number = phone_number
        self.organization_id = organization_id
        self.lead_id = lead_id
        self._seen: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    def submit(self, text: str) -> None:
        normalized = _clean(text).casefold()
        if not normalized or normalized in self._seen:
            return
        self._seen.add(normalized)
        task = asyncio.create_task(self._process(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, text: str) -> None:
        updates = extract_client_requirements(text)
        if not updates:
            return
        try:
            from db import update_call_property_fields, upsert_contact_requirements

            async with self._lock:
                await update_call_property_fields(self.call_id, updates)
                await upsert_contact_requirements(
                    self.phone_number,
                    updates,
                    organization_id=self.organization_id,
                    lead_id=self.lead_id,
                )
            logger.info("Captured client requirements call_id=%s fields=%s", self.call_id, sorted(updates))
        except Exception as exc:
            logger.warning("Requirement capture failed call_id=%s: %s", self.call_id, exc)

    async def flush(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
