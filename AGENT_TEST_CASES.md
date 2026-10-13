# AI Calling Agent Test Catalogue

This is the canonical test inventory for the outbound real-estate calling agent.
Test IDs are permanent: do not renumber or reuse them. Add new cases at the end of
the relevant section. Execution history belongs in `AGENT_TEST_RESULTS.md`.

## Status and evidence rules

- `PASS`: observed result matches every expected result.
- `FAIL`: at least one expected result was not met.
- `BLOCKED`: execution could not start because a dependency or credential was unavailable.
- `NOT RUN`: deliberately excluded from the run.
- Automated results must name the pytest file/test or command used as evidence.
- Live-call results must include call ID, environment, worker name, and sanitized evidence.
- Never put API keys, SIP passwords, auth tokens, full phone numbers, or raw credentials in either file.

Priorities: `P0` security/data-loss, `P1` primary call behavior, `P2` important edge case,
`P3` cosmetic or low-risk behavior.

## A. API and request validation

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| API-001 | P1 | Auto | Submit valid single-call snake_case payload | Request validates and dispatch metadata contains supplied fields | `scripts/ci_smoke_test.py` |
| API-002 | P1 | Auto | Submit camelCase aliases | Aliases normalize without losing values | `scripts/ci_smoke_test.py` |
| API-003 | P1 | Auto | Submit `projectDetails` as string | Single-project mode selected | `scripts/ci_smoke_test.py` |
| API-004 | P1 | Auto | Submit `projectDetails` as object | Object becomes speech-friendly project text | `scripts/ci_smoke_test.py` |
| API-005 | P1 | Auto | Submit `projectDetails` as one-item array | Single-project mode selected | `scripts/ci_smoke_test.py` |
| API-006 | P1 | Auto | Submit `projectDetails` as multi-item array | Multi-project mode preserves request order | `scripts/ci_smoke_test.py` |
| API-007 | P1 | Auto | Missing phone/contacts | Validation rejects request | `scripts/ci_smoke_test.py` |
| API-008 | P1 | Auto | Missing organization or lead ID | Call initiation is rejected | `scripts/test_organization_scope.py` |
| API-009 | P1 | Manual | Invalid or unleased caller ID | Request fails safely without dialing | Not automated |
| API-010 | P1 | Manual | Worker name does not exist | Dispatch failure is explicit and call is finalized | Not automated |

## B. Campaign context propagation

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| CAM-001 | P1 | Auto | Campaign-level instructions only | Instructions appear in final prompt | `scripts/test_campaign_context.py` |
| CAM-002 | P1 | Auto | Campaign-level single project | Project ID and details reach prompt | `scripts/test_campaign_context.py` |
| CAM-003 | P1 | Auto | Campaign-level multiple projects | All projects reach prompt in supplied order | `scripts/test_campaign_context.py` |
| CAM-004 | P1 | Auto | Contact has its own project | Contact project overrides campaign default | `scripts/test_campaign_context.py` |
| CAM-005 | P2 | Auto | Campaign and contact instructions differ | Both instruction layers are retained | `scripts/test_campaign_context.py` |
| CAM-006 | P2 | Auto | Same instruction appears twice | Final metadata contains it once | `scripts/test_campaign_context.py` |
| CAM-007 | P1 | Auto | Single-project per-project notes | Notes reach final project prompt | `scripts/test_campaign_context.py` |
| CAM-008 | P1 | Manual | Campaign DB lacks optional instruction column | Contact-carried instructions still reach call | Not automated end-to-end |
| CAM-009 | P2 | Manual | Manual and scheduled campaign runs | Both attach campaign ID to metadata/webhook | Not automated end-to-end |
| CAM-010 | P1 | Manual | `campaign_id` supplied to `/api/call` without content | ID remains metadata only; content is not silently fetched | Not automated |

## C. Opening, permission, identity, and language

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| CONV-001 | P1 | Auto | Fresh call with known full name | Turn 1 confirms full identity only | `scripts/test_trust_conversation_rules.py` |
| CONV-002 | P1 | Auto | Missing/generic lead name | Agent asks for name and never speaks placeholder | `scripts/test_trust_conversation_rules.py` |
| CONV-003 | P1 | Auto | Campaign greeting supplied | Greeting style plus identity confirmation; no pitch | `scripts/test_trust_conversation_rules.py` |
| CONV-004 | P1 | Auto | No campaign greeting | Exact default identity opening used | `scripts/test_trust_conversation_rules.py` |
| CONV-005 | P1 | Auto | Identity confirmed | Agent gives intro/provenance and asks permission | `scripts/test_trust_conversation_rules.py` |
| CONV-006 | P1 | Auto | Silence or ambiguous response to permission | Project pitch remains blocked | `scripts/test_trust_conversation_rules.py` |
| CONV-007 | P1 | Auto | Campaign selects English/Hindi/Hinglish | Resolved language matches instructions | `scripts/test_trust_conversation_rules.py` |
| CONV-008 | P1 | Auto | Campaign selects agent name | Spoken agent name changes consistently | `scripts/test_trust_conversation_rules.py` |
| CONV-009 | P1 | Manual | Lead asks whether agent is AI | Agent answers honestly and briefly | Live call required |
| CONV-010 | P2 | Manual | Lead changes language mid-call | Agent follows requested language without resetting flow | Live call required |
| CONV-011 | P2 | Manual | Reconnect after cut call | No new identity check; resumes from prior context | `scripts/test_reconnect_context.py` plus live call |

## D. Natural conversation and sales behavior

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| TALK-001 | P1 | Auto | Direct factual question | Answer comes before any qualification question | Prompt-rule assertion |
| TALK-002 | P1 | Auto | Simple confirmation | Brief acknowledgement; no forced empathy formula | Prompt-rule assertion |
| TALK-003 | P1 | Auto | Real objection | Specific acknowledgement, verified answer, at most one next question | Prompt-rule assertion |
| TALK-004 | P1 | Auto | Lead is stressed/upset | Pitch stops; response is considerate and brief | Prompt-rule assertion |
| TALK-005 | P1 | Auto | Lead is busy/interrupted | Pitch stops; callback offered only when appropriate | Prompt-rule assertion |
| TALK-006 | P1 | Auto | Lead is confused or audio unclear | Rephrases or asks only for missed part; does not guess | Prompt-rule assertion |
| TALK-007 | P1 | Auto | Lead says not interested | Acknowledges once, closes, no renewed pitch | Prompt-rule assertion |
| TALK-008 | P1 | Auto | Several questions in one turn | Answers known questions briefly; unknown fields handled separately | Prompt-rule assertion |
| TALK-009 | P1 | Auto | Agent asks a question | At most one question, then waits | `scripts/test_trust_conversation_rules.py` |
| TALK-010 | P2 | Manual | Lead gives repeated one-word answers | Agent remains concise and does not interrogate | Live call required |
| TALK-011 | P2 | Manual | Lead requests long explanation | Agent expands in small chunks without monologue | Live call required |
| TALK-012 | P1 | Manual | Lead ends conversation | Goodbye is spoken before silent `end_call` | Live call required |

## E. Project facts and project selection

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| PROJ-001 | P1 | Auto | Known project field requested | Agent is instructed to use present catalog fact | `scripts/test_organization_scope.py` |
| PROJ-002 | P1 | Auto | Specific field absent | Missing-field response applies only to that field | `scripts/test_organization_scope.py` |
| PROJ-003 | P0 | Auto | Custom prompt injects another project | Verified scoped project wins | `scripts/test_organization_scope.py` |
| PROJ-004 | P0 | Auto | Project belongs to another organization | Request is rejected | `scripts/test_organization_scope.py` |
| PROJ-005 | P1 | Auto | Project is inactive | Request is rejected | `scripts/test_organization_scope.py` |
| PROJ-006 | P1 | Auto | Previously unseen trusted project | Project is registered only for requesting organization | `scripts/test_organization_scope.py` |
| PROJ-007 | P0 | Review | Existing project supplied with different facts | Must follow explicitly approved catalog-refresh policy; no accidental overwrite | Current behavior needs product/security decision |
| PROJ-008 | P1 | Auto | Multiple projects supplied | Agent begins with project 1 only | Prompt-rule assertion |
| PROJ-009 | P1 | Auto | Current project rejected | Agent asks before presenting matching alternative | Prompt-rule assertion |
| PROJ-010 | P1 | Auto | Campaign fact conflicts with catalog | Conflicting campaign fact is ignored | `scripts/test_trust_conversation_rules.py` |
| PROJ-011 | P2 | Manual | Price/BHK spoken aloud | Indian units and configurations are pronounced naturally | Live call required |
| PROJ-012 | P2 | Manual | No project matches hard requirements | Agent states limitation and does not invent a match | Live call required |

## F. Brochure, WhatsApp, booking, and tools

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| TOOL-001 | P1 | Auto | Brochure/floor-plan request | Confirms post-call follow-up; never claims already sent | Prompt-rule assertion |
| TOOL-002 | P1 | Auto | WhatsApp confirmed | Number and exact material request are remembered | Prompt-rule assertion |
| TOOL-003 | P1 | Manual | Valid appointment time confirmed | Booking tool called once with confirmed name/time | `scripts/test_atomic_booking.py` plus live call |
| TOOL-004 | P1 | Manual | Appointment tool fails | Agent does not claim booking succeeded | Live call required |
| TOOL-005 | P1 | Manual | Lead asks for human transfer | Transfer occurs only after request/acceptance | Live call required |
| TOOL-006 | P1 | Auto | Callback outcome has no time | Tool/runtime requests or stores appropriate callback schedule | `scripts/test_callback_schedule.py` |
| TOOL-007 | P1 | Auto | Slow tool action | Prompt instructs short hold phrase without false completion claim | Prompt-rule assertion |

## G. Silence, disconnects, reconnects, and call lifecycle

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| SIL-001 | P1 | Auto | Silence after opening | First short hear-check fires after configured delay | Partial: `scripts/test_silence_failsafe.py` |
| SIL-002 | P1 | Auto | Hear-check is speaking | A second hear-check is not stacked | `scripts/test_silence_failsafe.py` |
| SIL-003 | P1 | Auto | Lead speaks after hear-check | Counter and pending state reset | `scripts/test_silence_failsafe.py` |
| SIL-004 | P1 | Auto | Two unanswered hear-checks | Call finalizes and hangs up once | Partial: `scripts/test_silence_failsafe.py` |
| SIL-005 | P1 | Auto | Agent speech follows recent lead speech | Watchdog rearms after grace period | **Not covered; known risk** |
| SIL-006 | P1 | Manual | Lead interrupts hear-check | Hear-check stops and no hangup follows | Live call required |
| SIL-007 | P2 | Manual | Speech-end event is missing | Four-second fallback advances state once | Live/runtime simulation required |
| LIFE-001 | P1 | Auto | SIP disconnect after clear intent | Intent outcome replaces generic disconnect | `scripts/test_client_requirements.py` |
| LIFE-002 | P1 | Auto | SIP disconnect without intent | Outcome stays `disconnect_in_between` | `scripts/test_client_requirements.py` |
| LIFE-003 | P1 | Auto | Last user intent changes | Last clear user signal wins | `scripts/test_client_requirements.py` |
| LIFE-004 | P1 | Auto | Stale ringing call | Finalized after ringing timeout | DB integration coverage required |
| LIFE-005 | P1 | Auto | Stale in-progress call | Finalized after true transcript inactivity | DB integration coverage required |
| LIFE-006 | P1 | Auto | Older duplicate active call | Older row is finalized even if recently active | **Not covered; known risk** |
| LIFE-007 | P2 | Auto | Stale call has property-data intent only | Reconciliation includes and uses property data | **Not covered; known risk** |

## H. Outcomes, CRM, scoring, and webhooks

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| CRM-001 | P1 | Auto | Interested phrase | Outcome becomes `interested` | `scripts/test_client_requirements.py` |
| CRM-002 | P1 | Auto | Negated interest | Outcome becomes `not_interested`, never interested | `scripts/test_client_requirements.py` |
| CRM-003 | P1 | Auto | Do-not-call phrase | Outcome becomes `do_not_call` | `scripts/test_client_requirements.py` |
| CRM-004 | P1 | Auto | Interested webhook | Sentiment is positive and requirements are included | `scripts/test_call_webhook.py` |
| CRM-005 | P1 | Auto | Not-interested/DNC webhook | Sentiment is negative | `scripts/test_call_webhook.py` |
| CRM-006 | P2 | Auto | Neutral outcomes | Sentiment is zero | `scripts/test_call_webhook.py` |
| CRM-007 | P2 | Auto | Interested AI score | Positive base score is applied | Unit coverage in DB tests needed |
| CRM-008 | P1 | Auto | Terminal outcome reconciliation | Terminal result is not downgraded to failed | DB integration coverage required |
| CRM-009 | P1 | Manual | Completion webhook retry/failure | Failure is logged and retry policy behaves as configured | Integration environment required |

## I. Security, privacy, and tenant isolation

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| SEC-001 | P0 | Auto | Cross-organization project ID | Rejected without disclosing other tenant data | `scripts/test_organization_scope.py` |
| SEC-002 | P0 | Auto | Cross-organization call/project binding | Rejected | `scripts/test_organization_scope.py` |
| SEC-003 | P0 | Auto | Prompt injection requests internal rules | Agent refuses and stays in real-estate scope | Prompt-rule assertion |
| SEC-004 | P0 | Review | Logs contain credentials | Secrets must be redacted | Static/integration review |
| SEC-005 | P0 | Manual | Lead asks source of phone number | Only verified provenance is stated | Live call required |
| SEC-006 | P0 | Manual | Unknown provenance | Agent does not invent portal, partner, consent, or date | Live call required |
| SEC-007 | P0 | Review | Call request refreshes existing project record | Must be explicitly authorized, audited, and validated | Current behavior needs decision |

## J. Voice and live-call quality

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| VOICE-001 | P1 | Manual | Hinglish call | Stable configured voice; clear natural Hinglish | Live call required |
| VOICE-002 | P1 | Manual | Full English call | Natural Indian English; no persona change | Live call required |
| VOICE-003 | P1 | Manual | Hindi call with English property terms | Same voice/pitch across mixed terms | Live call required |
| VOICE-004 | P2 | Manual | Names, project names, numbers, prices | Pronounced clearly without reading punctuation | Live call required |
| VOICE-005 | P1 | Manual | Greeting-to-conversation transition | No accent, pitch, or persona jump | Live call required |
| VOICE-006 | P1 | Manual | Farewell | Same voice/language; exactly one closing | Live call required |
| VOICE-007 | P2 | Manual | Slow network/tool response | No long unexplained dead air or repeated filler | Live call required |
| VOICE-008 | P2 | Manual | Short/long lead responses | Agent adapts length without sounding scripted | Live call required |

## Release gate

A production release requires:

1. All automated P0/P1 cases passing.
2. No unresolved P0 defects.
3. Every unresolved P1 defect explicitly accepted by the owner.
4. At least one successful live call each in Hinglish, English, and Hindi.
5. One silence/hangup call, one multi-project call, one booking call, and one rejection call.
6. Credentials rotated if they appeared in chat, logs, screenshots, or test fixtures.

## K. Production engineering and operations

| ID | Pri | Type | Test | Expected result | Automation |
|---|---:|---|---|---|---|
| OPS-001 | P0 | Auto | Production API authentication | Auth token is configured; protected routes reject missing/invalid credentials | Middleware test plus environment check |
| OPS-002 | P0 | Review | Browser credential handling | Gemini/SIP/LiveKit secrets are not persisted in browser storage or URLs | Static frontend review |
| OPS-003 | P1 | Auto | CORS policy | Only approved production origins are allowed with credentials | Middleware configuration review |
| OPS-004 | P1 | Auto | Health/readiness route | Exactly one handler exists and reports critical dependency/worker readiness | Route uniqueness plus live probe |
| OPS-005 | P1 | Auto | Supabase schema/connectivity | Required tables and columns are reachable | `scripts/check_all.py` plus schema checks |
| OPS-006 | P1 | Auto | LiveKit connectivity | Read-only room listing succeeds | `scripts/check_all.py` |
| OPS-007 | P1 | Auto | Recording storage | Configured Azure container exists and is writable/readable | `scripts/check_all.py` plus upload test in staging |
| OPS-008 | P1 | Auto | Vobiz configuration/connectivity | SIP/trunk and optional REST credentials match deployment mode | `scripts/check_all.py` plus staging call |
| OPS-009 | P1 | Auto | Webhook reachability | Real webhook health contract returns success; completion POST tested in staging | Integration check |
| OPS-010 | P1 | Auto | Worker/API supervision | Container becomes unhealthy/restarts if either API or agent worker dies | Container integration test |
| OPS-011 | P1 | Auto | Docker build and Python parity | Image builds and tests pass under Docker's Python 3.11 | Docker/CI required |
| OPS-012 | P0 | Auto | Dependency vulnerability audit | No unaccepted high/critical vulnerabilities; lockfile is reproducible | `pip-audit` in clean image |
| OPS-013 | P1 | Auto | Static correctness | No undefined names or other release-blocking lint errors | Ruff |
| OPS-014 | P1 | Auto | Security static analysis | No unaccepted medium/high findings | Bandit |
| OPS-015 | P1 | Auto | Test coverage | Critical runtime/API/DB modules meet agreed thresholds (target ≥80%) | Coverage.py |
| OPS-016 | P0 | Review | Supabase access control | RLS/policies or an equivalent private-network guarantee prevents client access | Schema/security review |
| OPS-017 | P1 | Auto | Requirements reproducibility | Exact versions and hashes/lockfile produce repeatable builds | Clean-environment install |
| OPS-018 | P1 | Manual | End-to-end staging call matrix | Hinglish, English, Hindi, silence, reject, booking, reconnect, and multi-project calls pass | Staging LiveKit/SIP |

