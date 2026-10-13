"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import re
import ssl
import time
import certifi
import aiohttp
from pathlib import Path
from typing import Any, Optional, Union

import config  # Azure/process env is source of truth; .env only when LOAD_DOTENV=true
import latency_log
import voice_settings
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_calls_by_phone, get_campaign,
    get_contacts, get_errors, get_logs, get_setting, get_stats, init_db, log_error,
    save_settings, set_setting, update_call_notes, update_campaign_run_stats, update_campaign_status,
    get_call_by_id, get_active_calls, get_active_call_for_phone, create_active_call, finalize_call, parse_property_data,
    ActiveCallConflictError,
    normalize_sentiment, compute_ai_lead_score,
    get_org_call_usage, phone_pool_status, count_pool_capacity, set_phone_number_organization,
    get_gemini_token_summary, mark_call_ringing,
    attach_tokens_per_minute, backfill_call_gemini_tokens,
    get_running_campaign, get_campaign_run_lock_owner, clear_stale_running_campaigns,
    try_acquire_job_lock, release_job_lock,
    bind_call_projects, resolve_organization_projects, ProjectScopeError,
)
from prompts import DEFAULT_SYSTEM_PROMPT, normalize_projects, adapt_call_project_payload
from phone_utils import normalize_e164_phone
from recordings import recording_blob_exists, signed_recording_url
from billing import (
    billing_rate_payload_live,
    enrich_call_billing_async,
    enrich_calls_billing,
    summarize_usage,
)
from callback_schedule import callback_fields_from_record
from api_auth import ApiAuthMiddleware, auth_required
from key_pool import key_pool_public_status, redact_secrets, list_key_slots

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")
logging.getLogger("call-webhook").setLevel(logging.INFO)

_lk_session: Optional[aiohttp.ClientSession] = None
_lk_client = None
_lk_lock = asyncio.Lock()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    IntervalTrigger = None  # type: ignore
    logger.warning("APScheduler not installed — campaign scheduling disabled")

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiAuthMiddleware)


@app.get("/health")
async def health_check():
    """Lightweight liveness probe. Readiness details live on GET /api/health."""
    return {"status": "ok", "service": "OutboundAI"}


def _json_safe_value(value: Any) -> Any:
    """Make validation error payloads JSON-serializable (Pydantic ctx may hold exceptions)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(v) for v in value]
    return str(value)


@app.exception_handler(RequestValidationError)
async def _redact_validation_errors(request: Request, exc: RequestValidationError):
    """Redact secrets from 422 validation payloads (never echo API keys)."""
    safe_errors = []
    for err in exc.errors():
        item = dict(err)
        if "ctx" in item:
            item["ctx"] = redact_secrets(_json_safe_value(item["ctx"]))
        if "input" in item:
            item["input"] = "[redacted]"
        if "msg" in item:
            item["msg"] = redact_secrets(str(item["msg"]))
        safe_errors.append(item)
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.exception_handler(Exception)
async def _redact_unhandled_errors(request: Request, exc: Exception):
    """Never echo API keys in 500 bodies."""
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": redact_secrets(f"Internal server error: {exc}")[:500]},
    )


@app.on_event("startup")
async def _startup():
    if auth_required():
        logger.info("API auth ENABLED (API_AUTH_TOKEN set)")
    elif config.is_env_only_mode():
        logger.warning(
            "API auth DISABLED — set API_AUTH_TOKEN in Azure. "
            "All /api/* routes are open until then."
        )
    try:
        pool_status = await key_pool_public_status()
        logger.info(
            "Gemini key pool: %s keys=%s available=%s",
            pool_status.get("backend"),
            pool_status.get("total_keys"),
            pool_status.get("available"),
        )
    except Exception as exc:
        logger.warning("Gemini key pool status unavailable: %s", exc)
    try:
        from db import _adb
        await _adb()
        print("OK: Supabase async client ready")
    except Exception as exc:
        print(f"WARN: Supabase init failed: {exc}")
    if _scheduler:
        _scheduler.start()
        asyncio.create_task(_reschedule_all_campaigns())
        asyncio.create_task(_schedule_phone_pool_jobs())
        # Kick an initial sync shortly after boot (locked across replicas).
        async def _boot_sync():
            await asyncio.sleep(3)
            try:
                from phone_pool import run_locked_sync
                await run_locked_sync()
            except Exception as exc:
                logger.warning("Boot phone pool sync failed: %s", exc)
        asyncio.create_task(_boot_sync())


async def _schedule_phone_pool_jobs() -> None:
    if not _scheduler or IntervalTrigger is None:
        return
    from phone_pool import run_locked_reclaim, run_locked_sync, sync_interval_minutes

    interval = sync_interval_minutes()
    _scheduler.add_job(
        run_locked_sync,
        trigger=IntervalTrigger(minutes=interval),
        id="phone_number_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        run_locked_reclaim,
        trigger=IntervalTrigger(minutes=5),
        id="phone_number_reclaim",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    from db import run_locked_active_call_reconcile
    import uuid as _uuid

    reconcile_owner = f"api-{_uuid.uuid4().hex[:8]}"

    async def _reconcile_job() -> None:
        try:
            await run_locked_active_call_reconcile(reconcile_owner)
        except Exception as exc:
            logger.warning("active call reconcile failed: %s", exc)

    _scheduler.add_job(
        _reconcile_job,
        trigger=IntervalTrigger(seconds=60),
        id="active_call_reconcile",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Phone pool jobs scheduled — sync every %d min, reclaim every 5 min, "
        "stale-call reconcile every 60s",
        interval,
    )

@app.on_event("shutdown")
async def _shutdown():
    global _lk_session, _lk_client
    if _lk_client is not None:
        try:
            await _lk_client.aclose()
        except Exception:
            pass
        _lk_client = None
    if _lk_session is not None:
        try:
            await _lk_session.close()
        except Exception:
            pass
        _lk_session = None
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    """Effective config value — always from environment variables."""
    return config.get_env(key)


_dynamic_lk_clients: dict[tuple[str, str, str], Any] = {}


async def _get_livekit(livekit_config: Optional[dict] = None):
    """Reuse one LiveKit HTTP session. If client livekit_config is provided, use it."""
    global _lk_session, _lk_client
    if livekit_config:
        url = (livekit_config.get("url") or "").strip()
        key = (livekit_config.get("api_key") or livekit_config.get("apiKey") or "").strip()
        secret = (livekit_config.get("api_secret") or livekit_config.get("apiSecret") or "").strip()
        if url and key and secret:
            cache_key = (url, key, secret)
            client = _dynamic_lk_clients.get(cache_key)
            if client is not None:
                from livekit import api as lk_api
                return client, lk_api
            from livekit import api as lk_api
            ssl_ctx = ssl.create_default_context()
            sess = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx, limit=20, ttl_dns_cache=300),
            )
            client = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=sess)
            _dynamic_lk_clients[cache_key] = client
            return client, lk_api

    if _lk_client is not None:
        from livekit import api as lk_api
        return _lk_client, lk_api

    async with _lk_lock:
        if _lk_client is not None:
            from livekit import api as lk_api
            return _lk_client, lk_api
        url = config.get_env("LIVEKIT_URL")
        key = config.get_env("LIVEKIT_API_KEY")
        secret = config.get_env("LIVEKIT_API_SECRET")
        if not all([url, key, secret]):
            raise HTTPException(400, "LiveKit credentials not configured. Please provide livekit_config in request or configure environment.")
        from livekit import api as lk_api
        # Verify TLS with certifi-backed default context (see ssl patch at module top).
        ssl_ctx = ssl.create_default_context()
        _lk_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_ctx, limit=20, ttl_dns_cache=300),
        )
        _lk_client = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=_lk_session)
        return _lk_client, lk_api


async def _resolve_sip_trunk(
    lk,
    lk_api,
    vobiz_config: Optional[dict] = None,
    livekit_trunk_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve a pre-created trunk without creating or guessing one at call time."""
    requested = (livekit_trunk_id or "").strip()
    if requested:
        return requested

    # Backward compatibility for callers that previously nested the ID under
    # credentials.vobiz.trunk_id / trunkId.
    vobiz_config = vobiz_config or {}
    nested = (vobiz_config.get("trunk_id") or vobiz_config.get("trunkId") or "").strip()
    if nested:
        return nested

    # Never fall back to a global or arbitrary trunk in multi-tenant dispatch.
    # A missing ID must fail clearly instead of routing through another org.
    return None


# ── Health (Azure Container Apps / App Service probes) ────────────────────────

@app.get("/api/health")
async def api_health():
    from call_webhook import webhook_status
    az_account = config.get_env("AZURE_STORAGE_ACCOUNT")
    az_key = config.get_env("AZURE_STORAGE_KEY")
    az_container = config.get_env("AZURE_STORAGE_CONTAINER", "recordings")
    azure_ok = bool(az_account and az_key)
    return {
        "status": "ok",
        "build_id": config.get_env("BUILD_ID") or "unknown",
        "env_only": config.is_env_only_mode(),
        "api_auth": auth_required(),
        "gemini_key_pool": await key_pool_public_status(),
        "supabase": bool(config.get_env("SUPABASE_URL") and config.get_env("SUPABASE_SERVICE_KEY")),
        "livekit": bool(config.get_env("LIVEKIT_URL")),
        "call_completed_webhook": webhook_status(),
        "recording_storage": {
            "backend": "azure_blob",
            "azure_configured": azure_ok,
            "azure_account": az_account or None,
            "azure_container": az_container if azure_ok else None,
            "hint": (
                None if azure_ok else
                "Recordings use Azure Blob only. Set AZURE_STORAGE_ACCOUNT + "
                "AZURE_STORAGE_KEY + AZURE_STORAGE_CONTAINER. Remove Supabase S3 "
                "vars — they are not used for call recordings."
            ),
        },
    }


# ── Request models ────────────────────────────────────────────────────────────

class VobizCredentials(BaseModel):
    model_config = {"populate_by_name": True}

    sip_domain: Optional[str] = Field(default=None, alias="sipDomain")
    username: Optional[str] = None
    password: Optional[str] = None
    from_number: Optional[str] = Field(default=None, alias="fromNumber")
    auth_id: Optional[str] = Field(default=None, alias="authId")
    auth_token: Optional[str] = Field(default=None, alias="authToken")
    trunk_id: Optional[str] = Field(default=None, alias="trunkId")


class LiveKitCredentials(BaseModel):
    model_config = {"populate_by_name": True}

    url: Optional[str] = None
    api_key: Optional[str] = Field(default=None, alias="apiKey")
    api_secret: Optional[str] = Field(default=None, alias="apiSecret")


class ClientCredentials(BaseModel):
    model_config = {"populate_by_name": True}

    gemini_api_key: Optional[str] = Field(default=None, alias="geminiApiKey")
    vobiz: Optional[VobizCredentials] = None
    livekit: Optional[LiveKitCredentials] = None


class ProjectItem(BaseModel):
    model_config = {"populate_by_name": True}

    project_id: Optional[str] = Field(default=None, alias="projectId")
    id: Optional[str] = None
    project_name: Optional[str] = Field(default=None, alias="projectName")
    name: Optional[str] = None
    project_details: Optional[Union[str, dict, Any]] = Field(default=None, alias="projectDetails")
    # Optional per-project notes appended into the projects catalog (not a full prompt replace).
    additional_instructions: Optional[str] = Field(default=None, alias="additionalInstructions")
    notes: Optional[str] = None


class CallContactItem(BaseModel):
    model_config = {"populate_by_name": True}

    phone: Optional[str] = None
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    lead_name: str = "there"
    organization_id: Optional[str] = None
    lead_id: Optional[str] = None
    livekit_trunk_id: Optional[str] = Field(default=None, alias="livekitTrunkId")
    # Optional outbound caller ID (must be in phone pool).
    from_number: Optional[str] = None
    caller_id: Optional[str] = None
    sip_number: Optional[str] = None
    calling_number: Optional[str] = None
    # Optional callback context (Node scheduled dial or CRM carry-over).
    scheduled_at: Optional[str] = None
    callback_urgency: Optional[str] = None
    # Verified provenance shown to the agent only when the lead asks about their data.
    lead_source: Optional[str] = Field(default=None, alias="leadSource")
    lead_source_date: Optional[str] = Field(default=None, alias="leadSourceDate")
    # Client-provided credentials per contact
    credentials: Optional[ClientCredentials] = None
    gemini_api_key: Optional[str] = Field(default=None, alias="geminiApiKey")
    vobiz_config: Optional[VobizCredentials] = Field(default=None, alias="vobizConfig")
    livekit_config: Optional[LiveKitCredentials] = Field(default=None, alias="livekitConfig")

    @model_validator(mode="after")
    def _normalize_phone(self):
        if not self.phone and self.phone_number:
            self.phone = self.phone_number
        return self

    def resolved_phone(self) -> str:
        return (self.phone or self.phone_number or "").strip()


class CallRequest(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    # Single-number (legacy) OR multi-number simultaneous dial via `phones`.
    phone: Optional[str] = None
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    phones: Optional[list[str]] = None
    # Per-contact name + phone (e.g. Single Call UI with 2 contact boxes).
    contacts: Optional[list[CallContactItem]] = None
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    # Full prompt override (replaces default script when no project template wins).
    system_prompt: Optional[str] = None
    # Extra campaign/call notes — appended onto the usual base prompt (does NOT replace it).
    additional_instructions: Optional[str] = Field(default=None, alias="additionalInstructions")
    agent_profile_id: Optional[str] = None
    # Optional LiveKit worker name override (default: env AI_AGENT_NAME / outbound-caller).
    agent_name: Optional[str] = None
    organization_id: Optional[str] = None
    lead_id: Optional[str] = None
    # Pre-created LiveKit SIP trunk for this organization.
    livekit_trunk_id: Optional[str] = Field(default=None, alias="livekitTrunkId")
    project_id: Optional[str] = None
    # Pitch string OR (via before-validator) Node CRM object. Arrays rewritten to projects[].
    project_details: Optional[Union[str, dict, Any]] = Field(default=None, alias="projectDetails")
    campaign_id: Optional[str] = Field(default=None, alias="campaignId")
    # Broker may send 2+ projects — agent presents each and lead picks one.
    projects: Optional[list[ProjectItem]] = None
    # When phones[] is set, dial all at once (capped by Gemini key pool size).
    simultaneous: bool = True
    # Optional outbound caller ID (must be free in pool). Omit → agent auto-leases a free DID.
    from_number: Optional[str] = None
    caller_id: Optional[str] = None
    sip_number: Optional[str] = None
    calling_number: Optional[str] = None
    # Optional callback schedule — stored on call_logs; echoed in dispatch + GET poll.
    scheduled_at: Optional[str] = None
    callback_urgency: Optional[str] = None
    # Verified provenance shown to the agent only when the lead asks about their data.
    lead_source: Optional[str] = Field(default=None, alias="leadSource")
    lead_source_date: Optional[str] = Field(default=None, alias="leadSourceDate")
    # Client-provided credentials
    credentials: Optional[ClientCredentials] = None
    gemini_api_key: Optional[str] = Field(default=None, alias="geminiApiKey")
    vobiz_config: Optional[VobizCredentials] = Field(default=None, alias="vobizConfig")
    livekit_config: Optional[LiveKitCredentials] = Field(default=None, alias="livekitConfig")

    @model_validator(mode="before")
    @classmethod
    def _adapt_node_project_payload(cls, data: Any):
        """Accept Node CRM projectDetails as object/array — convert to pitch strings."""
        if isinstance(data, dict):
            return adapt_call_project_payload(data)
        return data

    @model_validator(mode="after")
    def _require_phone_or_phones(self):
        if not self.phone and self.phone_number:
            self.phone = self.phone_number
        has_phone = bool((self.phone or "").strip()) or bool(self.phones)
        has_contacts = bool(self.contacts) and any((c.resolved_phone() or "").strip() for c in self.contacts)
        if not has_phone and not has_contacts:
            raise ValueError("Provide phone, phone_number, phones[], or contacts[]")
        return self

    def resolved_from_number(self) -> Optional[str]:
        raw = (
            self.from_number
            or self.caller_id
            or self.sip_number
            or self.calling_number
            or ""
        ).strip()
        return raw or None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Leda"
    model: str = "gemini-3.8-live"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 0
    max_parallel_calls: int = 5
    # When True: busy/no-answer/end → automatically dial next queued lead on that slot.
    queue_advance: bool = True
    system_prompt: Optional[str] = None
    # Extra notes appended to the base agent script (preferred over full system_prompt replace).
    additional_instructions: Optional[str] = Field(default=None, alias="additionalInstructions")
    # Optional campaign-wide project defaults. A contact's own project payload wins.
    project_id: Optional[str] = Field(default=None, alias="projectId")
    project_details: Optional[Union[str, dict, list, Any]] = Field(
        default=None,
        alias="projectDetails",
    )
    projects: Optional[list[ProjectItem]] = None
    agent_profile_id: Optional[str] = None
    livekit_trunk_id: Optional[str] = Field(default=None, alias="livekitTrunkId")
    # Client-provided credentials
    credentials: Optional[ClientCredentials] = None
    gemini_api_key: Optional[str] = Field(default=None, alias="geminiApiKey")
    vobiz_config: Optional[VobizCredentials] = Field(default=None, alias="vobizConfig")
    livekit_config: Optional[LiveKitCredentials] = Field(default=None, alias="livekitConfig")


class BatchCallRequest(BaseModel):
    model_config = {"populate_by_name": True}

    contacts: list
    call_delay_seconds: int = 0
    max_parallel_calls: int = 5
    queue_advance: bool = True
    system_prompt: Optional[str] = None
    additional_instructions: Optional[str] = Field(default=None, alias="additionalInstructions")
    agent_profile_id: Optional[str] = None
    livekit_trunk_id: Optional[str] = Field(default=None, alias="livekitTrunkId")
    # Client-provided credentials
    credentials: Optional[ClientCredentials] = None
    gemini_api_key: Optional[str] = Field(default=None, alias="geminiApiKey")
    vobiz_config: Optional[VobizCredentials] = Field(default=None, alias="vobizConfig")
    livekit_config: Optional[LiveKitCredentials] = Field(default=None, alias="livekitConfig")


class StatusRequest(BaseModel):
    status: str


def _extract_client_credentials(item: Any) -> dict:
    """Extract and normalize client credentials from request object or dictionary."""
    out = {
        "gemini_api_key": None,
        "vobiz": {},
        "livekit": {},
    }
    if not item:
        return out

    creds = getattr(item, "credentials", None) if hasattr(item, "credentials") else (item.get("credentials") if isinstance(item, dict) else None)
    if isinstance(creds, ClientCredentials):
        if creds.gemini_api_key:
            out["gemini_api_key"] = creds.gemini_api_key.strip()
        if creds.vobiz:
            out["vobiz"] = creds.vobiz.model_dump(exclude_none=True)
        if creds.livekit:
            out["livekit"] = creds.livekit.model_dump(exclude_none=True)
    elif isinstance(creds, dict):
        if creds.get("gemini_api_key") or creds.get("geminiApiKey"):
            out["gemini_api_key"] = str(creds.get("gemini_api_key") or creds.get("geminiApiKey")).strip()
        if isinstance(creds.get("vobiz"), dict):
            out["vobiz"] = {k: str(v).strip() for k, v in creds["vobiz"].items() if v is not None}
        if isinstance(creds.get("livekit"), dict):
            out["livekit"] = {k: str(v).strip() for k, v in creds["livekit"].items() if v is not None}

    # Top-level fields
    gemini_key = getattr(item, "gemini_api_key", None) if hasattr(item, "gemini_api_key") else (item.get("gemini_api_key") if isinstance(item, dict) else None)
    if not gemini_key and isinstance(item, dict):
        gemini_key = item.get("geminiApiKey")
    if gemini_key and not out["gemini_api_key"]:
        out["gemini_api_key"] = str(gemini_key).strip()

    vobiz_raw = getattr(item, "vobiz_config", None) if hasattr(item, "vobiz_config") else (item.get("vobiz_config") if isinstance(item, dict) else None)
    if not vobiz_raw and isinstance(item, dict):
        vobiz_raw = item.get("vobizConfig") or item.get("vobiz")
    if isinstance(vobiz_raw, VobizCredentials):
        for k, v in vobiz_raw.model_dump(exclude_none=True).items():
            out["vobiz"].setdefault(k, v)
    elif isinstance(vobiz_raw, dict):
        for k, v in vobiz_raw.items():
            if v is not None:
                out["vobiz"].setdefault(k, str(v).strip())

    livekit_raw = getattr(item, "livekit_config", None) if hasattr(item, "livekit_config") else (item.get("livekit_config") if isinstance(item, dict) else None)
    if not livekit_raw and isinstance(item, dict):
        livekit_raw = item.get("livekitConfig") or item.get("livekit")
    if isinstance(livekit_raw, LiveKitCredentials):
        for k, v in livekit_raw.model_dump(exclude_none=True).items():
            out["livekit"].setdefault(k, v)
    elif isinstance(livekit_raw, dict):
        for k, v in livekit_raw.items():
            if v is not None:
                out["livekit"].setdefault(k, str(v).strip())

    return out


def _gemini_key_required() -> bool:
    """Gemini keys gate the Gemini Live voice. The premium cascade speaks without one."""
    stack = (config.get_env("VOICE_STACK") or "").strip().lower()
    return stack not in {"cascade", "mercury", "sarvam"}


def _validate_client_credentials(creds: dict) -> None:
    """Strict validation: ensure client has supplied Gemini API Key (Gemini Live only)."""
    if not _gemini_key_required():
        return
    gemini_key = creds.get("gemini_api_key")
    if not gemini_key:
        raise HTTPException(
            400,
            "Client credentials required: 'gemini_api_key' must be provided in client credentials.",
        )


def _validate_call_initiation_requirements(
    *,
    organization_id: Optional[str],
    lead_id: Optional[str],
    creds: dict,
) -> tuple[str, str]:
    """Strict validation: organization_id, lead_id, and gemini_api_key are strictly required to start a call."""
    org_id = (str(organization_id).strip() if organization_id else "")
    lead_id_val = (str(lead_id).strip() if lead_id else "")
    gemini_key = (str(creds.get("gemini_api_key") or "").strip() if creds.get("gemini_api_key") else "")

    if not org_id:
        raise HTTPException(
            400,
            "Missing required parameter: 'organization_id' is strictly required to initiate a call.",
        )
    if not lead_id_val:
        raise HTTPException(
            400,
            "Missing required parameter: 'lead_id' is strictly required to initiate a call.",
        )
    if not gemini_key and _gemini_key_required():
        raise HTTPException(
            400,
            "Client credentials required: 'gemini_api_key' must be provided in client credentials.",
        )
    return org_id, lead_id_val


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)


# ── Call dispatch ─────────────────────────────────────────────────────────────

def _resolve_dispatch_agent_name(explicit: Optional[str] = None) -> str:
    """LiveKit agent_name for CreateAgentDispatchRequest.

    Default LIVEKIT_AGENT_NAME=outbound-caller (prod). Local test: set
    LIVEKIT_AGENT_NAME=outbound-caller-test-local and/or pass agent_name on POST /api/call
    so jobs never hit the Azure worker. Retired names (e.g. outbound-caller-sarvam) are
    ignored and fall back to env default.
    """
    requested = (explicit or "").strip()
    resolved = config.resolve_livekit_agent_name(explicit)
    if requested and resolved != requested:
        logger.warning(
            "Ignoring retired LiveKit agent_name=%r — dispatching to %r",
            requested,
            resolved,
        )
    return resolved


async def _reject_if_phone_already_active(
    phone: str,
    *,
    organization_id: Optional[str] = None,
) -> None:
    """Block parallel dials that cause busy-pool FAILED + Node redial loops."""
    existing = await get_active_call_for_phone(phone, organization_id=organization_id)
    if not existing:
        return
    existing_id = existing.get("id") or "?"
    existing_status = (existing.get("status") or "active").strip()
    raise HTTPException(
        409,
        f"Phone {phone} already has an active call "
        f"(id={existing_id}, status={existing_status}). "
        "Wait for it to finish before dialing again.",
    )


def _resolve_dispatch_callback_fields(
    *,
    scheduled_at: Optional[str] = None,
    callback_urgency: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Normalize optional callback fields for dispatch / create_active_call."""
    if not (scheduled_at or callback_urgency):
        return {}
    from callback_schedule import resolve_callback_schedule
    return resolve_callback_schedule(
        scheduled_at_raw=scheduled_at,
        callback_urgency=callback_urgency,
    )


def _call_dispatch_dict(
    *,
    call_id: str,
    room_name: str,
    phone: str,
    lead_name: str,
    organization_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    livekit_trunk_id: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    status: str = "dispatched",
    scheduled_at: Optional[str] = None,
    callback_urgency: Optional[str] = None,
) -> dict:
    payload = {
        "id": call_id,
        "status": status,
        "room": room_name,
        "phone": phone,
        "agent_profile_id": agent_profile_id,
        "agent_name": agent_name or _resolve_dispatch_agent_name(),
        "organization_id": organization_id,
        "lead_id": lead_id,
        "lead_name": lead_name,
    }
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    if callback_urgency:
        payload["callback_urgency"] = callback_urgency
    if livekit_trunk_id:
        payload["livekit_trunk_id"] = livekit_trunk_id
    return payload


def _contact_org_lead(contact: dict) -> tuple[Optional[str], Optional[str]]:
    org_id = str(contact.get("organization_id")).strip() if contact.get("organization_id") else None
    lead_id_val = str(contact.get("lead_id")).strip() if contact.get("lead_id") else None
    return org_id or None, lead_id_val or None


def _livekit_trunk_id_from(item: Any) -> Optional[str]:
    """Accept snake_case and camelCase trunk IDs from models or raw contacts."""
    if isinstance(item, dict):
        raw = item.get("livekit_trunk_id") or item.get("livekitTrunkId")
    else:
        raw = getattr(item, "livekit_trunk_id", None)
    value = str(raw).strip() if raw else ""
    return value or None


def _projects_from_contact(contact: dict) -> list[dict]:
    """Normalize single projectDetails or projects[] from a contact dict."""
    normalized = normalize_projects(
        projects=contact.get("projects"),
        project_id=str(contact.get("project_id")).strip() if contact.get("project_id") else None,
        project_details=contact.get("projectDetails") or contact.get("project_details"),
    )
    if not normalized and contact.get("project_id"):
        return [{
            "project_id": str(contact["project_id"]).strip(),
            "project_details": "",
            "additional_instructions": "",
        }]
    return normalized


def _merge_instruction_text(*values: object) -> Optional[str]:
    """Combine instruction layers without repeating identical text."""
    merged = ""
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if not merged:
            merged = text
            continue
        current = merged.casefold()
        candidate = text.casefold()
        if candidate == current or current.startswith(candidate + "\n\n"):
            continue
        if candidate.startswith(current + "\n\n"):
            merged = text
            continue
        merged = f"{merged}\n\n{text}"
    return merged or None


def _campaign_default_projects(req: CampaignRequest) -> list[dict]:
    """Normalize project context supplied once at the campaign root."""
    raw_projects = None
    if req.projects:
        raw_projects = [project.model_dump(by_alias=True, exclude_none=True) for project in req.projects]
    return normalize_projects(
        projects=raw_projects,
        project_id=(req.project_id or "").strip() or None,
        project_details=req.project_details,
    )


def _apply_campaign_context_to_contact(
    contact: dict,
    *,
    campaign_id: Optional[str] = None,
    additional_instructions: Optional[str] = None,
    default_projects: Optional[list[dict]] = None,
) -> dict:
    """Persist campaign context on each contact so dispatch cannot lose it."""
    result = dict(contact)
    if campaign_id:
        result["campaign_id"] = campaign_id

    contact_notes = result.get("additional_instructions") or result.get("additionalInstructions")
    merged_notes = _merge_instruction_text(additional_instructions, contact_notes)
    if merged_notes:
        result["additional_instructions"] = merged_notes
        result.pop("additionalInstructions", None)

    if not _projects_from_contact(result) and default_projects:
        result["projects"] = [dict(project) for project in default_projects]
    return result


def _projects_from_call_request(req: CallRequest) -> list[dict]:
    raw = None
    if req.projects:
        raw = []
        for idx, p in enumerate(req.projects):
            pid = str(
                p.project_id
                or getattr(p, "id", None)
                or getattr(p, "projectId", None)
                or ""
            ).strip()
            if not pid and req.project_id and len(req.projects) == 1:
                pid = req.project_id.strip()
            name = (
                getattr(p, "project_name", None)
                or getattr(p, "projectName", None)
                or getattr(p, "name", None)
            )
            if not pid and name:
                pid = re.sub(r"[^a-zA-Z0-9_-]", "-", str(name).strip().lower()).strip("-")
            if not pid:
                pid = f"proj-{idx + 1}"
            raw.append({
                "project_id": pid,
                "project_details": p.project_details,
                "project_name": name,
                "additional_instructions": (
                    (p.additional_instructions or p.notes or "").strip() or None
                ),
                "notes": (p.notes or p.additional_instructions or "").strip() or None,
            })
    normalized = normalize_projects(
        projects=raw,
        project_id=req.project_id,
        project_details=req.project_details,
    )
    if not normalized and req.project_id:
        return [{
            "project_id": req.project_id.strip(),
            "project_details": "",
            "additional_instructions": "",
        }]
    return normalized


def _apply_projects_metadata(metadata: dict, projects: list[dict]) -> None:
    """Attach project context to LiveKit dispatch metadata."""
    if len(projects) >= 2:
        metadata["projects"] = projects
        if projects[0].get("project_id"):
            metadata["project_id"] = projects[0]["project_id"]
        logger.info("Call dispatch: MULTI-PROJECT mode (%d projects)", len(projects))
    elif len(projects) == 1:
        if projects[0].get("project_id"):
            metadata["project_id"] = projects[0]["project_id"]
        metadata["project_details"] = projects[0]["project_details"]
        # Preserve per-project notes as well as the compatibility text field.
        metadata["projects"] = projects
        logger.info(
            "Call dispatch: single project (%d chars), project_id=%s",
            len(projects[0]["project_details"]),
            projects[0].get("project_id") or "none",
        )
    else:
        logger.warning("Call dispatch has NO project details — agent will use generic script")


async def _prepare_call_work_items(
    contacts: list,
    *,
    room_prefix: str,
    agent_profile_id: Optional[str] = None,
    default_credentials: Optional[dict] = None,
    default_livekit_trunk_id: Optional[str] = None,
    projects_payload_is_fresh: bool = True,
) -> tuple[list, list, list]:
    """Create call_logs rows as ``queued``. Returns (work_items, call_responses, invalid_contacts).

    ``projects_payload_is_fresh=False`` for contacts saved in a campaign row: their
    project details may be stale, so Supabase wins and is never updated from them.
    """
    work_items: list = []
    call_responses: list = []
    invalid: list = []

    for contact in contacts:
        phone = normalize_e164_phone(contact.get("phone") or "")
        if not phone:
            invalid.append({
                "phone": (contact.get("phone") or "").strip() or None,
                "error": "Invalid phone — use 9876543210 or +919876543210",
            })
            continue

        creds = _extract_client_credentials(contact)
        if default_credentials:
            if not creds.get("gemini_api_key") and default_credentials.get("gemini_api_key"):
                creds["gemini_api_key"] = default_credentials["gemini_api_key"]
            for k, v in (default_credentials.get("vobiz") or {}).items():
                creds["vobiz"].setdefault(k, v)
            for k, v in (default_credentials.get("livekit") or {}).items():
                creds["livekit"].setdefault(k, v)

        room_name = f"{room_prefix}-{phone.replace('+', '')}-{random.randint(100, 999)}"
        org_id, lead_id_val = _contact_org_lead(contact)
        livekit_trunk_id = _livekit_trunk_id_from(contact) or (
            (default_livekit_trunk_id or "").strip() or None
        )
        if not livekit_trunk_id:
            nested_trunk = (
                (creds.get("vobiz") or {}).get("trunk_id")
                or (creds.get("vobiz") or {}).get("trunkId")
                or ""
            )
            livekit_trunk_id = str(nested_trunk).strip() or None

        if not org_id:
            invalid.append({
                "phone": phone,
                "error": "Missing required parameter: 'organization_id' is strictly required to initiate a call.",
            })
            continue

        if not lead_id_val:
            invalid.append({
                "phone": phone,
                "error": "Missing required parameter: 'lead_id' is strictly required to initiate a call.",
            })
            continue

        if not (creds.get("gemini_api_key") or "").strip() and _gemini_key_required():
            invalid.append({
                "phone": phone,
                "error": "Client credentials required: 'gemini_api_key' must be provided in client credentials.",
            })
            continue

        if not livekit_trunk_id:
            invalid.append({
                "phone": phone,
                "error": "Missing required parameter: 'livekit_trunk_id' is required to select this organization's SIP trunk.",
            })
            continue

        try:
            await _reject_if_phone_already_active(phone, organization_id=org_id)
        except HTTPException as exc:
            invalid.append({"phone": phone, "error": exc.detail})
            continue

        lead_name = contact.get("lead_name", "there")
        profile_id = contact.get("agent_profile_id") or agent_profile_id
        projects_list = _projects_from_contact(contact)
        if projects_list:
            try:
                projects_list = await resolve_organization_projects(
                    org_id,
                    projects_list,
                    register_missing=True,
                    payload_is_fresh=projects_payload_is_fresh,
                )
            except ProjectScopeError as exc:
                invalid.append({"phone": phone, "error": str(exc)})
                continue
            except Exception as exc:
                logger.exception("Project ownership lookup failed for organization %s", org_id)
                invalid.append({
                    "phone": phone,
                    "error": f"Unable to validate organization projects: {exc}",
                })
                continue
        project_id = projects_list[0]["project_id"] if projects_list else None

        cb = _resolve_dispatch_callback_fields(
            scheduled_at=contact.get("scheduled_at"),
            callback_urgency=contact.get("callback_urgency"),
        )
        try:
            call_id = await create_active_call(
                phone,
                lead_name,
                room_name=room_name,
                organization_id=org_id,
                lead_id=lead_id_val,
                project_id=project_id,
                status="queued",
                scheduled_at=cb.get("scheduled_at"),
                callback_urgency=cb.get("callback_urgency"),
            )
        except ActiveCallConflictError as exc:
            invalid.append({"phone": phone, "error": str(exc)})
            continue
        if projects_list:
            try:
                await bind_call_projects(call_id, org_id, projects_list)
            except Exception as exc:
                await finalize_call(
                    call_id,
                    outcome="failed",
                    reason=f"Could not bind organization projects: {exc}",
                    duration_seconds=0,
                )
                invalid.append({
                    "phone": phone,
                    "error": "Unable to bind verified projects to this call",
                })
                continue
        work_items.append({
            "contact": contact,
            "room_name": room_name,
            "call_id": call_id,
            "phone": phone,
            "lead_name": lead_name,
            "organization_id": org_id,
            "lead_id": lead_id_val,
            "agent_profile_id": profile_id,
            "project_id": project_id,
            "projects": projects_list,
            "client_credentials": creds,
            "livekit_trunk_id": livekit_trunk_id,
        })
        call_responses.append(_call_dispatch_dict(
            call_id=call_id,
            room_name=room_name,
            phone=phone,
            lead_name=lead_name,
            organization_id=org_id,
            lead_id=lead_id_val,
            livekit_trunk_id=livekit_trunk_id,
            agent_profile_id=profile_id,
            status="queued",
            scheduled_at=cb.get("scheduled_at"),
            callback_urgency=cb.get("callback_urgency"),
        ))

    return work_items, call_responses, invalid


async def _dispatch_one(
    lk,
    lk_api,
    work_item: dict,
    prompt: Optional[str],
    profile: Optional[dict] = None,
    additional_instructions: Optional[str] = None,
) -> Optional[dict]:
    contact = work_item["contact"]
    room_name = work_item["room_name"]
    call_id = work_item["call_id"]
    phone = work_item["phone"]
    lead_name = work_item["lead_name"]
    org_id = work_item.get("organization_id")
    lead_id_val = work_item.get("lead_id")
    agent_profile_id = work_item.get("agent_profile_id")
    extra_notes = _merge_instruction_text(
        additional_instructions,
        work_item.get("additional_instructions"),
        contact.get("additional_instructions") or contact.get("additionalInstructions"),
    )

    try:
        await mark_call_ringing(call_id)
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None

        # Client-provided credentials and identity validation
        creds = work_item.get("client_credentials") or _extract_client_credentials(contact)
        org_id, lead_id_val = _validate_call_initiation_requirements(
            organization_id=org_id,
            lead_id=lead_id_val,
            creds=creds,
        )

        if creds.get("livekit"):
            lk, lk_api = await _get_livekit(creds["livekit"])

        vobiz_conf = creds.get("vobiz") or {}
        resolved_trunk_id = await _resolve_sip_trunk(
            lk,
            lk_api,
            vobiz_conf,
            work_item.get("livekit_trunk_id"),
        )
        if not resolved_trunk_id:
            raise ValueError(
                "livekit_trunk_id is required to select this organization's SIP trunk"
            )

        metadata: dict = {
            "phone_number": phone,
            "lead_name": lead_name,
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": saved_prompt,
            "call_id": call_id,
            "gemini_api_key": creds.get("gemini_api_key"),
        }
        lead_source = (
            contact.get("lead_source")
            or contact.get("leadSource")
            or contact.get("enquiry_source")
            or contact.get("enquirySource")
        )
        lead_source_date = (
            contact.get("lead_source_date")
            or contact.get("leadSourceDate")
            or contact.get("enquiry_date")
            or contact.get("enquiryDate")
        )
        if lead_source:
            metadata["lead_source"] = str(lead_source).strip()
        if lead_source_date:
            metadata["lead_source_date"] = str(lead_source_date).strip()
        campaign_id = (
            contact.get("campaign_id")
            or contact.get("campaignId")
            or work_item.get("campaign_id")
        )
        if campaign_id:
            metadata["campaign_id"] = str(campaign_id)
        if resolved_trunk_id:
            metadata["trunk_id"] = resolved_trunk_id
        if extra_notes:
            metadata["additional_instructions"] = extra_notes
        if work_item.get("gemini_key_slot"):
            metadata["gemini_key_slot"] = work_item["gemini_key_slot"]
        from_number = (
            vobiz_conf.get("from_number")
            or contact.get("from_number")
            or contact.get("caller_id")
            or contact.get("sip_number")
            or contact.get("calling_number")
            or work_item.get("from_number")
        )
        if from_number:
            normalized_from = normalize_e164_phone(str(from_number).strip()) or str(from_number).strip()
            metadata["from_number"] = normalized_from
        if vobiz_conf.get("auth_id"):
            metadata["vobiz_auth_id"] = vobiz_conf.get("auth_id")
        if vobiz_conf.get("auth_token"):
            metadata["vobiz_auth_token"] = vobiz_conf.get("auth_token")
        if org_id:
            metadata["organization_id"] = org_id
        if lead_id_val:
            metadata["lead_id"] = lead_id_val
        projects_list = work_item.get("projects") or _projects_from_contact(contact)
        _apply_projects_metadata(metadata, projects_list)
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]
        dispatch_agent = _resolve_dispatch_agent_name(work_item.get("agent_name"))
        await lk.room.create_room(
            lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5)
        )
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name=dispatch_agent, room=room_name,
                metadata=json.dumps({**metadata, "dispatched_at": time.time()}),
            )
        )
        logger.info(
            "LIVEKIT DISPATCH room=%s phone=%s agent_name=%s (queue/parallel)",
            room_name, phone, dispatch_agent,
        )
        cb = _resolve_dispatch_callback_fields(
            scheduled_at=contact.get("scheduled_at"),
            callback_urgency=contact.get("callback_urgency"),
        )
        return _call_dispatch_dict(
            call_id=call_id,
            room_name=room_name,
            phone=phone,
            lead_name=lead_name,
            organization_id=org_id,
            lead_id=lead_id_val,
            livekit_trunk_id=resolved_trunk_id,
            agent_profile_id=agent_profile_id,
            agent_name=dispatch_agent,
            status="dispatched",
            scheduled_at=cb.get("scheduled_at"),
            callback_urgency=cb.get("callback_urgency"),
        )
    except Exception as exc:
        logger.error("Dispatch error for %s: %s", phone, redact_secrets(str(exc)))
        try:
            await finalize_call(
                call_id,
                outcome="failed",
                reason=f"dispatch failed: {redact_secrets(str(exc))}",
                duration_seconds=0,
            )
        except Exception:
            pass
        return None


# Outcomes that free a dial slot so the next queued lead can start immediately.
_TERMINAL_CALL_STATUSES = frozenset({"completed", "failed"})
_TERMINAL_OUTCOMES = frozenset({
    "busy", "no_answer", "failed", "voicemail", "completed",
    "booked", "interested", "not_interested", "callback_requested", "wrong_number",
    "transferred", "do_not_call", "disconnect_in_between",
})


async def _wait_call_terminal(
    call_id: str,
    *,
    poll_sec: float = 2.0,
    max_wait_sec: float = 0.0,
) -> dict:
    """Block until the call leaves ringing/in_progress (busy, answered-and-ended, etc.).

    Does NOT advance the campaign queue while the call is still active — a soft
    timeout only logs; the slot keeps waiting until terminal status or a hard
    ceiling (prevents dialing the next lead while SIP may still be live).
    """
    import time as _time
    # Default: ring timeout + 45 min talk ceiling (override with QUEUE_CALL_MAX_WAIT_SEC).
    if max_wait_sec <= 0:
        ring = max(30, int(config.get_env("RINGING_TIMEOUT_SEC", "30") or 30))
        max_wait_sec = float(config.get_env("QUEUE_CALL_MAX_WAIT_SEC") or (ring + 45 * 60))
    soft_deadline = _time.monotonic() + max(60.0, float(max_wait_sec))
    hard_ceiling = _time.monotonic() + max(float(max_wait_sec), 3 * 3600.0)
    last: dict = {}
    warned_soft = False
    while _time.monotonic() < hard_ceiling:
        try:
            row = await get_call_by_id(call_id)
        except Exception as exc:
            logger.warning("wait_call_terminal read failed for %s: %s", call_id, exc)
            row = None
        if row:
            last = row
            status = (row.get("status") or "").lower()
            outcome = (row.get("outcome") or "").lower()
            if status in _TERMINAL_CALL_STATUSES:
                return row
            if status not in ("ringing", "in_progress", "queued") and outcome in _TERMINAL_OUTCOMES:
                return row
            # Busy / no-answer finalized as completed+outcome — already covered by status check.
            if outcome in ("busy", "no_answer", "voicemail", "failed") and status == "completed":
                return row
            still_active = status in ("ringing", "in_progress", "queued")
            if still_active and _time.monotonic() > soft_deadline:
                if not warned_soft:
                    logger.warning(
                        "wait_call_terminal soft-timeout for %s status=%s — "
                        "holding queue slot until SIP/finalize (not advancing)",
                        call_id,
                        status,
                    )
                    warned_soft = True
                # Keep holding; do not treat as terminal.
        await asyncio.sleep(poll_sec)
    logger.warning(
        "wait_call_terminal hard-ceiling for %s — last status=%s",
        call_id,
        (last.get("status") if last else None),
    )
    return last


async def _wait_caller_id_free(
    *,
    organization_id: Optional[str] = None,
    timeout_sec: float = 20.0,
    poll_sec: float = 0.5,
) -> bool:
    """Wait until at least one pool DID is free before dialing the next FIFO lead."""
    import time as _time
    deadline = _time.monotonic() + max(2.0, float(timeout_sec))
    while _time.monotonic() < deadline:
        try:
            pool = await phone_pool_status(organization_id=organization_id)
            if int(pool.get("free") or 0) > 0:
                return True
        except Exception as exc:
            logger.debug("wait_caller_id_free poll failed: %s", exc)
        await asyncio.sleep(poll_sec)
    logger.warning(
        "No free caller-ID within %.0fs — next lead may fail lease (org=%s)",
        timeout_sec, organization_id or "(shared)",
    )
    return False


async def _cap_parallel_by_key_pool(requested: int, *, allow_over_cap: bool = False) -> int:
    """Limit concurrent dials to Gemini key-pool size (1 key per live call).

    When ``allow_over_cap`` is True (simultaneous Single Call), dispatch all numbers
    at once; agents may share/wait on keys but both SIP legs start immediately.
    Prefer ``_resolve_parallel_slots`` which also clamps to phone-pool DIDs.
    """
    details = await _resolve_parallel_slots(
        requested,
        allow_over_key_cap=allow_over_cap,
        apply_phone_cap=False,
    )
    return int(details["effective"])


async def _resolve_parallel_slots(
    requested: int,
    *,
    organization_id: Optional[str] = None,
    allow_over_key_cap: bool = False,
    apply_phone_cap: bool = True,
) -> dict:
    """Compute effective parallel dial slots.

    Hard limits (in order):
      1. User request (clamped 1–20)
      2. Gemini key-pool size (unless allow_over_key_cap)
      3. Phone pool capacity (free+busy DIDs eligible for this org) — **each live
         call needs one caller-ID number**, so 2 DIDs ⇒ max 2 parallel calls
    """
    requested = max(1, min(20, int(requested or 1)))
    details: dict = {
        "requested": requested,
        "gemini_key_cap": None,
        "phone_pool_cap": None,
        "organization_id": (organization_id or "").strip() or None,
        "effective": requested,
        "clamped_by": None,
    }
    effective = requested

    try:
        status = await key_pool_public_status()
        total = int(status.get("total_keys") or 0)
        if total > 0:
            details["gemini_key_cap"] = total
            if not allow_over_key_cap and effective > total:
                effective = total
                details["clamped_by"] = "gemini_keys"
    except Exception as exc:
        logger.warning("Could not read key pool for parallel cap: %s", exc)

    if apply_phone_cap:
        try:
            capacity = await count_pool_capacity(organization_id=organization_id)
            details["phone_pool_cap"] = int(capacity)
            if capacity > 0 and effective > capacity:
                effective = capacity
                details["clamped_by"] = "phone_pool"
            elif capacity == 0:
                logger.warning(
                    "Phone pool capacity is 0 for org=%s — parallel dials will fail until "
                    "POST /api/numbers/sync adds free DIDs",
                    organization_id or "(shared)",
                )
        except Exception as exc:
            logger.debug("Pool capacity check skipped: %s", exc)

    details["effective"] = max(1, int(effective))
    return details


def _org_id_from_contacts(contacts: list) -> Optional[str]:
    """If every contact shares one organization_id, use it for pool capacity."""
    orgs = set()
    for c in contacts or []:
        if not isinstance(c, dict):
            continue
        org = (c.get("organization_id") or "").strip()
        if org:
            orgs.add(org)
    if len(orgs) == 1:
        return next(iter(orgs))
    return None


async def _run_parallel_dials(
    work_items: list,
    *,
    call_delay_seconds: int = 0,
    max_parallel_calls: int = 5,
    system_prompt: Optional[str] = None,
    additional_instructions: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
    queue_advance: bool = True,
    organization_id: Optional[str] = None,
    allow_over_key_cap: bool = False,
) -> tuple[int, int, list]:
    """Dial with N concurrent slots (FIFO queue).

    queue_advance=True (default / campaigns):
      N = min(requested, phone DIDs, Gemini keys). First N leads dial; remaining
      wait FIFO. When a call ends, that slot takes the next lead after a caller-ID
      is free again.

    queue_advance=False:
      Legacy wave dispatch — fire N at a time, then next wave after delay
      (does not wait for busy/end before starting more dials).
    """
    org = (organization_id or "").strip() or _org_id_from_contacts(work_items)
    slot_info = await _resolve_parallel_slots(
        max_parallel_calls,
        organization_id=org,
        allow_over_key_cap=allow_over_key_cap,
        apply_phone_cap=True,
    )
    max_parallel = int(slot_info["effective"])
    phone_cap = slot_info.get("phone_pool_cap")
    if phone_cap == 0:
        logger.error(
            "Parallel dials aborted — phone pool has 0 caller-ID numbers (org=%s)",
            org or "(shared)",
        )
        for item in work_items:
            try:
                await finalize_call(
                    item["call_id"],
                    outcome="failed",
                    reason="No outbound caller-ID numbers in pool — sync from Vobiz",
                    duration_seconds=0,
                )
            except Exception:
                pass
        return 0, len(work_items), []

    logger.info(
        "FIFO dial plan: leads=%d parallel_slots=%d (requested=%s phone_pool=%s "
        "gemini_keys=%s clamped_by=%s queue_advance=%s)",
        len(work_items),
        max_parallel,
        slot_info.get("requested"),
        phone_cap,
        slot_info.get("gemini_key_cap"),
        slot_info.get("clamped_by"),
        queue_advance,
    )
    delay = max(0, int(call_delay_seconds or 0))

    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    if not work_items:
        return 0, 0, []

    # Assign unique Gemini key slots across this wave so LiveKit job subprocesses
    # do not collide on the same API key (in-memory pools cannot coordinate).
    slots = list_key_slots()
    if slots:
        for i, item in enumerate(work_items):
            item["gemini_key_slot"] = slots[i % len(slots)]

    try:
        lk, lk_api_module = await _get_livekit()
    except HTTPException:
        logger.error("Parallel dial: LiveKit not configured")
        for item in work_items:
            try:
                await finalize_call(
                    item["call_id"],
                    outcome="failed",
                    reason="LiveKit not configured",
                    duration_seconds=0,
                )
            except Exception:
                pass
        return 0, len(work_items), []

    # ── Legacy wave mode (no wait-for-busy) ─────────────────────────────────
    if not queue_advance:
        ok_count = 0
        fail_count = 0
        dispatched: list = []
        try:
            for i in range(0, len(work_items), max_parallel):
                wave = work_items[i:i + max_parallel]
                results = await asyncio.gather(
                    *[_dispatch_one(lk, lk_api_module, item, system_prompt, profile, additional_instructions) for item in wave],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, dict):
                        ok_count += 1
                        dispatched.append(r)
                    else:
                        fail_count += 1
                if i + max_parallel < len(work_items) and delay > 0:
                    await asyncio.sleep(delay)
        except Exception as exc:
            logger.error("Wave dial error: %s", redact_secrets(str(exc)))
            fail_count += len(work_items) - ok_count
        return ok_count, fail_count, dispatched

    # ── FIFO queue: N slots; free DID → next lead ───────────────────────────
    queue: asyncio.Queue = asyncio.Queue()
    for item in work_items:
        queue.put_nowait(item)

    ok_count = 0
    fail_count = 0
    dispatched = []
    stats_lock = asyncio.Lock()

    async def _slot_worker(slot_id: int) -> None:
        nonlocal ok_count, fail_count
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            phone = item.get("phone")
            call_id = item.get("call_id")
            remaining = queue.qsize()
            logger.info(
                "FIFO_SLOT_%s start lead phone=%s call_id=%s queued_remaining=%s",
                slot_id, phone, call_id, remaining,
            )
            try:
                result = await _dispatch_one(lk, lk_api_module, item, system_prompt, profile, additional_instructions)
            except Exception as exc:
                logger.error("FIFO_SLOT_%s dispatch error: %s", slot_id, redact_secrets(str(exc)))
                result = None

            if isinstance(result, dict):
                async with stats_lock:
                    ok_count += 1
                    dispatched.append(result)
                ended = await _wait_call_terminal(call_id)
                ended_status = ((ended or {}).get("status") or "").lower()
                if ended_status in ("ringing", "in_progress", "queued"):
                    logger.warning(
                        "FIFO_SLOT_%s call still active after wait phone=%s status=%s — "
                        "not advancing to next lead",
                        slot_id,
                        phone,
                        ended_status,
                    )
                    # Hold this slot forever for this lead rather than overlapping dials.
                    return
                outcome = (ended.get("outcome") if ended else None) or "?"
                logger.info(
                    "FIFO_SLOT_%s call ended outcome=%s phone=%s — waiting for free DID",
                    slot_id, outcome, phone,
                )
                # Caller-ID must return to pool before the next lead on this slot.
                await _wait_caller_id_free(organization_id=org, timeout_sec=20.0)
            else:
                async with stats_lock:
                    fail_count += 1
                logger.info(
                    "FIFO_SLOT_%s dispatch failed phone=%s → next lead",
                    slot_id, phone,
                )

            if delay > 0 and not queue.empty():
                await asyncio.sleep(delay)

            if not queue.empty():
                logger.info(
                    "FIFO_SLOT_%s assigning next queued lead (remaining≈%s)",
                    slot_id, queue.qsize(),
                )

    workers = [asyncio.create_task(_slot_worker(i + 1)) for i in range(max_parallel)]
    try:
        await asyncio.gather(*workers)
    except Exception as exc:
        logger.error("FIFO dial error: %s", redact_secrets(str(exc)))
        fail_count += max(0, len(work_items) - ok_count - fail_count)

    return ok_count, fail_count, dispatched


@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    """Dispatch one or many outbound calls."""
    logger.info(
        "RECEIVED /api/call -> phone=%s lead=%s has_proj=%s proj_id=%s",
        req.phone, req.lead_name, bool(req.project_details), req.project_id
    )
    try:
        raw_payload = req.model_dump(by_alias=True, exclude_none=False)
        safe_payload = redact_secrets(_json_safe_value(raw_payload))
        notes = (req.additional_instructions or "").strip()
        payload_text = json.dumps(safe_payload, ensure_ascii=False, default=str, indent=2)
        print(
            f"\n========== POST /api/call PAYLOAD ==========\n"
            f"notes_len={len(notes)} notes_preview={notes[:200]!r}\n"
            f"{payload_text}\n"
            f"============================================\n",
            flush=True,
        )
        logger.info(
            "POST /api/call payload (secrets redacted) notes_len=%s\n%s",
            len(notes),
            payload_text,
        )
        await log_error(
            "api/call",
            f"POST /api/call payload phone={req.phone or req.phone_number} "
            f"lead={req.lead_name} notes_len={len(notes)} "
            f"has_contacts={bool(req.contacts)} has_projects={bool(req.projects)} "
            f"project_id={req.project_id}",
            detail=json.dumps(safe_payload, ensure_ascii=False, default=str)[:8000],
            level="info",
        )
    except Exception as payload_log_exc:
        logger.warning("Failed to log /api/call payload: %s", payload_log_exc)

    projects_payload = (
        [
            {
                "project_id": proj.project_id,
                "project_details": proj.project_details,
                "additional_instructions": (
                    (proj.additional_instructions or proj.notes or "").strip() or None
                ),
                "notes": (proj.notes or proj.additional_instructions or "").strip() or None,
            }
            for proj in req.projects
        ]
        if req.projects else None
    )

    req_client_creds = _extract_client_credentials(req)
    # contacts[] — each entry keeps its own lead_name (Single Call UI).
    if req.contacts:
        contacts = [
            {
                "phone": (c.phone or "").strip(),
                "lead_name": (c.lead_name or req.lead_name or "there").strip() or "there",
                "business_name": req.business_name,
                "service_type": req.service_type,
                "organization_id": (c.organization_id or req.organization_id or "").strip() or None,
                "lead_id": (c.lead_id or req.lead_id or "").strip() or None,
                "livekit_trunk_id": c.livekit_trunk_id or req.livekit_trunk_id,
                "from_number": (
                    c.from_number or c.caller_id or c.sip_number or c.calling_number
                    or req.resolved_from_number()
                ),
                "agent_profile_id": req.agent_profile_id,
                "agent_name": req.agent_name,
                "campaign_id": req.campaign_id,
                "project_id": req.project_id,
                "projectDetails": req.project_details,
                "projects": projects_payload,
                "scheduled_at": (c.scheduled_at or req.scheduled_at or "").strip() or None,
                "callback_urgency": (c.callback_urgency or req.callback_urgency or "").strip() or None,
                "lead_source": (c.lead_source or req.lead_source or "").strip() or None,
                "lead_source_date": (
                    c.lead_source_date or req.lead_source_date or ""
                ).strip() or None,
                "credentials": getattr(c, "credentials", None) or req.credentials,
                "gemini_api_key": getattr(c, "gemini_api_key", None) or req.gemini_api_key,
                "vobiz_config": getattr(c, "vobiz_config", None) or req.vobiz_config,
                "livekit_config": getattr(c, "livekit_config", None) or req.livekit_config,
            }
            for c in req.contacts
            if (c.phone or "").strip()
        ]
        work_items, call_responses, prep_invalid = await _prepare_call_work_items(
            contacts,
            room_prefix="call",
            agent_profile_id=req.agent_profile_id,
            default_credentials=req_client_creds,
            default_livekit_trunk_id=req.livekit_trunk_id,
        )
        invalid = list(prep_invalid)
        if not work_items:
            raise HTTPException(400, "No valid phone numbers — use 9876543210 or +919876543210")

        if len(work_items) == 1:
            item = work_items[0]
            for wi in work_items:
                wi["agent_name"] = req.agent_name
                wi["contact"]["agent_name"] = req.agent_name
            creds_to_use = item.get("client_credentials") or req_client_creds
            _validate_client_credentials(creds_to_use)
            lk, lk_api = await _get_livekit(creds_to_use.get("livekit"))
            profile = None
            if req.agent_profile_id:
                profile = await get_agent_profile(req.agent_profile_id)
            dispatched = await _dispatch_one(
                lk,
                lk_api,
                item,
                (req.system_prompt or "").strip() or None,
                profile=profile,
                additional_instructions=(req.additional_instructions or "").strip() or None,
            )
            if not dispatched:
                raise HTTPException(500, "Call dispatch failed")
            return dispatched

        for item in work_items:
            item["agent_name"] = req.agent_name
            item["contact"]["agent_name"] = req.agent_name

        slot_info = await _resolve_parallel_slots(
            len(work_items) if req.simultaneous else 1,
            organization_id=_org_id_from_contacts(work_items) or (
                (req.organization_id or "").strip() or None
            ),
            allow_over_key_cap=bool(req.simultaneous),
            apply_phone_cap=True,
        )
        max_p = int(slot_info["effective"])
        delay = 0 if req.simultaneous else 3
        if req.simultaneous and len(work_items) > max_p:
            why = slot_info.get("clamped_by") or "capacity"
            logger.warning(
                "Simultaneous dial: %d numbers but only %d parallel slot(s) "
                "(clamped_by=%s gemini=%s phone_pool=%s)",
                len(work_items), max_p, why,
                slot_info.get("gemini_key_cap"), slot_info.get("phone_pool_cap"),
            )
        ok, fail, dispatched = await _run_parallel_dials(
            work_items,
            call_delay_seconds=delay,
            max_parallel_calls=max_p,
            system_prompt=(req.system_prompt or "").strip() or None,
            additional_instructions=(req.additional_instructions or "").strip() or None,
            agent_profile_id=req.agent_profile_id,
            queue_advance=not req.simultaneous,
            organization_id=slot_info.get("organization_id"),
            allow_over_key_cap=bool(req.simultaneous),
        )
        pool = await key_pool_public_status()
        return {
            "status": "dispatched" if ok else "failed",
            "simultaneous": bool(req.simultaneous),
            "total": len(work_items),
            "ok": ok,
            "fail": fail,
            "max_parallel_calls": max_p,
            "parallel_slots": slot_info,
            "gemini_key_pool": pool,
            "calls": dispatched or call_responses,
            "invalid": invalid,
        }

    raw_numbers: list[str] = []
    if req.phones:
        raw_numbers.extend(req.phones)
    if req.phone and (req.phone or "").strip():
        raw_numbers.insert(0, req.phone)

    # Deduplicate while preserving order
    seen: set[str] = set()
    phones: list[str] = []
    invalid: list[dict] = []
    for raw in raw_numbers:
        normalized = normalize_e164_phone((raw or "").strip())
        if not normalized:
            invalid.append({
                "phone": (raw or "").strip() or None,
                "error": "Invalid phone — use 9876543210 or +919876543210",
            })
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        phones.append(normalized)

    if not phones:
        raise HTTPException(400, "No valid phone numbers — use 9876543210 or +919876543210")

    # Multi-phone path: prepare work items and dial in parallel.
    if len(phones) > 1:
        contacts = [
            {
                "phone": phone_num,
                "lead_name": req.lead_name,
                "business_name": req.business_name,
                "service_type": req.service_type,
            "organization_id": req.organization_id,
            "lead_id": req.lead_id,
            "livekit_trunk_id": req.livekit_trunk_id,
                "from_number": req.resolved_from_number(),
                "agent_profile_id": req.agent_profile_id,
                "agent_name": req.agent_name,
                "campaign_id": req.campaign_id,
                "project_id": req.project_id,
                "projectDetails": req.project_details,
                "projects": projects_payload,
                "scheduled_at": req.scheduled_at,
                "callback_urgency": req.callback_urgency,
                "lead_source": req.lead_source,
                "lead_source_date": req.lead_source_date,
                "credentials": req.credentials,
                "gemini_api_key": req.gemini_api_key,
                "vobiz_config": req.vobiz_config,
                "livekit_config": req.livekit_config,
            }
            for phone_num in phones
        ]
        work_items, call_responses, prep_invalid = await _prepare_call_work_items(
            contacts,
            room_prefix="call",
            agent_profile_id=req.agent_profile_id,
            default_credentials=req_client_creds,
            default_livekit_trunk_id=req.livekit_trunk_id,
        )
        for item in work_items:
            item["agent_name"] = req.agent_name
            item["contact"]["agent_name"] = req.agent_name

        invalid.extend(prep_invalid)
        if not work_items:
            raise HTTPException(400, "No valid phone numbers after normalization")

        slot_info = await _resolve_parallel_slots(
            len(work_items) if req.simultaneous else 1,
            organization_id=_org_id_from_contacts(work_items) or (
                (req.organization_id or "").strip() or None
            ),
            allow_over_key_cap=bool(req.simultaneous),
            apply_phone_cap=True,
        )
        max_p = int(slot_info["effective"])
        delay = 0 if req.simultaneous else 3
        if req.simultaneous and len(work_items) > max_p:
            why = slot_info.get("clamped_by") or "capacity"
            logger.warning(
                "Simultaneous dial: %d numbers but only %d parallel slot(s) "
                "(clamped_by=%s gemini=%s phone_pool=%s)",
                len(work_items), max_p, why,
                slot_info.get("gemini_key_cap"), slot_info.get("phone_pool_cap"),
            )
        ok, fail, dispatched = await _run_parallel_dials(
            work_items,
            call_delay_seconds=delay,
            max_parallel_calls=max_p,
            system_prompt=(req.system_prompt or "").strip() or None,
            additional_instructions=(req.additional_instructions or "").strip() or None,
            agent_profile_id=req.agent_profile_id,
            queue_advance=not req.simultaneous,
            organization_id=slot_info.get("organization_id"),
            allow_over_key_cap=bool(req.simultaneous),
        )
        pool = await key_pool_public_status()
        return {
            "status": "dispatched" if ok else "failed",
            "simultaneous": bool(req.simultaneous),
            "total": len(work_items),
            "ok": ok,
            "fail": fail,
            "max_parallel_calls": max_p,
            "parallel_slots": slot_info,
            "gemini_key_pool": pool,
            "calls": dispatched or call_responses,
            "invalid": invalid,
        }

    # Single-number path (legacy response shape).
    phone = phones[0]
    org_id, lead_id_val = _validate_call_initiation_requirements(
        organization_id=req.organization_id,
        lead_id=req.lead_id,
        creds=req_client_creds,
    )
    await _reject_if_phone_already_active(phone, organization_id=org_id)
    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    projects_list = _projects_from_call_request(req)
    if projects_list:
        try:
            projects_list = await resolve_organization_projects(
                org_id,
                projects_list,
                register_missing=True,
            )
        except ProjectScopeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            logger.exception("Project ownership lookup failed for organization %s", org_id)
            raise HTTPException(503, "Unable to validate organization projects") from exc
    project_id = projects_list[0]["project_id"] if projects_list else (req.project_id.strip() if req.project_id else None)
    cb = _resolve_dispatch_callback_fields(
        scheduled_at=req.scheduled_at,
        callback_urgency=req.callback_urgency,
    )

    effective_prompt = (req.system_prompt or "").strip() or None
    effective_voice = None
    effective_model = None
    effective_tools = None
    profile = None

    if req.agent_profile_id:
        try:
            profile, call_id = await asyncio.gather(
                get_agent_profile(req.agent_profile_id),
                create_active_call(
                    phone, req.lead_name, room_name=room_name,
                    organization_id=org_id, lead_id=lead_id_val, project_id=project_id,
                    scheduled_at=cb.get("scheduled_at"),
                    callback_urgency=cb.get("callback_urgency"),
                ),
            )
        except ActiveCallConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")
    else:
        try:
            call_id = await create_active_call(
                phone, req.lead_name, room_name=room_name,
                organization_id=org_id, lead_id=lead_id_val, project_id=project_id,
                scheduled_at=cb.get("scheduled_at"),
                callback_urgency=cb.get("callback_urgency"),
            )
        except ActiveCallConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    if projects_list:
        try:
            await bind_call_projects(call_id, org_id, projects_list)
        except Exception as exc:
            await finalize_call(
                call_id,
                outcome="failed",
                reason=f"Could not bind organization projects: {exc}",
                duration_seconds=0,
            )
            raise HTTPException(503, "Unable to bind verified projects to this call") from exc

    if not effective_prompt:
        effective_prompt = (await get_setting("system_prompt", "") or "").strip() or None

    lk, lk_api = await _get_livekit(req_client_creds.get("livekit"))
    vobiz_conf = req_client_creds.get("vobiz") or {}
    resolved_trunk_id = await _resolve_sip_trunk(
        lk,
        lk_api,
        vobiz_conf,
        req.livekit_trunk_id,
    )
    if not resolved_trunk_id:
        raise HTTPException(
            400,
            "livekit_trunk_id is required to select this organization's SIP trunk",
        )

    metadata: dict = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "system_prompt": effective_prompt,
        "call_id": call_id,
        "gemini_api_key": req_client_creds.get("gemini_api_key"),
    }
    if req.lead_source:
        metadata["lead_source"] = req.lead_source.strip()
    if req.lead_source_date:
        metadata["lead_source_date"] = req.lead_source_date.strip()
    if req.campaign_id:
        metadata["campaign_id"] = req.campaign_id
    if resolved_trunk_id:
        metadata["trunk_id"] = resolved_trunk_id
    extra_notes = (req.additional_instructions or "").strip() or None
    if extra_notes:
        metadata["additional_instructions"] = extra_notes
    preferred_from = (
        vobiz_conf.get("from_number")
        or req.resolved_from_number()
    )
    if preferred_from:
        metadata["from_number"] = normalize_e164_phone(preferred_from) or preferred_from
    if vobiz_conf.get("auth_id"):
        metadata["vobiz_auth_id"] = vobiz_conf.get("auth_id")
    if vobiz_conf.get("auth_token"):
        metadata["vobiz_auth_token"] = vobiz_conf.get("auth_token")
    if org_id:
        metadata["organization_id"] = org_id
    if lead_id_val:
        metadata["lead_id"] = lead_id_val
    _apply_projects_metadata(metadata, projects_list)
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools

    try:
        dispatch_agent = _resolve_dispatch_agent_name(req.agent_name)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name=dispatch_agent, room=room_name,
                metadata=json.dumps({**metadata, "dispatched_at": time.time()}),
            )
        )
        logger.info(
            "LIVEKIT DISPATCH room=%s phone=%s agent_name=%s",
            room_name, phone, dispatch_agent,
        )
        asyncio.create_task(log_error(
            "server",
            f"Call dispatched to {phone}",
            f"room={room_name} agent_name={dispatch_agent}",
            "info",
        ))
        return _call_dispatch_dict(
            call_id=call_id,
            room_name=room_name,
            phone=phone,
            lead_name=req.lead_name,
            organization_id=req.organization_id,
            lead_id=req.lead_id,
            livekit_trunk_id=resolved_trunk_id,
            agent_profile_id=req.agent_profile_id,
            agent_name=dispatch_agent,
            status="dispatched",
            scheduled_at=cb.get("scheduled_at"),
            callback_urgency=cb.get("callback_urgency"),
        )
    except Exception as exc:
        logger.error("Dispatch error: %s", redact_secrets(str(exc)))
        try:
            await finalize_call(
                call_id,
                outcome="failed",
                reason=f"dispatch failed: {redact_secrets(str(exc))}",
                duration_seconds=0,
            )
        except Exception:
            pass
        raise HTTPException(500, f"Dispatch failed: {redact_secrets(str(exc))}")


@app.post("/api/batch-call")
async def api_batch_call(req: BatchCallRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    batch_creds = _extract_client_credentials(req)
    work_items, calls, invalid = await _prepare_call_work_items(
        req.contacts,
        room_prefix="batch",
        agent_profile_id=req.agent_profile_id,
        default_credentials=batch_creds,
        default_livekit_trunk_id=req.livekit_trunk_id,
    )
    if not work_items and invalid:
        raise HTTPException(400, "No valid contacts — check phone numbers (9876543210 or +919876543210)")

    org = _org_id_from_contacts(work_items) or _org_id_from_contacts(req.contacts)
    slot_info = await _resolve_parallel_slots(
        req.max_parallel_calls,
        organization_id=org,
        apply_phone_cap=True,
    )
    max_p = int(slot_info["effective"])
    if slot_info.get("phone_pool_cap") == 0:
        raise HTTPException(
            400,
            "No outbound caller-ID numbers in the pool. Sync under Numbers tab "
            "(POST /api/numbers/sync) before starting a batch.",
        )

    async def _job() -> None:
        ok, fail, _ = await _run_parallel_dials(
            work_items,
            call_delay_seconds=req.call_delay_seconds,
            max_parallel_calls=max_p,
            system_prompt=req.system_prompt,
            additional_instructions=(req.additional_instructions or "").strip() or None,
            agent_profile_id=req.agent_profile_id,
            queue_advance=bool(req.queue_advance),
            organization_id=org,
        )
        await log_error(
            "server", "Batch call finished",
            f"ok={ok} fail={fail} total={len(work_items)} invalid={len(invalid)} "
            f"slots={max_p} clamped_by={slot_info.get('clamped_by')}",
            "info",
        )

    asyncio.create_task(_job())
    pool = await key_pool_public_status()
    return {
        "status": "dispatching",
        "total": len(calls),
        "max_parallel_calls": max_p,
        "requested_parallel_calls": slot_info.get("requested"),
        "parallel_slots": slot_info,
        "queue_advance": bool(req.queue_advance),
        "simultaneous_slots": max_p,
        "gemini_key_pool": pool,
        "calls": calls,
        "invalid": invalid,
        "note": (
            f"Running {max_p} parallel slot(s)"
            + (
                f" (clamped by {slot_info['clamped_by']}; "
                f"phone pool={slot_info.get('phone_pool_cap')}, "
                f"gemini keys={slot_info.get('gemini_key_cap')})"
                if slot_info.get("clamped_by")
                else ""
            )
        ),
    }


@app.get("/api/gemini/key-pool")
async def api_gemini_key_pool():
    """Public key-pool capacity (slot ids only — never raw API keys)."""
    return await key_pool_public_status()


@app.get("/api/gemini/tokens")
async def api_gemini_tokens(organization_id: Optional[str] = Query(None)):
    """Aggregate estimated Gemini token usage for Call Logs KPIs."""
    summary = await get_gemini_token_summary(organization_id=organization_id)
    pool = await key_pool_public_status()
    return {**summary, "gemini_key_pool": pool}


# ── Calls ─────────────────────────────────────────────────────────────────────

def _enrich_call_base(call: dict) -> dict:
    url = call.get("recording_url")
    if url and recording_blob_exists(url):
        call["recording_play_url"] = signed_recording_url(url)
    else:
        call["recording_play_url"] = None
    call["property_data"] = parse_property_data(call.get("property_data"))
    cb = callback_fields_from_record(call)
    props = call["property_data"]
    if cb.get("scheduled_at"):
        call["scheduled_at"] = cb["scheduled_at"]
        props["scheduled_at"] = cb["scheduled_at"]
    if cb.get("callback_urgency"):
        call["callback_urgency"] = cb["callback_urgency"]
        props["callback_urgency"] = cb["callback_urgency"]
    call["property_data"] = props
    call["sentiment"] = normalize_sentiment(call.get("sentiment"), call.get("outcome"))
    score_info = compute_ai_lead_score(call)
    call["ai_score"] = score_info["ai_score"]
    call["lead_score"] = score_info["lead_score"]
    call["ai_score_label"] = score_info["ai_score_label"]
    attach_tokens_per_minute(call)
    return call


async def _enrich_call(call: dict) -> dict:
    _enrich_call_base(call)
    await backfill_call_gemini_tokens(call, persist=True)
    await enrich_call_billing_async(call)
    call.pop("_vobiz_fetch", None)
    return call


async def _enrich_calls(calls: list) -> list:
    for call in calls:
        _enrich_call_base(call)
    # Fill missing Tokens / Tok/min from stored transcripts (and persist).
    await asyncio.gather(
        *[backfill_call_gemini_tokens(c, persist=True) for c in calls],
        return_exceptions=True,
    )
    await enrich_calls_billing(calls)
    for call in calls:
        call.pop("_vobiz_fetch", None)
    return calls


@app.get("/api/calls")
async def api_get_calls(
    page: int = 1,
    limit: int = 20,
    organization_id: Optional[str] = Query(None, description="Filter by organization ID"),
    lead_id: Optional[str] = Query(None, description="Filter by lead ID"),
):
    org_id = organization_id.strip() if organization_id else None
    lead_id_val = lead_id.strip() if lead_id else None
    calls = await get_all_calls(
        page=page,
        limit=limit,
        organization_id=org_id or None,
        lead_id=lead_id_val or None,
    )
    return await _enrich_calls(calls)


@app.get("/api/billing/rate")
async def api_billing_rate():
    """Vobiz rate metadata (env fallback + optional live account hint)."""
    return await billing_rate_payload_live()


@app.get("/api/billing")
async def api_billing_summary(
    organization_id: Optional[str] = Query(None, description="Required for org-scoped billing"),
    lead_id: Optional[str] = Query(None, description="Optional lead filter"),
):
    """Org billing using live Vobiz CDR costs when Auth ID/Token are configured."""
    org_id = organization_id.strip() if organization_id else None
    lead_id_val = lead_id.strip() if lead_id else None
    rows = await get_org_call_usage(organization_id=org_id, lead_id=lead_id_val)
    # Match every usage row to live Vobiz CDR total_cost (no fixed dashboard rate).
    await enrich_calls_billing(rows)
    return summarize_usage(rows, organization_id=org_id)


@app.get("/api/calls/active")
async def api_get_active_calls(
    organization_id: Optional[str] = Query(None, description="Optional filter by organization ID"),
    lead_id: Optional[str] = Query(None, description="Optional filter by lead ID"),
):
    org_id = organization_id.strip() if organization_id else None
    lead_id_val = lead_id.strip() if lead_id else None
    calls = await get_active_calls(organization_id=org_id, lead_id=lead_id_val)
    return await _enrich_calls(calls)


@app.get("/api/calls/{call_id}")
async def api_get_call(call_id: str):
    call = await get_call_by_id(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    return await _enrich_call(call)


@app.get("/api/calls/{call_id}/transcript")
async def api_get_transcript(call_id: str):
    call = await get_call_by_id(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    _enrich_call_base(call)
    payload = {
        "call_id": call_id,
        "status": call.get("status", "completed"),
        "outcome": call.get("outcome"),
        "outcome_reason": call.get("reason"),
        "live_transcript": call.get("live_transcript") or "",
        "final_transcript": call.get("final_transcript") or "",
        "transcript_json": call.get("transcript_json") or "[]",
        "property_data": call.get("property_data") or {},
        "sentiment": call.get("sentiment"),
        "ai_score": call.get("ai_score"),
        "lead_score": call.get("lead_score"),
        "ai_score_label": call.get("ai_score_label"),
    }
    if call.get("scheduled_at"):
        payload["scheduled_at"] = call["scheduled_at"]
    if call.get("callback_urgency"):
        payload["callback_urgency"] = call["callback_urgency"]
    return payload


@app.get("/api/calls/{call_id}/recording")
async def api_get_recording(call_id: str):
    call = await get_call_by_id(call_id)
    if not call or not call.get("recording_url"):
        raise HTTPException(404, "Recording not found")
    if not recording_blob_exists(call["recording_url"]):
        raise HTTPException(
            404,
            "Recording file not uploaded — LiveKit egress may have failed "
            "(e.g. egress minutes quota exceeded). Call data is still saved.",
        )
    play_url = signed_recording_url(call["recording_url"])
    if not play_url:
        raise HTTPException(404, "Recording not available")
    return RedirectResponse(play_url)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Prompt ────────────────────────────────────────────────────────────────────

def _prompt_locked_by_env() -> bool:
    return bool((config.get_env("SYSTEM_PROMPT") or "").strip())


@app.get("/api/prompt")
async def api_get_prompt():
    saved = (await get_setting("system_prompt", "") or "").strip()
    locked = _prompt_locked_by_env()
    return {
        "prompt": saved or DEFAULT_SYSTEM_PROMPT,
        "is_custom": bool(saved) and not locked,
        "locked_by_env": locked,
        "source": "env" if locked else ("dashboard" if saved else "code_default"),
    }


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    if _prompt_locked_by_env():
        raise HTTPException(
            403,
            "SYSTEM_PROMPT is set in Azure env — clear that variable to edit from the dashboard.",
        )
    text = (req.prompt or "").strip()
    if not text:
        raise HTTPException(400, "Prompt cannot be empty")
    await set_setting("system_prompt", text)
    return {"status": "saved", "is_custom": True, "source": "dashboard"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    if _prompt_locked_by_env():
        raise HTTPException(
            403,
            "SYSTEM_PROMPT is set in Azure env — clear that variable to reset from the dashboard.",
        )
    await set_setting("system_prompt", "")
    return {
        "status": "reset",
        "prompt": DEFAULT_SYSTEM_PROMPT,
        "is_custom": False,
        "source": "code_default",
    }


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    try:
        await save_settings(filtered)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    return {"status": "saved", "count": len(filtered), "env_only": config.is_env_only_mode()}


# ── Voice (cascade stack: Cartesia Sonic) ─────────────────────────────────────

class VoiceRequest(BaseModel):
    provider: str = "cartesia"
    voice_id: str = Field(min_length=1, max_length=64)
    speed: float
    volume: float
    model: str = "sonic-3.6"


class VoicePreviewRequest(VoiceRequest):
    text: str = Field(min_length=1, max_length=400)
    language: str = "hi-IN"


_PREVIEW_LANGUAGES = ("hi-IN", "en-IN")


def _voice_fields(req: VoiceRequest) -> dict:
    return req.model_dump(exclude_none=True, exclude={"text", "language"})


@app.get("/api/voice")
async def api_get_voice():
    voice, source = voice_settings.load()
    return {
        "voice": voice,
        "label": voice_settings.describe(voice),
        "source": source,  # dashboard | env | default
        "cascade_enabled": (config.get_env("VOICE_STACK") or "").strip().lower() in {"cascade", "mercury", "sarvam"},
        "cartesia_configured": bool((config.get_env("CARTESIA_API_KEY") or "").strip()),
        "options": {
            "languages": list(_PREVIEW_LANGUAGES),
            "cartesia_voices": [{"id": vid, "name": name, "language": lang}
                                for vid, name, lang in voice_settings.CARTESIA_VOICES],
            "cartesia_models": list(voice_settings.CARTESIA_MODELS),
            "speed": list(voice_settings.SPEED_RANGE),
            "volume": list(voice_settings.VOLUME_RANGE),
            "defaults": voice_settings.CARTESIA_DEFAULTS,
        },
    }


@app.post("/api/voice")
async def api_save_voice(req: VoiceRequest):
    try:
        saved = voice_settings.save(_voice_fields(req))
    except voice_settings.VoiceSettingsError as exc:
        raise HTTPException(422, str(exc)) from None
    await log_error("voice", f"VOICE_UPDATED {voice_settings.describe(saved)}", "", "info")
    return {"status": "saved", "voice": saved, "applies": "from the next call"}


@app.delete("/api/voice")
async def api_reset_voice():
    voice_settings.reset()
    voice, source = voice_settings.load()
    return {"status": "reset", "voice": voice, "source": source}


@app.post("/api/voice/preview")
async def api_preview_voice(req: VoicePreviewRequest):
    """Speak a sample with exactly the call's TTS settings; returns WAV audio."""
    try:
        voice = voice_settings.validate(_voice_fields(req))
    except voice_settings.VoiceSettingsError as exc:
        raise HTTPException(422, str(exc)) from None
    if req.language not in _PREVIEW_LANGUAGES:
        raise HTTPException(422, f"language must be one of: {', '.join(_PREVIEW_LANGUAGES)}")
    api_key = (config.get_env("CARTESIA_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(400, "CARTESIA_API_KEY is not set")
    try:
        from livekit.plugins import cartesia as tts_plugin
    except ImportError:
        raise HTTPException(500, "livekit-plugins-cartesia is not installed") from None
    kwargs = voice_settings.cartesia_kwargs(voice, req.language, api_key)

    async def render() -> list:
        async with aiohttp.ClientSession() as http:
            tts = tts_plugin.TTS(**kwargs, http_session=http)
            try:
                frames = []
                async with tts.synthesize(req.text) as stream:
                    async for chunk in stream:
                        frames.append(chunk.frame)
                return frames
            finally:
                await tts.aclose()

    try:
        frames = await asyncio.wait_for(render(), timeout=20)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Cartesia did not return audio in time") from None
    except Exception as exc:
        raise HTTPException(502, f"Cartesia preview failed: {type(exc).__name__}") from None
    if not frames:
        raise HTTPException(502, "Cartesia returned no audio")

    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(frames[0].num_channels)
        wav.setsampwidth(2)
        wav.setframerate(frames[0].sample_rate)
        for frame in frames:
            wav.writeframes(bytes(frame.data))
    return Response(content=buf.getvalue(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


# ── Latency monitor (per-call timing + transcript, written by the worker) ─────

@app.get("/api/latency/calls")
async def api_latency_calls(limit: int = 30):
    return {"calls": latency_log.list_calls(max(1, min(limit, 100))), "today": latency_log.today_summary()}


@app.get("/api/latency/calls/{call_id}")
async def api_latency_call(call_id: str):
    try:
        events = latency_log.read(call_id)
    except ValueError:
        raise HTTPException(422, "invalid call id") from None
    except FileNotFoundError:
        raise HTTPException(404, "no latency data for this call") from None
    return {"call_id": call_id, "events": events, "summary": latency_log.summarize(events)}


# ── SIP trunk setup ───────────────────────────────────────────────────────────

@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username   = await eff("VOBIZ_USERNAME")
    password   = await eff("VOBIZ_PASSWORD")

    if not all([url, key, secret, sip_domain, username, password]):
        raise HTTPException(400, "Configure LiveKit and Vobiz SIP credentials in Settings first.")

    # Dynamic pool only — sync from Vobiz first so trunk gets every owned DID.
    trunk_numbers: list[str] = []
    try:
        from db import list_active_pool_e164
        trunk_numbers = await list_active_pool_e164()
    except Exception:
        trunk_numbers = []

    if not trunk_numbers:
        raise HTTPException(
            400,
            "Phone pool is empty. Call POST /api/numbers/sync first so all Vobiz numbers "
            "are loaded, then create the trunk.",
        )

    try:
        lk, lk_api = await _get_livekit()
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=trunk_numbers,
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        return {
            "status": "created",
            "trunk_id": trunk_id,
            "numbers": trunk_numbers,
            "dynamic": True,
            "message": "Add OUTBOUND_TRUNK_ID to your Azure environment variables and restart the app. "
                       "Future numbers: POST /api/numbers/sync (auto every 15 min) updates the pool + trunk.",
        }
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# ── Phone number pool ─────────────────────────────────────────────────────────

@app.get("/api/numbers")
async def api_list_numbers(
    organization_id: Optional[str] = Query(None, description="Filter to this org + shared numbers"),
    status: Optional[str] = Query(
        None,
        description="Optional filter: free | busy | disabled. Omit for all.",
    ),
):
    """List outbound caller-ID pool numbers for team integrations.

    Use this to show a dropdown of dialable DIDs. Prefer ``status=free`` when
    picking a number for ``POST /api/call`` ``from_number``.
    Poll this endpoint (or use ``GET /api/numbers/stream``) to show busy/free
    and popup when a DID becomes free.
    """
    try:
        org = organization_id.strip() if organization_id else None
        pool = await phone_pool_status(organization_id=org)
        rows = list(pool.get("numbers") or [])
        want = (status or "").strip().lower()
        if want in ("free", "busy", "disabled"):
            rows = [r for r in rows if (r.get("status") or "").lower() == want]
        numbers = []
        for r in rows:
            e164 = r.get("e164")
            if not e164:
                continue
            st = (r.get("status") or "").lower()
            numbers.append({
                "e164": e164,
                "status": st,
                "is_busy": st == "busy",
                "is_free": st == "free",
                "label": (
                    f"{e164} (busy)" if st == "busy"
                    else f"{e164} (free)" if st == "free"
                    else f"{e164} ({st or 'unknown'})"
                ),
                "organization_id": r.get("organization_id"),
                "current_call_id": r.get("current_call_id"),
                "current_room_name": r.get("current_room_name"),
                "last_used_at": r.get("last_used_at"),
                "country": r.get("country"),
                "region": r.get("region"),
            })
        return {
            "total": pool.get("total", 0),
            "free": pool.get("free", 0),
            "busy": pool.get("busy", 0),
            "disabled": pool.get("disabled", 0),
            "organization_id": pool.get("organization_id"),
            "dynamic": True,
            "note": "Pool is dynamic — POST /api/numbers/sync (or wait ~15 min) after buying new Vobiz DIDs.",
            "live": {
                "poll": "GET /api/numbers every 3–5s",
                "stream": "GET /api/numbers/stream (SSE)",
                "webhook": "Set NUMBER_STATUS_WEBHOOK_URL for POST events on busy/free",
            },
            "numbers": numbers,
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to list phone numbers: {exc}")


@app.get("/api/numbers/stream")
async def api_numbers_stream(
    request: Request,
    organization_id: Optional[str] = Query(None, description="Filter to this org + shared numbers"),
    interval_sec: float = Query(2.0, ge=1.0, le=30.0, description="DB poll interval for cross-process updates"),
):
    """Server-Sent Events stream of pool DID busy/free changes.

    Emits:
      - ``event: snapshot`` — initial status map
      - ``event: number_status`` — each transition (``status`` = free|busy|disabled)

    Use this in your app for a popup when a number becomes free::

        const es = new EventSource('/api/numbers/stream?api_key=…');
        es.addEventListener('number_status', (e) => {
          const data = JSON.parse(e.data);
          if (data.status === 'free') alert(data.message); // or your toast
        });

    Auth: same as other APIs — ``Authorization: Bearer``, ``X-API-Key``, or ``?api_key=``.
    """
    from number_status import (
        diff_status_maps,
        status_map_from_pool,
        subscribe,
        unsubscribe,
    )

    org = organization_id.strip() if organization_id else None
    poll = max(1.0, float(interval_sec))

    async def event_gen():
        q = await subscribe()
        last_map: dict = {}
        try:
            pool = await phone_pool_status(organization_id=org)
            last_map = status_map_from_pool(pool.get("numbers") or [])
            snap = {
                "type": "snapshot",
                "free": pool.get("free", 0),
                "busy": pool.get("busy", 0),
                "disabled": pool.get("disabled", 0),
                "total": pool.get("total", 0),
                "statuses": last_map,
                "numbers": [
                    {
                        "e164": e164,
                        "status": st,
                        "is_busy": st == "busy",
                        "is_free": st == "free",
                        "label": f"{e164} ({st})",
                    }
                    for e164, st in sorted(last_map.items())
                ],
            }
            yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                # Drain same-process pub/sub (fast path)
                try:
                    while True:
                        evt = q.get_nowait()
                        if org:
                            # Org filter is approximate here; DB poll is authoritative.
                            pass
                        yield f"event: number_status\ndata: {json.dumps(evt)}\n\n"
                        if evt.get("e164") and evt.get("status"):
                            last_map[str(evt["e164"])] = str(evt["status"]).lower()
                except asyncio.QueueEmpty:
                    pass

                # Cross-process: agent lease/release updates Supabase — poll diffs
                try:
                    pool = await phone_pool_status(organization_id=org)
                    cur_map = status_map_from_pool(pool.get("numbers") or [])
                    for evt in diff_status_maps(last_map, cur_map):
                        yield f"event: number_status\ndata: {json.dumps(evt)}\n\n"
                    last_map = cur_map
                except Exception as exc:
                    err = {"type": "error", "message": str(exc)}
                    yield f"event: error\ndata: {json.dumps(err)}\n\n"

                yield f": ping\n\n"
                await asyncio.sleep(poll)
        finally:
            await unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/numbers/status")
async def api_numbers_status(
    organization_id: Optional[str] = Query(None, description="Filter to this org + shared numbers"),
):
    try:
        org = organization_id.strip() if organization_id else None
        return await phone_pool_status(organization_id=org)
    except Exception as exc:
        raise HTTPException(500, f"Failed to load phone numbers: {exc}")


@app.post("/api/numbers/sync")
async def api_numbers_sync():
    """Manual sync from Vobiz (also runs on the interval job)."""
    from phone_pool import run_locked_sync
    result = await run_locked_sync()
    if result is None:
        return {"status": "skipped", "reason": "another replica is syncing"}
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "sync failed")
    return {"status": "ok", **result}


class PhoneNumberOrgRequest(BaseModel):
    e164: str
    organization_id: Optional[str] = None  # null/empty = shared pool


@app.patch("/api/numbers")
async def api_assign_number_org(req: PhoneNumberOrgRequest):
    """Assign a Vobiz DID to an organization (or clear to shared)."""
    e164 = (req.e164 or "").strip()
    if not e164:
        raise HTTPException(400, "e164 is required")
    try:
        row = await set_phone_number_organization(e164, req.organization_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not row:
        raise HTTPException(404, f"Number not found in pool: {e164}")
    return {"status": "ok", "number": row}


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# ── CRM ───────────────────────────────────────────────────────────────────────

@app.get("/api/crm")
async def api_get_contacts(
    organization_id: Optional[str] = Query(None, description="Filter calls used to build contacts"),
    lead_id: Optional[str] = Query(None, description="Filter calls used to build contacts"),
    last_outcome: Optional[str] = Query(
        None,
        description="Filter contacts whose latest outcome matches (e.g. booked, callback_requested)",
    ),
    phone: Optional[str] = Query(None, description="Filter to one E.164 phone number"),
):
    return {
        "data": await get_contacts(
            organization_id=organization_id.strip() if organization_id else None,
            lead_id=lead_id.strip() if lead_id else None,
            last_outcome=last_outcome.strip() if last_outcome else None,
            phone=phone.strip() if phone else None,
        )
    }


@app.get("/api/crm/calls")
async def api_get_contact_calls(
    phone: str = Query(...),
    organization_id: str = Query(..., description="Organization owning this contact"),
    lead_id: Optional[str] = Query(None),
):
    calls = await get_calls_by_phone(
        phone,
        organization_id=organization_id.strip(),
        lead_id=lead_id.strip() if lead_id else None,
    )
    for call in calls:
        call["property_data"] = parse_property_data(call.get("property_data"))
        call["sentiment"] = normalize_sentiment(call.get("sentiment"), call.get("outcome"))
    return {"data": calls}


# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools, is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Campaigns ─────────────────────────────────────────────────────────────────

# ── Campaigns ─────────────────────────────────────────────────────────────────

_CAMPAIGN_RUN_LOCK = "campaign_run"
# Long TTL — campaigns can dial large CSVs; lock is always released in finally.
_CAMPAIGN_RUN_LOCK_TTL_SEC = 6 * 60 * 60  # 6 hours


async def _acquire_campaign_run_lock(campaign_id: str) -> tuple[bool, Optional[dict]]:
    """Exclusive lock: only one campaign may dial at a time.

    Returns (acquired, blocking_campaign_or_none).
    """
    await clear_stale_running_campaigns()
    running = await get_running_campaign()
    if running and running.get("id") != campaign_id:
        return False, running

    acquired = await try_acquire_job_lock(
        _CAMPAIGN_RUN_LOCK,
        campaign_id,
        ttl_sec=_CAMPAIGN_RUN_LOCK_TTL_SEC,
    )
    if not acquired:
        owner = await get_campaign_run_lock_owner()
        blocker = running
        if not blocker and owner:
            blocker = await get_campaign(owner)
        if not blocker:
            blocker = {"id": owner or "unknown", "name": "another campaign"}
        return False, blocker

    try:
        await update_campaign_status(campaign_id, "running")
    except Exception:
        await release_job_lock(_CAMPAIGN_RUN_LOCK, campaign_id)
        raise
    return True, None


async def _release_campaign_run_lock(campaign_id: str) -> None:
    try:
        camp = await get_campaign(campaign_id)
        if camp and (camp.get("status") or "") == "running":
            # Recurring stays active; one-shot incomplete run goes back to active.
            await update_campaign_status(campaign_id, "active")
    except Exception as exc:
        logger.warning("Could not reset campaign %s after run: %s", campaign_id, exc)
    try:
        await release_job_lock(_CAMPAIGN_RUN_LOCK, campaign_id)
    except Exception as exc:
        logger.warning("Could not release campaign run lock: %s", exc)


async def _run_campaign(
    campaign_id: str,
    work_items: Optional[list] = None,
    *,
    lock_held: bool = False,
) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        if lock_held:
            await _release_campaign_run_lock(campaign_id)
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts and not work_items:
        if lock_held:
            await _release_campaign_run_lock(campaign_id)
        return

    if not lock_held:
        ok, blocker = await _acquire_campaign_run_lock(campaign_id)
        if not ok:
            logger.warning(
                "Skipping campaign %s — another campaign is already running: %s (%s)",
                campaign_id,
                (blocker or {}).get("name"),
                (blocker or {}).get("id"),
            )
            return
        lock_held = True

    try:
        if work_items is None:
            work_items, _, _ = await _prepare_call_work_items(
                contacts,
                room_prefix=f"camp-{campaign_id[:8]}",
                agent_profile_id=campaign.get("agent_profile_id"),
                projects_payload_is_fresh=False,
            )

        # Keep campaign provenance in LiveKit metadata/webhooks for both scheduled
        # and manually started campaigns.
        for item in work_items:
            item["campaign_id"] = campaign_id
            contact = item.get("contact")
            if isinstance(contact, dict):
                contact["campaign_id"] = campaign_id

        org = _org_id_from_contacts(work_items) or _org_id_from_contacts(contacts)
        # Always re-resolve against live phone pool (2 DIDs ⇒ 2 slots), ignore stale high values.
        slot_info = await _resolve_parallel_slots(
            int(campaign.get("max_parallel_calls") or 5),
            organization_id=org,
            apply_phone_cap=True,
        )
        max_p = int(slot_info["effective"])
        if slot_info.get("phone_pool_cap") == 0:
            logger.error("Campaign %s aborted — no caller-ID numbers in pool", campaign_id)
            await update_campaign_run_stats(campaign_id, 0, len(work_items))
            return

        logger.info(
            "Campaign %s FIFO: %d leads, %d live slots (phone_pool=%s gemini=%s)",
            campaign_id,
            len(work_items),
            max_p,
            slot_info.get("phone_pool_cap"),
            slot_info.get("gemini_key_cap"),
        )

        # Campaigns always use FIFO queue-advance (never fire all leads at once).
        ok_count, fail_count, _ = await _run_parallel_dials(
            work_items,
            call_delay_seconds=int(campaign.get("call_delay_seconds") or 0),
            max_parallel_calls=max_p,
            system_prompt=campaign.get("system_prompt"),
            additional_instructions=(campaign.get("additional_instructions") or "").strip() or None,
            agent_profile_id=campaign.get("agent_profile_id"),
            queue_advance=True,
            organization_id=org,
        )
        await update_campaign_run_stats(campaign_id, ok_count, fail_count)
        logger.info("Campaign %s done — %d dispatched, %d failed", campaign_id, ok_count, fail_count)
    except Exception as exc:
        logger.error("Campaign %s failed: %s", campaign_id, redact_secrets(str(exc)))
    finally:
        if lock_held:
            await _release_campaign_run_lock(campaign_id)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    camp_creds = _extract_client_credentials(req)
    campaign_notes = (req.additional_instructions or "").strip() or None
    default_projects = _campaign_default_projects(req)
    processed_contacts = []
    for c in req.contacts:
        if isinstance(c, dict):
            c_copy = _apply_campaign_context_to_contact(
                c,
                additional_instructions=campaign_notes,
                default_projects=default_projects,
            )
            c_creds = _extract_client_credentials(c_copy)
            if not c_creds.get("gemini_api_key") and camp_creds.get("gemini_api_key"):
                c_copy["gemini_api_key"] = camp_creds["gemini_api_key"]
            if camp_creds.get("vobiz"):
                c_copy.setdefault("vobiz_config", camp_creds["vobiz"])
            if camp_creds.get("livekit"):
                c_copy.setdefault("livekit_config", camp_creds["livekit"])
            if req.livekit_trunk_id:
                c_copy.setdefault("livekit_trunk_id", req.livekit_trunk_id)
            processed_contacts.append(c_copy)
        else:
            processed_contacts.append(c)

    for c in processed_contacts:
        c_creds = _extract_client_credentials(c)
        nested_trunk = (
            (c_creds.get("vobiz") or {}).get("trunk_id")
            or (c_creds.get("vobiz") or {}).get("trunkId")
        )
        if not (_livekit_trunk_id_from(c) or nested_trunk):
            raise HTTPException(
                400,
                "livekit_trunk_id is required for every campaign organization/contact",
            )

    org = _org_id_from_contacts(processed_contacts)
    slot_info = await _resolve_parallel_slots(
        req.max_parallel_calls,
        organization_id=org,
        apply_phone_cap=True,
    )
    max_p = int(slot_info["effective"])

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(processed_contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        max_parallel_calls=max_p,
        queue_advance=True,  # campaigns always FIFO (slots = phone pool size)
        system_prompt=req.system_prompt,
        additional_instructions=campaign_notes,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type in ("daily", "weekdays"):
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {
        "status": "created",
        "campaign_id": campaign_id,
        "campaign": _public_campaign(campaign),
        "max_parallel_calls": max_p,
        "parallel_slots": slot_info,
        "calls": [],
        "invalid": [],
    }


def _public_campaign(campaign: Optional[dict]) -> Optional[dict]:
    """Campaign row for API responses — stored per-contact credentials redacted."""
    if not campaign:
        return campaign
    out = dict(campaign)
    raw = out.get("contacts_json")
    if raw:
        try:
            out["contacts_json"] = json.dumps(
                redact_secrets(json.loads(raw)), ensure_ascii=False
            )
        except (TypeError, ValueError):
            out["contacts_json"] = "[]"
    return out


@app.get("/api/campaigns")
async def api_list_campaigns():
    await clear_stale_running_campaigns()
    return [_public_campaign(c) for c in await get_all_campaigns()]


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    camp = await get_campaign(campaign_id)
    if camp and (camp.get("status") or "") == "running":
        raise HTTPException(409, "Cannot delete a running campaign — wait until it finishes")
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    """Start dialing this campaign. Only one campaign may run at a time."""
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    if (campaign.get("status") or "") == "paused":
        raise HTTPException(400, "Campaign is paused — resume it before starting calls")

    ok, blocker = await _acquire_campaign_run_lock(campaign_id)
    if not ok:
        name = (blocker or {}).get("name") or "another campaign"
        bid = (blocker or {}).get("id") or ""
        raise HTTPException(
            409,
            f"Only one campaign can run at a time. Currently running: {name}"
            + (f" ({bid})" if bid else ""),
        )

    contacts = json.loads(campaign.get("contacts_json") or "[]")
    work_items, calls, invalid = await _prepare_call_work_items(
        contacts,
        room_prefix=f"camp-{campaign_id[:8]}",
        agent_profile_id=campaign.get("agent_profile_id"),
        projects_payload_is_fresh=False,
    )
    asyncio.create_task(_run_campaign(campaign_id, work_items, lock_held=True))
    return {
        "status": "dispatching",
        "campaign_id": campaign_id,
        "single_run_only": True,
        "calls": calls,
        "invalid": invalid,
    }


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    camp = await get_campaign(campaign_id)
    if camp and (camp.get("status") or "") == "running":
        raise HTTPException(409, "Campaign is running — wait until it finishes")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}
