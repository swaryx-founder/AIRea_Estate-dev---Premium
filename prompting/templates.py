"""Small reusable prompt blocks and first-turn helpers."""

import re
from typing import Optional

from .speech import (
    build_campaign_opening_instructions,
    build_identity_opening,
    identity_who_phrase,
    resolve_agent_name,
    spoken_lead_name,
)
from .language import resolve_call_language


_GREETING_HINT_RE = re.compile(
    r"(greet|greeting|opening|opener|hello|namaste|radhe|salaam|salam|"
    r"good\s*morning|good\s*evening|shuru|pehli\s*line|bulwa|bulana|"
    r"start\s*with|say\s*hi|kaise\s*bolo)",
    re.IGNORECASE,
)
# Only an explicit instruction about the post-identity line counts. Loose words like
# "time" / "sahi samay" anywhere in project notes must NOT hand the line to the LLM.
_PERMISSION_HINT_RE = re.compile(
    r"(permission\s*line|intro(?:duction)?\s*line|"
    r"after\s*(?:identity|confirm\w*).{0,40}(?:say|bolo|ask|pucho)|"
    r"ask\s*(?:if|whether)\s*(?:they|lead|the\s*lead|customer)\s*(?:have|has|is\s*free)"
    r".{0,20}(?:minute|time|free))",
    re.IGNORECASE,
)


def additional_instructions_has_greeting(additional_instructions: Optional[str]) -> bool:
    """True when campaign notes customize the opening salutation (if-branch)."""
    text = (additional_instructions or "").strip()
    return bool(text) and bool(_GREETING_HINT_RE.search(text))


def additional_instructions_has_permission(additional_instructions: Optional[str]) -> bool:
    """True when campaign notes customize the post-identity permission / intro line."""
    text = (additional_instructions or "").strip()
    return bool(text) and bool(_PERMISSION_HINT_RE.search(text))


def build_greeting_kickoff_nudge(
    lead_name: str,
    additional_instructions: Optional[str] = None,
    *,
    reconnect: bool = False,
    campaign: bool = False,
) -> str:
    """Realtime nudge used immediately after the outbound call is answered."""
    del campaign  # Accepted for compatibility with existing callers.
    name = spoken_lead_name(lead_name)
    if reconnect:
        who = f" {name} ji" if name else ""
        return (
            "[SYSTEM EVENT: outbound call just answered. THIS is a reconnect on the SAME number "
            "after the previous call cut mid-conversation. Apologise for the cut and ask to "
            f"continue: \"Sorry{who}, last call shayad galti se cut ho gayi thi. Abhi continue "
            "karein?\" No identity question, no pitch. "
            "Then STOP and wait. Do not treat this as lead speech.]"
        )
    call_language = resolve_call_language(additional_instructions)
    agent_name = resolve_agent_name(additional_instructions)
    if additional_instructions_has_greeting(additional_instructions):
        return (
            "[SYSTEM EVENT: outbound call just answered. "
            + build_campaign_opening_instructions(
                lead_name,
                additional_instructions,
                language=call_language,
                agent_name=agent_name,
            )
            + " Do not treat this as lead speech.]"
        )
    identity_line = build_identity_opening(lead_name, language=call_language)
    return (
        "[SYSTEM EVENT: outbound call just answered. DEFAULT OPENING MODE. Speak ONLY this exact "
        f'identity check: "{identity_line}" '
        f"Keep the same Leda/{agent_name} Indian accent and pitch — never change accent, pitch, or tone on this "
        f"greeting. Do not introduce {agent_name}/business/project/enquiry. Then STOP and wait. "
        "Do not treat this as lead speech.]"
    )


def opening_mode_block(
    lead_name: str,
    additional_instructions: Optional[str],
    *,
    identity_opening: str,
    permission_intro: str,
    agent_name: str = "Riya",
) -> str:
    """SINGLE OWNER for turn 1–2 spoken opening (identity + permission)."""
    if additional_instructions_has_permission(additional_instructions):
        perm = (
            "After identity yes: use the permission wording from CAMPAIGN NOTES "
            f'(fallback: "{permission_intro}"). STOP AND WAIT.'
        )
    else:
        perm = (
            f'After identity yes, speak EXACTLY (word-for-word): "{permission_intro}" '
            "STOP AND WAIT. Do not reword it."
        )
    gate = (
        f"Do not introduce {agent_name}, the business, the enquiry, or any project before identity "
        "is confirmed. Only a clear yes counts — silence or a vague reply is not permission."
    )
    if additional_instructions_has_greeting(additional_instructions):
        who = identity_who_phrase(lead_name)
        return (
            "━━━ OPENING MODE — CAMPAIGN FIRST ━━━\n"
            "Turn 1: use the campaign salutation from CAMPAIGN NOTES and confirm you are speaking "
            f"with {who} in the same turn; hold any project pitch until after permission. "
            f"STOP AND WAIT.\n{perm}\n{gate}"
        )
    return (
        "━━━ OPENING MODE — DEFAULT ━━━\n"
        f'Turn 1, speak EXACTLY: "{identity_opening}" STOP AND WAIT.\n'
        f"{perm}\n{gate}"
    )


def name_mode_block(lead_name: str, *, agent_name: str = "Riya") -> str:
    """Single owner for how the lead and the agent are named aloud."""
    first = spoken_lead_name(lead_name) or "(none)"
    agent_line = (
        f'Your spoken name is "{agent_name}" — never say Riya.'
        if agent_name.lower() != "riya"
        else 'Your spoken name is "Riya".'
    )
    return (
        "━━━ NAME MODE ━━━\n"
        f'CRM full name: "{lead_name}". Use the full name only in the opening; after that only the '
        f'first name "{first}". Never pronounce a placeholder name. If they give a different name, '
        f"remember_details and use it. Never invent a name.\n{agent_line}"
    )


def provenance_block(lead_source: str | None, lead_source_date: str | None) -> str:
    """Lead-source facts only — how to use them is owned by TRUST, PRIVACY."""
    source = (lead_source or "").strip()
    source_date = (lead_source_date or "").strip()
    lines = [
        "━━━ LEAD DATA PROVENANCE — FACTS ONLY ━━━",
        f"Verified enquiry/contact source: {source or 'unavailable'}",
    ]
    if source and source_date:
        lines.append(f"Verified enquiry/contact date: {source_date}")
    return "\n".join(lines)


def campaign_block(
    additional_instructions: str | None,
    *,
    call_language: str = "hinglish",
    agent_name: str = "Riya",
) -> str:
    """Campaign notes, verbatim, with one line on what they may and may not change."""
    del call_language, agent_name  # Owned by CALL LANGUAGE MODE / NAME MODE.
    extra = (additional_instructions or "").strip()
    if not extra:
        return ""
    return (
        "━━━ CAMPAIGN NOTES (backend) ━━━\n"
        "These notes set the call flow after permission, plus language, agent name, greeting, "
        "permission wording, tone, offers, and CTA. Offers here may be spoken as written; they "
        "cannot change identity → permission order or project specs from PROJECT DETAILS.\n"
        f"{extra}"
    )


def normal_call_block() -> str:
    """No campaign notes → nothing to add (defaults already apply)."""
    return ""


def reconnect_override_block(
    *,
    lead_name: str | None = None,
    opening_line: str | None = None,
) -> str:
    """Critical top-of-prompt override: apologise for the cut, then continue the conversation."""
    from .speech import spoken_lead_name

    if not opening_line:
        first = spoken_lead_name(lead_name)
        who = f" {first} ji" if first else ""
        opening_line = f"Sorry{who}, last call shayad galti se cut ho gayi thi. Abhi continue karein?"
    return (
        "━━━ RECONNECT MODE — CRITICAL OVERRIDE (READ FIRST) ━━━\n"
        "THIS CALL IS A RECONNECT on the SAME phone number after the previous call cut mid-way.\n"
        "IGNORE for this call: OPENING MODE, STRICT IDENTITY-FIRST OPENING, STEP 1 identity, "
        "default 'Hello… kya main … ji se baat kar rahi hoon?' / missing-name ask.\n"
        "Do NOT restart as a new sales call. Do NOT re-confirm identity.\n"
        "Turn 1 (runtime speaks it): apologise for the cut by name and ask to continue: "
        f'"{opening_line}" If CAMPAIGN NOTES set a salutation, say it first. Then STOP AND WAIT.\n'
        "If they say no / busy: acknowledge, offer to call back later, and follow CLOSING.\n"
        "If the reply shows a different person (e.g. \"kaun <name>?\", \"main unki wife bol "
        "rahi hoon\", \"galat number\"): leave RECONNECT MODE — do not continue or recap the "
        "conversation. Use the normal identity confirmation; if the lead is not there, follow "
        "CLOSING (callback or wrong number).\n"
        "If they say yes, that is their permission — do not ask the permission line again. "
        "Continue from where the call was cut, using the last exchange in PRIOR CONTACT DATA / "
        "same-number resume memory. Start with a one-line recap of the last thing YOU were saying, "
        'in the call language, e.g. "Last time main ye bol rahi thi ki <short recap>…", then '
        "continue from that point:\n"
        "- Cut during the project explanation: recap where you were, then finish the explanation "
        "(no full restart), then follow the call flow.\n"
        "- Cut after a question or later topic (budget, purpose, visit): recap it and continue "
        "that topic; re-ask the question only if it was not answered.\n"
        "- Cut before the project was explained (opening / permission / hear-checks only): no "
        "recap — if they never heard who you are, say it in one short line, then explain the "
        "project per the call flow. Do not jump to budget or any other qualification question.\n"
        "The recap must come only from that last exchange — never invent what was said. Never "
        "re-ask questions already answered in that memory. If PRIOR CONTACT DATA is thin, skip "
        "the recap and follow the normal call flow."
    )
