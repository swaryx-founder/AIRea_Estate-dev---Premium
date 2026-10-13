# OutboundAI — Complete API Integration Guide

**Version:** 1.4  
**Backend:** FastAPI + LiveKit Agent + Gemini Live (Azure Container Apps)  
**Purpose:** Connect any website or app to outbound AI voice calling.

---

## Table of contents

1. [Overview](#overview)
2. [Base URL](#base-url)
3. [Architecture](#architecture)
4. [Integration guide](#integration-guide)
5. [Authentication & limits](#authentication--limits)
6. [Common conventions](#common-conventions)
7. [Endpoint index](#endpoint-index)
8. [API reference](#api-reference)
9. [Data models](#data-models)
10. [Call lifecycle & automatic hangup](#call-lifecycle--automatic-hangup)
11. [Call outcomes](#call-outcomes)
12. [Supabase (database)](#supabase-database)
13. [Node Calling Service webhook](#node-calling-service-webhook)
14. [Environment variables](#environment-variables)
15. [Testing with cURL](#testing-with-curl)
16. [FAQ](#faq)

---

## Overview

This Azure Application URL is the **AI calling backend**. It provides REST APIs to:

- Start a single outbound AI phone call (`POST /api/call`)
- Dispatch **parallel batch calls** instantly (`POST /api/batch-call`)
- Create **scheduled campaigns** with parallel wave dialling (`POST /api/campaigns`)
- Fetch **one call by id** (`GET /api/calls/{id}`) — full details using the UUID from `POST /api/call`
- Read **lead property requirements per call** (`property_data` on each `call_logs` row — budget, city, BHK, etc., keyed by `call_id`)
- Stream **live transcripts** during a call (`GET /api/calls/active`, `GET /api/calls/{id}/transcript`)
- Play **call recordings** via signed Azure Blob URLs (`GET /api/calls/{id}/recording`)
- Read call history (paginated + org/lead filters), stats, appointments, CRM data
- **Vobiz billing** — per-call cost from live CDRs (`GET /api/billing`, fields on each call) when `VOBIZ_AUTH_ID` / `VOBIZ_AUTH_TOKEN` are set
- Manage the **outbound caller-ID pool** (`GET /api/numbers/status`, `POST /api/numbers/sync`) — free/busy Vobiz numbers leased per dial
- Manage agent profiles and prompts
- **Automatically disconnect the phone line** when the AI finishes the conversation (no separate “end call” API)

The same URL also serves a **legacy admin dashboard** at `/`. Integrators should **only use `/api/*` routes**.

**Do not share `.env` or Azure secrets with frontend developers.** They only need this base URL and this document.

---

## Base URL

```
https://swaryx-realestate.icymushroom-70c67802.canadacentral.azurecontainerapps.io
```

| Route | Purpose |
|---|---|
| `GET /api/health` | Health check |
| `POST /api/call` | Start outbound call |
| `GET /api/calls/{id}` | Single call details by UUID |
| `GET /api/calls` | Call history (paginated + billing fields) |
| `GET /api/billing` | Org-scoped Vobiz cost summary |
| `GET /api/billing/rate` | Rate metadata (env + optional Vobiz hint) |
| `GET /api/numbers` | List pool DIDs (optional `status=free`; includes `is_busy`) |
| `GET /api/numbers/stream` | SSE live busy/free events (popup when free) |
| `GET /api/numbers/status` | Outbound caller-ID pool (free/busy) |
| `POST /api/numbers/sync` | Sync owned Vobiz numbers into the pool |
| `GET /api/stats` | Dashboard metrics |
| `GET /` | Legacy admin UI — **do not use for integration** |

Append `/api/...` to the base URL for all API calls.

---

## Architecture

```
┌─────────────────────────┐
│  Your website / app     │
│  (frontend + server)    │
└───────────┬─────────────┘
            │  HTTP REST (POST /api/call, GET /api/stats, …)
            ▼
┌─────────────────────────┐
│  This AI backend        │  ← Application URL (Azure)
│  server.py + agent.py   │
└───────────┬─────────────┘
            │
     ┌──────┴──────┬──────────────┐
     ▼             ▼              ▼
 LiveKit      Gemini Live     Vobiz SIP
 (rooms)      (voice AI)      (phone dial)
     │                            │
     └────────────┬───────────────┘
                  ▼
            Supabase (PostgreSQL)
            call_logs, phone_numbers, appointments, …
                  │
                  │  POST call-completed (optional)
                  ▼
            Your Node Calling Service
            (Prisma DB — frontend reads here)
```

**Recommended flow:** Your **server** calls this API (server-to-server). If you use a separate **Node Calling Service**, save the dispatch `id` as `thirdPartyCallId` and set `CALL_COMPLETED_WEBHOOK_URL` on Azure so completed calls sync to Node automatically. Do not call `/api/call` directly from the browser unless CORS is configured.

**What your frontend does NOT need:**
- LiveKit credentials
- Google/Gemini API keys
- Vobiz/SIP credentials
- Supabase service key

All of that runs on this backend.

---

## Integration guide

### Step 1 — Verify backend is up

```http
GET /api/health
```

Expect `"status": "ok"` and `"livekit": true`.

### Step 2 — Start a test call from your server

```javascript
const AI_API_BASE =
  "https://swaryx-realestate.icymushroom-70c67802.canadacentral.azurecontainerapps.io";

/**
 * POST /api/call — start a single outbound AI call.
 * Returns { id, status: "dispatched", phone, lead_name, ... } — save `id` for polling.
 */
async function startCall({
  phone,                          // required — E.164, e.g. "+919876543210"
  lead_name = "there",
  business_name = "Swaryx",
  service_type = "property visit",
  from_number = null,             // REQUIRED — outbound caller ID; call blocked if missing
  organization_id = null,         // your org id — used for call history / billing filters
  lead_id = null,                 // your lead id — used for active-call filtering + webhook
  project_id = null,              // project id — stored on call log + completion webhook
  projectDetails = null,          // single project pitch text (use this OR projects[], not both)
  projects = null,                // multi-project: [{ project_id, projectDetails }, ...]
  agent_profile_id = null,        // agent profile UUID (overrides voice/model/prompt)
  system_prompt = null,           // per-call full prompt override (optional)
  additional_instructions = null, // extra notes appended to base prompt (optional; alias additionalInstructions)
  agent_name = null,              // LiveKit worker name override (optional; default from env)
}) {
  if (!from_number) {
    throw new Error("You have not selected the from number. Pass from_number before calling.");
  }
  const body = {
    phone,
    lead_name,
    business_name,
    service_type,
    from_number,
  };

  if (organization_id) body.organization_id = organization_id;
  if (lead_id) body.lead_id = lead_id;
  if (project_id) body.project_id = project_id;
  if (projectDetails) body.projectDetails = projectDetails;
  if (projects?.length) body.projects = projects;
  if (agent_profile_id) body.agent_profile_id = agent_profile_id;
  if (system_prompt) body.system_prompt = system_prompt;
  if (agent_name) body.agent_name = agent_name;

  const res = await fetch(`${AI_API_BASE}/api/call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Example — single project
const { id } = await startCall({
  phone: "+919876543210",
  lead_name: "Rahul",
  from_number: "+918065354603",   // REQUIRED — which DID to call from
  organization_id: "org_abc123",
  lead_id: "lead_xyz789",
  business_name: "Swaryx Real Estate",
  service_type: "property visit",
  project_id: "proj_green_valley",
  projectDetails:
    "Project Name: Green Valley\nDeveloper: ABC Group\nLocation: Sector 150, Noida\nStarting Price: 80 Lakh",
});

// Example — multiple projects (lead picks one during the call)
// const { id } = await startCall({
//   phone: "+919876543210",
//   lead_name: "Rahul",
//   organization_id: "org_abc123",
//   lead_id: "lead_xyz789",
//   projects: [
//     { project_id: "proj_green_valley", projectDetails: "Project Name: Green Valley\n..." },
//     { project_id: "proj_sky_towers", projectDetails: "Project Name: Sky Towers\n..." },
//   ],
// });
```

### Step 3 — Poll a single call by id

After `POST /api/call`, save the returned `id` and poll until `status` is `completed`:

```javascript
async function getCall(callId) {
  const res = await fetch(`${AI_API_BASE}/api/calls/${callId}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Poll every 3s until done (id from Step 2 startCall response)
let call;
do {
  await new Promise(r => setTimeout(r, 3000));
  call = await getCall(id);
} while (call.status === "in_progress");

console.log(call.outcome, call.duration_seconds, call.recording_play_url);
```

### Step 4 — Call history / billing / stats (optional)

```javascript
const orgId = "org_1234";
const calls = await fetch(
  `${AI_API_BASE}/api/calls?page=1&limit=20&organization_id=${encodeURIComponent(orgId)}`
).then(r => r.json());
// Each call has estimated_cost, rate_source ("vobiz_cdr" | "estimate"), billable_minutes
const billing = await fetch(
  `${AI_API_BASE}/api/billing?organization_id=${encodeURIComponent(orgId)}`
).then(r => r.json());
const stats = await fetch(`${AI_API_BASE}/api/stats`).then(r => r.json());
```

### Minimum endpoints for a new frontend

| Priority | Method | Endpoint | Purpose |
|---|---|---|---|
| 1 | `POST` | `/api/call` | Start a call |
| 2 | `GET` | `/api/calls/{id}` | Single call status + details (use dispatch `id`) |
| 3 | `GET` | `/api/calls` | Call history list (+ billing fields) |
| 4 | `GET` | `/api/billing` | Org Vobiz cost summary |
| 5 | `GET` | `/api/stats` | Dashboard metrics |
| 6 | `GET` | `/api/health` | Uptime check |

Optional: `/api/appointments`, `/api/campaigns`, `/api/agent-profiles`, `/api/crm`, `/api/billing/rate`.

**With Node Calling Service:** On dispatch, store response `id` in your DB as `thirdPartyCallId`. After the call ends, this AI backend POSTs to your Node webhook (no polling required on Node if webhook is configured). See [Node Calling Service webhook](#node-calling-service-webhook).

### Phone number rules

- Must be **E.164** format: starts with `+`
- India example: `+919876543210`
- Invalid format → HTTP `400`

---

## Authentication & limits

| Feature | Current status |
|---|---|
| API key / JWT | **Not implemented** — all endpoints are open |
| Rate limiting | **Not implemented** |
| Per-client quotas | **Not implemented** |
| Usage tracking | Via `call_logs` in Supabase / `GET /api/stats` |

**Production recommendation:** Add API keys and monthly call quotas before public use. Until then, restrict who knows the base URL.

**Usage limits today:** Implement on **your server** (count calls before hitting `/api/call`) or ask the AI backend team to add API key middleware.

---

## Common conventions

### Headers

```http
Content-Type: application/json
```

Required for all `POST`, `PUT`, `PATCH` requests with a body.

### Success responses

- `200 OK` — read/update success
- `201` — not used explicitly; creates return `200` with `{ "status": "created", ... }`

### Error responses

FastAPI format:

```json
{
  "detail": "Phone must be in E.164 format: +919876543210"
}
```

| HTTP code | Meaning |
|---|---|
| `400` | Bad request (invalid phone, missing config) |
| `403` | Forbidden (e.g. editing prompt in env-only mode) |
| `404` | Resource not found |
| `500` | Server/dispatch error |

### CORS

If your frontend runs on a **different domain** and calls the API **from the browser**, CORS must be enabled on this backend. Server-to-server calls do not need CORS.

---

## Endpoint index

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/call` | Start single outbound call |
| `GET` | `/api/calls` | List call logs (paginated, `property_data`, billing fields, signed recording URL) |
| `GET` | `/api/calls/{call_id}` | Single call details by id (includes `property_data` + billing) |
| `GET` | `/api/calls/active` | Active calls (optional `organization_id` / `lead_id`; live `property_data`) |
| `GET` | `/api/calls/{call_id}/transcript` | Live/final transcript + `property_data` for this call |
| `GET` | `/api/calls/{call_id}/recording` | Stream recording (signed URL redirect) |
| `PATCH` | `/api/calls/{call_id}/notes` | Update call notes |
| `GET` | `/api/billing` | Vobiz cost summary (org/lead filters; live CDR when Auth set) |
| `GET` | `/api/billing/rate` | Billing rate metadata |
| `GET` | `/api/stats` | Dashboard statistics |
| `GET` | `/api/appointments` | List appointments |
| `DELETE` | `/api/appointments/{id}` | Cancel appointment |
| `GET` | `/api/prompt` | Get system prompt |
| `POST` | `/api/prompt` | Save system prompt |
| `DELETE` | `/api/prompt` | Reset prompt to default |
| `GET` | `/api/settings` | Get config status |
| `POST` | `/api/settings` | Save settings (dev only) |
| `POST` | `/api/setup/trunk` | Create SIP trunk (admin; registers pool numbers when synced) |
| `GET` | `/api/numbers` | List pool DIDs for pickers (`status=free\|busy\|disabled`, optional `organization_id`) — includes `is_busy` / `is_free` |
| `GET` | `/api/numbers/stream` | **SSE** live busy/free events (popup when a DID becomes free) |
| `GET` | `/api/numbers/status` | Outbound caller-ID pool status (total/free/busy/disabled + full rows; optional `organization_id`) |
| `POST` | `/api/numbers/sync` | Sync owned Vobiz DIDs into `phone_numbers` (+ refresh LiveKit trunk) |
| `PATCH` | `/api/numbers` | Assign a DID to an `organization_id` (or clear → shared) |
| `GET` | `/api/logs` | System logs |
| `DELETE` | `/api/logs` | Clear logs |
| `GET` | `/api/crm` | Contact list with `last_outcome` (filterable) |
| `GET` | `/api/crm/calls?phone=...` | All calls for one phone (outcome + `property_data` per call) |
| `GET` | `/api/agent-profiles` | List agent profiles |
| `POST` | `/api/agent-profiles` | Create agent profile |
| `GET` | `/api/agent-profiles/{id}` | Get profile |
| `PUT` | `/api/agent-profiles/{id}` | Update profile |
| `DELETE` | `/api/agent-profiles/{id}` | Delete profile |
| `POST` | `/api/agent-profiles/{id}/set-default` | Set default profile |
| `POST` | `/api/campaigns` | Create campaign (parallel waves) |
| `GET` | `/api/campaigns` | List campaigns |
| `DELETE` | `/api/campaigns/{id}` | Delete campaign |
| `POST` | `/api/campaigns/{id}/run` | Run campaign now |
| `PATCH` | `/api/campaigns/{id}/status` | Update campaign status |
| `POST` | `/api/batch-call` | Parallel batch dial (no schedule) |
| `GET` | `/` | Legacy dashboard (ignore) |

---

## API reference

### Health

#### `GET /health`

Lightweight liveness probe (`{"status":"ok","service":"OutboundAI"}`).

#### `GET /api/health`

Readiness check with build/env flags, Gemini key pool, Supabase/LiveKit config, webhook status, and Azure recording storage.

**Response `200` (shape):**
```json
{
  "status": "ok",
  "build_id": "unknown",
  "env_only": true,
  "api_auth": false,
  "gemini_key_pool": {},
  "supabase": true,
  "livekit": true,
  "call_completed_webhook": {},
  "recording_storage": {
    "backend": "azure_blob",
    "azure_configured": false
  }
}
```

---

### Calls

#### `POST /api/call`

Start a single outbound AI voice call. **Main integration endpoint.**

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `phone` | string | **Yes*** | — | E.164, e.g. `+919876543210` (*or use `phones[]` / `contacts[]`) |
| `phones` | string[] | No | — | Multiple lead phones (simultaneous when `simultaneous=true`) |
| `contacts` | array | No | — | Per-lead objects: `{ phone, lead_name, from_number?, organization_id?, lead_id? }` |
| `lead_name` | string | No | `"there"` | Lead name (single-phone path) |
| `from_number` | string | No | auto-lease | **Outbound caller ID** (must be free in pool). Omit → agent auto-leases a free DID from the pool. Aliases: `caller_id`, `sip_number`, `calling_number`. |
| `business_name` | string | No | `"our company"` | Company name |
| `service_type` | string | No | `"our service"` | Service type |
| `system_prompt` | string \| null | No | env/default | Full AI prompt override for this call (replaces default script when applicable) |
| `additional_instructions` | string \| null | No | null | Extra notes appended onto the usual base prompt (does **not** replace it). Alias: `additionalInstructions` |
| `agent_profile_id` | string \| null | No | null | Agent profile UUID |
| `organization_id` | string \| null | No | null | Organization ID — stored on call log for active-call filtering |
| `lead_id` | string \| null | No | null | Lead ID — stored on call log for active-call filtering |
| `project_id` | string \| null | No | null | Project ID — stored on call log; returned in completion webhook |
| `projectDetails` | array \| object \| string \| null | No | null | **Broker contract: always send an array of flat project objects.** Array length 1 → single-project mode; length 2+ → multi-project mode; empty array → generic/default flow. When array items omit IDs, the top-level `project_id` is used for the first item and stable internal IDs are derived for the rest. Legacy object/string remains accepted for backward compatibility. `project_details` also accepted. |
| `projects` | array \| null | No | null | Multiple projects from broker. Each item: `{ project_id, projectDetails }`. Agent presents each one-by-one; lead picks preferred project. Use **either** `projectDetails` **or** `projects` — if `projects` has 2+ items, multi-project mode is used. |
| `simultaneous` | bool | No | `true` | When multiple phones/contacts: dial together |
| `scheduled_at` | string \| null | No | null | Optional callback dial time (ISO UTC or parseable phrase). Stored on `call_logs`; echoed in dispatch response. Usually set by AI after `callback_requested`; Node may pass when firing a scheduled callback dial |
| `callback_urgency` | string \| null | No | null | `now` \| `later` \| `specific_time` — paired with `scheduled_at` |

**Dispatch response** (single phone) includes the same callback fields when provided or when already stored on the call row after completion (via `GET /api/calls/{id}` poll).

**Example — optional caller ID (omit `from_number` to auto-lease from pool):**
```json
{
  "phone": "+919876543210",
  "lead_name": "Rahul",
  "livekit_trunk_id": "ST_org_trunk_id",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789"
}
```

`livekit_trunk_id` selects the organization's pre-created outbound SIP trunk.
Snake case (`livekit_trunk_id`) and camel case (`livekitTrunkId`) are accepted.
The server never creates, guesses, or falls back to a global trunk during call
dispatch. The request fails clearly when this field is missing. During migration,
the older nested `credentials.vobiz.trunk_id` form is also accepted.

**Example — choose caller ID explicitly:**

**Example — choose caller ID (same as above; from_number required):**
```json
{
  "phone": "+919876543210",
  "lead_name": "Rahul",
  "from_number": "+918065354603",
  "livekit_trunk_id": "ST_org_trunk_id",
  "organization_id": "org_abc123"
}
```

**Example — two leads in parallel (per-contact caller ID):**
```json
{
  "contacts": [
    { "phone": "+919811100001", "lead_name": "Lead A", "from_number": "+918065354603" },
    { "phone": "+919811100002", "lead_name": "Lead B", "from_number": "+918071578722" }
  ],
  "simultaneous": true
}
```

**Example — single project:**
```json
{
  "phone": "+919876543210",
  "lead_name": "Rahul",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789",
  "project_id": "proj_123",
  "business_name": "Swaryx Real Estate",
  "service_type": "property visit",
  "projectDetails": "Project Name: Green Valley\nDeveloper: ABC Group\nLocation: Sector 150, Noida\n..."
}
```

**Example — multiple projects (broker choice):**
```json
{
  "phone": "+919876543210",
  "lead_name": "Rahul",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789",
  "business_name": "Swaryx Real Estate",
  "projects": [
    {
      "project_id": "proj_green_valley",
      "projectDetails": "Project Name: Green Valley\nLocation: Sector 150, Noida\nStarting Price: 80 Lakh\n..."
    },
    {
      "project_id": "proj_sky_towers",
      "projectDetails": "Project Name: Sky Towers\nLocation: Golf Course Road, Gurgaon\nStarting Price: 1.2 Cr\n..."
    }
  ]
}
```

> **Single project:** agent pitches only that project. **Multiple projects:** agent says available options, explains each briefly, lead chooses one → qualify + site visit for chosen project. `preferred_project` stored in `property_data` on the call.

**Success `200`:**
```json
{
  "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
  "status": "dispatched",
  "room": "call-919876543210-4821",
  "phone": "+919876543210",
  "agent_profile_id": "799e2d7a-d5f4-4fab-bc36-5c3a5bfaa0e8",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789",
  "lead_name": "Rahul"
}
```

| Field | Description |
|---|---|
| `id` | Supabase `call_logs` record UUID — use for transcript, recording, status polling, and **Node `thirdPartyCallId`** when using the completion webhook |
| `status` | Always `"dispatched"` on success |
| `room` | LiveKit room name for this call |
| `phone` | E.164 phone number dialled |
| `agent_profile_id` | Agent profile UUID from request (null if not sent) |
| `organization_id` | Organization ID from request (null if not sent) |
| `lead_id` | Lead ID from request (null if not sent) |
| `lead_name` | Lead name from request |

**Errors:**
- `400` / `422` — Invalid phone, missing `from_number`, or LiveKit not configured
- `500` — Dispatch failed

**Internal flow:** Creates LiveKit room → dispatches agent worker → SIP dial → Gemini Live conversation → on completion the agent runs internal `wrap_up` → saves result to Supabase → **POSTs to your Node webhook** (if `CALL_COMPLETED_WEBHOOK_URL` is set) → **deletes LiveKit room so the lead's phone line disconnects** (~0.4s after goodbye).

**There is no `POST /api/call/end` endpoint.** Hanging up is handled entirely by the voice agent after the conversation ends. Your app should poll **`GET /api/calls/{call_id}`** (recommended) or `GET /api/calls/{call_id}/transcript` until `status` is `completed`.

**When the call ends (automatic hangup):**

| Step | What happens |
|---|---|
| 1 | Agent says final goodbye in Hindi (last spoken sentence) |
| 2 | Agent invokes internal tool `wrap_up` (configured in profiles as `end_call`) with `outcome` + `reason` |
| 3 | Backend writes `call_logs`: `status: completed`, `outcome`, `duration_seconds`, transcripts |
| 4 | Backend POSTs `call-completed` webhook to your Node app (if configured) |
| 5 | Backend deletes the LiveKit room → SIP BYE → **call cuts on the lead's phone** |
| 6 | If the lead hangs up first, SIP disconnect ends the session; call is finalized with outcome `disconnect_in_between` when wrap_up was not invoked |

**Poll for completion after dispatch (recommended):**

```http
GET /api/calls/{call_id}
```

Returns the full [call log](#call_log) — `status`, `outcome`, `duration_seconds`, `recording_play_url`, transcripts, etc.

When `status` changes from `in_progress` to `completed`, the call has ended and the phone line should be disconnected.

**Lighter poll (transcript only):**

```http
GET /api/calls/{call_id}/transcript
```

**List fallback (when you do not have the id handy):**

```http
GET /api/calls?page=1&limit=20&organization_id=org_1234&lead_id=lead_5678
```

---

#### `GET /api/calls`

Paginated call history. Each record includes a `recording_play_url` (time-limited signed URL) when a recording exists — use this URL to play audio, not the raw `recording_url`.

Each row is also enriched with **billing fields** (see [Billing](#billing) and [call_log](#call_log)):

| Field | Meaning |
|---|---|
| `estimated_cost` | Cost in `currency` — from Vobiz CDR `total_cost` when matched, else local estimate |
| `billable_minutes` | `vobiz_billsec/60` when CDR matched, else `duration_seconds/60` |
| `rate_per_minute` | Effective per-minute rate |
| `currency` | Usually `INR` |
| `rate_source` | `vobiz_cdr` (live) or `estimate` (fallback) |
| `vobiz_billsec` | Present when CDR matched — Vobiz billable seconds |
| `vobiz_cdr_uuid` | Present when CDR matched |

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `page` | int | `1` | Page number |
| `limit` | int | `20` | Rows per page |
| `organization_id` | string | — | Optional — only calls for this org |
| `lead_id` | string | — | Optional — only calls for this lead |

When `organization_id` and/or `lead_id` are sent, only matching rows are returned (calls without those fields stored are excluded).

**Response `200`:** Array of [call log objects](#call_log).

**Example:**
```http
GET /api/calls?page=1&limit=20
GET /api/calls?page=1&limit=20&organization_id=org_1234
GET /api/calls?page=1&limit=20&organization_id=org_1234&lead_id=lead_5678
```

---

#### `GET /api/calls/{call_id}`

Fetch a **single call** by the UUID returned from `POST /api/call` (same value as Node `thirdPartyCallId`).

**Path parameter:**

| Param | Type | Description |
|---|---|---|
| `call_id` | string (UUID) | `id` from `POST /api/call` / batch / campaign dispatch response |

**Response `200`:** One [call log object](#call_log) — same shape as list items, plus full transcript fields:

```json
{
  "id": "a2a5f8d2-b62e-44e5-95c2-c39a2746b375",
  "phone_number": "+919876543210",
  "lead_name": "Rahul Verma",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789",
  "outcome": "callback_requested",
  "reason": "wants site visit next week",
  "scheduled_at": "2026-08-07T11:30:00.000Z",
  "callback_urgency": "specific_time",
  "sentiment": 0,
  "ai_score": 55,
  "lead_score": "WARM",
  "duration_seconds": 245,
  "billable_minutes": 4.0,
  "estimated_cost": 1.80,
  "rate_per_minute": 0.45,
  "currency": "INR",
  "rate_source": "vobiz_cdr",
  "vobiz_billsec": 209,
  "vobiz_duration": 215,
  "vobiz_cdr_uuid": "bcd1c45d-3f27-41ea-bea1-aa5d4e06dca4",
  "timestamp": "2026-06-08T12:30:00",
  "status": "completed",
  "recording_url": "swaryxbucket/recordings/call-919876543210-123.ogg",
  "recording_play_url": "https://swaryxbucket.blob.core.windows.net/recordings/...?sv=...",
  "live_transcript": "Lead: Hello.\nAgent: Namaste!...",
  "final_transcript": "",
  "transcript_json": "[{\"role\":\"agent\",\"text\":\"Namaste!\",\"ts\":\"...\"}]",
  "property_data": {
    "budget": "50-70 lakh",
    "city": "Pune",
    "bhk": "3",
    "property_type": "ready-to-move",
    "location_preference": "green area",
    "whatsapp_number": "+919876543210"
  },
  "notes": null
}
```

| Field | Notes |
|---|---|
| `property_data` | Object — lead requirements collected **on this call only** (see [property_data](#property_data)). Empty `{}` if none saved yet. |
| `recording_play_url` | Signed URL (~1 hour). `null` if no recording or blob missing |
| `status` | `in_progress` while call is live; `completed` or `failed` when done |
| `transcript_json` | JSON **string** (parse with `JSON.parse` in JS) |
| `rate_source` | `vobiz_cdr` = live Vobiz console cost; `estimate` = local fallback |
| `estimated_cost` | Always present on list/detail; use this for UI “Cost” |

**Errors:**

| HTTP | `detail` | Meaning |
|---|---|---|
| `404` | `"Call not found"` | No `call_logs` row with that UUID |
| `404` | `"Not Found"` | Endpoint not deployed yet (old backend image) — redeploy pipeline |

**Examples:**
```http
GET /api/calls/a2a5f8d2-b62e-44e5-95c2-c39a2746b375
```

```js
const callId = "a2a5f8d2-b62e-44e5-95c2-c39a2746b375";
const call = await fetch(`${AI_API_BASE}/api/calls/${callId}`).then(r => r.json());
console.log(call.status, call.outcome, call.property_data?.budget, call.property_data?.city);
```

**When to use:** Always prefer this over paginating `GET /api/calls` when you already have the dispatch `id`.

---

#### `GET /api/calls/active`

Returns **in-progress** calls. Query filters are **optional** but recommended for multi-tenant isolation.

**Query parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `organization_id` | string | no | Only calls for this org |
| `lead_id` | string | no | Only calls for this lead |

**Response `200`:** Array of call log objects, each including `live_transcript`, `transcript_json`, `property_data`, and billing fields (same enrichment as `GET /api/calls`).

**Example:**
```http
GET /api/calls/active
GET /api/calls/active?organization_id=org_abc123&lead_id=lead_xyz789
```

**Example polling loop (JavaScript):**
```js
const orgId = 'org_abc123';
const leadId = 'lead_xyz789';
setInterval(async () => {
  const calls = await fetch(
    `/api/calls/active?organization_id=${encodeURIComponent(orgId)}&lead_id=${encodeURIComponent(leadId)}`
  ).then(r => r.json());
  calls.forEach(c => console.log(c.live_transcript));
}, 2000);
```

---

#### `GET /api/calls/{call_id}/transcript`

Get the full transcript for a call — live (during call) or final (post-call via Deepgram).

**Response `200`:**
```json
{
  "call_id": "uuid",
  "status": "in_progress",
  "live_transcript": "Agent: Hello… kya main Rahul ji se baat kar rahi hoon?\nLead: Haan ji",
  "final_transcript": "",
  "transcript_json": "[{\"role\":\"agent\",\"text\":\"Namaste...\",\"ts\":\"2026-05-29T...\"}]",
  "outcome": "callback_requested",
  "outcome_reason": "wants site visit next week",
  "scheduled_at": "2026-08-07T11:30:00.000Z",
  "callback_urgency": "specific_time",
  "sentiment": 0,
  "ai_score": 55,
  "lead_score": "WARM",
  "property_data": {
    "budget": "50-70 lakh",
    "city": "Pune",
    "scheduled_at": "2026-08-07T11:30:00.000Z",
    "callback_urgency": "specific_time"
  }
}
```

| Field | Description |
|---|---|
| `status` | `in_progress` (call live) or `completed` |
| `outcome` / `outcome_reason` | Final call outcome when completed |
| `scheduled_at` / `callback_urgency` | Callback schedule — same as webhook (when `callback_requested`) |
| `sentiment` | `1` / `0` / `-1` |
| `ai_score` / `lead_score` | Lead quality from this call |
| `live_transcript` | Plain-text chat format, updated every ~2s during call |
| `final_transcript` | Full Deepgram transcript after call (if `ENABLE_POST_CALL_TRANSCRIPT=true`) |
| `transcript_json` | JSON array with role, text, timestamp per line |
| `property_data` | Lead requirements for **this `call_id` only** — populated live when agent invokes `remember_details` (see [property_data](#property_data)) |

---

#### `GET /api/calls/{call_id}/recording`

Redirects to a **time-limited signed URL** for the call recording (Azure Blob SAS, valid 1 hour). Works directly in browser / `<audio>` tag.

**Response:** `302 Redirect` → signed Azure Blob URL

**Errors:** `404` if no recording exists for this call.

**Usage:**
```html
<audio controls src="/api/calls/CALL_ID/recording"></audio>
```

---

#### `PATCH /api/calls/{call_id}/notes`

Update notes on a call record.

**Request body:**
```json
{
  "notes": "Lead interested in 2BHK"
}
```

**Response `200`:**
```json
{
  "status": "updated"
}
```

**Errors:** `404` if call not found.

---

### Billing {#billing}

Per-call and org-level **Vobiz SIP cost**. When `VOBIZ_AUTH_ID` + `VOBIZ_AUTH_TOKEN` are set on Azure, the backend fetches live CDRs from Vobiz (`GET /api/v1/Account/{auth_id}/cdr`) and matches each call by destination phone + timestamp. Matched rows use Vobiz `total_cost` / `billsec` (`rate_source: "vobiz_cdr"`). If Auth is missing or a CDR is not found yet, cost falls back to:

`ceil(duration_seconds / 60) × VOBIZ_RATE_PER_MINUTE` (`rate_source: "estimate"`).
Default rate is **₹0.45/min** (Vobiz pulse: 209s → 4 × 0.45 = ₹1.80).

Auth ID / Token come from the [Vobiz Console](https://console.vobiz.ai) dashboard (not SIP username/password).

---

#### `GET /api/billing`

Org-scoped (or global) cost summary over recent call logs (up to 5000 rows).

**Query parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `organization_id` | string | no | Scope to one org (recommended for tenants) |
| `lead_id` | string | no | Optional lead filter |

**Response `200`:**

```json
{
  "organization_id": "org_abc123",
  "rate_per_minute": 0.45,
  "currency": "INR",
  "rate_source": "vobiz_cdr",
  "billing_increment": "vobiz_cdr_preferred",
  "provider": "vobiz",
  "vobiz_api_configured": true,
  "total_calls": 42,
  "total_duration_seconds": 5586,
  "billable_minutes": 93.1,
  "estimated_cost": 56.75,
  "vobiz_matched_calls": 38
}
```

| Field | Description |
|---|---|
| `estimated_cost` | Sum of per-call costs (live CDR where matched) |
| `vobiz_matched_calls` | How many rows in the summary sample used live CDR cost |
| `rate_source` | `vobiz_cdr` if any matches; else `estimate` |
| `vobiz_api_configured` | `true` when Auth ID/Token env vars are set |

**Examples:**
```http
GET /api/billing
GET /api/billing?organization_id=org_abc123
GET /api/billing?organization_id=org_abc123&lead_id=lead_xyz789
```

```js
const orgId = "org_abc123";
const bill = await fetch(
  `${AI_API_BASE}/api/billing?organization_id=${encodeURIComponent(orgId)}`
).then(r => r.json());
console.log(bill.estimated_cost, bill.currency, bill.vobiz_matched_calls);
```

---

#### `GET /api/billing/rate`

Returns rate metadata used for estimates / display.

**Response `200`:**

```json
{
  "rate_per_minute": 0.45,
  "currency": "INR",
  "rate_source": "env",
  "billing_increment": "vobiz_cdr_preferred",
  "provider": "vobiz",
  "vobiz_api_configured": true,
  "vobiz_account_rate_hint": 0.45
}
```

| Field | Description |
|---|---|
| `rate_per_minute` | From `VOBIZ_RATE_PER_MINUTE` (fallback estimate) |
| `vobiz_api_configured` | Whether live CDR fetch is enabled |
| `vobiz_account_rate_hint` | Optional value from Vobiz `/auth/me` when available |

```http
GET /api/billing/rate
```

---

### Stats

#### `GET /api/stats`

Aggregated dashboard metrics from `call_logs`.

**Response `200`:**
```json
{
  "total_calls": 50,
  "booked": 12,
  "not_interested": 8,
  "avg_duration_seconds": 45.2,
  "booking_rate_percent": 24.0,
  "outcomes": {
    "booked": 12,
    "no_answer": 5,
    "not_interested": 8
  },
  "timeline": [
    { "date": "2026-05-14", "count": 2 },
    { "date": "2026-05-26", "count": 3 }
  ],
  "duration_by_outcome": {
    "booked": 120.5,
    "no_answer": 15.0
  }
}
```

---

### Appointments

#### `GET /api/appointments`

List booked appointments.

**Query parameters:**

| Param | Type | Description |
|---|---|---|
| `date` | string | Optional filter `YYYY-MM-DD` |

**Response `200`:** Array of [appointment objects](#appointment).

---

#### `DELETE /api/appointments/{appointment_id}`

Cancel an appointment (sets status to `cancelled`).

**Response `200`:**
```json
{
  "status": "cancelled"
}
```

**Errors:** `404` if not found or already cancelled.

---

### System prompt

#### `GET /api/prompt`

**Response `200`:**
```json
{
  "prompt": "You are riya...",
  "is_custom": false
}
```

---

#### `POST /api/prompt`

Save a custom system prompt.

**Request body:**
```json
{
  "prompt": "Your custom prompt text..."
}
```

**Response `200`:** `{ "status": "saved" }`

**Errors:** `403` when `CONFIG_FROM_ENV_ONLY=true` and `SYSTEM_PROMPT` is set in Azure env vars.

---

#### `DELETE /api/prompt`

Reset to built-in default prompt.

**Response `200`:**
```json
{
  "status": "reset",
  "prompt": "..."
}
```

---

### Settings

#### `GET /api/settings`

Returns configuration status. Sensitive keys show `configured: true/false` without exposing values.

**Response `200` (example):**
```json
{
  "env_only": true,
  "LIVEKIT_URL": { "value": "wss://...", "configured": true },
  "GEMINI_TTS_VOICE": { "value": "Leda", "configured": true },
  "GOOGLE_API_KEY": { "value": "", "configured": true }
}
```

---

#### `POST /api/settings`

Save settings via dashboard (local dev). Blocked for env keys in production.

**Request body:**
```json
{
  "settings": {
    "ENABLED_TOOLS": "[\"book_appointment\",\"end_call\"]"
  }
}
```

---

### SIP trunk (one-time admin)

#### `POST /api/setup/trunk`

Creates a LiveKit outbound SIP trunk using Vobiz credentials from env.  
If the phone pool already has synced numbers, those E.164 values are registered on the trunk (dynamic — no fixed default DID). Per-call caller ID uses LiveKit `sip_number` from the pool.

**Response `200`:**
```json
{
  "status": "created",
  "trunk_id": "ST_xxxx",
  "numbers": ["+919876543210", "+912271263984"],
  "message": "Add OUTBOUND_TRUNK_ID to your Azure environment variables and restart the app."
}
```

---

### Phone number pool (outbound caller ID)

Vobiz DIDs are synced into Supabase `phone_numbers` and leased atomically for each outbound dial (`free` → `busy` → `free` on call end).  
Map each number to an **`organization_id`** (dashboard Numbers tab or `PATCH /api/numbers`). Empty/null = **shared** pool. Dial lease order: that org’s numbers first, then shared — never another org’s DID.  
A locked background job syncs from Vobiz every `PHONE_NUMBER_SYNC_INTERVAL_MINUTES` (default `15`). Batch/campaign `max_parallel_calls` is soft-clamped to pool capacity. If every eligible number is busy, the dial fails with outcome `failed` / no-numbers reason.

**Team integration flow (dynamic pool):**
1. Buy / add DIDs in Vobiz  
2. `POST /api/numbers/sync` — pulls **all** owned numbers into the pool + refreshes LiveKit trunk  
3. `GET /api/numbers?status=free` — dropdown always shows the current free set (grows as you add numbers)  
4. `POST /api/call` with optional `from_number` — omit = auto-lease from whatever is free  
5. Show busy/free in your UI via poll or `GET /api/numbers/stream` — popup when a DID becomes free  

No hardcoded default DID. Scheduled sync also runs every `PHONE_NUMBER_SYNC_INTERVAL_MINUTES` (default `15`).

#### `GET /api/numbers`

Lightweight list for pickers / team apps. Each row includes `is_busy` / `is_free` / `label` so you can disable busy numbers in dropdowns.

**Query parameters:**

| Param | Description |
|---|---|
| `organization_id` | Optional — that org’s numbers plus shared |
| `status` | Optional — `free` \| `busy` \| `disabled` |

**Example:**
```http
GET /api/numbers?status=free
```

**Response `200`:**
```json
{
  "total": 2,
  "free": 1,
  "busy": 1,
  "disabled": 0,
  "organization_id": null,
  "dynamic": true,
  "live": {
    "poll": "GET /api/numbers every 3–5s",
    "stream": "GET /api/numbers/stream (SSE)",
    "webhook": "Set NUMBER_STATUS_WEBHOOK_URL for POST events on busy/free"
  },
  "numbers": [
    {
      "e164": "+918065354603",
      "status": "busy",
      "is_busy": true,
      "is_free": false,
      "label": "+918065354603 (busy)",
      "organization_id": null,
      "current_call_id": "abc123…",
      "current_room_name": "call-…",
      "last_used_at": "2026-07-29T12:00:00",
      "country": "IN",
      "region": null
    },
    {
      "e164": "+918071578722",
      "status": "free",
      "is_busy": false,
      "is_free": true,
      "label": "+918071578722 (free)",
      "organization_id": null,
      "current_call_id": null,
      "current_room_name": null,
      "last_used_at": null,
      "country": "IN",
      "region": null
    }
  ]
}
```

#### `GET /api/numbers/stream` (live popup)

Server-Sent Events stream of DID status changes. Use this in your Node/React app to show **busy** and popup when the number becomes **free**.

**Query parameters:**

| Param | Description |
|---|---|
| `organization_id` | Optional filter |
| `interval_sec` | DB poll interval `1–30` (default `2`) — catches leases from the agent process |
| `api_key` | Same as `API_AUTH_TOKEN` when auth is enabled (EventSource cannot set headers) |

**Events:**

| Event | When |
|---|---|
| `snapshot` | On connect — full status map |
| `number_status` | Each transition (`status`: `free` \| `busy` \| `disabled`) |
| `error` | Transient DB/read errors |

**`number_status` payload:**
```json
{
  "type": "number_status",
  "e164": "+918065354603",
  "status": "free",
  "previous_status": "busy",
  "message": "+918065354603 is now free",
  "reason": "call_ended",
  "call_id": "…",
  "timestamp": "2026-07-29T12:05:00Z"
}
```

**Browser / frontend example:**
```javascript
const es = new EventSource(`${BASE}/api/numbers/stream?api_key=${API_KEY}`);
es.addEventListener('number_status', (e) => {
  const data = JSON.parse(e.data);
  if (data.status === 'busy') {
    // show in UI: this number is busy
    toast(`${data.e164} is busy`);
  }
  if (data.status === 'free') {
    // popup when free again
    toast(data.message); // "+91… is now free"
  }
});
```

**Node webhook (optional):** set `NUMBER_STATUS_WEBHOOK_URL` to a full URL. On every lease/release the API/agent POSTs the same JSON payload. Optional timeout: `NUMBER_STATUS_WEBHOOK_TIMEOUT` (default `5`).

#### `GET /api/numbers/status`

Pool summary + all number rows (dashboard **Numbers** tab).

**Query parameters:**

| Param | Description |
|---|---|
| `organization_id` | Optional — return that org’s numbers plus shared (`organization_id` null) |

**Response `200`:**
```json
{
  "total": 2,
  "free": 1,
  "busy": 1,
  "disabled": 0,
  "organization_id": null,
  "numbers": [
    {
      "e164": "+919876543210",
      "status": "free",
      "organization_id": "org_builder_a",
      "current_call_id": null,
      "current_room_name": null,
      "assigned_agent": null,
      "last_used_at": "2026-07-24T10:00:00",
      "synced_at": "2026-07-24T11:00:00",
      "created_at": "2026-07-24T09:00:00",
      "vobiz_id": "aabbccdd-…",
      "vobiz_status": "active",
      "country": "IN",
      "region": "Karnataka"
    }
  ]
}
```

| `status` | Meaning |
|---|---|
| `free` | Available to lease for the next outbound call |
| `busy` | Leased to `current_call_id` / `current_room_name` |
| `disabled` | Missing from Vobiz, blocked, or voice-disabled — kept for history |

---

#### `POST /api/numbers/sync`

Manual sync from Vobiz `GET /Account/{auth_id}/numbers`. Also runs on a schedule. Uses a DB job lock so only one ACA replica executes it. Refreshes LiveKit trunk `numbers` when `OUTBOUND_TRUNK_ID` is set. **Does not overwrite** existing `organization_id` mappings.

Requires `VOBIZ_AUTH_ID` + `VOBIZ_AUTH_TOKEN`.

**Response `200` (ok):**
```json
{
  "status": "ok",
  "ok": true,
  "vobiz_count": 2,
  "seen": 2,
  "inserted": 1,
  "updated": 1,
  "disabled": 0,
  "synced_at": "2026-07-24T11:00:00",
  "trunk": { "updated": true, "count": 2, "numbers": ["+919876543210", "+912271263984"] }
}
```

**Response `200` (skipped — another replica holds the lock):**
```json
{
  "status": "skipped",
  "reason": "another replica is syncing"
}
```

**Errors:** `502` if Vobiz Auth is missing or the sync fails.

---

#### `PATCH /api/numbers`

Assign a pool number to an organization (or clear for shared).

**Request body:**
```json
{
  "e164": "+919876543210",
  "organization_id": "org_builder_a"
}
```

Pass `"organization_id": null` (or omit / empty string) to make the number **shared**.

**Response `200`:** `{ "status": "ok", "number": { ... } }`  
**Errors:** `404` if the E.164 is not in the pool yet (sync first).

---

### Logs#### `GET /api/logs`

**Query parameters:**

| Param | Default | Description |
|---|---|---|
| `limit` | `200` | Max rows |
| `level` | — | `info`, `warning`, `error` |
| `source` | — | `agent`, `server` |

**Response `200`:** Array of log objects.

---

#### `DELETE /api/logs`

Clear all error logs.

**Response `200`:** `{ "status": "cleared" }`

---

### CRM & last outcome

Contacts are built from `call_logs` — **one row per phone**.  
`last_outcome` = `outcome` of that phone's **most recent** call (same values as [call outcomes](#call-outcomes), plus `in_progress` while a call is live).

#### `GET /api/crm`

Contact summary list — includes **last outcome** per phone.

**Query parameters (all optional):**

| Param | Description |
|---|---|
| `organization_id` | Only aggregate calls for this org |
| `lead_id` | Only aggregate calls for this lead |
| `last_outcome` | Return contacts whose **latest** outcome matches (case-insensitive), e.g. `booked`, `callback_requested` |
| `phone` | Return one contact by E.164 number, e.g. `+919876543210` |

**Response `200`:**
```json
{
  "data": [
    {
      "phone_number": "+919876543210",
      "lead_name": "Rahul",
      "organization_id": "org_1234",
      "lead_id": "lead_12345",
      "total_calls": 3,
      "booked": 1,
      "last_call": "2026-06-11T07:21:00",
      "last_call_id": "a2a5f8d2-b62e-44e5-95c2-c39a2746b375",
      "last_outcome": "callback_requested",
      "last_reason": "wants site visit next week",
      "last_duration_seconds": 245
    }
  ]
}
```

| Field | Description |
|---|---|
| `last_outcome` | Latest call outcome for this phone — use for CRM badges / filters |
| `last_call_id` | UUID of that latest call — use with `GET /api/calls/{id}` for full details |
| `last_reason` | Agent notes from `wrap_up` on the latest call |
| `booked` | Count of calls where `outcome == booked` (within filter scope) |

**Examples:**
```http
GET /api/crm
GET /api/crm?organization_id=org_1234&lead_id=lead_12345
GET /api/crm?last_outcome=booked
GET /api/crm?last_outcome=callback_requested&organization_id=org_1234
GET /api/crm?phone=+919876543210
```

```js
// Contacts where last call ended as callback_requested
const res = await fetch(
  `${AI_API_BASE}/api/crm?organization_id=${orgId}&last_outcome=callback_requested`
).then(r => r.json());

res.data.forEach(c => {
  console.log(c.phone_number, c.last_outcome, c.last_call_id);
});
```

**Possible `last_outcome` values:**

| Value | Meaning |
|---|---|
| `in_progress` | Call currently live |
| `booked` | Appointment confirmed |
| `not_interested` | Lead declined |
| `callback_requested` | Lead asked to call back |
| `voicemail` | Voicemail |
| `no_answer` | No response |
| `wrong_number` | Wrong person |
| `completed` | Call ended without explicit agent outcome |
| `disconnect_in_between` | Call cut mid-conversation (lead hangup / SIP drop) |
| `failed` | Dispatch / dial failed |

---

#### `GET /api/crm/calls?phone={phone}`

Full call history for one phone — every row includes `outcome` (not just the latest).

**Query:** `phone` (required), e.g. `+919876543210`

**Response `200`:** `{ "data": [ ...call_log objects ] }` — newest first. Each row includes `property_data` for that specific call (budget/city/BHK are **not** shared across calls, even if `lead_name` or `phone_number` match).

Use when the user clicks a contact row and you need **all past outcomes**, not only `last_outcome`.

**Example:**
```http
GET /api/crm/calls?phone=+919876543210
```

---

#### Related: single call details

For the **latest call only** (transcript, recording, full payload):

```http
GET /api/calls/{last_call_id}
```

Use `last_call_id` from `GET /api/crm` → then fetch full call record.

---

### Agent profiles

Agent profiles override voice, model, prompt, and tools per call when `agent_profile_id` is passed to `/api/call` or campaigns.

#### `GET /api/agent-profiles`

List all profiles.

---

#### `POST /api/agent-profiles`

**Request body:**

| Field | Type | Default |
|---|---|---|
| `name` | string | required |
| `voice` | string | `"Leda"` |
| `model` | string | `"gemini-3.8-live"` |
| `system_prompt` | string \| null | null |
| `enabled_tools` | string | `"[]"` (JSON array string) |
| `is_default` | boolean | `false` |

**Example:**
```json
{
  "name": "riya - Hindi",
  "voice": "Leda",
  "model": "gemini-3.8-live",
  "system_prompt": null,
  "enabled_tools": "[]",
  "is_default": true
}
```

**Response `200`:**
```json
{
  "status": "created",
  "id": "uuid-here"
}
```

**Available Gemini voices (examples):** `Leda`, `Kore`, `Aoede`, `Charon`, `Puck`, `Zephyr`

---

#### `GET /api/agent-profiles/{profile_id}`

Get one profile by ID.

---

#### `PUT /api/agent-profiles/{profile_id}`

Update profile (same body as create).

**Response `200`:** `{ "status": "updated" }`

---

#### `DELETE /api/agent-profiles/{profile_id}`

**Response `200`:** `{ "status": "deleted" }`

---

#### `POST /api/agent-profiles/{profile_id}/set-default`

**Response `200`:** `{ "status": "default set" }`

---

### Campaigns (bulk parallel calling)

#### `POST /api/campaigns`

Create and schedule a bulk calling campaign. Calls use a **FIFO queue**:  
`max_parallel_calls` contacts dial at once (hard-capped by phone-pool DIDs + Gemini keys). Remaining leads wait. When a call ends and a caller-ID becomes free, that slot dials the **next** lead — until every contact is called.  
Example: 4 leads + 2 DIDs → dial 2 now; as each finishes, dial the next queued lead.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | Yes | — | Campaign name |
| `contacts` | array | Yes | — | List of contact objects |
| `schedule_type` | string | No | `"once"` | `"once"` \| `"daily"` \| `"weekdays"` |
| `schedule_time` | string | No | `"09:00"` | `"HH:MM"` 24-hour time |
| `max_parallel_calls` | int | No | `5` | Simultaneous calls per wave (1–20) |
| `call_delay_seconds` | int | No | `3` | Seconds to wait between waves |
| `system_prompt` | string \| null | No | `null` | Override default prompt |
| `additional_instructions` | string \| null | No | `null` | Campaign-wide instructions appended to the standard prompt. Alias: `additionalInstructions` |
| `project_id` | string \| null | No | `null` | Campaign-wide default project ID. Alias: `projectId` |
| `projectDetails` | array \| object \| string \| null | No | `null` | Campaign-wide default project content for contacts without their own project payload. |
| `projects` | array \| null | No | `null` | Campaign-wide default multi-project catalog; contact-level project data takes precedence. |
| `agent_profile_id` | string \| null | No | `null` | Profile UUID |

**Contact object:**
```json
{
  "phone": "+919876543210",
  "lead_name": "Rahul",
  "business_name": "Swaryx",
  "service_type": "site visit",
  "organization_id": "org_abc123",
  "lead_id": "lead_xyz789",
  "project_id": "proj_123",
  "projectDetails": "Project Name: Green Valley\nLocation: Sector 150, Noida\nStarting Price: 80 Lakh\n..."
}
```

> Phone must be E.164 format (`+91...`). Contacts with invalid phone are skipped and counted as failed.
> `project_id`, `projectDetails`, or `projects[]` are optional per contact — same behaviour as `POST /api/call`. One project → single pitch; 2+ projects → multi-project flow.

**Full example — 100 contacts, 5 at a time:**
```json
{
  "name": "Monday Leads",
  "contacts": [
    { "phone": "+919876543210", "lead_name": "Rahul", "business_name": "Swaryx", "service_type": "site visit" },
    { "phone": "+919876543211", "lead_name": "riya", "business_name": "Swaryx", "service_type": "site visit" }
  ],
  "schedule_type": "once",
  "schedule_time": "09:00",
  "max_parallel_calls": 5,
  "call_delay_seconds": 3,
  "system_prompt": null,
  "agent_profile_id": null
}
```

**Response `200`:**
```json
{
  "status": "created",
  "campaign_id": "uuid",
  "campaign": { },
  "calls": [],
  "invalid": []
}
```

`calls` is empty on create. Use `POST /api/campaigns/{campaign_id}/run` to dial (or wait for a scheduled `"daily"` / `"weekdays"` run).

**How wave dispatch works:**
```
Wave 1: contacts 1–5  → dispatched simultaneously
        wait 3 sec
Wave 2: contacts 6–10 → dispatched simultaneously
        wait 3 sec
...
```

- `schedule_type: "once"` → saved only; dial manually via `POST /api/campaigns/{campaign_id}/run`
- `"daily"` / `"weekdays"` → scheduled via APScheduler cron (auto-runs at `schedule_time`)

---

#### `POST /api/batch-call`

Dispatch a one-off parallel batch of calls — **no schedule, no campaign record**. Useful for instant multi-dial from your own backend.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `contacts` | array | Yes | — | List of contact objects (same format as campaigns) |
| `max_parallel_calls` | int | No | `5` | Simultaneous calls per wave (1–20) |
| `call_delay_seconds` | int | No | `3` | Seconds between waves |
| `system_prompt` | string \| null | No | `null` | Override prompt |
| `agent_profile_id` | string \| null | No | `null` | Profile UUID |

**Example:**
```json
{
  "contacts": [
    { "phone": "+919876543210", "lead_name": "Rahul", "business_name": "Swaryx", "service_type": "site visit" },
    { "phone": "+919876543211", "lead_name": "riya", "business_name": "Swaryx", "service_type": "site visit" },
    { "phone": "+919876543212", "lead_name": "Ankit", "business_name": "Swaryx", "service_type": "price query" }
  ],
  "max_parallel_calls": 3,
  "call_delay_seconds": 2
}
```

**Response `200`:**
```json
{
  "status": "dispatching",
  "total": 3,
  "max_parallel_calls": 3,
  "calls": [
    {
      "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
      "status": "queued",
      "room": "batch-919876543210-482",
      "phone": "+919876543210",
      "agent_profile_id": "799e2d7a-d5f4-4fab-bc36-5c3a5bfaa0e8",
      "organization_id": "org_abc123",
      "lead_id": "lead_xyz789",
      "lead_name": "Rahul"
    }
  ],
  "invalid": []
}
```

Each item in `calls` is a Supabase `call_logs` row created immediately. `status: "queued"` means the record exists and LiveKit dispatch is running in the background. Use each `id` for transcript/recording polling.

> Response is returned immediately. Calls are dispatched in the background.

**Difference from `POST /api/call`:**

| | `/api/call` | `/api/batch-call` |
|---|---|---|
| Contacts | 1 | Many |
| Parallel dispatch | No | Yes |
| Campaign record | No | No |
| Schedule | No | No |

---

#### `GET /api/campaigns`

List all campaigns.

---

#### `DELETE /api/campaigns/{campaign_id}`

Delete campaign and remove scheduled job.

**Response `200`:** `{ "status": "deleted" }`

---

#### `POST /api/campaigns/{campaign_id}/run`

Run campaign and start dialing all contacts (regardless of schedule). Use this for `schedule_type: "once"` campaigns after create.

**Only one campaign can run at a time.** If another campaign is already dialing, this returns **`409`**. Scheduled daily/weekday jobs also skip when a run is in progress.

While dialing, the campaign `status` is `running`. When finished: `completed` (once) or back to `active` (recurring).

**Response `200`:**
```json
{
  "status": "dispatching",
  "campaign_id": "uuid",
  "single_run_only": true,
  "calls": [
    {
      "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
      "status": "queued",
      "room": "camp-uuid123-919876543210-482",
      "phone": "+919876543210",
      "agent_profile_id": "799e2d7a-d5f4-4fab-bc36-5c3a5bfaa0e8",
      "organization_id": "org_abc123",
      "lead_id": "lead_xyz789",
      "lead_name": "Rahul"
    }
  ],
  "invalid": []
}
```

**Response `409`:** `{ "detail": "Only one campaign can run at a time. Currently running: …" }`

---

#### `PATCH /api/campaigns/{campaign_id}/status`

**Request body:**
```json
{
  "status": "active"
}
```

Allowed values: `"active"` | `"paused"` | `"completed"`

---

### Transcripts & Recordings

#### How live transcription works

Transcription is powered by **Gemini Live** built-in audio transcription — no separate STT cost.
Enabled by default (`ENABLE_LIVE_TRANSCRIPTION=true`). Set `false` only if you need to disable the transcription side-channel.

- When a call is answered, the agent creates a `call_log` with `status: in_progress`
- User and agent speech are captured as `user_input_transcribed` / `conversation_item_added` events
- `live_transcript` is updated in Supabase every ~2 seconds
- Poll `GET /api/calls/active?organization_id=...&lead_id=...` every 2s to stream text to your dashboard

#### How recording works

- Call audio is uploaded to **Azure Blob Storage** (`recordings/` container) as `.ogg`
- `recording_url` (private Azure URL) is stored in `call_logs`
- `GET /api/calls` returns a `recording_play_url` — a signed SAS URL valid for 1 hour
- `GET /api/calls/{id}/recording` redirects to the signed URL — use this in `<audio>` tags

#### Optional: post-call Deepgram transcript

Set `ENABLE_POST_CALL_TRANSCRIPT=true` + `DEEPGRAM_API_KEY` to run Deepgram after each call. Result saved to `final_transcript` column. Hindi + English supported (nova-3 model, speaker diarization).

---

### Legacy dashboard

#### `GET /`

Serves legacy OutboundAI admin UI (`ui/index.html`). **Do not use for integration.** Your custom frontend should only call `/api/*`.

---

## Data models

### `call_log` {#call_log}

Stored in Supabase table `call_logs`.

| Field | Type | Description |
|---|---|---|
| `id` | string (UUID) | Call record ID |
| `phone_number` | string | E.164 phone |
| `lead_name` | string \| null | Lead name |
| `organization_id` | string \| null | Organization ID (for multi-tenant isolation) |
| `lead_id` | string \| null | Lead ID (for per-lead filtering) |
| `outcome` | string \| null | See [call outcomes](#call-outcomes). `in_progress` during live call |
| `reason` | string \| null | Outcome detail |
| `duration_seconds` | int | Agent-measured call length in seconds (from answer) |
| `billable_minutes` | float | Billable minutes (API-enriched; not a DB column) |
| `estimated_cost` | float | Cost in `currency` (API-enriched) |
| `rate_per_minute` | float | Effective rate used for this row |
| `currency` | string | e.g. `INR` |
| `rate_source` | string | `vobiz_cdr` \| `estimate` |
| `vobiz_billsec` | int \| null | Vobiz billable seconds when CDR matched |
| `vobiz_duration` | int \| null | Vobiz total duration when CDR matched |
| `vobiz_cdr_uuid` | string \| null | Matched Vobiz CDR UUID |
| `timestamp` | string (ISO) | When call started |
| `status` | string | `in_progress` \| `completed` |
| `recording_url` | string \| null | Private Azure Blob URL (do not use directly) |
| `recording_play_url` | string \| null | Signed playable URL (1-hour expiry) — use this |
| `live_transcript` | string \| null | Plain-text transcript updated every ~2s during call |
| `final_transcript` | string \| null | Full Deepgram transcript after call (optional feature) |
| `transcript_json` | string \| null | JSON array `[{role, text, ts}]` |
| `property_data` | object | Lead requirements for **this call** — see [property_data](#property_data) |
| `sentiment` | int | Outcome-mapped score: `1` positive (`booked`), `-1` negative (`not_interested` / `do_not_call`), `0` neutral (all other outcomes). Stored on `call_logs`; also sent on completion webhook |
| `notes` | string \| null | Manual notes |

Billing fields (`billable_minutes`, `estimated_cost`, `rate_*`, `vobiz_*`) are computed at read time by the API — they are not stored columns in Supabase.

---

### `property_data` (per call) {#property_data}

Stored on each `call_logs` row as JSON. **Scoped to `call_id` (UUID)** — never merged across calls or leads by name.

During a call, the AI agent saves structured fields when it invokes the internal `remember_details` tool with insights like `Budget: 50-70 lakh` or `City/area: Pune`. The backend parses known prefixes and merges them into `property_data` for the active `call_id`.

| Key | Source insight prefix | Example value |
|---|---|---|
| `budget` | `Budget:` | `50-70 lakh` |
| `city` | `City/area:` or `City:` | `Pune` |
| `location_preference` | `Location preference:` | `green area` |
| `property_type` | `Property type:` | `ready-to-move` |
| `bhk` | `BHK:` | `3` |
| `whatsapp_number` | `WhatsApp number:` | `+919876543210` |

**Isolation rules:**

- Two leads with the same `lead_name` → separate `call_id` → separate `property_data`.
- Same phone called twice → two rows, two `property_data` snapshots (one per call).
- `contact_memory` (phone-wide) is separate — used for long-term CRM notes, not per-call dashboard columns.

**Exposed on:**

| Endpoint | When |
|---|---|
| `GET /api/calls` | List/history — each row has its own `property_data` |
| `GET /api/calls/{id}` | Single call — full object |
| `GET /api/calls/{id}/transcript` | Transcript poll — includes `property_data` (updates during call) |
| `GET /api/calls/active` | Live calls — `property_data` fills in as lead answers |
| `GET /api/crm/calls?phone=` | Per-call history for one phone |
| Node webhook | `property_data` object on call completed (if non-empty) |

**Database migration** (run once in Supabase SQL Editor if upgrading):

```sql
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS property_data TEXT DEFAULT '{}';
```

**Example (JavaScript):**

```js
const call = await fetch(`${AI_API_BASE}/api/calls/${callId}`).then(r => r.json());
console.log(call.property_data?.budget, call.property_data?.city);
```

---

### `appointment` {#appointment}

Stored in Supabase table `appointments`.

| Field | Type | Description |
|---|---|---|
| `id` | string (UUID) | Appointment ID |
| `name` | string | Lead name |
| `phone` | string | Phone number |
| `date` | string | `YYYY-MM-DD` |
| `time` | string | `HH:MM` |
| `service` | string | Service type |
| `status` | string | `booked` \| `cancelled` |
| `created_at` | string (ISO) | Created timestamp |
| `calcom_booking_uid` | string \| null | Cal.com UID if used |

---

### `campaign`

| Field | Type | Description |
|---|---|---|
| `id` | string | Campaign UUID |
| `name` | string | Name |
| `status` | string | `active` \| `paused` \| `completed` |
| `contacts_json` | string | JSON string of contacts array |
| `schedule_type` | string | `once` \| `daily` \| `weekdays` |
| `schedule_time` | string | `HH:MM` |
| `max_parallel_calls` | int | Simultaneous calls per wave (1–20) |
| `call_delay_seconds` | int | Seconds between waves |
| `total_dispatched` | int | Success count |
| `total_failed` | int | Failed count |
| `last_run_at` | string \| null | ISO timestamp of last run |
| `agent_profile_id` | string \| null | Linked profile |

---

### `agent_profile`

| Field | Type | Description |
|---|---|---|
| `id` | string | Profile UUID |
| `name` | string | Display name |
| `voice` | string | Gemini TTS voice |
| `model` | string | Gemini model |
| `system_prompt` | string \| null | Custom prompt |
| `enabled_tools` | string | JSON array of tool names |
| `is_default` | int | `1` = default profile |

**Enabled tools:** JSON array of tool names. Use `"end_call"` in profiles and settings — at runtime this maps to the internal `wrap_up` tool (same behavior: log + hang up). The caller must never hear tool names spoken aloud.

---

## Call lifecycle & automatic hangup {#call-lifecycle--automatic-hangup}

This section describes what integrators need to know about **call start**, **live state**, and **automatic phone disconnect** — not a separate REST API.

### Lifecycle diagram

```
POST /api/call
    → returns { id, status: "dispatched", room, ... }
    → call_logs row: status = in_progress

SIP dial → lead answers → Gemini Live conversation

Call ending (agent-driven):
    goodbye spoken → wrap_up(outcome, reason)
    → DB: status = completed, outcome, duration, transcripts
    → delete_room → phone disconnects on lead side

Your app:
    GET /api/calls/{id}        (recommended — status + full details)
    GET /api/calls/active      (while in_progress, by org + lead)
    GET /api/calls/{id}/transcript  (transcript-only poll)
```

### What integrators should implement

1. **On dispatch:** Store `id` from `POST /api/call` (or each item in `calls[]` from batch/campaign) as `thirdPartyCallId` on Node.
2. **During / after call:** Poll **`GET /api/calls/{id}`** every 2–5s until `status` is not `in_progress`. Alternatively: `GET /api/calls/active?organization_id=...&lead_id=...` or `GET /api/calls/{id}/transcript` for transcript-only UI.
3. **On completed:** Read `outcome`, `reason`, `duration_seconds`, `recording_play_url`, `transcript_json`, and `property_data` from the same `GET /api/calls/{id}` response (or wait for Node webhook).

### What you do NOT call

| Action | REST API? |
|---|---|
| Start call | `POST /api/call` (or batch/campaign) |
| Hang up / end call manually | **No endpoint** — automatic via agent `wrap_up` |
| Force hangup from your server | Not implemented (contact backend team if required) |

### If the phone stays connected after goodbye

Usually means an **old container image** is running (before hangup fix) or the agent did not invoke `wrap_up`. Verify deployed image includes commit with `delete_room` hangup in `tools.py`. Check agent logs for `Call hung up — room deleted`.

---

## Call outcomes {#call-outcomes}

Stored on each `call_logs` row as `outcome`. Exposed on contacts as **`last_outcome`** via `GET /api/crm`.

Values written by the AI agent when it invokes **`wrap_up`** (enabled in profiles as `end_call`):

| Outcome | Meaning | CRM `last_outcome` |
|---|---|---|
| `in_progress` | Call live | Yes (until completed) |
| `booked` | Appointment confirmed | Yes |
| `not_interested` | Lead declined | Yes |
| `no_answer` | No response / silence | Yes |
| `wrong_number` | Wrong person answered | Yes |
| `voicemail` | Voicemail left | Yes |
| `callback_requested` | Lead asked to call back later | Yes |
| `completed` | Ended without explicit wrap_up outcome | Yes |
| `disconnect_in_between` | Call cut mid-conversation (SIP disconnect before wrap_up) | Yes |
| `failed` | Dispatch / SIP dial failed (includes pool exhausted) | Yes |

### Sentiment (from outcome)

Not transcript NLP — mapped when the call is finalized:

| Score | Meaning | Outcomes |
|---|---|---|
| `1` | positive | `booked` |
| `-1` | negative | `not_interested`, `do_not_call` |
| `0` | neutral | `wrong_number`, `voicemail`, `no_answer`, `callback_requested`, `completed`, `disconnect_in_between`, `failed`, `busy`, … |

Persisted on `call_logs.sentiment` and included as `sentiment` on the call-completed webhook + `GET /api/calls*`.

### AI lead score (from this call)

Computed when the call is finalized / when Node receives the webhook (no extra LLM call):

| Field | Type | Meaning |
|---|---|---|
| `ai_score` | int `0–100` | Lead quality from **this call** |
| `lead_score` / `ai_score_label` | `HOT` / `WARM` / `COLD` | `HOT` ≥75, `WARM` ≥45, else `COLD` |

**How the number is built**

| Signal | Effect |
|---|---|
| Outcome base | e.g. `booked` 80, `callback_requested` 55, `not_interested` 12, `do_not_call` 0 |
| Budget collected | +3 to +8 |
| City / location | +3 to +8 |
| BHK / property type / project | +3 to +5 each |
| Talk time ≥1m / ≥3m | +3 / +6 |

Also mirrored into `property_data.ai_score` + `property_data.lead_score` on the webhook when `property_data` is present. Included on `GET /api/calls/{id}` as `ai_score` / `lead_score`.

---

## Supabase (database)

This backend stores application **data** in Supabase (PostgreSQL). Credentials and AI keys stay in **Azure env vars**, not Supabase.

| Table | Purpose |
|---|---|
| `call_logs` | All call records |
| `phone_numbers` | Outbound caller-ID pool (`free` / `busy` / `disabled`) + optional `organization_id` |
| `appointments` | Bookings from AI agent |
| `contact_memory` | Lead insights across calls |
| `campaigns` | Bulk call campaigns |
| `agent_profiles` | Voice/model/prompt profiles |
| `error_logs` | System logs |
| `settings` | Local dev prompt + job locks (`job_lock:phone_number_sync`) |

**Phone pool SQL:** run `supabase_schema.sql` (includes `phone_numbers` + `acquire_phone_number()` RPC).

**View data:** Supabase Dashboard → **Table Editor** → select table.

**RLS:** Disabled on all tables. Backend uses service role key. Frontend should access data via these APIs, not direct Supabase (unless RLS policies are added later).
---

## Node Calling Service webhook

When your main app uses a **Node / Prisma Calling Service** (separate from this AI backend), completed calls can sync to Node automatically. Full contract and Node handler example: **`webhook.md`**.

### Who calls whom

| Step | Actor | Action |
|---|---|---|
| 1 | Your Node server | `POST` this AI backend `/api/call` |
| 2 | This AI backend | Returns `{ "id": "<uuid>", "status": "dispatched", ... }` |
| 3 | Your Node server | Create/update `Call` row with `thirdPartyCallId = id` |
| 4 | This AI backend | Runs call; on end runs `wrap_up` → Supabase update |
| 5 | This AI backend | `POST` your Node `…/api/calls/webhook/call-completed` |
| 6 | Your Node server | Updates Prisma `Call` + `CallTranscript` |
| 7 | Your frontend | Reads call data from **Node DB** (not Supabase) |

### Azure configuration (this AI backend)

Set on **Azure Container App** → Environment variables:

```env
CALL_COMPLETED_WEBHOOK_URL=https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed
```

Or set base URL only (path appended automatically):

```env
CALLING_SERVICE_BASE_URL=https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io
```

Local dev (Node on same machine):

```env
CALL_COMPLETED_WEBHOOK_URL=http://localhost:5000/api/calls/webhook/call-completed
```

| Variable | Required | Description |
|---|---|---|
| `CALL_COMPLETED_WEBHOOK_URL` | Recommended | **Full URL** to your Node webhook (preferred) |
| `CALLING_SERVICE_BASE_URL` | Alternative | Base only, e.g. `https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io` — path `/api/calls/webhook/call-completed` is appended |
| `CALL_COMPLETED_WEBHOOK_SECRET` | No | If set, sent as `Authorization: Bearer <secret>` |
| `CALL_COMPLETED_WEBHOOK_ENABLED` | No | Set `false` to disable without removing URL |
| `CALL_COMPLETED_WEBHOOK_TIMEOUT` | No | HTTP timeout seconds (default `10`) |

If **no webhook URL** is set, calls still work; data stays in Supabase only (no error).

### Outbound webhook (AI → Node)

**Method:** `POST`  
**URL:** Your `CALL_COMPLETED_WEBHOOK_URL` (must match your Node route)

**Minimum JSON body:**

```json
{
  "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
  "status": "COMPLETED"
}
```

`id` must equal the `id` returned from `POST /api/call` (stored in Node as `thirdPartyCallId`).

**Typical full payload** (sent automatically by `call_webhook.py`):

```json
{
  "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
  "status": "COMPLETED",
  "outcome": "booked",
  "outcome_reason": "wants site visit Saturday",
  "duration": 245,
  "recording_url": "https://account.blob.core.windows.net/recordings/call.ogg",
  "summary": "booked: wants site visit Saturday",
  "transcript": [
    { "speaker": "agent", "text": "Namaste", "timestamp": 0 },
    { "speaker": "customer", "text": "Haan boliye", "timestamp": 3 }
  ],
  "started_at": "2026-06-03T10:00:00",
  "ended_at": "2026-06-03T10:04:05Z",
  "room_name": "call-919876543210-4821",
  "sentiment": 1,
  "ai_score": 92,
  "lead_score": "HOT",
  "ai_score_label": "HOT",
  "property_data": {
    "budget": "50-70 lakh",
    "city": "Pune",
    "bhk": "3",
    "preferred_location": "Pune",
    "min_budget": 5000000,
    "max_budget": 7000000,
    "ai_score": 92,
    "lead_score": "HOT"
  }
}
```

| Field | Node Prisma mapping (typical) |
|---|---|
| `id` | Lookup `thirdPartyCallId` |
| `status` | `COMPLETED` or `FAILED` |
| `outcome` | `outcome` |
| `outcome_reason` | `outcomeReason` |
| `duration` | `durationSeconds` |
| `recording_url` | `recordingUrl` |
| `summary` | `notes` |
| `sentiment` | `sentiment` (`1` / `0` / `-1`) |
| `ai_score` | Lead `aiScore` (0–100) |
| `lead_score` | Lead `leadScore` (`HOT` / `WARM` / `COLD`) |
| `transcript[]` | `CallTranscript` rows (`speaker`, `text`, `timestamp`) |
| `property_data` | **Required for lead UI** — update Lead `preferredLocation` / `minBudget` / `maxBudget` / `bhk` / `aiScore` from this object (see `webhook.md`) |
| `property_data.budget` | Free-text budget (e.g. `"3-4 Lakhs"`) |
| `property_data.min_budget` / `max_budget` | INR integers for Node budget cards |
| `property_data.preferred_location` / `city` | Location string for lead location field |
| `started_at` / `ended_at` | `answeredAt` / `endedAt` |
| `room_name` | `roomName` |

**Expected Node response:** `200` with `{ "success": true, "message": "call synced" }`

**When webhook fires:** After `wrap_up`, SIP auto-finalize, failed dispatch, and optionally again after Deepgram post-call transcript (`ENABLE_POST_CALL_TRANSCRIPT=true`).

### Node requirements (your team)

1. Implement `POST /api/calls/webhook/call-completed` (see `webhook.md`).
2. On call start, save AI dispatch `id` → `thirdPartyCallId`.
3. Production: use a **public HTTPS** Node URL in Azure (not `localhost`).

### Polling vs webhook

| Approach | When to use |
|---|---|
| Webhook | Production — frontend reads Node DB; no duplicate polling of AI API |
| `GET /api/calls/{id}` | Poll single call by dispatch UUID (status, outcome, recording, transcripts, property_data) |
| `GET /api/calls/{id}/transcript` | Transcript-only poll; debugging if webhook not configured |

---

## Environment variables

Set on **Azure Container App** only (not in frontend code):

| Variable | Purpose |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit connection |
| `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GEMINI_TTS_VOICE` | Gemini Live AI |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Database |
| `CALL_COMPLETED_WEBHOOK_URL` | **Full URL** to Node webhook (recommended) — see [Node Calling Service webhook](#node-calling-service-webhook) |
| `CALLING_SERVICE_BASE_URL` | Alternative: Node base URL only (path appended automatically) |
| `CALL_COMPLETED_WEBHOOK_SECRET` | Optional Bearer token for Node |
| `CALL_COMPLETED_WEBHOOK_ENABLED` | Set `false` to disable webhook |
| `CALL_COMPLETED_WEBHOOK_TIMEOUT` | HTTP timeout (default `10`) |
| `OUTBOUND_TRUNK_ID` | LiveKit outbound SIP trunk ID |
| `VOBIZ_SIP_DOMAIN`, `VOBIZ_USERNAME`, `VOBIZ_PASSWORD`, `VOBIZ_OUTBOUND_NUMBER` | SIP trunk credentials (`VOBIZ_OUTBOUND_NUMBER` = legacy single FROM / trunk seed) |
| `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN` | **Vobiz Console Auth** — CDR billing + **phone pool sync** |
| `VOBIZ_RATE_PER_MINUTE` | Fallback rate when CDR not matched (default `0.45`; Vobiz ceil-minute pulse) |
| `VOBIZ_BILLING_CURRENCY` | Default `INR` |
| `PHONE_NUMBER_SYNC_INTERVAL_MINUTES` | Pool sync interval (default `15`) |
| `PHONE_NUMBER_BUSY_MAX_MINUTES` | Stuck-`busy` reclaim threshold (default `75`) |
| `PHONE_NUMBER_JOB_LOCK_TTL_SEC` | Replica job-lock TTL for sync (default `180`) |
| `NUMBER_STATUS_WEBHOOK_URL` | Optional — POST busy/free DID events to your app (popup webhook) |
| `NUMBER_STATUS_WEBHOOK_TIMEOUT` | Webhook HTTP timeout seconds (default `5`) |
| `AI_AGENT_NAME` | LiveKit worker name (prod default `outbound-caller`; prefer over `AGENT_NAME`) |
| `LIVEKIT_AGENT_NAME` | Optional override for LiveKit worker name |
| `AGENT_NAME` | Legacy LiveKit worker name (avoid in Azure DevOps — it is the build agent name) |
| `AMD_ENABLED` | `true`/`false` — answering-machine detection before greeting |
| `AMD_TIMEOUT_SEC` | Max AMD wait seconds (default `2.5`) |
| `AEC_WARMUP_SECONDS` | Ignore barge-in at session start while AEC settles (default `1.5`) |
| `RINGING_TIMEOUT_SEC` | SIP ring timeout (default `30`) |
| `CONFIG_FROM_ENV_ONLY` | `true` in production |
| `SYSTEM_PROMPT` | Optional prompt override |

**Azure Blob Storage (recording + transcript):**

| Variable | Purpose |
|---|---|
| `AZURE_STORAGE_ACCOUNT` | Storage account name (e.g. `swaryxbucket`) |
| `AZURE_STORAGE_KEY` | Storage account key (from Azure Portal → Access keys) |
| `AZURE_STORAGE_CONTAINER` | Container name (e.g. `recordings`) |

**Optional features:**

| Variable | Purpose |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | SMS confirmation after booking |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION` | S3/R2 recording fallback (if Azure not set) |
| `CALCOM_API_KEY`, `CALCOM_EVENT_TYPE_ID` | Cal.com appointment sync |
| `ENABLE_POST_CALL_TRANSCRIPT` | `true` to run Deepgram transcription after each call |
| `DEEPGRAM_API_KEY` | Required only if `ENABLE_POST_CALL_TRANSCRIPT=true` |

---

## Testing with cURL

Replace phone with a real test number in E.164 format.

```bash
BASE="https://swaryx-realestate.icymushroom-70c67802.canadacentral.azurecontainerapps.io"

# Health
curl -s "$BASE/api/health" | jq

# Start call (save the returned "id" for polling)
curl -s -X POST "$BASE/api/call" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+919876543210",
    "lead_name": "Test User",
    "organization_id": "org_test",
    "lead_id": "lead_test",
    "business_name": "Swaryx",
    "service_type": "property visit"
  }' | jq

# Single call by id (recommended — full details)
CALL_ID="<id-from-dispatch-response>"
curl -s "$BASE/api/calls/$CALL_ID" | jq

# Poll until status is completed
curl -s "$BASE/api/calls/$CALL_ID" | jq '.status, .outcome, .duration_seconds'

# Transcript only (lighter)
curl -s "$BASE/api/calls/$CALL_ID/transcript" | jq

# Call history (optional filters) — includes estimated_cost / rate_source
curl -s "$BASE/api/calls?page=1&limit=10&organization_id=org_test&lead_id=lead_test" | jq

# Org billing summary (live Vobiz CDR when Auth env set)
curl -s "$BASE/api/billing?organization_id=org_test" | jq

# Rate metadata
curl -s "$BASE/api/billing/rate" | jq

# Phone number pool
curl -s "$BASE/api/numbers?status=free" | jq
curl -s "$BASE/api/numbers/status" | jq
curl -s -X POST "$BASE/api/numbers/sync" | jq
curl -s -X PATCH "$BASE/api/numbers" -H "Content-Type: application/json" \
  -d '{"e164":"+919876543210","organization_id":"org_builder_a"}' | jq

# Stats
curl -s "$BASE/api/stats" | jq
```
---

## FAQ

### Is the Application URL the same as the API URL?

**Yes.** Use the same base URL and append `/api/...`. You do not need a separate API domain (optional: custom subdomain like `api.yourdomain.com` pointing to the same Azure app).

### Will the legacy dashboard clash with our frontend?

**No.** Both can run in parallel. Your app uses `/api/*` only. The dashboard at `/` is independent.

### Do we need Supabase credentials?

**No**, if you use these REST APIs. The backend reads/writes Supabase. Give developers only the Application URL + this doc.

### How do we limit calls per client?

Not built-in yet. Options: (1) enforce limits on your server before calling `/api/call`, or (2) add API keys + quotas to this backend.

### Can we call `/api/call` from the browser?

Possible but not recommended. Use your server as a proxy to avoid CORS and to add auth/limits.

### What happens after `POST /api/call` returns?

The API returns immediately with `"status": "dispatched"` and a Supabase **`id`**. The phone call runs asynchronously in the background. When the AI finishes (goodbye + internal `wrap_up`), the backend sets `status: completed` and **automatically disconnects the lead's phone** by deleting the LiveKit room. Poll **`GET /api/calls/{id}`** until `status` is `completed`.

### Does the API hang up the call?

**Not via a REST endpoint.** Hangup is automatic inside the voice agent after `wrap_up`. You only **read** the final state via **`GET /api/calls/{id}`** — you do not POST to end the call.

### `GET /api/calls/{id}` returns 404 — what does it mean?

| Response body | Cause |
|---|---|
| `{"detail":"Call not found"}` | UUID is wrong or call never created in `call_logs` |
| `{"detail":"Not Found"}` | Backend image is old — endpoint not deployed; run Azure pipeline |
| `GET /api/calls/{id}/transcript` works but `GET /api/calls/{id}` is 404 | Same as above — redeploy required |

### Why is there no `POST /api/call/end`?

Ending the call requires tearing down the LiveKit SIP session, which only the running agent worker can do safely after logging outcome and transcript. A future manual-hangup API could be added if needed.

### How does the Node Calling Service get call results?

Set `CALL_COMPLETED_WEBHOOK_URL` on Azure to your Node endpoint. This backend POSTs when the call completes. Save the dispatch `id` as `thirdPartyCallId` when starting the call. Details: [Node Calling Service webhook](#node-calling-service-webhook) and `webhook.md`.

### How does outbound caller ID / number pool work?

Owned Vobiz numbers are synced into `phone_numbers` (`POST /api/numbers/sync` or the scheduled job). Assign each DID an `organization_id` via **Numbers** tab or `PATCH /api/numbers`. Before each dial, the agent leases one eligible number (org-owned first, then shared) and passes it as LiveKit `sip_number` (FROM). On call end (`finalize_call`), the number returns to `free`. Inspect with `GET /api/numbers/status`.
---

## Support checklist for integrators

- [ ] Base URL configured in server environment
- [ ] `GET /api/health` returns OK
- [ ] Run `supabase_schema.sql` (includes `phone_numbers` + `acquire_phone_number`)
- [ ] `VOBIZ_AUTH_ID` / `VOBIZ_AUTH_TOKEN` set; `POST /api/numbers/sync` shows free numbers
- [ ] Test call with valid E.164 number
- [ ] Store `id` from dispatch response; poll `GET /api/calls/{id}` until `status: completed` (or use Node webhook)
- [ ] If using Node: `thirdPartyCallId` = dispatch `id`; `CALL_COMPLETED_WEBHOOK_URL` on Azure
- [ ] Confirm phone line disconnects automatically after agent goodbye (not left open)
- [ ] CORS configured if browser → API direct
- [ ] Call limits / auth planned for production
- [ ] Do not expose Azure or Supabase secrets in frontend

---

*Document generated for Swaryx Real Estate OutboundAI backend.*
