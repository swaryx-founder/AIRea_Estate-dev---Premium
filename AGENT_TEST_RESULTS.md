# AI Calling Agent Test Results

Append one section per execution. Do not rewrite older runs. Use test IDs from
`AGENT_TEST_CASES.md`. Never record credentials or unsanitized phone numbers.

## Run template

```text
Run ID:
Date/time (IST):
Tester:
Environment:
Git revision / workspace state:
Commands:

Automated summary:
- Passed:
- Failed:
- Skipped/Xfailed:
- Warnings:

Manual/live summary:
- Passed:
- Failed:
- Blocked:
- Not run:

Defects/observations:
- Test ID | Severity | Result | Evidence | Follow-up

Release recommendation:
```

---

## Run QA-2026-10-03-01

- **Date/time (IST):** 2026-10-03
- **Tester:** Codex senior test review
- **Environment:** Local Windows workspace; no outbound call placed
- **Git revision:** `29f355e` plus uncommitted working-tree changes
- **Scope:** Initial baseline created before executing the complete local suite
- **Commands:**
  - `python -m pytest scripts -q`
  - `python -m compileall -q agent_runtime.py client_requirements.py db.py prompting server.py tools.py scripts`
  - `git diff --check`

### Automated summary

- **Pytest:** PASS — 100 passed, 0 failed, 0 skipped in 9.40 seconds
- **Python compilation:** PASS
- **Diff whitespace/error validation:** PASS
- **Warnings:** 5
  - Four FastAPI `on_event`/lifespan deprecation warnings.
  - One `google-auth` warning that `grpcio < 1.83.0` loses supported non-PQC compatibility in October 2026.

### Covered areas

- API payload normalization, including rich `projectDetails` objects and arrays.
- Campaign instructions, defaults, contact overrides, and single-project notes.
- Organization/project isolation and current catalog-refresh behavior.
- Prompt trust, opening, permission, language, project, and closing rules.
- Customer requirement and intent extraction.
- Webhook outcome and sentiment mappings.
- Silence-failsafe state guards currently represented by the unit suite.
- Reconnect, transcript, booking, callback, billing, and follow-up helpers represented in `scripts/`.

### Manual/live summary

- **Live outbound calls:** NOT RUN
- **Hinglish/English/Hindi voice-quality cases:** NOT RUN
- **Real SIP silence, interruption, transfer, booking, webhook retry, and caller-ID cases:** NOT RUN
- **Reason:** this run intentionally performed local, non-destructive automated validation only.

### Defects and observations

| Test ID | Severity | Result | Evidence | Follow-up |
|---|---|---|---|---|
| SIL-005 | P1 | NOT COVERED | Current unit test expects no immediate timer during recent speech but does not prove delayed rearming | Add delayed-rearm regression test and correct watchdog scheduling before release |
| LIFE-006 | P1 | NOT COVERED | No test proves an older, recently active duplicate is finalized | Add DB reconciliation ordering test |
| LIFE-007 | P1 | NOT COVERED | Stale-call query does not currently prove property-data fallback is available | Include `property_data` in reconciliation coverage/query |
| PROJ-007 / SEC-007 | P0 decision | REVIEW REQUIRED | Current automated suite confirms that trusted call dispatch refreshes an existing catalog record | Decide whether call dispatch is authorized to mutate catalog; otherwise move to explicit upsert API |
| VOICE-001–008 | P1/P2 | NOT RUN | Prompt assertions cannot validate actual synthesized speech | Execute and review sanitized recordings in a test environment |

### Release recommendation

**Conditional — not yet approved for production based solely on this run.** The automated baseline is green,
but the P0 catalog-mutation decision, SIL-005 watchdog risk, and mandatory live-call matrix remain open.

---

## Run QA-2026-10-03-02 — Production-readiness audit

- **Date/time (IST):** 2026-10-03
- **Tester:** Codex senior developer/tester
- **Environment:** Local Windows Python 3.12 plus read-only checks against configured external services
- **Git revision:** `29f355e` plus uncommitted working-tree changes
- **Scope:** Functional, static, security, dependency, API, prompt-performance, infrastructure, and deployment review

### Commands and checks

- `python -m pytest scripts -q`
- `python scripts/ci_smoke_test.py`
- `python scripts/check_all.py` (approved external read-only connectivity)
- `python -m compileall -q .`
- `git diff --check`
- `python -m pip check`
- Coverage.py test run/report
- Ruff static analysis
- Bandit static security scan
- pip-audit against the local environment and direct requirements attempt
- FastAPI TestClient health/auth checks
- OpenAPI route and duplicate route analysis
- Prompt construction benchmark
- Tracked-file secret-pattern scan

### Passed

- **Pytest:** 100 passed, 0 failed, 0 skipped.
- **Offline smoke suite:** all checks passed.
- **Compilation:** passed.
- **Git diff validation:** passed.
- **Package consistency:** `pip check` reported no broken requirements, but warned about an invalid local `~emini-key-pool` distribution.
- **FastAPI import/OpenAPI/root:** passed; 49 registered routes and OpenAPI generation succeeded.
- **Auth middleware behavior when configured:** missing/bad token returned 401; valid header token passed middleware; health remained public.
- **Supabase connectivity:** passed; `call_logs`, `appointments`, `campaigns`, `settings`, `agent_profiles`, and `error_logs` were readable.
- **Supabase production schema:** passed explicit required-column checks for all 11 application tables.
- **LiveKit API connectivity:** passed; room listing succeeded.
- **Prompt build performance:** 250 three-project prompts built in about 0.054 seconds (~0.22 ms each).
- **Bandit:** 0 high-severity findings; 1 medium and 54 low findings.

### Failed or blocked

| Test ID | Severity | Result | Evidence / impact |
|---|---|---|---|
| OPS-001 | P0 | FAIL | `API_AUTH_TOKEN` is unset; production-mode API logs that all `/api/*` routes remain open |
| OPS-002 | P0 | FAIL | Dashboard persists API/client credentials in `localStorage`; SSE authentication also places API token in URL query string |
| OPS-003 | P1 | FAIL | CORS uses `allow_origins=["*"]` with credentials enabled; a preflight from `https://evil.example` was accepted with credentials |
| OPS-004 | P1 | FAIL | Duplicate `GET /api/health` handlers; actual response contains only `status` and `service`, proving the first generic handler masks detailed readiness |
| OPS-007 | P1 | FAIL | Azure Blob returned `AccountIsDisabled`; recording storage is unavailable |
| OPS-008 | P1 | FAIL/CONFIG | Environment-level Vobiz SIP/trunk fields and Vobiz REST credentials are missing; per-request credentials may work but deployment fallback is not ready |
| OPS-009 | P1 | FAIL | Gateway host is reachable but the assumed `/api/health` check returned 404; current checker treats any status below 500 as success |
| OPS-010 | P1 | FAIL BY REVIEW | `start.sh` waits only for Uvicorn; agent-worker death does not make the container exit or become unhealthy |
| OPS-011 | P1 | BLOCKED | Docker is unavailable locally; image build and Python 3.11 parity were not tested |
| OPS-012 | P0 | FAIL | Local audit found 73 vulnerability records across 17 installed packages; requirements are unpinned, so deployed resolution is not reproducible/auditable |
| OPS-013 | P1 | FAIL | Ruff found 788 issues, including two real undefined names: missing `re` imports in `db.py` and `server.py` |
| OPS-014 | P1 | FAIL | Bandit found one medium issue and 54 low issues; findings require triage/acceptance |
| OPS-015 | P1 | FAIL | Overall coverage is 34%; critical coverage: `agent_runtime.py` 19%, `db.py` 24%, `server.py` 29%, `tools.py` 21% |
| OPS-016 | P0 | FAIL BY REVIEW | Supabase schema explicitly disables RLS on application tables and defines no policies |
| OPS-017 | P1 | FAIL | `requirements.txt` uses broad ranges/no hashes; direct requirements audit cannot establish one deployable dependency set |
| OPS-018 | P1 | NOT RUN | No real outbound call was placed; requires explicit staging numbers and permission |

### Additional observations

- Two undefined-name defects can crash valid branches when a project ID must be derived from a project name.
- Prompt construction is computationally fast, but a representative three-project prompt is ~38,211 characters / ~6,033 words, increasing model-context cost and instruction dilution risk.
- Local tests run on Python 3.12 while the Docker image declares Python 3.11.
- The frontend/source scan found no committed literal key matching the tested Gemini/OpenAI patterns; the flagged UI line was runtime query-string token handling, not a hardcoded secret.
- Five runtime warnings remain: FastAPI lifespan deprecations and a `grpcio` support warning.
- The earlier SIL-005, LIFE-006, and LIFE-007 concerns now have code/tests in the current working tree; they passed locally, but still need live/integration validation.

### Production verdict

**NO-GO. Do not deploy this working tree to production yet.** P0 blockers are open in API authentication,
browser secret handling, Supabase access control, and dependency assurance. P1 blockers remain in recording
storage, health/readiness, worker supervision, undefined-name defects, coverage, Docker parity, and live-call validation.

---

## Run QA-2026-10-03-03 — Latest call review

- **Call ID:** `ac8ca943-f704-4a96-8c19-a74edc92d610`
- **Mode:** Same-number reconnect
- **Duration:** 134 seconds
- **Outcome:** `disconnect_in_between`
- **Recording:** unavailable

### Passed

- Reconnect opening acknowledged the prior technical cut instead of restarting the full campaign greeting.
- Prior DLF Midtown context resumed correctly from verified database catalog data.
- When the lead requested another project, the agent selected one alternative—ATS Knightsbridge—rather than listing every project.
- ATS location, ready-to-move status, and 4-BHK configuration matched the verified catalog.
- The final disconnect outcome was appropriate because the lead gave no clear positive/negative intent before disconnecting.
- Previously reviewed project claims about DLF amenities, parking, elevators, EV charging, pricing, possession, and connectivity were confirmed by the stored catalog; they were not hallucinations.

### Defects and observations

| Severity | Finding |
|---|---|
| P1 | Approximately 62 seconds elapsed after the ATS question before the lead said “Hello”; no hear-check appeared in the transcript. The deployed silence watchdog did not visibly recover the dead air. |
| P1 | `recording_url` is null, consistent with the disabled Azure Storage account found during production checks. |
| P2 | Backend lead name was `Prahsant`; earlier calls for the same lead/number used a different name. CRM identity quality needs validation. |
| P2 | A preceding fresh call opening appended the incomplete phrase “Pichli baar” after identity confirmation, violating the intended identity-only campaign opening. |
| P2 | The deployed prompt was logged around 40,890 characters, reinforcing prompt-size/instruction-dilution concerns. |
| Info | The database catalog contains duplicate logical ATS and Gaur projects under both derived IDs and explicit `--ats`/`--gaur` IDs. Catalog cleanup/deduplication is recommended. |

### Verdict

**Campaign and multi-project selection worked, but the call is not a clean pass.** Dead-air recovery,
recording, deterministic opening wording, CRM name consistency, and duplicate catalog records require follow-up.

