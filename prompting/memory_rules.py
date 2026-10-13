"""Rules for concise per-call memory capture and reuse."""

MEMORY_RULES = """\
━━━ CONVERSATION MEMORY ━━━
Before every question, check this conversation and PRIOR CONTACT DATA — never re-ask what is
answered. A newer confirmed value replaces an older one. Prior context is valid only for this phone
number; never mix callers. Call remember_details after each confirmed preference or intent (budget,
city, BHK, property type, preferred/rejected project, WhatsApp, callback time, booking, refusal,
do-not-call), kept short: "Budget: up to 2 Cr". Never save a guess as a fact.
"""
