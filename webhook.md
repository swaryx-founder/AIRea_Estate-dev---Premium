# AI Call Completion Webhook

## Purpose

Call complete hone ke baad Python AI Service Calling Service ko webhook hit karegi taaki:

- Call Status update ho
- Duration save ho
- Recording URL save ho
- Summary save ho
- Transcript save ho
- Sentiment score save ho (`1` / `0` / `-1` from call outcome)
- AI lead score save ho (`ai_score` 0–100 + `lead_score` HOT/WARM/COLD)
- Callback schedule save ho (`scheduled_at` ISO UTC + `callback_urgency`) jab lead baad mein call maange

---

## Endpoint

```http
POST /api/calls/webhook/call-completed
```

---

## Request Body

```json
{
  "id": "call_123",
  "status": "COMPLETED",
  "duration": 245,
  "recording_url": "https://example.com/recording.mp3",
  "summary": "Customer interested in demo",
  "sentiment": 1,
  "ai_score": 92,
  "lead_score": "HOT",
  "ai_score_label": "HOT",
  "budget": "3-4 Lakhs",
  "min_budget": 300000,
  "max_budget": 400000,
  "transcript": [
    {
      "speaker": "agent",
      "text": "Hello",
      "timestamp": 0
    },
    {
      "speaker": "customer",
      "text": "Hi",
      "timestamp": 2
    }
  ],
  "property_data": {
    "budget": "3-4 Lakhs",
    "bhk": "3BHK",
    "property_type": "Ready-to-move",
    "location_preference": "Less pollution and good connectivity",
    "preferred_location": "Ghaziabad",
    "city": "Ghaziabad",
    "min_budget": 300000,
    "max_budget": 400000,
    "ai_score": 92,
    "lead_score": "HOT"
  }
}
```

> **Lead UI note:** Budget/location/score cards on the Lead page fill only if Node copies `property_data` (+ top-level `ai_score` / `lead_score`) onto the Lead row.

`sentiment` is always sent as an integer:

| Score | Meaning | Typical outcomes |
|---|---|---|
| `1` | positive | `booked` |
| `-1` | negative | `not_interested`, `do_not_call` |
| `0` | neutral | `callback_requested`, `voicemail`, `no_answer`, `wrong_number`, `failed`, … |

### AI lead score

| Field | Type | Meaning |
|---|---|---|
| `ai_score` | int `0–100` | Lead quality from this call |
| `lead_score` | `HOT` / `WARM` / `COLD` | `HOT` ≥75, `WARM` ≥45, else `COLD` |
| `ai_score_label` | same as `lead_score` | Alias for Node mapping |
| `budget` | string | Free-text budget from the call (e.g. `"3-4 Lakhs"`) |
| `min_budget` | int (INR) | Parsed minimum budget |
| `max_budget` | int (INR) | Parsed maximum budget |

### Callback schedule (when lead asks to be called later)

| Field | Type | Meaning |
|---|---|---|
| `outcome` | `"callback_requested"` | Lead wants a later call |
| `scheduled_at` | ISO-8601 UTC string | When to dial. Always sent for `callback_requested` (defaults below) |
| `callback_urgency` | `"now"` \| `"later"` \| `"specific_time"` | How they asked |

**Rules**

- Relative phrases (`"kal 5 baje"`, `"tomorrow 5pm"`) parse in **IST (Asia/Kolkata)** → UTC (today unless they named another day).
- `"now"` with no minutes → `scheduled_at` = now + **2 minutes**.
- `"after 2 minutes"` / `"5 min baad"` → today + that many minutes (UTC ISO).
- Vague `"later"` / `"baad mein"` with no number → `scheduled_at` = now + **10 minutes**.

```json
{
  "id": "<thirdPartyCallId>",
  "status": "COMPLETED",
  "outcome": "callback_requested",
  "outcome_reason": "Lead asked to be called tomorrow evening",
  "scheduled_at": "2026-08-07T11:30:00.000Z",
  "callback_urgency": "specific_time",
  "summary": "...",
  "duration": 180
}
```

### Required Fields

```json
{
  "id": "call_123",
  "status": "COMPLETED"
}
```

> `id` wahi AI Call ID honi chahiye jo call create karte time return hui thi aur Node DB me `thirdPartyCallId` me save hai.

---

## Python Example

```python
import requests

requests.post(
    "https://your-domain.com/api/calls/webhook/call-completed",
    json={
        "id": call_id,
        "status": "COMPLETED",
        "duration": duration,
        "recording_url": recording_url,
        "summary": summary,
        "transcript": transcript
    },
    timeout=10
)
```

---

## Flow

1. Node Service → AI Service Call Create
2. AI Service → Call Complete
3. AI Service → Generate Transcript & Recording
4. AI Service → Hit Webhook
5. Calling Service → Save Data in DB
6. Frontend → Read Directly from DB

---

## Python AI service (this repo)

Call complete hone par `call_webhook.py` automatically `POST` karta hai jab env set ho.

### Environment variables

| Variable | Required | Example |
|----------|----------|---------|
| `CALL_COMPLETED_WEBHOOK_URL` | One of these | `https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io/api/calls/webhook/call-completed` |
| `CALLING_SERVICE_BASE_URL` | Or full URL above | `https://gateway.icymushroom-70c67802.canadacentral.azurecontainerapps.io` (path auto-appended) |
| `CALL_COMPLETED_WEBHOOK_ENABLED` | No | `false` to disable |
| `CALL_COMPLETED_WEBHOOK_TIMEOUT` | No | `10` (seconds) |
| `CALL_COMPLETED_WEBHOOK_SECRET` | No | Bearer token if Node validates it |

`id` = `POST /api/call` response ka `id` (Node `thirdPartyCallId` me save kare).

Triggers: `wrap_up` → `log_call` → `finalize_call`; SIP disconnect auto-finalize; failed dispatch; post-call Deepgram (second sync with `summary` from `final_transcript`).

## Complete Api Structure

```js
API fullpath is `http://localhost:5000/api/calls/webhook/call-completed`

router.post("/webhook/call-completed", (req, res) => {
  try {
    const {
      id,
      status,
      outcome,
      outcome_reason,
      duration,
      recording_url,
      transcript,
      summary,
      sentiment,
      started_at,
      ended_at,
      room_name,
      property_data,
      ai_score,
      lead_score,
      budget,
      min_budget,
      max_budget,
    } = req.body;

    if (!id) {
      return res.status(400).json({
        success: false,
        message: "call id required",
      });
    }

    const call = await prisma.call.findFirst({
      where: {
        thirdPartyCallId: String(id),
      },
      include: {
        transcript: true,
      },
    });

    if (!call) {
      return res.status(404).json({
        success: false,
        message: "call not found",
      });
    }

    await prisma.$transaction(async (tx) => {
      await tx.call.update({
        where: {
          id: call.id,
        },
        data: {
          status: status || call.status,
          outcome: outcome || null,
          outcomeReason: outcome_reason || null,
          durationSeconds: duration || 0,
          recordingUrl: recording_url || null,
          notes: summary || null,
          sentiment: typeof sentiment === "number" ? sentiment : null,
          answeredAt: started_at ? new Date(started_at) : null,
          endedAt: ended_at ? new Date(ended_at) : null,
          roomName: room_name || call.roomName,
          // Optional: persist raw JSON on Call if you have a Json column
          // propertyData: property_data || undefined,
        },
      });

      // ★ REQUIRED for Lead budget/location UI — map AI property_data → Lead fields
      if (property_data && call.leadId) {
        const leadUpdate = {};
        const loc =
          property_data.preferred_location ||
          property_data.city ||
          property_data.location_preference;
        if (loc) leadUpdate.preferredLocation = String(loc);
        const minB = min_budget ?? property_data.min_budget;
        const maxB = max_budget ?? property_data.max_budget;
        if (minB != null) leadUpdate.minBudget = Number(minB);
        if (maxB != null) leadUpdate.maxBudget = Number(maxB);
        if (property_data.bhk) leadUpdate.bhkPreference = String(property_data.bhk);
        if (property_data.property_type) leadUpdate.preference = String(property_data.property_type);
        // AI lead score from this call (0–100 + HOT/WARM/COLD)
        const score = ai_score ?? property_data.ai_score;
        const label = lead_score || property_data.lead_score;
        if (score != null) leadUpdate.aiScore = Number(score);
        if (label) leadUpdate.leadScore = String(label);
        // Adjust field names to match your Prisma Lead model.
        if (Object.keys(leadUpdate).length) {
          await tx.lead.update({
            where: { id: call.leadId },
            data: leadUpdate,
          });
        }
      }

      if (Array.isArray(transcript)) {
        await tx.callTranscript.deleteMany({
          where: {
            callId: call.id,
          },
        });

        await tx.callTranscript.createMany({
          data: transcript.map((item) => ({
            callId: call.id,
            speaker: item.speaker || "unknown",
            text: item.text || "",
            timestamp: item.timestamp || 0,
          })),
        });
      }
    });

    return res.status(200).json({
      success: true,
      message: "call synced",
    });
  } catch (err) {
    console.error(
      "[CALLING] call-completed webhook error:",
      err
    );

    return res.status(500).json({
      success: false,
      message: "failed to process webhook",
    });
  }
};);



```
