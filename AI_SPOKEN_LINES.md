# AI spoken lines — every situation

Reference of what the voice agent says (or is instructed to say) in each call situation.

**Legend**

| Tag | Meaning |
|-----|---------|
| **EXACT** | Runtime / code speaks this fixed sentence (or near-exact via `session.say`) |
| **TEMPLATE** | Fixed wording with `{name}`, `{agent}`, `{business}`, `{source}` filled at runtime |
| **GUIDED** | Model speaks in its own words following meaning / example — not locked to one sentence |

Placeholders used below:

- `{Name}` = CRM full name (opening only)
- `{name}` = first name only
- `{agent}` = spoken agent name (default **Riya**, or campaign override)
- `{business}` = business / company name
- `{source}` = verified lead enquiry source (when available)
- `{project}` = catalog project name (never invent)

Default call language is **Hinglish** unless campaign notes set English / Hindi.

---

## 1. Call answered — Turn 1 (identity / opening)

### 1a. Default opening (no campaign greeting notes) — EXACT

| Language | With name | Without usable name |
|----------|-----------|---------------------|
| Hinglish / Hindi | `Hello… kya main {FirstName} ji se baat kar rahi hoon?` | `Hello… main kisse baat kar rahi hoon?` |
| English | `Hello… am I speaking with {FirstName}?` | `Hello… may I know who I'm speaking with?` |

**Rules:** No agent intro, no business, no project, no pitch. STOP and wait.

**Source:** `prompting/speech.py` → `build_identity_opening`

### 1b. Campaign opening (campaign notes customize greeting)

**EXACT** when the notes have a `Greeting:` line with the salutation in quotes
(e.g. `Greeting: Start with "Radhe Radhe!" then confirm…`). Pre-rendered while the
phone rings and played on answer:

| Language | With name | Without usable name |
|----------|-----------|---------------------|
| Hinglish / Hindi | `{Salutation} Kya main {FirstName} ji se baat kar rahi hoon?` | `{Salutation} Main kisse baat kar rahi hoon?` |
| English | `{Salutation} Am I speaking with {FirstName}?` | `{Salutation} May I know who I'm speaking with?` |

**GUIDED** fallback (no quoted `Greeting:` salutation, or audio not ready at answer): Gemini
uses the campaign salutation **and** confirms identity in the **same** turn. Still no
`{agent}` / business / project / pitch.

**Source:** `build_campaign_opening_line` (exact) / `build_campaign_opening_instructions` (guided)

### 1c. Reconnect (same number, previous call cut) — EXACT

With name:

```text
Mujhe maaf kar dijiye, last call isi number par technical glitch se cut ho gaya tha. {name} ji, kya aap ab sun pa rahe hain?
```

Without name:

```text
Mujhe maaf kar dijiye, last call isi number par technical glitch se cut ho gaya tha. Kya aap ab sun pa rahe hain?
```

**Rules:** No fresh identity check, no fresh permission intro. After they confirm hearing → resume from prior memory.

**Source:** `agent_runtime.py` reconnect branch + `reconnect_override_block`

---

## 2. After identity confirmed — permission / intro (Turn 2) — EXACT / TEMPLATE

Spoken **only after** identity yes (runtime gate). Campaign may override wording if notes customize permission.

### Hinglish (default)

| Source known? | Line |
|---------------|------|
| Yes | `{name} ji, main {agent} {business} se bol rahi hoon. Hamme aapki property enquiry {source} se mili thi. Kya aap k pass 1 minute h?` |
| No | `{name} ji, main {agent} {business} se bol rahi hoon. Main property enquiry ke silsile mein call kar rahi hoon. Kya aap k pass 1 minute h?` |

(If no first name: start with `Ji,` instead of `{name} ji,`)

### Hindi

| Source known? | Line |
|---------------|------|
| Yes | `{name} ji, main {agent} {business} se baat kar rahi hoon. Hamme aapki property enquiry {source} se mili thi. Kya aap k pass 1 minute h?` |
| No | `{name} ji, main {agent} {business} se baat kar rahi hoon. Main property enquiry ke baare mein call kar rahi hoon. Kya aap k pass 1 minute h?` |

### English

| Source known? | Line |
|---------------|------|
| Yes | `{name}, this is {agent} calling from {business}. We received your property enquiry from {source}. Do you have 1 minute?` |
| No | `{name}, this is {agent} calling from {business}. I'm calling about a property enquiry. Do you have 1 minute?` |

**Source:** `prompting/speech.py` → `build_permission_intro`

---

## 3. Silence failsafe (mid-call dead air) — EXACT

Triggers after **6 seconds** of silence (while armed). Repeats up to **8** times per silence streak. When the lead speaks, the streak resets to attempt 1.

| Attempt | Line |
|---------|------|
| 1 | `Hello {name} ji` (or `Hello ji` if no name) |
| 2 | `Hello, aap ko meri awaz aa rhi h` |
| 3+ | Cycles attempt 1 ↔ 2 |

English campaign variants (prompt helper only): `Hello {name}` / `Hello, can you hear me?`

**Rules:** Hear-check only — no pitch, no new sales question, no hangup.

**Source:** `prompting/language.py` → `silence_restart_line` · `agent_runtime.py` → `SilenceFailsafe`  
**Env:** `SILENCE_FAILSAFE_SEC=6`, `SILENCE_FAILSAFE_MAX=8`

---

## 4. Voicemail / AMD (machine detected) — EXACT / TEMPLATE

Left after AMD `machine-vm` / `machine-ivr`, then call ends as `voicemail`.

### Hinglish

```text
Namaste {name} ji, main {agent} {business} se bol rahi hoon. Aapki property enquiry ke silsile mein call kia tha. Jab free ho to please return call kar dijiye. Bahut shukriya.
```

### Hindi

```text
Namaste {name} ji, main {agent} {business} se bol rahi hoon. Aapki property enquiry ke baare mein call kiya tha. Jab free ho to hamme return call kar dijiye. Dhanyavaad.
```

### English

```text
Hi {name}, this is {agent} from {business}. We called regarding your property enquiry. Please give us a call back when you are free. Thank you.
```

(If no name, the `{name} ji,` / `{name},` address is omitted.)

**Source:** `prompting/speech.py` → `build_voicemail_message`

---

## 5. Privacy / “where did you get my number?” — GUIDED

Preferred shape (adapt naturally; keep short):

1. Empathy: `Main samajh sakti hoon, aapko doubt hona natural hai.`
2. Reason (only verified provenance):
   - With source: `Aapki property enquiry {business} ko mili thi ({source} se).`
   - Without source: `Aapki property enquiry {business} ko mili thi.`
3. Soft continue: `Agar aap comfortable ho to main briefly property options bataun — warna main call yahin rok sakti hoon. Aap kya prefer karenge?`

Then STOP and wait. No project pitch until they clearly agree.

**Source:** `prompting/policies.py` → `TRUST_AND_SAFETY_RULES`

---

## 6. After permission — project pitch (single / multi project) — GUIDED

Short turns only (2–4 sentences), then WAIT. Fill facts only from PROJECT DETAILS.

| Turn | Meaning / example ask |
|------|------------------------|
| T3 Brand + hook | Company + project + location; ask: `Kya main briefly bataun kyun yeh interesting ho sakta hai?` |
| T4 Location | Pehla reason location / connectivity (verified only); optional `Yeh clear hai?` |
| T5 Landmark | Nearest landmark + distance **only if in catalog** |
| T6 Soft value | Soft appreciation; never invent “X–Yx returns” |
| T7 CTA | Pricing / floor plans offer — wait for yes, then 1–2 facts + qualify |

**Source:** `prompting/talk_track.py` → `PROJECT_PITCH_TRACK`

---

## 7. After permission — consultant / no project — GUIDED

| Step | Ask |
|------|-----|
| D1 | City / area preference → WAIT |
| D2 | Intent (investment vs own stay) if useful → WAIT |
| D3 | Budget / BHK / possession — one question per turn → WAIT |
| D4 | Soft-offer matching options or site visit → WAIT |

**Source:** `prompting/talk_track.py` → `DISCOVERY_TRACK`

---

## 8. Booking confirmation — GUIDED (example locked)

Before `book_appointment`, confirm remembered name once:

- Hinglish: `Kya main {name} naam se aapki booking confirm kar doon?`
- English: `Shall I confirm the booking in the name of {name}?`

STOP and wait for yes. Never re-ask name if already known.

**Source:** `prompting/closing_rules.py`

---

## 9. Mid-call language switch — GUIDED

If lead asks (e.g. “English mein baat karo”, “Hindi mein boliye”), switch immediately, brief ack, continue **including goodbye** in that language. Do not switch unprompted.

**Source:** `prompting/language.py` → mid-call switch rule

---

## 10. Closing / goodbye — GUIDED (examples by language)

Speak a warm goodbye **first**, then `end_call` silently (never say tool names or “call ended”).

| Language | Example goodbye |
|----------|-----------------|
| Hinglish | `Bahut shukriya, aapka din shubh ho.` |
| Hindi | `Bahut dhanyavaad, aapka din shubh ho.` |
| English | `Thank you so much, have a great day.` |

### Situation → outcome (spoken close + tool)

| Lead situation | Spoken close (guided) | Outcome |
|----------------|----------------------|---------|
| Busy / call later | Acknowledge; confirm day/time if given | `callback_requested` |
| Vague “baad mein” | Ask once; if still vague → system default **in 10 minutes** | `callback_requested` |
| Not interested (whole call) | Warm goodbye | `not_interested` |
| “Call mat karo” / DNC | Confirm removal + warm goodbye | `not_interested` |
| Wrong person | Brief apology + goodbye | `wrong_number` |
| Off-topic ×2 | Short warm goodbye | `callback_requested` (no auto-dial) |
| Abuse continues | Warn once, then close | `not_interested` |
| Human requested | Transfer path | `transfer_to_human` |
| Site visit booked | Confirm + warm goodbye | `booked` |
| Interested hang-up | (classification; may already have spoken) | `interested` |

**Source:** `prompting/closing_rules.py` · `prompting/language.py` · `prompting/policies.py`

---

## 11. Other fixed spoken policies — GUIDED

| Situation | What to say |
|-----------|-------------|
| Apology (real mistake / glitch / wrong number) | Prefer `Mujhe maaf kar dijiye` — never the English word “Apologies” |
| Missing verified project fact | Natural: exact verified info nahi hai / similar — do not invent |
| Ambiguous answer (e.g. “dealer”) | Do **not** say “samajh gayi”; ask what they mean → WAIT |
| Off-topic question | Brief redirect to last property question |
| Competitor ask | Only discuss `{business}` projects |
| “Bot ho?” | Say you are `{business}`’s virtual assistant |
| Brochure request | Confirm WhatsApp + remember; no live send tool on call |

---

## 12. Speech delivery (all turns)

- Max **2–4 short sentences**, then STOP AND WAIT
- Same Leda / `{agent}` Indian tone every turn — never flip to US/UK accent
- Say “Riya” as **Ree-yaa** when agent name is Riya
- Configurations: “three BHK” / “teen BHK” — never “three-minus-BHK”
- Prices in natural Indian units when possible
- Never recite internal labels (“STEP 3”, “Reason 1”) aloud
- Never invent landmarks, distances, returns, portals, or consent claims

---

## Quick call timeline

```text
Ring → Answer
  ├─ AMD machine? → Voicemail line (§4) → end voicemail
  ├─ Reconnect?   → Glitch apology + hear-check (§1c) → resume memory
  └─ Fresh call
        Turn 1: Identity opening (§1a / §1b)
        Turn 2: Permission intro (§2)  [only after identity yes]
        Then:   Pitch (§6) or Discovery (§7)
        Anytime silence ≥6s: Failsafe lines (§3), rotating
        Privacy objection: Empathy path (§5)
        End:    Goodbye (§10) → silent end_call
```

---

## Code map

| Situation | Primary code |
|-----------|----------------|
| Identity / permission / voicemail | `prompting/speech.py` |
| Silence failsafe lines | `prompting/language.py` |
| Silence timer | `agent_runtime.py` → `SilenceFailsafe` |
| Greeting kickoff / reconnect prompt | `prompting/templates.py` |
| Pitch / discovery talk-track | `prompting/talk_track.py` |
| Privacy / reconnect / scope | `prompting/policies.py` |
| Booking / goodbye / outcomes | `prompting/closing_rules.py` |
| Flow steps | `prompting/flows.py` |
| Full prompt assembly | `prompting/builder.py` |
