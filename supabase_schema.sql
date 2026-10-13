-- ═══════════════════════════════════════════════════════
-- OutboundAI — Complete Database Schema
-- Run once in Supabase Dashboard → SQL Editor (safe to re-run).
--
-- Azure deployment note:
--   • Credentials & integration config → Azure environment variables ONLY
--   • Supabase stores application DATA (calls, campaigns, CRM, appointments)
--   • The `settings` table is kept for local-dev prompt editing only.
--     In production (CONFIG_FROM_ENV_ONLY=true), credentials are never
--     read from or written to `settings`.
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL,
    organization_id TEXT,
    lead_id TEXT,
    project_id TEXT,
    call_id TEXT
);

CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    outcome TEXT,
    reason TEXT,
    duration_seconds INTEGER,
    timestamp TEXT NOT NULL
);

-- Local dev only: stores `system_prompt` when edited via dashboard.
-- Production: use SYSTEM_PROMPT env var in Azure instead.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);

ALTER TABLE appointments  DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs     DISABLE ROW LEVEL SECURITY;
ALTER TABLE settings      DISABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs    DISABLE ROW LEVEL SECURITY;

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_url TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'completed';
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS live_transcript TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS final_transcript TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transcript_json TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS lead_id TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS property_data TEXT DEFAULT '{}';
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS sentiment INTEGER;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS scheduled_at TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS callback_urgency TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_key_slot TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_in INTEGER DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_out INTEGER DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS gemini_tokens_total INTEGER DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS parent_call_id TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS conversation_stage TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS last_agent_question TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS last_customer_answer TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS conversation_completed BOOLEAN DEFAULT FALSE;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS resume_context JSONB DEFAULT '{}'::jsonb;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transcript_status TEXT DEFAULT 'pending';
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transcript_source TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS last_transcript_error TEXT;

CREATE INDEX IF NOT EXISTS idx_call_logs_parent_call_id
    ON call_logs(parent_call_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_phone_timestamp
    ON call_logs(phone_number, timestamp DESC);

CREATE TABLE IF NOT EXISTS transcript_write_queue (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    transcript_json TEXT,
    live_transcript TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE transcript_write_queue DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);
ALTER TABLE campaigns DISABLE ROW LEVEL SECURITY;

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calcom_booking_uid TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS lead_id TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS call_id TEXT;

CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL,
    organization_id TEXT,
    lead_id TEXT,
    memory_key TEXT
);
ALTER TABLE contact_memory ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE contact_memory ADD COLUMN IF NOT EXISTS lead_id TEXT;
ALTER TABLE contact_memory ADD COLUMN IF NOT EXISTS memory_key TEXT;
ALTER TABLE contact_memory DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS organization_projects (
    organization_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    project_details JSONB NOT NULL DEFAULT '{}'::JSONB,
    additional_instructions TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (organization_id, project_id)
);
ALTER TABLE organization_projects DISABLE ROW LEVEL SECURITY;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS agent_profile_id TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS max_parallel_calls INTEGER DEFAULT 5;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS queue_advance BOOLEAN DEFAULT TRUE;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS additional_instructions TEXT;

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT 'Leda',
    model TEXT NOT NULL DEFAULT 'gemini-3.8-live',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE agent_profiles DISABLE ROW LEVEL SECURITY;

-- Existing projects: CREATE IF NOT EXISTS does not change column defaults — run these once:
ALTER TABLE agent_profiles
    ALTER COLUMN model SET DEFAULT 'gemini-3.8-live';
UPDATE agent_profiles
SET model = 'gemini-3.8-live'
WHERE model IN ('gemini-3.1-flash-live-preview', 'gemini-3.8 live')
   OR model LIKE '%2.5%'
   OR model LIKE '%native-audio%';

-- ── Performance indexes (safe to re-run) ──────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_contact_memory_phone ON contact_memory (phone_number);
CREATE INDEX IF NOT EXISTS idx_contact_memory_scope ON contact_memory (organization_id, lead_id, phone_number);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_memory_scope_key
    ON contact_memory (organization_id, lead_id, phone_number, memory_key)
    WHERE organization_id IS NOT NULL AND lead_id IS NOT NULL AND memory_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_call_logs_phone      ON call_logs (phone_number);
CREATE INDEX IF NOT EXISTS idx_call_logs_timestamp  ON call_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_call_logs_active_org_lead ON call_logs (status, organization_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_org_phone ON call_logs (organization_id, phone_number);
CREATE INDEX IF NOT EXISTS idx_call_logs_org_lead_phone ON call_logs (organization_id, lead_id, phone_number);
-- At most one active/queued dial per destination (org-scoped when org is set).
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_logs_one_active_phone_org
    ON call_logs (organization_id, phone_number)
    WHERE status IN ('ringing', 'in_progress', 'queued')
      AND organization_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_logs_one_active_phone_no_org
    ON call_logs (phone_number)
    WHERE status IN ('ringing', 'in_progress', 'queued')
      AND organization_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_logs_id_org ON call_logs (id, organization_id);
CREATE INDEX IF NOT EXISTS idx_appointments_date    ON appointments (date, time);
CREATE INDEX IF NOT EXISTS idx_appointments_phone   ON appointments (phone);
CREATE INDEX IF NOT EXISTS idx_appointments_scope ON appointments (organization_id, lead_id, phone);
CREATE INDEX IF NOT EXISTS idx_appointments_project ON appointments (organization_id, project_id);
CREATE INDEX IF NOT EXISTS idx_organization_projects_active
    ON organization_projects (organization_id, is_active);

CREATE TABLE IF NOT EXISTS call_projects (
    call_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (call_id, project_id),
    CONSTRAINT fk_call_projects_call_scope
        FOREIGN KEY (call_id, organization_id)
        REFERENCES call_logs (id, organization_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_call_projects_project_scope
        FOREIGN KEY (organization_id, project_id)
        REFERENCES organization_projects (organization_id, project_id)
        ON DELETE RESTRICT
);
ALTER TABLE call_projects DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_call_projects_org_call
    ON call_projects (organization_id, call_id);
-- Dedupe before unique index: keep earliest booked row per (date, time), cancel the rest.
-- Required when legacy data already has double-booked slots (unique index would fail otherwise).
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY date, time
               ORDER BY created_at ASC NULLS LAST, id ASC
           ) AS rn
    FROM appointments
    WHERE status = 'booked'
)
UPDATE appointments a
SET status = 'cancelled'
FROM ranked r
WHERE a.id = r.id
  AND r.rn > 1
  AND a.status = 'booked';
-- One booked appointment per slot (cancelled rows may reuse the same date/time).
CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_slot_booked
    ON appointments (date, time)
    WHERE status = 'booked';

-- One network round trip for voice-agent booking. The unique booked-slot index
-- prevents double booking; an unavailable request returns the next business-hour
-- slot from one set-based query instead of checking hours sequentially.
CREATE OR REPLACE FUNCTION book_appointment_atomic(
    p_id TEXT,
    p_name TEXT,
    p_phone TEXT,
    p_date TEXT,
    p_time TEXT,
    p_service TEXT,
    p_created_at TEXT
)
RETURNS TABLE (
    result_status TEXT,
    result_appointment_id TEXT,
    next_date TEXT,
    next_time TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    BEGIN
        INSERT INTO appointments (id, name, phone, date, time, service, status, created_at)
        VALUES (p_id, p_name, p_phone, p_date, p_time, p_service, 'booked', p_created_at);

        RETURN QUERY SELECT 'booked'::TEXT, p_id, NULL::TEXT, NULL::TEXT;
        RETURN;
    EXCEPTION WHEN unique_violation THEN
        -- Continue below and find one alternative in the same RPC call.
    END;

    RETURN QUERY
    WITH candidates AS (
        SELECT
            to_char(candidate, 'YYYY-MM-DD') AS candidate_date,
            to_char(candidate, 'HH24:MI') AS candidate_time,
            candidate
        FROM generate_series(
            (p_date || ' ' || p_time)::timestamp + interval '1 hour',
            (p_date || ' ' || p_time)::timestamp + interval '7 days',
            interval '1 hour'
        ) AS candidate
        WHERE EXTRACT(HOUR FROM candidate) >= 9
          AND EXTRACT(HOUR FROM candidate) < 18
    )
    SELECT
        'unavailable'::TEXT,
        NULL::TEXT,
        c.candidate_date,
        c.candidate_time
    FROM candidates c
    WHERE NOT EXISTS (
        SELECT 1
        FROM appointments a
        WHERE a.date = c.candidate_date
          AND a.time = c.candidate_time
          AND a.status = 'booked'
    )
    ORDER BY c.candidate
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'unavailable'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT;
    END IF;
END;
$$;
CREATE INDEX IF NOT EXISTS idx_error_logs_timestamp ON error_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_campaigns_status     ON campaigns (status);

-- ── Outbound caller-ID pool (Vobiz numbers) ───────────────────────────────────
-- status: free | busy | disabled  (app-enforced TEXT, not a Postgres enum)
CREATE TABLE IF NOT EXISTS phone_numbers (
    e164 TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'free',
    current_call_id TEXT,
    current_room_name TEXT,
    assigned_agent TEXT,
    organization_id TEXT,
    last_used_at TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL,
    vobiz_id TEXT,
    vobiz_status TEXT,
    country TEXT,
    region TEXT
);
ALTER TABLE phone_numbers DISABLE ROW LEVEL SECURITY;
-- Existing projects that already created phone_numbers without org column:
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS organization_id TEXT;

CREATE INDEX IF NOT EXISTS idx_phone_numbers_status
    ON phone_numbers (status);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_busy_used
    ON phone_numbers (status, last_used_at);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_org_status
    ON phone_numbers (organization_id, status);

-- Atomic lease: prefer org-owned free numbers, then shared (NULL org). Never steal another org's DID.
CREATE OR REPLACE FUNCTION acquire_phone_number(
    p_call_id TEXT,
    p_room_name TEXT DEFAULT NULL,
    p_assigned_agent TEXT DEFAULT NULL,
    p_organization_id TEXT DEFAULT NULL
)
RETURNS SETOF phone_numbers
LANGUAGE plpgsql
AS $$
DECLARE
    picked TEXT;
    now_iso TEXT := to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
    org_id TEXT := NULLIF(btrim(COALESCE(p_organization_id, '')), '');
BEGIN
    SELECT e164 INTO picked
    FROM phone_numbers
    WHERE status = 'free'
      AND (
        CASE
          WHEN org_id IS NOT NULL THEN
            (organization_id = org_id
             OR organization_id IS NULL
             OR btrim(COALESCE(organization_id, '')) = '')
          ELSE
            (organization_id IS NULL OR btrim(COALESCE(organization_id, '')) = '')
        END
      )
    ORDER BY
      CASE
        WHEN org_id IS NOT NULL AND organization_id = org_id THEN 0
        ELSE 1
      END,
      last_used_at NULLS FIRST,
      e164
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF picked IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    UPDATE phone_numbers
    SET
        status = 'busy',
        current_call_id = p_call_id,
        current_room_name = p_room_name,
        assigned_agent = COALESCE(p_assigned_agent, assigned_agent),
        last_used_at = now_iso
    WHERE e164 = picked
    RETURNING *;
END;
$$;

-- ── Optional: remove stale credential rows from settings (Azure migration) ───
-- Run this if you previously saved API keys via the dashboard before Azure.
-- Safe to run anytime — only deletes known env/credential keys.
-- DELETE FROM settings WHERE key IN (
--     'LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
--     'GOOGLE_API_KEY', 'GEMINI_MODEL', 'GEMINI_TTS_VOICE', 'USE_GEMINI_REALTIME',
--     'VOBIZ_SIP_DOMAIN', 'VOBIZ_USERNAME', 'VOBIZ_PASSWORD',
--     'VOBIZ_OUTBOUND_NUMBER', 'OUTBOUND_TRUNK_ID', 'DEFAULT_TRANSFER_NUMBER',
--     'DEEPGRAM_API_KEY', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER',
--     'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_ENDPOINT_URL', 'S3_REGION', 'S3_BUCKET',
--     'CALCOM_API_KEY', 'CALCOM_EVENT_TYPE_ID', 'CALCOM_TIMEZONE', 'ENABLED_TOOLS'
-- );
