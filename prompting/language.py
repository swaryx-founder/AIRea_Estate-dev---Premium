"""Voice identity, language, and speech-delivery policy.

Call language if/else:
  IF campaign notes mention English / Hindi / Hinglish → use that language for the call
  ELSE → default Hinglish (with English default identity opening exception)
"""

from __future__ import annotations

import re
from typing import Optional

# english | hindi | hinglish
CallLanguage = str

_ENGLISH_RE = re.compile(
    r"\b(?:speak|talk|use|reply|respond|switch|change|conversation|call)?\s*"
    r"(?:only\s+|pure\s+|full\s+|in\s+)?english\b"
    r"|\bonly\s+english\b"
    r"|\bpure\s+english\b"
    r"|\bin\s+english\b"
    r"|\benglish\s+only\b",
    re.IGNORECASE,
)
_HINDI_RE = re.compile(
    r"\b(?:speak|talk|use|reply|respond|switch|change)?\s*"
    r"(?:only\s+|pure\s+|full\s+|in\s+)?(?:hindi|हिंदी|हिन्दी)\b"
    r"|\bonly\s+hindi\b"
    r"|\bpure\s+hindi\b"
    r"|\bin\s+hindi\b"
    r"|\bhindi\s+only\b",
    re.IGNORECASE,
)
_HINGLISH_RE = re.compile(r"\bhinglish\b", re.IGNORECASE)

# Lead mid-call requests to switch language (last matching user turn wins).
_MIDCALL_ENGLISH_RE = re.compile(
    r"(?:speak|talk|reply|respond|switch|change|baat|bolo|boliye|karo|kariye)\s+"
    r"(?:to\s+|in\s+|only\s+)?english"
    r"|\bin\s+english\b"
    r"|\benglish\s+(?:mein|me|m\u0947|please|pls)\b"
    r"|\benglish\s+mein\s+baat\b"
    r"|\bonly\s+english\b",
    re.IGNORECASE,
)
_MIDCALL_HINDI_RE = re.compile(
    r"(?:speak|talk|reply|respond|switch|change|baat|bolo|boliye|karo|kariye)\s+"
    r"(?:to\s+|in\s+|only\s+)?(?:hindi|हिंदी|हिन्दी)"
    r"|\bin\s+(?:hindi|हिंदी|हिन्दी)\b"
    r"|\b(?:hindi|हिंदी|हिन्दी)\s+(?:mein|me|m\u0947|please|pls)\b"
    r"|\b(?:hindi|हिंदी|हिन्दी)\s+mein\s+baat\b"
    r"|\bonly\s+(?:hindi|हिंदी|हिन्दी)\b",
    re.IGNORECASE,
)
_MIDCALL_HINGLISH_RE = re.compile(
    r"(?:speak|talk|reply|respond|switch|change|baat|bolo|boliye|karo|kariye)\s+"
    r"(?:to\s+|in\s+|only\s+)?hinglish"
    r"|\bin\s+hinglish\b"
    r"|\bhinglish\s+(?:mein|me|please|pls)\b"
    r"|\bmix\s+(?:mein|me)\s+baat\b",
    re.IGNORECASE,
)


def resolve_call_language(additional_instructions: Optional[str] = None) -> CallLanguage:
    """Return campaign language if mentioned; otherwise hinglish."""
    text = (additional_instructions or "").strip()
    if not text:
        return "hinglish"

    # Explicit hinglish wins when named.
    if _HINGLISH_RE.search(text):
        return "hinglish"

    english_match = _ENGLISH_RE.search(text)
    hindi_match = _HINDI_RE.search(text)
    if english_match and hindi_match:
        # Prefer whichever appears last in the notes.
        return "english" if english_match.start() >= hindi_match.start() else "hindi"
    if english_match:
        return "english"
    if hindi_match:
        return "hindi"
    return "hinglish"


def detect_midcall_language_request(text: Optional[str]) -> Optional[CallLanguage]:
    """Return a language if the lead asked to switch; otherwise None."""
    raw = (text or "").strip()
    if not raw:
        return None
    matches: list[tuple[int, CallLanguage]] = []
    for regex, lang in (
        (_MIDCALL_HINGLISH_RE, "hinglish"),
        (_MIDCALL_ENGLISH_RE, "english"),
        (_MIDCALL_HINDI_RE, "hindi"),
    ):
        found = regex.search(raw)
        if found:
            matches.append((found.start(), lang))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def resolve_active_call_language(
    default_language: Optional[str] = "hinglish",
    transcript_tracker=None,
) -> CallLanguage:
    """Call language mode, overridden by the lead's latest mid-call language request."""
    active = (default_language or "hinglish").strip().lower() or "hinglish"
    if active not in ("english", "hindi", "hinglish"):
        active = "hinglish"
    lines = getattr(transcript_tracker, "lines", None) if transcript_tracker else None
    if not lines:
        return active
    for line in lines:
        if str(line.get("role") or "").lower() not in ("user", "lead", "human"):
            continue
        requested = detect_midcall_language_request(line.get("text"))
        if requested:
            active = requested
    return active


_MIDCALL_SWITCH_RULE = (
    "MID-CALL LANGUAGE SWITCH: switch only when the lead asks (e.g. \"English mein baat karo\", "
    "\"Hindi mein boliye\"), then keep that language for the rest of the call, including the "
    "final goodbye."
)

# Shared across every language mode — Indian accent + pitch never flip.
# Gemini Live often drifts to a US/UK accent on English words; this lock must stay
# explicit and high-priority so language content never changes voice character.
# It is the ONLY place the prompt states the accent rule — do not repeat it elsewhere.
def _leda_voice_continuity_rule(agent_name: str = "Riya") -> str:
    return (
        f"INDIAN ACCENT + PITCH LOCK (NON-NEGOTIABLE): You are ALWAYS the configured Leda voice "
        f"({agent_name}) — one fixed Indian feminine identity, natural Indian speaking tone, same "
        "accent, pitch, pace, and warmth on EVERY turn — greeting, mid-call, and goodbye — in any "
        "language. Language may change; accent and pitch NEVER change. Speak EVERY English word or "
        "phrase (BHK, budget, possession, site visit, project and company names, numbers, dates, "
        "full English sentences) in the same Indian accent — never slip into US, UK, Australian, or "
        "any Western accent, not even for one word. Never flip to a foreign accent. Never imitate "
        "the lead's voice, accent, or gender."
    )


def indian_accent_runtime_reminder(agent_name: str = "Riya") -> str:
    """Short accent lock appended to generate_reply instructions (greeting/farewell/nudges)."""
    spoken = (agent_name or "Riya").strip() or "Riya"
    return (
        f"VOICE LOCK: Keep the same Leda/{spoken} Indian accent and pitch for every word — "
        "including English words. Never use a foreign / US / UK accent."
    )


def _speech_safe_name_line(agent_name: str) -> str:
    if (agent_name or "").strip().lower() == "riya":
        return 'Say "Riya" as "Ree-yaa".'
    return f'Say "{agent_name}" clearly and naturally.'


_LANGUAGE_BODY = {
    "english": (
        "CAMPAIGN LANGUAGE — ENGLISH: speak clear, natural Indian English for the whole call. If the "
        "lead can't follow, simplify in English first before any brief Hindi help.",
        '"three BHK"',
        '"forty-two lakh rupees"',
    ),
    "hindi": (
        "CAMPAIGN LANGUAGE — HINDI: speak natural conversational Hindi for the whole call; common "
        "property words (budget, BHK, site visit) may stay in English.",
        '"teen BHK"',
        '"bayalees lakh rupaye"',
    ),
    "hinglish": (
        "CALL LANGUAGE — HINGLISH: speak natural conversational Hinglish — English property terms "
        "(budget, BHK, location, possession, site visit) with Hindi connectors. Never insert a "
        "standalone corporate English sentence (no corporate lines like \"property-related "
        "outreach\"). Understand English or Hindi input.",
        '"three BHK"',
        '"bayalees lakh rupees"',
    ),
}


def language_policy_block(language: CallLanguage, *, agent_name: str = "Riya") -> str:
    """Single language/accent/speech section for the chosen call language."""
    lang = language if language in _LANGUAGE_BODY else "hinglish"
    body, bhk_example, price_example = _LANGUAGE_BODY[lang]
    return (
        f"{language_mode_banner(lang)}\n"
        f"{_leda_voice_continuity_rule(agent_name)}\n"
        f"{body}\n"
        f"{_MIDCALL_SWITCH_RULE}\n"
        f"{_speech_safe_name_line(agent_name)} Say names slowly and clearly; configurations as "
        f"{bhk_example} (never \"three-minus-BHK\"); prices in Indian units ({price_example}). "
        "Never speak punctuation.\n"
    )


def language_mode_banner(language: CallLanguage) -> str:
    label = {"english": "ENGLISH", "hindi": "HINDI"}.get(language, "HINGLISH")
    return f"━━━ CALL LANGUAGE MODE — {label} ━━━"


def silence_restart_line(
    *,
    lead_name: Optional[str] = None,
    attempt: int = 1,
) -> str:
    """Soft hear-check line for silence failsafe; rotates by attempt number."""
    from .speech import spoken_lead_name

    name = spoken_lead_name(lead_name or "")
    n = max(1, int(attempt or 1))
    first = f"Hello {name} ji" if name else "Hello ji"
    second = "Hello, aap ko meri awaz aa rhi h"
    # Attempt 1 / 2 use the dedicated lines; further silence cycles them.
    if n == 1:
        return first
    if n == 2:
        return second
    return first if (n % 2) == 1 else second


def silence_check_lines(
    language: CallLanguage,
    *,
    lead_name: Optional[str] = None,
) -> tuple[str, str]:
    """Return (first_check, second_check) spoken during silence recovery."""
    first = silence_restart_line(lead_name=lead_name, attempt=1)
    second = silence_restart_line(lead_name=lead_name, attempt=2)
    if language == "english":
        from .speech import spoken_lead_name

        name = spoken_lead_name(lead_name or "")
        eng_first = f"Hello {name}" if name else "Hello"
        return (
            eng_first,
            "Hello, can you hear me?",
        )
    if language == "hindi":
        return (first, second)
    return (first, second)


def build_silence_failsafe_nudge(
    *,
    attempt: int = 1,
    lead_name: Optional[str] = None,
) -> str:
    """Realtime/system nudge when the lead stays silent after the agent spoke (~7s)."""
    from .speech import spoken_lead_name

    n = max(1, int(attempt or 1))
    name = spoken_lead_name(lead_name)
    if n <= 1:
        spoken = f"{name} ji? Aap sun rahe hain?" if name else "Ji? Aap sun rahe hain?"
    else:
        spoken = (
            f"Hello {name} ji, aapko awaaz aa rahi hai?"
            if name
            else "Hello, aapko awaaz aa rahi hai?"
        )
    line = (
        f'Speak ONLY this exact short sentence aloud, nothing else, then STOP: "{spoken}"'
    )
    return (
        f"[SYSTEM EVENT: lead silent for several seconds (failsafe #{n}). "
        f"{line} "
        f"{indian_accent_runtime_reminder()} "
        "Do NOT pitch a project, do NOT ask a new sales question, do NOT end the call. "
        "Then STOP and wait for their reply. "
        "Do not treat this message as something the lead said.]"
    )


# Back-compat: policies.py historically imported LANGUAGE_RULES as a static string.
# Builder now injects language_policy_block() per call; this default is hinglish.
LANGUAGE_RULES = language_policy_block("hinglish")
