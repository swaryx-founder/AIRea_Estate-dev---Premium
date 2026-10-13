"""Stable LiveKit worker entrypoint for the outbound calling agent."""

import os
import sys
import logging

# Import runtime first: it loads environment configuration and installs the
# certificate-aware SSL context before LiveKit/Google networking is imported.
import agent_runtime as _runtime
import agent_context as _contact_context
from livekit import agents

import config
from db import init_db
from db import get_appointments_by_phone, get_calls_by_phone, get_contact_memory


entrypoint = _runtime.entrypoint
_resolve_gemini_model = _runtime._resolve_gemini_model
_resolve_gemini_voice = _runtime._resolve_gemini_voice
_run_outbound_job = _runtime._run_outbound_job
_speak_greeting = _runtime._speak_greeting


_CallHistoryLookupError = _contact_context.CallHistoryLookupError
_await_prefetched_contact_context = _contact_context.await_prefetched_contact_context


async def _fetch_contact_context(
    phone: str,
    *,
    organization_id: str | None = None,
    lead_id: str | None = None,
    exclude_call_id: str | None = None,
):
    """Compatibility wrapper for callers that historically imported from agent."""
    _contact_context.get_calls_by_phone = get_calls_by_phone
    _contact_context.get_appointments_by_phone = get_appointments_by_phone
    _contact_context.get_contact_memory = get_contact_memory
    return await _contact_context.fetch_contact_context(
        phone,
        organization_id=organization_id,
        lead_id=lead_id,
        exclude_call_id=exclude_call_id,
    )


def main() -> None:
    """Initialize configuration and run the LiveKit worker."""
    init_db()
    for key in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEYS",
    ):
        value = config.get_env(key)
        if value:
            os.environ[key] = value

    if len(sys.argv) > 1 and sys.argv[1] in ("start", "dev", "connect"):
        if "--url" not in sys.argv and config.get_env("LIVEKIT_URL"):
            sys.argv.extend(["--url", config.get_env("LIVEKIT_URL")])
        if "--api-key" not in sys.argv and config.get_env("LIVEKIT_API_KEY"):
            sys.argv.extend(["--api-key", config.get_env("LIVEKIT_API_KEY")])
        if "--api-secret" not in sys.argv and config.get_env("LIVEKIT_API_SECRET"):
            sys.argv.extend(["--api-secret", config.get_env("LIVEKIT_API_SECRET")])

    worker_name = config.livekit_agent_name()
    logging.getLogger("outbound-agent").info(
        "Registering LiveKit worker agent_name=%s",
        worker_name,
    )
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=worker_name,
            ws_url=config.get_env("LIVEKIT_URL"),
            api_key=config.get_env("LIVEKIT_API_KEY"),
            api_secret=config.get_env("LIVEKIT_API_SECRET"),
        )
    )


if __name__ == "__main__":
    main()
