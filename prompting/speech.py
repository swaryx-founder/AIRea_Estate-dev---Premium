"""Canonical first-turn speech — shared by prompt text and LiveKit runtime.

Keep opening/permission strings here so prompt rules and generate_reply cannot drift.

Opening if/else (fresh calls only):
  IF campaign notes include greeting/opener guidance → campaign salutation + identity
  ELSE → exact default identity line
Either branch: no agent intro, no project name, no pitch until after permission.

Agent name if/else:
  IF campaign notes set a spoken agent name → use that name in permission/persona/voicemail
  ELSE → Riya

Permission if/else:
  IF campaign notes customize permission/intro wording → follow campaign wording
  ELSE → exact default permission_intro (language-matched, with resolved agent name)

Language if/else (whole call):
  IF campaign notes mention english/hindi/hinglish → that language
  ELSE → hinglish (with "Hello…" default identity opening)

General: campaign instructions win for any topic they cover; silent topics use the
default single-project / multi-project / consultant flow.
"""

from __future__ import annotations

import re
from typing import Optional

from .language import CallLanguage, resolve_call_language

DEFAULT_AGENT_NAME = "Riya"

# Placeholders Node/API historically send when no real name is known.
_GENERIC_LEAD_NAMES = frozenset({
    "there",
    "name",
    "the lead",
    "unknown",
    "n/a",
    "na",
    "null",
    "none",
})

_BLOCKED_AGENT_NAMES = frozenset({
    "there",
    "name",
    "unknown",
    "n/a",
    "na",
    "null",
    "none",
    "agent",
    "assistant",
    "bot",
    "ai",
    "you",
    "me",
})

# IF campaign sets spoken agent name → capture it; ELSE default Riya.
_AGENT_NAME_RE = re.compile(
    r"(?:"
    r"(?:agent\s*name|your\s*name\s*is|call\s*yourself|introduce\s*yourself\s*as|"
    r"name\s*yourself\s*as|speak\s*as|i\s*am)\s*[:\-]?\s*"
    r"|name\s*:\s*(?:your\s*name\s*is\s*)?"
    r")"
    r"([A-Za-z][A-Za-z .'-]{0,38})",
    re.IGNORECASE,
)


def is_missing_lead_name(lead_name: Optional[str]) -> bool:
    cleaned = " ".join(str(lead_name or "").split()).strip()
    return (not cleaned) or cleaned.lower() in _GENERIC_LEAD_NAMES


def spoken_lead_name(lead_name: Optional[str]) -> str:
    """Return a natural first-name form for post-opening address, or empty if unusable."""
    if is_missing_lead_name(lead_name):
        return ""
    cleaned = " ".join(str(lead_name or "").split()).strip()
    return cleaned.split()[0]


def spoken_full_lead_name(lead_name: Optional[str]) -> str:
    """Return the full CRM name for the opening identity check only, or empty if unusable."""
    if is_missing_lead_name(lead_name):
        return ""
    return " ".join(str(lead_name or "").split()).strip()


_NAME_TITLES = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "shri", "sri", "smt", "sh", "kumari", "km",
})


def opening_lead_name(lead_name: Optional[str]) -> str:
    """First name for the default opening, cleaned of CRM noise, or empty if unusable.

    "RAHUL SHARMA" → "Rahul", "Mr. rahul" → "Rahul"; digits/symbols-only parts are skipped.
    """
    if is_missing_lead_name(lead_name):
        return ""
    for part in str(lead_name or "").split():
        word = part.strip(" .,-:;\"'()")
        if not word or word.lower() in _NAME_TITLES or not word.isalpha():
            continue
        return word.title() if word.isupper() or word.islower() else word
    return ""


def resolve_agent_name(additional_instructions: Optional[str] = None) -> str:
    """IF campaign notes set a spoken agent name → that name; ELSE Riya.

    Only the spoken persona name changes. Voice (Leda), identity-first order, and
    flow selection are unchanged.
    """
    text = (additional_instructions or "").strip()
    if not text:
        return DEFAULT_AGENT_NAME
    match = _AGENT_NAME_RE.search(text)
    if not match:
        return DEFAULT_AGENT_NAME
    raw = " ".join(match.group(1).split()).strip(" .,-:;\"'")
    if not raw:
        return DEFAULT_AGENT_NAME
    parts = [p for p in re.split(r"\s+", raw) if p]
    if not parts:
        return DEFAULT_AGENT_NAME
    stop = {
        "then", "and", "from", "calling", "bol", "rahi", "hoon", "with", "for",
        "on", "in", "to", "please", "only", "ji",
    }
    kept: list[str] = []
    for part in parts[:3]:
        if part.lower() in stop:
            break
        kept.append(part)
    if not kept:
        return DEFAULT_AGENT_NAME
    candidate = " ".join(kept).strip()
    if len(candidate) > 40 or candidate.lower() in _BLOCKED_AGENT_NAMES:
        return DEFAULT_AGENT_NAME
    if candidate.lower() == DEFAULT_AGENT_NAME.lower():
        return DEFAULT_AGENT_NAME
    return candidate.title() if candidate.islower() or candidate.isupper() else candidate


def build_identity_opening(
    lead_name: Optional[str] = None,
    *,
    language: CallLanguage = "hinglish",
) -> str:
    """Exact first spoken turn when no campaign greeting guidance is present.

    Starts with "Hello…" to mirror how the lead picks up, and uses the cleaned first
    name only (a full CRM name sounds read off a list). When no usable name exists,
    ask who is on the line.
    """
    name = opening_lead_name(lead_name)
    if name:
        if language == "english":
            return f"Hello… am I speaking with {name}?"
        return f"Hello… kya main {name} ji se baat kar rahi hoon?"
    if language == "english":
        return "Hello… may I know who I'm speaking with?"
    return "Hello… main kisse baat kar rahi hoon?"


_GREETING_LINE_RE = re.compile(r"^\s*greeting\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
_QUOTED_RE = re.compile(r"[\"“”]([^\"“”]{1,80})[\"“”]")
_MAX_SALUTATION_CHARS = 60


def extract_campaign_salutation(additional_instructions: Optional[str]) -> str:
    """Quoted salutation from the notes' ``Greeting:`` line, or empty if not exact.

    'Greeting: Start with "Radhe Radhe!" then confirm…' → "Radhe Radhe!". Without a
    ``Greeting:`` line or quotes the wording is not exact, so the caller stays on the
    LLM-composed opening.
    """
    match = _GREETING_LINE_RE.search(str(additional_instructions or ""))
    if not match:
        return ""
    quoted = _QUOTED_RE.search(match.group(1))
    if not quoted:
        return ""
    salutation = " ".join(quoted.group(1).split()).strip()
    if not salutation or len(salutation) > _MAX_SALUTATION_CHARS or "{" in salutation:
        return ""
    if salutation[-1] not in "!.?,":
        salutation += "!"
    return salutation


def build_campaign_opening_line(
    lead_name: Optional[str],
    additional_instructions: Optional[str],
    *,
    language: CallLanguage = "hinglish",
) -> Optional[str]:
    """Exact campaign turn 1: quoted salutation + the default identity question.

    None when the notes do not give an exact salutation (LLM-composed fallback).
    """
    salutation = extract_campaign_salutation(additional_instructions)
    if not salutation:
        return None
    name = opening_lead_name(lead_name)
    if language == "english":
        ask = f"Am I speaking with {name}?" if name else "May I know who I'm speaking with?"
    else:
        ask = (
            f"Kya main {name} ji se baat kar rahi hoon?"
            if name
            else "Main kisse baat kar rahi hoon?"
        )
    return f"{salutation} {ask}"


def identity_who_phrase(lead_name: Optional[str], *, language: CallLanguage = "hinglish") -> str:
    """Spoken identity target for turn 1 — full name when present; else ask for name."""
    full = spoken_full_lead_name(lead_name)
    if full:
        return f"{full} ji" if language != "english" else full
    return "the person on the line, and ask their name"


def build_campaign_opening_instructions(
    lead_name: Optional[str],
    additional_instructions: Optional[str],
    *,
    language: CallLanguage | None = None,
    agent_name: Optional[str] = None,
) -> str:
    """Generate_reply instructions for campaign-guided turn 1.

    Kept short: the full campaign notes are already in the system prompt, so only the
    greeting sentence(s) from the notes are repeated here.
    """
    # Local imports avoid a circular import with templates (greeting hint regex).
    from .language import indian_accent_runtime_reminder
    from .templates import _GREETING_HINT_RE

    lang = language or resolve_call_language(additional_instructions)
    who = identity_who_phrase(lead_name, language=lang)
    spoken_agent = (agent_name or resolve_agent_name(additional_instructions)).strip() or DEFAULT_AGENT_NAME
    sentences = re.split(r"(?<=[.!?])\s+|\n+", str(additional_instructions or ""))
    greeting = " ".join(
        " ".join(x.split()) for x in sentences if _GREETING_HINT_RE.search(x)
    ).strip()
    if len(greeting) > 300:
        greeting = greeting[:300].rstrip() + "…"
    lang_hint = {
        "english": "Speak this opening in English.",
        "hindi": "Speak this opening in Hindi.",
    }.get(lang, "")

    return (
        "CAMPAIGN OPENING MODE (turn 1 only): Use the campaign salutation — "
        f"{greeting} — and in the SAME turn explicitly confirm you are speaking "
        f"with {who}. {lang_hint} "
        f"{indian_accent_runtime_reminder(spoken_agent)} "
        f"Do not introduce {spoken_agent}, the business, an enquiry, or any "
        "project name or pitch. Never say the placeholder name \"there\". Then stop and wait."
    )


def build_permission_intro(
    *,
    lead_name: Optional[str],
    business_name: Optional[str],
    lead_source: Optional[str],
    language: CallLanguage = "hinglish",
    agent_name: str = DEFAULT_AGENT_NAME,
) -> str:
    """Exact second spoken turn after identity is confirmed (runtime gate).

    Hinglish/Hindi lines must stay spoken and natural — never dump corporate English
    phrases like "property-related outreach" mid-sentence.
    """
    name = spoken_lead_name(lead_name)
    business = " ".join(str(business_name or "our company").split()).strip() or "our company"
    source = " ".join(str(lead_source or "").split()).strip()[:300]
    agent = " ".join(str(agent_name or DEFAULT_AGENT_NAME).split()).strip() or DEFAULT_AGENT_NAME

    if language == "english":
        address = f"{name}, " if name else ""
        if source:
            return (
                f"{address}this is {agent} calling from {business}. We received your property "
                f"enquiry from {source}. Do you have 1 minute?"
            )
        return (
            f"{address}this is {agent} calling from {business}. I'm calling about a property "
            "enquiry. Do you have 1 minute?"
        )

    if language == "hindi":
        address = f"{name} ji, " if name else "Ji, "
        if source:
            return (
                f"{address}main {agent} {business} se baat kar rahi hoon. Hamme aapki property "
                f"enquiry {source} se mili thi. Kya aap k pass 1 minute h?"
            )
        return (
            f"{address}main {agent} {business} se baat kar rahi hoon. Main property enquiry ke "
            "baare mein call kar rahi hoon. Kya aap k pass 1 minute h?"
        )

    address = f"{name} ji, " if name else "Ji, "
    if source:
        return (
            f"{address}main {agent} {business} se bol rahi hoon. Hamme aapki property enquiry "
            f"{source} se mili thi. Kya aap k pass 1 minute h?"
        )
    return (
        f"{address}main {agent} {business} se bol rahi hoon. Main property enquiry ke silsile "
        "mein call kar rahi hoon. Kya aap k pass 1 minute h?"
    )


def build_reconnect_opening(
    *,
    lead_name: Optional[str],
    business_name: Optional[str],
    language: CallLanguage = "hinglish",
    agent_name: str = DEFAULT_AGENT_NAME,
) -> str:
    """Turn 1 of a same-number reconnect: apologise for the cut and ask to continue.

    ``business_name`` / ``agent_name`` are kept for callers; the line itself stays short.
    """
    del business_name, agent_name
    name = spoken_lead_name(lead_name)
    if language == "english":
        address = f" {name}" if name else ""
        return f"Sorry{address}, our last call probably got cut by mistake. Shall we continue now?"
    address = f" {name} ji" if name else ""
    if language == "hindi":
        return (
            f"Maaf kijiye{address}, pichli call shayad galti se kat gayi thi. "
            "Kya hum abhi baat aage badhayein?"
        )
    return f"Sorry{address}, last call shayad galti se cut ho gayi thi. Abhi continue karein?"


def build_voicemail_message(
    *,
    lead_name: Optional[str] = None,
    business_name: Optional[str] = None,
    language: CallLanguage = "hinglish",
    agent_name: str = DEFAULT_AGENT_NAME,
) -> str:
    """Short voicemail left after AMD detects machine-vm / machine-ivr."""
    name = spoken_lead_name(lead_name)
    business = " ".join(str(business_name or "our company").split()).strip() or "our company"
    agent = " ".join(str(agent_name or DEFAULT_AGENT_NAME).split()).strip() or DEFAULT_AGENT_NAME
    if language == "english":
        address = f"{name}, " if name else ""
        return (
            f"Hi {address}this is {agent} from {business}. We called regarding your property enquiry. "
            "Please give us a call back when you are free. Thank you."
        )
    if language == "hindi":
        address = f"{name} ji, " if name else ""
        return (
            f"Namaste {address}main {agent} {business} se bol rahi hoon. Aapki property enquiry ke "
            "baare mein call kiya tha. Jab free ho to hamme return call kar dijiye. Dhanyavaad."
        )
    address = f"{name} ji, " if name else ""
    return (
        f"Namaste {address}main {agent} {business} se bol rahi hoon. Aapki property enquiry ke "
        "silsile mein call kia tha. Jab free ho to please return call kar dijiye. Bahut shukriya."
    )
