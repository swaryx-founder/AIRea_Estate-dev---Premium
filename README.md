# OutboundAI — Real Estate Voice Calling Bot

**AI-powered outbound calling backend** for real estate sales teams. Hindi voice agent leads ko call karta hai, baat-cheet record karta hai, outcome log karta hai, aur aapke main app (Node Calling Service) ko sync karta hai.

---

## Hum kya bana rahe hain?

Ek **production-ready AI calling platform** jo:

1. **Outbound phone calls** place kare (SIP telephony se real numbers par)
2. **Gemini Live** se Hindi me natural conversation kare (site visit, callback, objections handle)
3. **Call data** save kare — transcript, outcome, duration, recording
4. **Node.js Calling Service** ko webhook se update kare (aapka frontend Azure DB se padhta hai)
5. **Admin dashboard** de (`/`) — stats, live transcript, call logs, campaigns

**Use case:** Real estate leads ko automatically call karna, interest capture karna, site visit book karna, aur team ko result + transcript dena.

---

## System architecture

For the complete backend-to-Supabase data flow, table ownership, multi-organization boundaries, call lifecycle, and deployment topology, see [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md).

```
┌─────────────────────────────────────────────────────────────┐
│  Your Frontend App (React / web)                            │
│  Reads call data from Node Azure DB                         │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Node Calling Service (Express + Prisma)                      │
│  • Dispatches calls to Python AI                            │
│  • Receives call-completed webhook                            │
│  • (Planned) WhatsApp (Meta) + Email (Azure Communication)    │
└───────────────────────────┬─────────────────────────────────┘
                            │ POST /api/call
                            │ webhook ← call-completed
┌───────────────────────────▼─────────────────────────────────┐
│  Python AI Backend (THIS REPO)                                │
│  server.py (FastAPI) + agent.py (LiveKit worker)              │
│  Deployed on Azure Container Apps                           │
└───────┬─────────────┬──────────────┬──────────────────────────┘
        │             │              │
        ▼             ▼              ▼
   LiveKit Cloud   Google Gemini   Vobiz SIP
   (real-time      Live (Hindi     (PSTN dial —
    rooms)          voice AI)       phone calls)
        │                            │
        └────────────┬───────────────┘
                     ▼
              Supabase (PostgreSQL)
              call_logs, campaigns, CRM
                     │
                     ▼ (LiveKit Egress)
              Azure Blob Storage
              call recordings (.ogg)
```

---

## Kya-kya use ho raha hai (tech stack)

| Layer | Technology | Kya kaam karta hai |
|-------|------------|-------------------|
| **API server** | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn | REST APIs, dashboard, dispatch calls |
| **Voice agent** | [LiveKit Agents](https://docs.livekit.io/agents/) | Room join, SIP, Gemini session |
| **AI model** | **Google Gemini Live** (`gemini-3.8-live`) | Real-time Hindi voice conversation |
| **Real-time infra** | [LiveKit Cloud](https://cloud.livekit.io/) | WebRTC rooms, agent dispatch, call egress (recording) |
| **Telephony** | **Vobiz SIP** | Outbound calls to `+91...` numbers |
| **Database** | [Supabase](https://supabase.com/) (PostgreSQL) | Call logs, transcripts, campaigns, CRM |
| **Recordings** | **Azure Blob Storage** | Private `.ogg` files + signed play URLs |
| **Production host** | **Azure Container Apps** | Docker deploy, env vars, health probes |
| **Main app sync** | **Node.js** webhook | `POST /api/calls/webhook/call-completed` |
| **Post-call STT** (optional) | Deepgram | Better transcript from recording |
| **Booking** (optional) | Cal.com | Online appointment tool for agent |
| **Notifications** (Node side) | Meta WhatsApp Cloud API, Azure Email | Post-call alerts (not in this repo) |

### Python dependencies (high level)

- `livekit-agents`, `livekit-plugins-google` — voice pipeline
- `supabase` — async DB client
- `azure-storage-blob` — recording signed URLs
- `httpx`, `aiohttp` — webhooks & HTTP
- `apscheduler` — scheduled campaigns

---

## Main features

| Feature | Description |
|---------|-------------|
| **Single outbound call** | `POST /api/call` — one lead, E.164 phone |
| **Batch / parallel calls** | `POST /api/batch-call` — multiple leads at once |
| **Campaigns** | Scheduled bulk dialing with delays |
| **Live transcript** | Real-time captions during call (Gemini transcription) |
| **Call outcomes** | `booked`, `callback_requested`, `not_interested`, `voicemail`, etc. |
| **Auto hangup** | Agent goodbye ke baad phone line cut (no manual end API) |
| **Call recording** | LiveKit Egress → Azure Blob (needs LiveKit egress quota) |
| **Node webhook** | Call complete → sync to Prisma DB |
| **Agent profiles** | Voice, model, prompt, tools per profile |
| **Admin dashboard** | Stats, charts, call list, live transcript panel |
| **CRM / contacts** | Per-phone history and insights |

---

## Call lifecycle (short)

```
1. Node (or dashboard) → POST /api/call { phone, lead_name, organization_id, lead_id }
2. Python creates call_logs row (in_progress) + LiveKit room + dispatches agent
3. Agent dials lead via Vobiz SIP → Gemini conversation in Hindi
4. Agent wrap_up(outcome, reason) → Supabase updated → webhook to Node
5. LiveKit room deleted → phone disconnects
6. (Optional) Recording uploaded to Azure Blob via LiveKit Egress
7. Frontend reads completed call from Node DB
```

**Important:** Node must save Python response `id` as `thirdPartyCallId` — webhook matching ke liye.

---

## Repository structure

| File / folder | Purpose |
|---------------|---------|
| `server.py` | FastAPI app — all `/api/*` routes |
| `agent.py` | LiveKit worker — voice agent entrypoint |
| `db.py` | Supabase data layer + caching |
| `call_webhook.py` | Outbound webhook to Node after call ends |
| `transcripts.py` | Live + post-call (Deepgram) transcription |
| `recordings.py` | Azure Blob signed URLs |
| `tools.py` | Agent tools — `wrap_up`, book slot, CRM memory |
| `prompts.py` | Hindi system prompts |
| `config.py` | Environment variable management |
| `ui/index.html` | Legacy admin dashboard |
| `supabase_schema.sql` | Database schema (run once in Supabase) |
| `Dockerfile` + `start.sh` | Production container (API + agent) |
| `scripts/` | Webhook tests, Azure storage test |

---

## Documentation

| Doc | Contents |
|-----|----------|
| [**SETUP_GUIDE.md**](SETUP_GUIDE.md) | Full setup — local + Azure, step by step |
| [**API.md**](API.md) | API reference for integrators / frontend team |
| [**webhook.md**](webhook.md) | Node `call-completed` webhook contract |
| [**AZURE_DEPLOY.md**](AZURE_DEPLOY.md) | Docker build + Container Apps deploy |

---

## Quick start (local)

### Prerequisites

- Python **3.11**
- Accounts: LiveKit Cloud, Google AI (Gemini), Supabase, Vobiz SIP
- Optional: Azure Blob (recordings), Node app (webhook)

### 1. Install

```bash
git clone https://github.com/swaryx-founder/AIReal_Estate.git
cd AIReal_Estate
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Configure

```bash
copy .env.example .env
# Fill all required values — see SETUP_GUIDE.md
```

Minimum `.env` keys: `LIVEKIT_*`, `GOOGLE_API_KEY`, `SUPABASE_*`, `VOBIZ_*`, `OUTBOUND_TRUNK_ID`

### 3. Database

Run `supabase_schema.sql` once in **Supabase → SQL Editor**.

### 4. Run (two terminals)

```powershell
# Terminal 1 — API
$env:LOAD_DOTENV="true"
uvicorn server:app --host 127.0.0.1 --port 8000

# Terminal 2 — Agent worker
$env:LOAD_DOTENV="true"
python agent.py start
```

### 5. Open dashboard

http://localhost:8000

Set **Organization ID** + **Lead ID** in the tenant bar for live transcript polling.

### 6. Health check

```bash
curl http://localhost:8000/api/health
```

---

## Environment variables (summary)

| Variable | Required | Purpose |
|----------|----------|---------|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Yes | LiveKit connection |
| `GOOGLE_API_KEY` | Yes | Gemini Live |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Yes | Database |
| `VOBIZ_*`, `OUTBOUND_TRUNK_ID` | Yes | Outbound phone calls |
| `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_KEY`, `AZURE_STORAGE_CONTAINER` | For recordings | Azure Blob |
| `CALL_COMPLETED_WEBHOOK_URL` | Recommended | Node sync after call |
| `LOAD_DOTENV` | Local only | Load `.env` file |
| `CONFIG_FROM_ENV_ONLY` | Azure | `true` — no dashboard secret edits |

Full list: [`.env.example`](.env.example)

---

## Production (Azure)

1. Build & push Docker image (see `AZURE_DEPLOY.md`)
2. Deploy to **Azure Container Apps**
3. Set all env vars in **Portal → Environment variables** (never commit `.env`)
4. Health probe: `GET /api/health`
5. Set `CALL_COMPLETED_WEBHOOK_URL` to `https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed` on Azure

**Production URL example:**

```
https://swaryx-realestate.icymushroom-70c67802.canadacentral.azurecontainerapps.io
```

---

## Integrations outside this repo

| System | Role |
|--------|------|
| **Node Calling Service** | Main app backend, Prisma DB, frontend API, webhook receiver |
| **Frontend** | UI for agents — reads Node DB, dispatches calls via Node |
| **Meta WhatsApp Cloud API** | Post-call WhatsApp alerts (Node) |
| **Azure Communication Email** | Post-call email (Node) |
| **Azure DB** | Main business database (Node Prisma) |

Python AI repo **sirf calling + AI + Supabase call data** handle karta hai. Email/WhatsApp Node se jayega.

---

## Call outcomes

| Outcome | Meaning |
|---------|---------|
| `booked` | Site visit / appointment confirmed |
| `callback_requested` | Lead asked to call back later |
| `not_interested` | Not interested |
| `wrong_number` | Wrong person / number |
| `voicemail` | Voicemail left |
| `no_answer` | No answer |
| `disconnect_in_between` | Call cut mid-conversation |
| `failed` | Technical / dispatch failure |

---

## Known limits

| Item | Note |
|------|------|
| **LiveKit egress (recording)** | Free tier ~60 min/month — upgrade plan for more |
| **API auth** | Set `API_AUTH_TOKEN` — Bearer / `X-API-Key` required on `/api/*` (except `/api/health`) |
| **Recordings** | Azure Blob only (not Supabase S3) |
| **Old Supabase S3 URLs** | Legacy recordings won't play — new calls use Azure |

---

## Scripts

```bash
# Test Azure Blob credentials
python scripts/test_azure_storage.py

# Test Node webhook
python scripts/test_node_webhook.py

# Test webhook payload builder
python scripts/test_call_webhook.py
```

---

## Team quick reference

| Role | Uses |
|------|------|
| **AI / backend dev** | This repo, Azure Container App, Supabase |
| **Node dev** | Webhook handler, `thirdPartyCallId`, Prisma |
| **Frontend dev** | Node APIs + [API.md](API.md) for dispatch |
| **DevOps** | Docker, Azure env vars, LiveKit + Vobiz setup |

---

## License & repo

**GitHub:** [swaryx-founder/AIReal_Estate](https://github.com/swaryx-founder/AIReal_Estate)

For detailed setup, see [**SETUP_GUIDE.md**](SETUP_GUIDE.md).  
For API integration, see [**API.md**](API.md).


<!-- full tutorial -->
