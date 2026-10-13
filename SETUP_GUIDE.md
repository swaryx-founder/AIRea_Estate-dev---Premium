# OutboundAI — Complete Setup Guide

**Purpose:** Koi bhi naya developer / DevOps engineer is guide se poora AI calling backend setup kar sake — local dev se lekar Azure production tak.

**Repo:** Real Estate Outbound AI Voice Bot  
**Stack:** FastAPI + LiveKit Agent + Gemini Live + Supabase + Vobiz SIP  
**Production target:** Azure Container Apps

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Accounts & services you need](#2-accounts--services-you-need)
3. [Setup order (follow this sequence)](#3-setup-order-follow-this-sequence)
4. [Clone & install](#4-clone--install)
5. [Supabase database](#5-supabase-database)
6. [LiveKit Cloud](#6-livekit-cloud)
7. [Google Gemini API](#7-google-gemini-api)
8. [Vobiz SIP (phone calls)](#8-vobiz-sip-phone-calls)
9. [Environment variables](#9-environment-variables)
10. [Local development (Windows)](#10-local-development-windows)
11. [Local development (Linux / Mac)](#11-local-development-linux--mac)
12. [First call test (local)](#12-first-call-test-local)
13. [SIP trunk one-time setup](#13-sip-trunk-one-time-setup)
14. [Azure deployment](#14-azure-deployment)
15. [Call recordings (Azure Blob)](#15-call-recordings-azure-blob)
16. [Node Calling Service integration](#16-node-calling-service-integration)
17. [Post-call notifications (Node side)](#17-post-call-notifications-node-side)
18. [Verification checklist](#18-verification-checklist)
19. [Troubleshooting](#19-troubleshooting)
20. [File reference](#20-file-reference)

---

## 1. System overview

### Kya karta hai ye system?

- Outbound AI phone calls karta hai (Hindi voice agent)
- Lead se baat karke outcome log karta hai (`booked`, `callback_requested`, `not_interested`, etc.)
- Call history, transcript, recording Supabase me save hoti hai
- Call complete hone par aapke **Node Calling Service** ko webhook bhejta hai
- Optional: post-call Deepgram transcript, Cal.com booking, campaigns

### Architecture

```
┌──────────────────────┐
│  Your Frontend App   │
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│  Node Calling Service│  ← Azure DB (Prisma), WhatsApp, Email
│  (port 5000)         │
└──────────┬───────────┘
           │ POST /api/call
           ▼
┌──────────────────────┐
│  Python AI Backend   │  ← THIS REPO (Azure Container Apps)
│  server.py + agent.py│
└──────────┬───────────┘
           │
    ┌──────┼──────┬─────────────┐
    ▼      ▼      ▼             ▼
 LiveKit Gemini  Vobiz SIP   Supabase
 (rooms)  (AI)   (dial)      (call data)
           │
           │ POST call-completed webhook
           ▼
     Node Calling Service
```

### Two processes run in production

| Process | File | Port | Role |
|---------|------|------|------|
| **API server** | `server.py` (uvicorn) | `8000` | REST API, dispatch calls, dashboard |
| **Agent worker** | `agent.py start` | — | LiveKit worker — joins rooms, runs Gemini voice |

Docker / `start.sh` dono ko ek saath start karta hai.

---

## 2. Accounts & services you need

Setup shuru karne se **pehle** ye accounts ready karo:

| # | Service | Kya chahiye | Kahan milega |
|---|---------|-------------|--------------|
| 1 | **GitHub** | Repo access | Clone URL |
| 2 | **Supabase** | Project URL + Service Role Key | [supabase.com](https://supabase.com) |
| 3 | **LiveKit Cloud** | URL, API Key, API Secret | [cloud.livekit.io](https://cloud.livekit.io) |
| 4 | **Google AI Studio** | Gemini API key | [aistudio.google.com](https://aistudio.google.com) |
| 5 | **Vobiz** | SIP domain, username, password, outbound number | Vobiz dashboard |
| 6 | **Azure** | Container Apps, ACR, (optional) Blob Storage | [portal.azure.com](https://portal.azure.com) |
| 7 | **Node app** | Calling Service running (webhook endpoint) | Your team's repo |

**Optional (baad me):**

| Service | Use |
|---------|-----|
| Azure Blob Storage | Call recordings |
| Deepgram | Post-call transcript (`ENABLE_POST_CALL_TRANSCRIPT`) |
| Cal.com | Online booking tool |
| Meta WhatsApp Cloud API | Post-call WhatsApp (Node side) |
| Azure Communication Email | Post-call email (Node side) |

---

## 3. Setup order (follow this sequence)

```
Step 1  → Supabase project + run supabase_schema.sql
Step 2  → LiveKit Cloud project
Step 3  → Google Gemini API key
Step 4  → Vobiz SIP credentials
Step 5  → Clone repo + pip install + .env file
Step 6  → Local: start server + agent worker
Step 7  → Create SIP trunk (one time) → copy OUTBOUND_TRUNK_ID
Step 8  → Test single call locally
Step 9  → Node webhook endpoint ready + thirdPartyCallId save
Step 10 → Docker build + Azure deploy + env vars
Step 11 → Production health check + test call
Step 12 → (Optional) Azure Blob recordings
```

**Important:** SIP trunk ID bina call dial nahi hogi. Webhook bina Node sync nahi hoga (calls phir bhi chalengi, data sirf Supabase me rahega).

---

## 4. Clone & install

### Prerequisites

- **Python 3.11** (Docker image bhi 3.11 use karta hai)
- **Git**
- **Docker Desktop** (Azure deploy ke liye)

### Clone

```bash
git clone https://github.com/swaryx-founder/AIReal_Estate.git
cd AIReal_Estate
```

### Install Python dependencies

```bash
python -m venv venv

# Windows
venv\Scripts\activate
pip install -r requirements.txt

# Linux / Mac
source venv/bin/activate
pip install -r requirements.txt
```

---

## 5. Supabase database

### 5.1 Create project

1. [supabase.com](https://supabase.com) → New Project
2. Region choose karo (India ke liye Singapore / Mumbai closest)
3. Database password save karo

### 5.2 Get credentials

**Project Settings → API:**

| Key | Use in `.env` |
|-----|---------------|
| Project URL | `SUPABASE_URL` |
| `service_role` key (secret) | `SUPABASE_SERVICE_KEY` |

> **Warning:** Service role key ko kabhi frontend / git me mat daalo. Sirf backend env me.

### 5.3 Run schema

1. Supabase Dashboard → **SQL Editor**
2. `supabase_schema.sql` file ka poora content paste karo
3. **Run** — safe to re-run (uses `IF NOT EXISTS`)

### 5.4 Tables created

| Table | Purpose |
|-------|---------|
| `call_logs` | All calls — outcome, transcript, recording |
| `appointments` | Site visit bookings |
| `agent_profiles` | Voice / model / prompt profiles |
| `campaigns` | Bulk calling campaigns |
| `contact_memory` | Lead insights across calls |
| `settings` | Local dev prompt only |
| `error_logs` | System errors |

### 5.5 Verify

Supabase → **Table Editor** → `call_logs` table dikhni chahiye.

---

## 6. LiveKit Cloud

### 6.1 Create project

1. [cloud.livekit.io](https://cloud.livekit.io) → Sign up
2. Create new project

### 6.2 Get credentials

**Project Settings → Keys:**

| Value | Env variable |
|-------|--------------|
| WebSocket URL (wss://...) | `LIVEKIT_URL` |
| API Key | `LIVEKIT_API_KEY` |
| API Secret | `LIVEKIT_API_SECRET` |

### 6.3 Notes

- LiveKit rooms automatically create hote hain jab call dispatch hoti hai
- Agent worker (`agent.py`) LiveKit se connect hota hai aur room me join karta hai
- SIP trunk LiveKit me configure hota hai (Step 13)

---

## 7. Google Gemini API

### 7.1 Get API key

1. [aistudio.google.com](https://aistudio.google.com) → Get API Key
2. New project ya existing Google Cloud project

| Value | Env variable |
|-------|--------------|
| API Key | `GOOGLE_API_KEY` |

### 7.2 Recommended model settings

```env
GEMINI_MODEL=gemini-3.8-live
GEMINI_TTS_VOICE=Leda
USE_GEMINI_REALTIME=true
```

Agent Hindi me baat karta hai — voice `Leda` default hai (change via agent profiles).

---

## 8. Vobiz SIP (phone calls)

Vobiz aapke phone calls ko PSTN (real phone numbers) se connect karta hai LiveKit ke through.

### Credentials needed

| Vobiz field | Env variable |
|-------------|--------------|
| SIP domain | `VOBIZ_SIP_DOMAIN` |
| Username | `VOBIZ_USERNAME` |
| Password | `VOBIZ_PASSWORD` |
| Outbound caller ID | `VOBIZ_OUTBOUND_NUMBER` |

### After trunk setup

| Field | Env variable |
|-------|--------------|
| LiveKit trunk ID | `OUTBOUND_TRUNK_ID` |

> Trunk ID Step 13 me create hota hai — pehle blank chhod sakte ho, trunk create ke baad add karo.

---

## 9. Environment variables

Copy template:

```bash
cp .env.example .env
```

### Required (minimum to run calls)

```env
# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxx
LIVEKIT_API_SECRET=xxxxx

# Gemini
GOOGLE_API_KEY=AIzaxxxxx
GEMINI_MODEL=gemini-3.8-live
GEMINI_TTS_VOICE=Leda
USE_GEMINI_REALTIME=true

# Supabase
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJxxxxx

# Vobiz SIP
VOBIZ_SIP_DOMAIN=sip.vobiz.ai
VOBIZ_USERNAME=your_username
VOBIZ_PASSWORD=your_password
VOBIZ_OUTBOUND_NUMBER=+91XXXXXXXXXX
OUTBOUND_TRUNK_ID=ST_xxxxx

# App
PORT=8000
CONFIG_FROM_ENV_ONLY=false
LOAD_DOTENV=true
```

### Node webhook (recommended)

```env
CALL_COMPLETED_WEBHOOK_URL=http://localhost:5000/api/calls/webhook/call-completed
# Production:
# CALL_COMPLETED_WEBHOOK_URL=https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed
# CALL_COMPLETED_WEBHOOK_SECRET=your-shared-secret
```

### Azure Blob recordings (optional)

```env
AZURE_STORAGE_ACCOUNT=yourstorageaccount
AZURE_STORAGE_KEY=your-key
AZURE_STORAGE_CONTAINER=recordings
```

### Local debugging only

```env
CALL_WEBHOOK_VERBOSE=true
WEBHOOK_ONLY_LOGS=true
```

| Variable | Local | Azure |
|----------|-------|-------|
| `LOAD_DOTENV` | `true` | **Never set** |
| `CONFIG_FROM_ENV_ONLY` | `false` | `true` |
| `CALL_WEBHOOK_VERBOSE` | `true` (optional) | `false` or unset |
| `WEBHOOK_ONLY_LOGS` | `true` (optional) | **Never set** |

Full list: `.env.example` and `API.md` Section 14.

---

## 10. Local development (Windows)

### Terminal 1 — API server

```powershell
cd "C:\path\to\AIReal_Estate"
.\venv\Scripts\activate
$env:LOAD_DOTENV="true"
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### Terminal 2 — Agent worker

```powershell
cd "C:\path\to\AIReal_Estate"
.\venv\Scripts\activate
$env:LOAD_DOTENV="true"
python agent.py start
```

### Port already in use?

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
```

### Dashboard

Browser: `http://localhost:8000/` — legacy admin UI (calls, stats, setup tab).

### Windows Unicode note

Code me Hindi text ke liye UTF-8 safe prints hain. Agar purana error aaye terminal me, latest `main` branch pull karo.

---

## 11. Local development (Linux / Mac)

```bash
export LOAD_DOTENV=true

# Terminal 1
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2
python agent.py start
```

Ya Docker:

```bash
docker build -t outboundai:latest .
docker run --rm -p 8000:8000 --env-file .env outboundai:latest
```

---

## 12. First call test (local)

### 12.1 Health check

```bash
curl http://localhost:8000/api/health
```

Expected:

```json
{
  "status": "ok",
  "supabase": true,
  "livekit": true,
  "call_completed_webhook": { "enabled": true, "configured": true }
}
```

### 12.2 Dispatch a call

```bash
curl -X POST http://localhost:8000/api/call \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+919876543210",
    "lead_name": "Test Lead",
    "business_name": "Swaryx",
    "service_type": "property visit",
    "organization_id": "org-1",
    "lead_id": "lead-1"
  }'
```

Response:

```json
{
  "status": "dispatched",
  "id": "e8f0c823-99a2-444f-9a20-edd189103e03",
  "room": "call-919876543210-xxxx"
}
```

> `id` ko save karo — ye Node me `thirdPartyCallId` hai.

### 12.3 Poll until complete

```bash
curl http://localhost:8000/api/calls/e8f0c823-99a2-444f-9a20-edd189103e03/transcript
```

Jab `status` = `completed` ho, call khatam.

### 12.4 Webhook debug (local)

`.env` me:

```env
CALL_WEBHOOK_VERBOSE=true
WEBHOOK_ONLY_LOGS=true
```

Terminal me `[WEBHOOK]` lines dikhengi step-by-step.

Test script:

```bash
python scripts/test_node_webhook.py
```

---

## 13. SIP trunk one-time setup

Outbound calls ke liye LiveKit me Vobiz SIP trunk chahiye.

### Option A — Dashboard UI

1. `http://localhost:8000/` → **Setup** tab
2. Vobiz credentials pehle `.env` me set karo
3. **Create SIP Trunk** click karo
4. Response me `trunk_id` copy karo

### Option B — API

```bash
curl -X POST http://localhost:8000/api/setup/trunk
```

Response:

```json
{
  "status": "created",
  "trunk_id": "ST_xxxxxxxxxx",
  "message": "Add OUTBOUND_TRUNK_ID to your Azure environment variables and restart the app."
}
```

### After trunk created

1. `.env` me add karo: `OUTBOUND_TRUNK_ID=ST_xxxxxxxxxx`
2. Azure me bhi same value Environment Variables me add karo
3. App restart karo

> Trunk ek baar ban jaye to same ID reuse karo — har baar naya mat banao.

---

## 14. Azure deployment

### 14.1 Build & push Docker image

```bash
az login

# Create resource group + container registry (one time)
az group create --name outboundai-rg --location eastus
az acr create --resource-group outboundai-rg --name outboundaiacr --sku Basic
az acr login --name outboundaiacr

# Build & push
docker build -t outboundaiacr.azurecr.io/outboundai:latest .
docker push outboundaiacr.azurecr.io/outboundai:latest
```

Windows: `.\build-docker.ps1` bhi use kar sakte ho.

### 14.2 Create Container App

```bash
az containerapp env create \
  --name outboundai-env \
  --resource-group outboundai-rg \
  --location eastus

az containerapp create \
  --name outboundai \
  --resource-group outboundai-rg \
  --environment outboundai-env \
  --image outboundaiacr.azurecr.io/outboundai:latest \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 2 --memory 4Gi \
  --registry-server outboundaiacr.azurecr.io
```

### 14.3 Set environment variables

**Azure Portal → Container App → Settings → Environment variables**

`.env.example` ki **saari required keys** add karo. Minimum:

| Variable | Value |
|----------|-------|
| `CONFIG_FROM_ENV_ONLY` | `true` |
| `PORT` | `8000` |
| `LIVEKIT_URL` | wss://... |
| `LIVEKIT_API_KEY` | ... |
| `LIVEKIT_API_SECRET` | ... |
| `GOOGLE_API_KEY` | ... |
| `SUPABASE_URL` | ... |
| `SUPABASE_SERVICE_KEY` | ... |
| `VOBIZ_*` | all four |
| `OUTBOUND_TRUNK_ID` | ST_... |
| `CALL_COMPLETED_WEBHOOK_URL` | **Public HTTPS Node URL** |

> **Never** upload `.env` file to Azure. Never commit secrets to git.

### 14.4 Health check

```bash
curl https://<your-app>.azurecontainerapps.io/api/health
```

Dockerfile me `HEALTHCHECK` `/api/health` use karta hai.

### 14.5 Post-deploy steps

1. `supabase_schema.sql` already run ho (same Supabase project)
2. `OUTBOUND_TRUNK_ID` set ho (local me bana hua trunk ID reuse karo)
3. `CALL_COMPLETED_WEBHOOK_URL` = `https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed` (**not localhost**)
4. Test call from Node ya curl

### 14.6 CI/CD (optional)

Azure DevOps pipeline: `azure-pipelines.yml` — push to `main` par test → ACR build → Container App deploy. See `AZURE_DEPLOY.md`.

---

## 15. Call recordings (Azure Blob)

### 15.1 Create storage

1. Azure Portal → **Storage Account** → Create
2. **Containers** → create `recordings`
3. **Access keys** → copy key

### 15.2 Env vars

```env
AZURE_STORAGE_ACCOUNT=yourstorageaccount
AZURE_STORAGE_KEY=your-key-here
AZURE_STORAGE_CONTAINER=recordings
```

### 15.3 How it works

- LiveKit call recording upload hoti hai blob me
- `call_logs.recording_url` me URL save hoti hai
- API `GET /api/calls/{id}/recording` signed URL return karta hai (1 hour valid)
- Webhook me `recording_url` Node ko bhi jata hai

---

## 16. Node Calling Service integration

Ye step **Node team** karegi — Python AI is repo me already ready hai.

### 16.1 Flow

```
1. Node → POST Python /api/call
2. Python → returns { id: "uuid" }
3. Node → saves id as thirdPartyCallId in Call table
4. Call runs...
5. Python → POST Node /api/calls/webhook/call-completed
6. Node → updates Call + CallTranscript in Azure DB
7. Frontend → reads from Node DB
```

### 16.2 Node webhook endpoint

Full contract: `webhook.md`

```http
POST /api/calls/webhook/call-completed
Content-Type: application/json

{
  "id": "ai-call-uuid",
  "status": "COMPLETED",
  "outcome": "booked",
  "outcome_reason": "Saturday site visit",
  "duration": 245,
  "recording_url": "https://...",
  "summary": "booked: Saturday site visit",
  "transcript": [
    { "speaker": "agent", "text": "Namaste", "timestamp": 0 }
  ]
}
```

### 16.3 Critical: thirdPartyCallId

```javascript
// Node — when dispatching call
const res = await fetch(`${AI_API}/api/call`, { method: "POST", body: ... });
const { id } = await res.json();

await prisma.call.update({
  where: { id: nodeCallId },
  data: { thirdPartyCallId: id, status: "IN_PROGRESS" },
});
```

Bina `thirdPartyCallId` ke webhook **404 call not found** dega.

### 16.4 Production webhook URL

```env
# Azure AI backend env — NOT localhost
CALL_COMPLETED_WEBHOOK_URL=https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed
```

### 16.5 Test webhook manually

```bash
python scripts/test_node_webhook.py
```

---

## 17. Post-call notifications

| Channel | Where | Trigger |
|---------|-------|---------|
| WhatsApp | `whatsapp.py` (this repo) | `finalize_call` → Meta Cloud API |
| Email | `email_followup.py` (this repo) | `finalize_call` → Azure Communication Services |
| Node sync | `call_webhook.py` | `finalize_call` → Node `/call-completed` |

### Email (ACS) env vars

```env
ACS_CONNECTION_STRING=endpoint=https://...;accesskey=...
ACS_SENDER_EMAIL=DoNotReply@xxxxx.azurecomm.net
ACS_EMAIL_ENABLED=true
# Fallback if lead has no assigned_to_email in property_data:
ACS_FOLLOWUP_TO=agent@example.com
```

Recipient resolution order: `property_data.assigned_to_email` → `agent_email` → `ACS_FOLLOWUP_TO`.

Skipped outcomes: `no_answer`, `voicemail`, `wrong_number`, `failed`, `busy`, `cancelled`.

Local test:

```bash
python scripts/test_followup_email.py --dry-run
python scripts/test_followup_email.py --to you@example.com
```

---

## 18. Verification checklist

Setup complete hone par ye sab ✅ hona chahiye:

### Infrastructure

- [ ] Supabase tables exist (`call_logs`, `agent_profiles`, etc.)
- [ ] LiveKit project active
- [ ] Gemini API key valid
- [ ] Vobiz credentials valid
- [ ] `OUTBOUND_TRUNK_ID` set

### Local

- [ ] `GET /api/health` → `"status": "ok"`
- [ ] `server.py` + `agent.py` dono running
- [ ] Test call dispatches without error
- [ ] Call completes → `call_logs` me row with `outcome`
- [ ] Webhook hits Node (if Node running locally)

### Azure production

- [ ] Container App running, health probe green
- [ ] All env vars set (`CONFIG_FROM_ENV_ONLY=true`)
- [ ] `CALL_COMPLETED_WEBHOOK_URL` = public Node HTTPS URL
- [ ] Test call from production Node app
- [ ] Node DB me call data + transcript save ho
- [ ] Frontend shows completed call

### Optional

- [ ] Azure Blob recordings play via `/api/calls/{id}/recording`
- [ ] Deepgram post-call transcript (`ENABLE_POST_CALL_TRANSCRIPT=true`)
- [ ] WhatsApp + Email notifications on Node

---

## 19. Troubleshooting

### Call dispatch fails (400/500)

| Symptom | Fix |
|---------|-----|
| `Invalid phone` | Use E.164: `+919876543210` |
| LiveKit error | Check `LIVEKIT_URL`, key, secret |
| Trunk error | Set `OUTBOUND_TRUNK_ID`, recreate trunk if needed |
| Agent not joining | Ensure `agent.py start` is running |

### Call connects but agent silent

| Symptom | Fix |
|---------|-----|
| No voice | Check `GOOGLE_API_KEY`, `GEMINI_MODEL` |
| Agent worker down | Restart `python agent.py start` |
| Wrong model | Use `gemini-3.8-live` |

### Webhook 404 "call not found"

| Symptom | Fix |
|---------|-----|
| Node 404 | Node me `thirdPartyCallId` = Python dispatch `id` save karo |
| Webhook skipped | Set `CALL_COMPLETED_WEBHOOK_URL` |
| localhost from Azure | Use public HTTPS Node URL, not `localhost` |

### Webhook not firing locally

```env
CALL_COMPLETED_WEBHOOK_URL=http://localhost:5000/api/calls/webhook/call-completed
CALL_WEBHOOK_VERBOSE=true
```

Check terminal for `[WEBHOOK]` lines.

### API slow locally

- Latest code me Supabase client reuse + caching hai — `git pull` karo
- Pehli request slow ho sakti hai (cold start)

### Unicode / Hindi crash on Windows

- Latest `db.py` / `call_webhook.py` me ASCII-safe prints hain
- `PYTHONIOENCODING=utf-8` set kar sakte ho

### Azure health check failing

- Port must be `8000`
- `start.sh` API pehle start karta hai, agent 5 sec baad
- Check logs: Container App → Log stream

---

## 20. File reference

| File | Purpose |
|------|---------|
| `server.py` | FastAPI REST API |
| `agent.py` | LiveKit voice agent worker |
| `db.py` | Supabase database layer |
| `call_webhook.py` | Node webhook after call complete |
| `tools.py` | Agent tools (wrap_up, book_slot, etc.) |
| `transcripts.py` | Live + post-call transcription |
| `recordings.py` | Azure Blob signed URLs |
| `config.py` | Environment variable management |
| `start.sh` | Docker entrypoint (API + agent) |
| `Dockerfile` | Production container image |
| `.env.example` | All env variable template |
| `supabase_schema.sql` | Database schema (run once) |
| `API.md` | Full API documentation for integrators |
| `webhook.md` | Node webhook contract |
| `AZURE_DEPLOY.md` | Short Azure deploy notes |

### Key API endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/call` | Start single call |
| `POST` | `/api/batch-call` | Parallel batch calls |
| `GET` | `/api/calls` | Call history |
| `GET` | `/api/calls/{id}` | Single call details by id |
| `GET` | `/api/calls/{id}/transcript` | Live / final transcript |
| `GET` | `/api/calls/{id}/recording` | Signed recording URL |
| `POST` | `/api/setup/trunk` | Create SIP trunk (one time) |
| `GET` | `/api/stats` | Dashboard metrics |

---

## Quick reference card

```
┌─────────────────────────────────────────────────────────┐
│  OUTBOUND AI — QUICK SETUP                              │
├─────────────────────────────────────────────────────────┤
│  1. Supabase → run supabase_schema.sql                  │
│  2. .env → fill LiveKit + Gemini + Supabase + Vobiz     │
│  3. Local → uvicorn server:app + python agent.py start  │
│  4. Setup → POST /api/setup/trunk → OUTBOUND_TRUNK_ID   │
│  5. Test → POST /api/call { phone: "+91..." }          │
│  6. Node → save dispatch id as thirdPartyCallId         │
│  7. Azure → docker push + Container App + env vars      │
│  8. Webhook → CALL_COMPLETED_WEBHOOK_URL (HTTPS)        │
└─────────────────────────────────────────────────────────┘
```

---

**Support docs:** `API.md` (integrators), `webhook.md` (Node team), `AZURE_DEPLOY.md` (short deploy).

**Questions?** Check `GET /api/health` and `GET /api/logs` first — most setup issues env vars ya missing agent worker se aate hain.
