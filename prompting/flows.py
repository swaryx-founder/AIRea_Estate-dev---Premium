"""Call-type-specific flow templates — *what stage comes next*.

Persona, accent/language, turn shape, catalog, memory, and closing rules are appended
once by the builder (MANDATORY_RULES); opening turns 1–2 live in OPENING MODE.
"""

from .talk_track import DISCOVERY_TRACK, PROJECT_PITCH_TRACK

_OPENING_POINTER = "STEP 1–2 — OPENING + PERMISSION: see OPENING MODE."

DEFAULT_FLOW = f"""━━━ MODE — CONSULTANT (no project assigned) ━━━
Goal: understand {{spoken_lead_name}}'s requirements for {{business_name}} and book a visit when
appropriate.
{_OPENING_POINTER}

{DISCOVERY_TRACK}
━━━ DEFAULT CALL FLOW (after permission; flexible guide — not a rigid script) ━━━
STEP 3 — DISCOVERY TRACK: D1–D4.
STEP 4 — VISIT: when enough is known, ask about a visit; if yes, follow BOOKING. If no, don't push.
STEP 5 — CLOSE: follow CLOSING.
"""

SINGLE_PROJECT_FLOW = f"""━━━ MODE — SINGLE PROJECT ━━━
Goal: help {{spoken_lead_name}} evaluate {{project_name}} for {{business_name}} and book a visit when
appropriate.
{_OPENING_POINTER}

━━━ PROJECT DETAILS — SOURCE OF TRUTH ━━━
{{project_details}}

{PROJECT_PITCH_TRACK}
━━━ PROJECT CALL FLOW (after permission; flexible guide — not a rigid script) ━━━
STEP 3 — PITCH: PROJECT PITCH TRACK P1→P2.
STEP 4 — QUALIFY: P3 budget, then P4 own use or investment.
STEP 5 — ANSWER BY FIT: P5.
STEP 6 — VISIT: offer a site visit and follow BOOKING. Not ready → offer details on WhatsApp
(confirm the number) and remember_details.
STEP 7 — CLOSE: follow CLOSING.
"""

# ── Campaign calls (additional_instructions present) ──────────────────────────
# The campaign notes ARE the call flow. Only the verified project data and a one-line
# fallback remain, so the model never sees two competing step lists.
_CAMPAIGN_FLOW_POINTER = (
    "━━━ CALL FLOW — FROM CAMPAIGN NOTES ━━━\n"
    "After permission, follow CAMPAIGN NOTES for what to say and in what order. If they give no "
    "steps: {fallback}"
)

CAMPAIGN_DEFAULT_FLOW = (
    _CAMPAIGN_FLOW_POINTER.format(
        fallback="ask what they are looking for (one question per turn), then offer a site visit."
    )
    + "\n"
)

# Campaign notes may ask questions mid-explanation; without such a note, explain first.
_CAMPAIGN_QUESTION_TIMING = (
    "Question timing: if CAMPAIGN NOTES say to ask a question before or during the project "
    "explanation, ask it where the notes say. Otherwise explain the whole project in one turn "
    "with no question inside it, end by asking how they liked it (\"aapko ye project kaisa laga?\"), "
    "then ask budget and purpose one per turn."
)

CAMPAIGN_SINGLE_PROJECT_FLOW = f"""━━━ PROJECT DETAILS — SOURCE OF TRUTH ━━━
{{project_details}}

{_CAMPAIGN_FLOW_POINTER.format(
    fallback="explain the project, ask budget and purpose, then offer a site visit."
)}
{_CAMPAIGN_QUESTION_TIMING}
"""

CAMPAIGN_MULTI_PROJECT_FLOW = f"""━━━ AVAILABLE PROJECTS — SOURCE OF TRUTH ({{project_count}} projects) ━━━
{{projects_catalog}}

{_CAMPAIGN_FLOW_POINTER.format(
    fallback="explain PROJECT 1, ask budget and purpose, then offer a site visit."
)}
{_CAMPAIGN_QUESTION_TIMING}
Discuss only one project at a time; never list several or present a known mismatch.
"""

MULTI_PROJECT_FLOW = f"""━━━ MODE — MULTI PROJECT ({{project_count}} projects) ━━━
Goal: help {{spoken_lead_name}} pick the best fit among {{project_count}} projects for
{{business_name}} and book a visit when appropriate.
{_OPENING_POINTER}

━━━ AVAILABLE PROJECTS — SOURCE OF TRUTH ({{project_count}} projects) ━━━
{{projects_catalog}}

{PROJECT_PITCH_TRACK}
━━━ MULTI-PROJECT CALL FLOW (after permission; flexible guide — not a rigid script) ━━━
Discuss only one project at a time. Never call a project "newly launched" unless the catalog says so.
STEP 3 — FIRST PROJECT PITCH: PROJECT PITCH TRACK P1→P2 for PROJECT 1 only.
STEP 4 — QUALIFY: P3 budget, then P4 own use or investment.
STEP 5 — ANSWER BY FIT: P5 for the current project; switch only as ONE PROJECT AT A TIME allows,
then pitch the new one with P1→P2.
Never list several projects, repeat one already shown, or present a known mismatch. If nothing
matches, say so and offer the team (transfer only after yes). If "not interested" is ambiguous, ask
whether they mean this project or property altogether. On a preference, remember_details
"Preferred project: <exact name>" and stay on it.
STEP 6 — VISIT: offer a site visit and follow BOOKING. Not ready → offer details on WhatsApp
(confirm the number) and remember_details.
STEP 7 — CLOSE: follow CLOSING.
"""
