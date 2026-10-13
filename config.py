"""
Central configuration — environment variables are the single source of truth.

Production (Azure): set all variables in Azure Portal / Container Apps / App Service.
Local dev only: set LOAD_DOTENV=true to load a .env file (never overrides existing env).
"""

import os
from typing import Optional

# Keys that MUST come from environment variables only (never Supabase settings table).
ENV_KEYS = frozenset({
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEYS",
    "GEMINI_KEY_LEASE_TIMEOUT_SEC", "REDIS_URL", "GEMINI_KEY_POOL_REDIS_URL",
    "GEMINI_KEY_POOL_REDIS_PREFIX",
    "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME", "VOICE_STACK",
    "INCEPTION_API_KEY", "INCEPTION_MODEL", "INCEPTION_BASE_URL",
    "INCEPTION_REASONING_EFFORT", "INCEPTION_TEMPERATURE", "INCEPTION_MAX_COMPLETION_TOKENS",
    "SARVAM_API_KEY", "SARVAM_STT_LANGUAGE", "STT_PROVIDER", "ELEVEN_API_KEY",
    "CARTESIA_API_KEY", "CARTESIA_VOICE_ID", "CARTESIA_TTS_MODEL",
    "CARTESIA_TTS_SPEED", "CARTESIA_TTS_VOLUME",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_APPLICATION_CREDENTIALS_PATH",
    "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
    "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
    "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN",
    "VOBIZ_RATE_PER_MINUTE", "VOBIZ_BILLING_CURRENCY",
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
    "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_KEY", "AZURE_STORAGE_CONTAINER",
    "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
    "ENABLED_TOOLS", "SYSTEM_PROMPT",
    "CALL_COMPLETED_WEBHOOK_URL", "CALLING_SERVICE_BASE_URL",
    "CALL_COMPLETED_WEBHOOK_ENABLED", "CALL_COMPLETED_WEBHOOK_TIMEOUT",
    "CALL_COMPLETED_WEBHOOK_SECRET",
    "CALL_WEBHOOK_VERBOSE", "LOAD_DOTENV", "WEBHOOK_ONLY_LOGS",
    "API_AUTH_TOKEN",
    "AI_AGENT_NAME", "AGENT_NAME", "LIVEKIT_AGENT_NAME", "RINGING_TIMEOUT_SEC", "AMD_ENABLED", "AMD_TIMEOUT_SEC",
    "AEC_WARMUP_SECONDS",
    "META_WHATSAPP_TOKEN", "META_WHATSAPP_PHONE_NUMBER_ID", "META_WHATSAPP_ENABLED",
    "META_WHATSAPP_TEMPLATE_PROJECT", "META_WHATSAPP_TEMPLATE_BOOKED",
    "META_WHATSAPP_TEMPLATE_LANGUAGE", "META_WHATSAPP_API_VERSION", "META_WHATSAPP_TIMEOUT",
    "ACS_CONNECTION_STRING", "ACS_SENDER_EMAIL", "ACS_EMAIL_ENABLED", "ACS_FOLLOWUP_TO",
    "EMAIL_FOLLOWUP_AGENT_NAME",
    "PHONE_NUMBER_SYNC_INTERVAL_MINUTES", "PHONE_NUMBER_BUSY_MAX_MINUTES",
    "PHONE_NUMBER_JOB_LOCK_TTL_SEC",
})

# App settings that may be stored in Supabase (prompt editor UI) when not set via env.
APP_SETTING_KEYS = frozenset({"system_prompt"})

SENSITIVE_KEYS = frozenset({
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "GEMINI_API_KEYS", "GOOGLE_API_KEYS",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "VOBIZ_PASSWORD", "VOBIZ_AUTH_TOKEN", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY", "AZURE_STORAGE_KEY",
    "META_WHATSAPP_TOKEN", "ACS_CONNECTION_STRING",
    "API_AUTH_TOKEN", "CALL_COMPLETED_WEBHOOK_SECRET",
    "gemini_api_key", "geminiApiKey", "apiKey", "api_key", "apiSecret", "api_secret",
    "authToken", "auth_token", "password", "vobiz_password", "credentials",
})

DEFAULTS = {
    "GEMINI_MODEL": "gemini-3.8-live",
    "GEMINI_TTS_VOICE": "Leda",
    "USE_GEMINI_REALTIME": "true",
    "AI_AGENT_NAME": "outbound-caller",
    "AGENT_NAME": "outbound-caller",
    "CALCOM_TIMEZONE": "Asia/Kolkata",
    "S3_REGION": "ap-northeast-1",
    "VOBIZ_RATE_PER_MINUTE": "0.45",
    "VOBIZ_BILLING_CURRENCY": "INR",
}


def load_env() -> None:
    """Load .env when present (local dev)."""
    try:
        from pathlib import Path
        from dotenv import load_dotenv
        _dir = Path(__file__).resolve().parent
        _env = _dir / ".env"
        if not _env.is_file() and (_dir.parent / ".env").is_file():
            _env = _dir.parent / ".env"
        if _env.is_file():
            load_dotenv(dotenv_path=str(_env), override=True, encoding="utf-8")
    except ImportError:
        pass


def get_env(key: str, default: str = "") -> str:
    """Read a config value from the process environment."""
    value = os.getenv(key, DEFAULTS.get(key, default))
    if key in ("AZURE_STORAGE_KEY", "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_CONTAINER"):
        value = value.strip().strip('"').strip("'")
    return value


def is_env_only_mode() -> bool:
    """True in production — env vars are managed externally (Azure), not via dashboard."""
    if os.getenv("CONFIG_FROM_ENV_ONLY", "").lower() in ("1", "true", "yes"):
        return True
    # Auto-detect common Azure runtime markers
    return bool(
        os.getenv("WEBSITE_SITE_NAME")  # App Service
        or os.getenv("CONTAINER_APP_NAME")  # Container Apps
        or os.getenv("WEBSITE_INSTANCE_ID")
    )


def port() -> int:
    return int(os.getenv("PORT", "8000"))


DEFAULT_LIVEKIT_AGENT_NAME = "outbound-caller"

# Retired worker names — never dispatch here (stale Node payloads / old local workers).
BLOCKED_LIVEKIT_AGENT_NAMES = frozenset({
    "outbound-caller-sarvam",
})


def livekit_agent_name() -> str:
    """LiveKit worker / dispatch name shared by the API and the agent worker.

    Preference order:
      1. LIVEKIT_AGENT_NAME (optional explicit override)
      2. AI_AGENT_NAME (preferred — not clobbered by Azure DevOps)
      3. Legacy AGENT_NAME only when it looks like a LiveKit worker name
         (Azure DevOps predefines AGENT_NAME as "Azure Pipelines 1")
      4. DEFAULT_LIVEKIT_AGENT_NAME
    """
    for key in ("LIVEKIT_AGENT_NAME", "AI_AGENT_NAME"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    legacy = (os.getenv("AGENT_NAME") or "").strip()
    if legacy and " " not in legacy and not legacy.lower().startswith("azure"):
        return legacy
    return DEFAULT_LIVEKIT_AGENT_NAME


def resolve_livekit_agent_name(explicit: Optional[str] = None) -> str:
    """Dispatch/worker name: env default wins over blocked or empty request overrides."""
    requested = (explicit or "").strip()
    if requested and requested.lower() not in BLOCKED_LIVEKIT_AGENT_NAMES:
        return requested
    return livekit_agent_name()


# Load local .env before any other module reads os.environ (when enabled).
load_env()
