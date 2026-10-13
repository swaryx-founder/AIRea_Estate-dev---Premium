"""Outbound call runtime implementation.

The deployment entrypoint lives in ``agent.py``. This module owns the per-call
runtime until its responsibilities are extracted into focused services.
"""

import asyncio
import json
import logging
import os
import ssl
import time
import certifi
import httpx
from typing import Optional

from pathlib import Path
from dotenv import load_dotenv

_script_dir = Path(__file__).resolve().parent
_env_file = _script_dir / ".env"
if not _env_file.is_file() and (_script_dir.parent / ".env").is_file():
    _env_file = _script_dir.parent / ".env"
if _env_file.is_file():
    load_dotenv(dotenv_path=str(_env_file), override=True, encoding="utf-8")

import config  # loads env before other imports


# Patch SSL before any network import (with cached context to eliminate ~246ms disk IO blocks)
_orig_ssl = ssl.create_default_context
_cached_ssl_context = None

def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    global _cached_ssl_context
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata") and purpose == ssl.Purpose.SERVER_AUTH:
        if _cached_ssl_context is None:
            kwargs["cafile"] = certifi.where()
            _cached_ssl_context = _orig_ssl(purpose, **kwargs)
        return _cached_ssl_context
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    RoomInputOptions,
    APIConnectOptions,
    AMD,
    AMDCategory,
    StopResponse,
)
from livekit.agents.metrics import EOUMetrics, LLMMetrics, STTMetrics, TTSMetrics
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice.turn import TurnHandlingOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import (
    init_db,
    get_enabled_tools,
    create_active_call,
    ActiveCallConflictError,
    set_call_answered,
    get_available_number,
    acquire_specific_number,
    NoNumbersAvailable,
    mark_transcript_capturing,
    bind_call_projects,
    resolve_organization_projects,
    ProjectScopeError,
)
from prompts import (
    additional_instructions_has_greeting,
    additional_instructions_has_permission,
    build_campaign_opening_instructions,
    build_campaign_opening_line,
    build_identity_opening,
    build_permission_intro,
    build_prompt,
    build_reconnect_opening,
    build_silence_failsafe_nudge,
    build_voicemail_message,
    indian_accent_runtime_reminder,
    normalize_projects,
    resolve_agent_name,
    resolve_call_language,
    spoken_lead_name,
)
from tools import AppointmentTools
from client_requirements import ClientRequirementsCollector
from transcripts import (
    CallTranscriptTracker,
    attach_transcript_handlers,
    replay_pending_transcript_writes,
)
from agent_context import (
    CallHistoryLookupError as _CallHistoryLookupError,
    await_prefetched_contact_context as _await_prefetched_contact_context,
    fetch_contact_context as _fetch_contact_context,
)
from agent_observability import log_event as _log
from agent_recording import start_recording as _start_recording
import latency_log
import voice_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# Local-only "webhook-only" view: quiet noisy libs and silence the duplicate
# call-webhook JSON log lines (the [WEBHOOK] trace uses plain print()).
# On Azure (flag unset) the call-webhook logger stays at INFO so webhook
# success/failure remains visible in container logs.
if os.getenv("WEBHOOK_ONLY_LOGS", "").lower() in ("1", "true", "yes"):
    logging.getLogger("call-webhook").setLevel(logging.CRITICAL)
    for _noisy in ("livekit", "livekit.agents", "httpx", "httpcore",
                   "google", "opentelemetry.sdk.metrics._internal",
                   "apscheduler", "outbound-agent", "appointment-tools"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
else:
    logging.getLogger("call-webhook").setLevel(logging.INFO)

SIP_DOMAIN = config.get_env("VOBIZ_SIP_DOMAIN")

# Turn handling for the non-realtime STT/LLM/TTS fallback. Gemini Live owns
# turn detection in realtime mode, so this configuration must not be passed to
# that session (doing so disables LiveKit adaptive handling anyway).
_PIPELINE_TURN_HANDLING: TurnHandlingOptions = {
    "endpointing": {"min_delay": 0.25, "max_delay": 0.6},
    "interruption": {
        "enabled": True,
        "mode": "adaptive",
        "min_duration": 2.0,
        "false_interruption_timeout": 1.0,
        "resume_false_interruption": True,
        "discard_audio_if_uninterruptible": True,
    },
    "preemptive_generation": {"enabled": False},
}

# Block barge-ins briefly while AEC/telephony settles. Keep short — 4s was dropping
# early "haan"/hello and creating a long dead-air gap in the opening.
_AEC_WARMUP_SECONDS = max(0.0, float(os.getenv("AEC_WARMUP_SECONDS", "1.5")))
# Reply latency target for TURN_LATENCY_SLOW logs and the dashboard Latency tab.
_LATENCY_TARGET_SECONDS = float(os.getenv("LATENCY_TARGET_SECONDS", "1.5"))

# ── Reply-latency tuning (Gemini Live) ────────────────────────────────────────
# End-of-speech silence before Gemini replies. Every ms here is added to every turn.
# Short endpointing so opening affirmatives ("haan", "ji") commit quickly; 100 is faster
# but may split Hinglish turns at natural pauses.
_GEMINI_SILENCE_MS = max(50, int(os.getenv("GEMINI_SILENCE_MS", "150")))
# Thinking adds time before the first audio of each reply. "minimal" | "low" | "medium" |
# "high"; empty/"off" leaves the model default. Default off: gemini-3.8-live rejects
# thinking_level and the job dies before dialing.
_GEMINI_THINKING_LEVEL = (os.getenv("GEMINI_THINKING_LEVEL", "off") or "").strip().upper()
# Keep long calls fast: compress older context once it passes the trigger size (0 = off).
_GEMINI_CONTEXT_TRIGGER_TOKENS = max(0, int(os.getenv("GEMINI_CONTEXT_TRIGGER_TOKENS", "16000")))
_GEMINI_CONTEXT_TARGET_TOKENS = max(1000, int(os.getenv("GEMINI_CONTEXT_TARGET_TOKENS", "8000")))
# Gemini Live has no TTS, so session.say() cannot speak text. Pre-render the default
# permission line with Gemini TTS (same voice) while the phone rings and play that audio,
# so the line is spoken word-for-word instead of being regenerated by the LLM.
_PERMISSION_AUDIO_PREFETCH = os.getenv("PERMISSION_AUDIO_PREFETCH", "true").lower() not in (
    "0", "false", "no", "off",
)
_PERMISSION_AUDIO_TIMEOUT_SEC = max(1.0, float(os.getenv("PERMISSION_AUDIO_TIMEOUT_SEC", "12")))
# Same for the opening line: render the exact greeting while the phone rings and play it
# the moment the lead answers, instead of waiting ~1.5-2s for Gemini Live to generate it
# after pickup. Falls back to live generation if the audio is not ready in time.
_GREETING_AUDIO_PREFETCH = os.getenv("GREETING_AUDIO_PREFETCH", "true").lower() not in (
    "0", "false", "no", "off",
)
_PERMISSION_TTS_STYLE = (
    "Read the text exactly as written, word for word — do not add, skip, or change any word. "
    "Voice: a warm, calm, professional young Indian woman on a phone call, natural Indian "
    "accent, Hinglish words pronounced the Indian way, normal conversational pace."
)

# Soft-prompt after lead stays silent following agent speech (code timer — not prompt-only).
# SILENCE_FAILSAFE_ENABLED=false disables hear-checks entirely (no legacy fallback path).
_SILENCE_FAILSAFE_ENABLED = os.getenv("SILENCE_FAILSAFE_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
    "off",
)
_SILENCE_FAILSAFE_SEC = max(3.0, float(os.getenv("SILENCE_FAILSAFE_SEC", "7")))
# Two hear-checks then hang up — higher values felt like a stuck IVR loop.
_SILENCE_FAILSAFE_MAX = max(1, int(os.getenv("SILENCE_FAILSAFE_MAX", "2")))
# While the lead has spoken recently, do not arm hear-checks (avoids IVR feel mid-chat).
_USER_SILENCE_ACTIVE_CHAT_SEC = max(
    0.0, float(os.getenv("USER_SILENCE_ACTIVE_CHAT_SEC", "12"))
)

# Bound idle Gemini Live time while the phone rings (AMD requires session.start before dial).
_RINGING_TIMEOUT_SEC = int(os.getenv("RINGING_TIMEOUT_SEC", "30"))
# AMD blocks agent speech until classification finishes. LiveKit known issue: execute()
# can take ~7–16s (or full timeout) on human answers — that is the "agent silent" gap.
# Keep AMD on by default but cap wait; set AMD_ENABLED=false for instant greeting.
_AMD_ENABLED = os.getenv("AMD_ENABLED", "true").lower() not in ("0", "false", "no")
_AMD_TIMEOUT_SEC = max(1.0, float(os.getenv("AMD_TIMEOUT_SEC", "2.5")))

# Hindi/Hinglish-tuned AMD classification prompt (overrides English-default AMD_PROMPT).
_AMD_HINGLISH_PROMPT = """Task:
Classify the start of an outbound phone call greeting into exactly one category:

human: A real person answered in any language (e.g. "Hello?", "Haan?", "Boliyo", "Kaun hai?", "Ji?").
machine-ivr: IVR / phone tree / call-screening menu (press a key, "press 1", "apna naam boliye", screening bots).
machine-vm: Voicemail greeting where leaving a message IS possible
  (e.g. "leave a message", "beep ke baad message chhodiye", carrier Hindi VM prompts).
machine-unavailable: Mailbox full / not set up / cannot leave a message / immediate disconnect prompt.
uncertain: Ambiguous or too little audio/transcript evidence.

Examples:
Input: "The person you called has a voice mailbox that hasn't been set up yet. Goodbye."
Output: machine-unavailable
Input: "Namaste, main abhi available nahi hoon. Beep ke baad message chhodiye."
Output: machine-vm
Input: "Please press 1 for sales, press 2 for support."
Output: machine-ivr
Input: "Haan, boliye?"
Output: human
Input: "Hello?"
Output: human
"""


# A job LiveKit delivers this long after the API dispatched it is not dialed: the
# CRM has usually given up or redialed, and a late dial double-rings the lead.
_DISPATCH_MAX_AGE_SEC = max(30.0, float(os.getenv("DISPATCH_MAX_AGE_SEC", "45")))


async def _fail_call_before_dial(call_id: Optional[str], reason: str, room_name: Optional[str]) -> None:
    """Finalize a call that never reached the phone line so Node is notified.

    Safe without SIP confirmation: nothing was dialed. Never downgrades a call
    that already has a final outcome (finalize_call guards that).
    """
    if not call_id:
        return
    try:
        from db import finalize_call, get_call_by_id
        row = await get_call_by_id(call_id)
        if row and (row.get("status") or "").lower() not in ("queued", "ringing"):
            return
        await finalize_call(
            call_id,
            outcome="failed",
            reason=reason[:500],
            duration_seconds=0,
            room_name=room_name,
        )
    except Exception as exc:
        await _log("warning", f"Could not finalize call {call_id} before dial: {exc}")


def _amd_category_str(category) -> str:
    return category.value if hasattr(category, "value") else str(category)


async def _finalize_and_hangup_session(
    *,
    ctx: agents.JobContext,
    session: AgentSession,
    tool_ctx: "AppointmentTools",
    call_id: Optional[str],
    outcome: str,
    reason: str,
    log_label: str,
) -> None:
    """Hang up SIP first, then finalize (webhook). Prevents Node redial while line still live."""
    await _log("info", f"{log_label} outcome={outcome} reason={reason[:200]!r}")
    cid = call_id or getattr(tool_ctx, "call_id", None)
    live_transcript = None
    transcript_json = None
    room_name = ctx.room.name if ctx.room else None
    if cid:
        try:
            from client_requirements import infer_hangup_outcome
            if tool_ctx.requirements_collector:
                await tool_ctx.requirements_collector.flush()
            if tool_ctx.transcript_tracker:
                await tool_ctx.transcript_tracker.flush()
                live_transcript = tool_ctx.transcript_tracker.live_text()
                transcript_json = tool_ctx.transcript_tracker.json_text()
            # Lead already said interested / not interested → prefer that over cut-mid-call.
            if (outcome or "").strip().lower() == "disconnect_in_between":
                existing_props = None
                try:
                    from db import get_call_by_id
                    existing = await get_call_by_id(cid)
                    if existing:
                        existing_props = existing.get("property_data")
                except Exception:
                    existing_props = None
                intent_outcome = infer_hangup_outcome(
                    live_transcript=live_transcript,
                    transcript_json=transcript_json,
                    property_data=existing_props,
                )
                if intent_outcome:
                    outcome = intent_outcome
                    reason = f"{reason} (reclassified from disconnect_in_between → {intent_outcome})"
                    await _log(
                        "info",
                        f"{log_label} reclassified hangup to outcome={intent_outcome}",
                    )
        except Exception as exc:
            await _log("warning", f"{log_label} pre-hangup transcript/outcome prep failed: {exc}")

    # Tear down SIP/room and confirm absence BEFORE webhook/DID release.
    hangup_ok = False
    try:
        hangup_ok = bool(await tool_ctx._hangup_call())
    except Exception as exc:
        await _log("warning", f"{log_label} hangup failed: {exc}")
    if not hangup_ok:
        try:
            hangup_ok = bool(await tool_ctx._hangup_call())
        except Exception as exc:
            await _log("warning", f"{log_label} hangup retry failed: {exc}")
    if not hangup_ok:
        await _log(
            "error",
            f"{log_label} skipping finalize/webhook — SIP not confirmed absent",
        )
        try:
            await session.aclose()
        except Exception as exc:
            await _log("warning", f"{log_label} session.aclose failed: {exc}")
        # Keep call_logs active + DID leased so campaign/Node cannot redial.
        return

    if cid:
        try:
            from db import finalize_call
            await finalize_call(
                cid,
                outcome=outcome,
                reason=reason[:500],
                duration_seconds=int(time.time() - tool_ctx._call_start_time),
                recording_url=tool_ctx.recording_url,
                live_transcript=live_transcript,
                transcript_json=transcript_json,
                room_name=room_name,
            )
        except Exception as exc:
            await _log("warning", f"{log_label} finalize_call failed: {exc}")
    try:
        await session.aclose()
    except Exception as exc:
        await _log("warning", f"{log_label} session.aclose failed: {exc}")
    ctx.shutdown()


# Brief pause after AMD VM detect so the mailbox beep / greeting can finish.
_AMD_VOICEMAIL_BEEP_WAIT_SEC = max(
    0.0, float(os.getenv("AMD_VOICEMAIL_BEEP_WAIT_SEC", "1.5"))
)


async def _leave_amd_voicemail_message(
    *,
    session: AgentSession,
    tool_ctx: "AppointmentTools",
    business_name: str,
    language: str,
    agent_name: str = "Riya",
) -> None:
    """Speak the fixed short voicemail, then wait for playout before hangup."""
    if _AMD_VOICEMAIL_BEEP_WAIT_SEC > 0:
        await asyncio.sleep(_AMD_VOICEMAIL_BEEP_WAIT_SEC)
    if tool_ctx._hangup_done or not _session_can_speak(session):
        return
    message = build_voicemail_message(
        lead_name=tool_ctx.lead_name,
        business_name=business_name,
        language=language or "hinglish",
        agent_name=agent_name,
    )
    try:
        if getattr(session, "tts", None) is not None:
            handle = session.say(message, allow_interruptions=False)
        else:
            handle = session.generate_reply(
                instructions=(
                    "Voicemail detected. Leave ONLY this exact short message aloud, then stop. "
                    f'Do not add a pitch or questions. Message: "{message}"'
                ),
                allow_interruptions=False,
            )
        await asyncio.wait_for(
            handle.wait_for_playout(),
            timeout=float(os.getenv("AMD_VOICEMAIL_PLAYOUT_TIMEOUT_SEC", "25")),
        )
        await _log("info", f"AMD voicemail message left ({len(message)} chars)")
    except Exception as exc:
        await _log("warning", f"AMD voicemail message failed (still hanging up): {exc}")


async def _finalize_amd_machine_and_hangup(
    *,
    ctx: agents.JobContext,
    session: AgentSession,
    tool_ctx: "AppointmentTools",
    call_id: Optional[str],
    category: str,
    reason: str,
    transcript: str,
    business_name: str = "our company",
    language: str = "hinglish",
    agent_name: str = "Riya",
) -> None:
    """On AMD machine: leave short VM for vm/ivr, silent hangup for unavailable."""
    cat = (category or "").strip().lower()
    # Mailbox full / cannot leave a message → no spoken VM.
    if cat in (AMDCategory.MACHINE_UNAVAILABLE.value, "machine-unavailable"):
        outcome = "no_answer"
        detail = f"AMD:{category} {reason} transcript={transcript[:200]!r}"
        await _finalize_and_hangup_session(
            ctx=ctx,
            session=session,
            tool_ctx=tool_ctx,
            call_id=call_id,
            outcome=outcome,
            reason=detail,
            log_label="AMD_HANGUP",
        )
        return

    # machine-vm / machine-ivr → leave short message, then voicemail outcome.
    outcome = "voicemail"
    detail = f"AMD:{category} {reason} transcript={transcript[:200]!r}"
    await _leave_amd_voicemail_message(
        session=session,
        tool_ctx=tool_ctx,
        business_name=business_name,
        language=language,
        agent_name=agent_name,
    )
    await _finalize_and_hangup_session(
        ctx=ctx,
        session=session,
        tool_ctx=tool_ctx,
        call_id=call_id,
        outcome=outcome,
        reason=detail,
        log_label="AMD_VOICEMAIL",
    )


def _session_can_speak(session: AgentSession) -> bool:
    """True only when generate_reply/say will work (not closing / not drained)."""
    if getattr(session, "_closing", False):
        return False
    activity = session._activity
    if activity is None:
        return False
    # LiveKit raises "AgentSession is closing" when scheduling is paused and there is no next activity.
    if getattr(activity, "scheduling_paused", False) and getattr(session, "_next_activity", None) is None:
        return False
    return True


async def _wait_for_audio_subscribed(session: AgentSession, timeout: float = 1.5) -> bool:
    """Wait briefly for SIP to subscribe to agent audio. Returns False on timeout (greet anyway)."""
    try:
        rio = session.room_io
    except RuntimeError:
        return True
    sub = rio.subscribed_fut
    if sub is None or sub.done():
        return True
    try:
        await asyncio.wait_for(asyncio.shield(sub), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _wait_for_agent_speech(
    session: AgentSession, *, min_secs: float = 0.8, timeout: float = 20.0
) -> float:
    """Wait until the agent speaks then returns to a non-speaking state (no SpeechHandle)."""
    t0 = time.time()
    deadline = t0 + timeout
    saw_speaking = False
    while time.time() < deadline:
        state = str(getattr(session, "agent_state", "") or "")
        if state == "speaking":
            saw_speaking = True
        elif saw_speaking and state in ("listening", "thinking", "idle", ""):
            elapsed = time.time() - t0
            if elapsed >= min_secs:
                return elapsed
        await asyncio.sleep(0.05)
    elapsed = time.time() - t0
    if not saw_speaking:
        raise RuntimeError(f"agent never entered speaking state within {timeout:.1f}s")
    return elapsed


def _send_realtime_input_nudge(session: AgentSession, nudge: str) -> None:
    """Push a SYSTEM EVENT via Gemini Live send_realtime_input (3.1-safe path)."""
    activity = session._activity
    rt = getattr(activity, "_rt_session", None) if activity is not None else None
    if rt is None or not hasattr(rt, "_send_client_event"):
        raise RuntimeError("realtime nudge requires an active Google realtime session")
    from google.genai import types as gt

    rt._send_client_event(gt.LiveClientRealtimeInput(text=nudge))


class SilenceFailsafe:
    """Code timer: after agent speech ends, soft-prompt if lead stays silent ~7s.

    Gemini Live will not spontaneously re-speak on wall-clock silence; the prompt
    rule alone is not enough. Uses generate_reply on 3.8; send_realtime_input on 3.1.

    Loop guards:
    - Do not arm during recent lead speech (active-chat window).
    - Failsafe's own hear-check must not restart an unbounded prompt loop.
    - After max hear-checks with no reply, stop and optionally hang up once.
    """

    def __init__(
        self,
        session: AgentSession,
        *,
        model: str,
        lead_name: str = "",
        on_exhausted=None,
    ) -> None:
        self._session = session
        self._model = (model or "").strip()
        self._lead_name = (lead_name or "").strip()
        self._on_exhausted = on_exhausted
        self._task: Optional[asyncio.Task] = None
        self._armed = False
        self._stopped = False
        self._count = 0
        self._nudge_in_flight = False
        self._awaiting_reply_to_nudge = False
        self._last_user_speech_at = 0.0
        self._exhausted_hangup_started = False

    def arm(self) -> None:
        self._armed = True
        self._stopped = False

    def arm_and_start(self) -> None:
        """Arm and start the first dead-air window (e.g. right after greeting)."""
        self.arm()
        self._schedule(force=True)

    def stop(self) -> None:
        self._stopped = True
        self._armed = False
        self._nudge_in_flight = False
        self._awaiting_reply_to_nudge = False
        self._cancel_timer()

    def _cancel_timer(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    def _recent_user_speech(self) -> bool:
        if _USER_SILENCE_ACTIVE_CHAT_SEC <= 0 or not self._last_user_speech_at:
            return False
        return (time.time() - self._last_user_speech_at) < _USER_SILENCE_ACTIVE_CHAT_SEC

    def _active_chat_remaining(self) -> float:
        if not self._recent_user_speech():
            return 0.0
        elapsed = time.time() - self._last_user_speech_at
        return max(0.05, _USER_SILENCE_ACTIVE_CHAT_SEC - elapsed)

    def _mark_user_activity(self) -> None:
        self._last_user_speech_at = time.time()
        self._count = 0
        self._nudge_in_flight = False
        self._awaiting_reply_to_nudge = False
        self._exhausted_hangup_started = False
        self._cancel_timer()

    def _schedule(self, *, force: bool = False) -> None:
        if (
            not self._armed
            or self._stopped
            or not _SILENCE_FAILSAFE_ENABLED
            or self._count >= _SILENCE_FAILSAFE_MAX
            or self._nudge_in_flight
        ):
            return
        if not force and self._recent_user_speech() and not self._awaiting_reply_to_nudge:
            # Active chat — defer until the grace window ends, then arm the
            # normal silence wait. Returning with no timer left the watchdog dead.
            remaining = self._active_chat_remaining()
            self._cancel_timer()
            self._task = asyncio.create_task(
                self._run_after_active_chat(remaining),
                name="silence_failsafe_deferred",
            )
            return
        self._cancel_timer()
        self._task = asyncio.create_task(self._run(), name="silence_failsafe")

    def _alive(self) -> bool:
        return bool(
            self._armed
            and not self._stopped
            and _SILENCE_FAILSAFE_ENABLED
            and self._count < _SILENCE_FAILSAFE_MAX
        )

    def _ensure_armed_timer(self, *, delay: float | None = None) -> None:
        """Guarantee a watchdog timer exists so dead air cannot strand the call."""
        if not self._alive() or self._nudge_in_flight:
            return
        if delay is not None:
            # Replace any pending timer. If called from the current timer task,
            # detach without cancelling ourselves mid-finally.
            task = self._task
            self._task = None
            current = asyncio.current_task()
            if task is not None and not task.done() and task is not current:
                task.cancel()
            self._task = asyncio.create_task(
                self._run_after_delay(max(0.25, float(delay))),
                name="silence_failsafe_retry",
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._schedule()

    async def _run_after_delay(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._alive() or self._nudge_in_flight:
            return
        self._schedule()

    async def _run_after_active_chat(self, remaining: float) -> None:
        try:
            await asyncio.sleep(max(0.05, float(remaining or 0.0)))
        except asyncio.CancelledError:
            return
        if not self._alive() or self._nudge_in_flight:
            return
        if self._recent_user_speech() and not self._awaiting_reply_to_nudge:
            # Lead spoke again during the grace window — defer once more.
            self._schedule()
            return
        # Grace expired. If the agent is mid-utterance, poll briefly instead of
        # relying solely on a speech-end event (those can be missed → 60s dead air).
        agent_st = str(getattr(self._session, "agent_state", "") or "")
        if agent_st in ("speaking", "thinking"):
            self._ensure_armed_timer(delay=1.0)
            return
        self._schedule(force=True)

    def on_agent_state_changed(self, ev) -> None:
        if not self._armed or self._stopped:
            return
        old = str(getattr(ev, "old_state", "") or "")
        new = str(getattr(ev, "new_state", "") or "")
        if new == "speaking" or new == "thinking":
            self._cancel_timer()
            return
        if new in ("listening", "idle") and old in ("speaking", "thinking"):
            if self._nudge_in_flight:
                # Hear-check just finished speaking — wait once for a reply.
                self._after_nudge_playout()
                return
            self._awaiting_reply_to_nudge = False
            self._schedule()

    def on_user_state_changed(self, ev) -> None:
        if not self._armed or self._stopped:
            return
        old = str(getattr(ev, "old_state", "") or "")
        new = str(getattr(ev, "new_state", "") or "")
        if new == "speaking":
            self._mark_user_activity()
            return
        # False VAD / brief noise previously cancelled the timer and never
        # rearmed when the lead went back to listening → long dead air.
        if old == "speaking" and new in ("listening", "away", "idle", ""):
            agent_st = str(getattr(self._session, "agent_state", "") or "")
            if agent_st in ("listening", "idle", ""):
                self._ensure_armed_timer()

    def on_user_input_transcribed(self, ev) -> None:
        if not self._armed or self._stopped:
            return
        text = (getattr(ev, "transcript", None) or getattr(ev, "text", None) or "").strip()
        if text:
            self._mark_user_activity()

    def _should_fire(self) -> bool:
        if self._stopped or not self._armed or not _session_can_speak(self._session):
            return False
        if self._count >= _SILENCE_FAILSAFE_MAX:
            return False
        if self._nudge_in_flight:
            return False
        if self._recent_user_speech() and not self._awaiting_reply_to_nudge:
            return False
        agent_st = str(getattr(self._session, "agent_state", "") or "")
        user_st = str(getattr(self._session, "user_state", "") or "")
        if agent_st in ("speaking", "thinking"):
            return False
        if user_st == "speaking":
            return False
        return True

    def _start_exhausted_hangup(self) -> None:
        if self._exhausted_hangup_started or self._on_exhausted is None:
            return
        self._exhausted_hangup_started = True
        self._armed = False
        self._cancel_timer()
        asyncio.create_task(self._run_exhausted(), name="silence_failsafe_hangup")

    async def _run_exhausted(self) -> None:
        try:
            # One short quiet window after the last hear-check, then hang up.
            await asyncio.sleep(_SILENCE_FAILSAFE_SEC)
            if self._stopped or self._last_user_speech_at and (
                time.time() - self._last_user_speech_at
            ) < _SILENCE_FAILSAFE_SEC:
                return
            if not _session_can_speak(self._session):
                return
            await self._on_exhausted()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await _log("warning", f"SILENCE_FAILSAFE hangup failed: {exc}")

    def _after_nudge_playout(self) -> None:
        """Advance state after a hear-check finishes (or if speech events never arrive)."""
        if self._stopped or not self._nudge_in_flight:
            return
        self._nudge_in_flight = False
        self._awaiting_reply_to_nudge = True
        if self._count >= _SILENCE_FAILSAFE_MAX:
            self._start_exhausted_hangup()
            return
        self._schedule(force=True)

    async def _nudge_playout_fallback(self) -> None:
        try:
            await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            return
        if self._nudge_in_flight and not self._stopped:
            await _log(
                "warning",
                "SILENCE_FAILSAFE hear-check had no agent speech-end event — advancing timer",
            )
            self._after_nudge_playout()

    async def _nudge(self) -> None:
        attempt = self._count + 1
        nudge = build_silence_failsafe_nudge(
            attempt=attempt,
            lead_name=self._lead_name,
        )
        self._nudge_in_flight = True
        if "3.1" in self._model and getattr(self._session._activity, "_rt_session", None) is not None:
            _send_realtime_input_nudge(self._session, nudge)
        else:
            self._session.generate_reply(instructions=nudge, allow_interruptions=True)
        asyncio.create_task(
            self._nudge_playout_fallback(),
            name="silence_failsafe_playout_fallback",
        )
        await _log(
            "info",
            f"SILENCE_FAILSAFE fired attempt={attempt}/{_SILENCE_FAILSAFE_MAX} "
            f"after {_SILENCE_FAILSAFE_SEC:.0f}s dead air",
        )

    async def _run(self) -> None:
        try:
            await asyncio.sleep(_SILENCE_FAILSAFE_SEC)
        except asyncio.CancelledError:
            return
        if not self._alive():
            return
        if self._recent_user_speech() and not self._awaiting_reply_to_nudge:
            # User activity landed during the wait — defer instead of dying.
            self._schedule()
            return
        if not self._should_fire():
            # Transient block (agent thinking, VAD glitch, session not ready).
            # Never leave the call without a timer — that caused 60s+ dead air.
            await _log(
                "info",
                "SILENCE_FAILSAFE deferred fire "
                f"(agent={getattr(self._session, 'agent_state', None)} "
                f"user={getattr(self._session, 'user_state', None)} "
                f"speakable={_session_can_speak(self._session)}) — retry in 1s",
            )
            self._ensure_armed_timer(delay=1.0)
            return
        try:
            await self._nudge()
            self._count += 1
        except Exception as exc:
            self._nudge_in_flight = False
            await _log("warning", f"SILENCE_FAILSAFE nudge failed: {exc}")
            # Retry the wait so a failed nudge cannot kill the watchdog.
            self._ensure_armed_timer(delay=1.0)
            return
        # Do NOT reschedule here. Wait for the hear-check playout to end
        # (on_agent_state_changed / fallback). Mid-nudge reschedule caused loops.


async def _speak_greeting(
    session: AgentSession,
    text: str,
    *,
    model: Optional[str] = None,
    lead_name: str = "there",
    additional_instructions: Optional[str] = None,
    reconnect: bool = False,
    campaign: bool = False,
    max_attempts: int = 2,
) -> float:
    """Speak greeting; retry if playout finishes instantly (no real audio generated)."""
    if not _session_can_speak(session):
        raise RuntimeError(
            f"session not speakable (closing={getattr(session, '_closing', None)} "
            f"activity={session._activity is not None} "
            f"paused={getattr(session._activity, 'scheduling_paused', None)})"
        )

    # Gemini 3.8 Live supports the standard LiveKit generate_reply path.
    if additional_instructions_has_greeting(additional_instructions) and not reconnect:
        reply_instructions = build_campaign_opening_instructions(
            lead_name, additional_instructions
        )
    elif additional_instructions_has_greeting(additional_instructions):
        # Reconnect on a campaign with its own salutation (e.g. "Radhe Radhe").
        reply_instructions = (
            "Reconnect after a cut call. Say the salutation from CAMPAIGN NOTES, then the quoted "
            "sentence below word-for-word. No other changes, no identity question, no project "
            "name or pitch. Then stop and wait for the lead.\n"
            f"{indian_accent_runtime_reminder()} "
            f'Sentence: "{text}"'
        )
    else:
        reply_instructions = (
            "Speak exactly the quoted sentence below, with no paraphrasing, additions, project name, "
            "or explanation. Then stop and wait for the lead.\n"
            f"{indian_accent_runtime_reminder()} "
            f'Exact sentence: "{text}"'
        )

    last_elapsed = 0.0
    for attempt in range(1, max_attempts + 1):
        if not _session_can_speak(session):
            raise RuntimeError("session became unspeakable during greeting")
        t0 = time.time()
        # Gemini Live uses server-side turn detection. Keep the greeting
        # interruptible so caller speech can barge in and become the next turn.
        handle = session.generate_reply(instructions=reply_instructions, allow_interruptions=True)
        await handle.wait_for_playout()
        last_elapsed = time.time() - t0

        # State 1: Lead interrupted (barge-in) — move forward naturally with user turn
        if getattr(handle, "interrupted", False):
            logger.info("Lead interrupted greeting (barge-in) — proceeding with conversation")
            return last_elapsed

        # State 2: Error or timeout (no real audio was generated) — do not count as played
        exc = handle.exception() if hasattr(handle, "exception") else None
        if exc is not None:
            logger.warning(
                "Greeting attempt %d/%d failed with error (%s) after %.2fs — retrying",
                attempt, max_attempts, exc, last_elapsed,
            )
            await asyncio.sleep(0.2)
            continue

        # State 3: Completed normally
        if last_elapsed >= 0.8:
            logger.info("Greeting completed normally in %.2fs", last_elapsed)
            return last_elapsed

        logger.warning(
            "Greeting playout too fast (%.2fs) on attempt %d/%d — retrying",
            last_elapsed, attempt, max_attempts,
        )
        await asyncio.sleep(0.3)
    return last_elapsed


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None
_google_stt = None
_gemini_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
        _google_stt = _gp.STT
    except AttributeError:
        pass
    try:
        _gemini_tts = _gp.beta.GeminiTTS
    except AttributeError:
        pass
    try:
        from livekit.plugins.google.realtime import realtime_api as _rt_api
        _rt_api.MODELS_WITHOUT_REPLY_PLACEHOLDER = ()
    except Exception:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


_sarvam_stt_realtime = None
_sarvam_stt = None
try:
    from livekit.plugins import sarvam as _sarvam
    _sarvam_stt_realtime = _sarvam.STTRealtime
    _sarvam_stt = _sarvam.STT
except ImportError:
    logger.warning("livekit-plugins-sarvam not installed")

_elevenlabs_stt = None
try:
    from livekit.plugins import elevenlabs as _elevenlabs
    _elevenlabs_stt = _elevenlabs.STT
except ImportError:
    logger.warning("livekit-plugins-elevenlabs not installed")

_cartesia_tts = None
try:
    from livekit.plugins import cartesia as _cartesia
    _cartesia_tts = _cartesia.TTS
except ImportError:
    logger.warning("livekit-plugins-cartesia not installed")

_openai_llm = None
try:
    from livekit.plugins import openai as _openai
    _openai_llm = _openai.LLM
except ImportError:
    logger.warning("livekit-plugins-openai not installed")

_INCEPTION_BASE_URL = "https://api.inceptionlabs.ai/v1"


# Locked stable Live model for AI Studio realtime (USE_GEMINI_REALTIME=true).
# Gemini 3.8 supports the normal generate_reply greeting path and mid-session
# client content without a private realtime-session workaround.
_NATIVE_AUDIO_MODEL = "gemini-3.8-live"
_NATIVE_AUDIO_VOICE = "Leda"


def _resolve_gemini_model(override: Optional[str] = None) -> str:
    """Return the locked Live model. Does not mutate process env (safe for concurrent jobs)."""
    raw = (override or os.getenv("GEMINI_MODEL") or "").strip()
    if raw and raw != _NATIVE_AUDIO_MODEL:
        logger.warning(
            "Ignoring GEMINI_MODEL %r → using %s (locked Live model for AI Studio realtime path)",
            raw,
            _NATIVE_AUDIO_MODEL,
        )
    return _NATIVE_AUDIO_MODEL


def _cascade_enabled() -> bool:
    """True when this worker should run Sarvam STT + Mercury + Cartesia instead of Gemini Live."""
    stack = (os.getenv("VOICE_STACK") or "").strip().lower()
    return stack in {"cascade", "mercury", "sarvam"}


def _voice_locale(call_language: str) -> str:
    """BCP-47 locale for Sarvam STT and Cartesia. English campaigns stay en-IN."""
    if str(call_language or "").strip().lower() == "english":
        return "en-IN"
    return "hi-IN"


def _stt_mode(call_language: str) -> str:
    if str(call_language or "").strip().lower() == "english":
        return "transcribe"
    return "codemix"


def _resolve_gemini_voice(override: Optional[str] = None) -> str:
    """Keep one speaker identity across calls while allowing spoken language changes."""
    raw = (override or os.getenv("GEMINI_TTS_VOICE") or "").strip()
    if raw and raw.lower() != _NATIVE_AUDIO_VOICE.lower():
        logger.warning(
            "Ignoring voice override %r → using %s (locked multilingual Riya voice)",
            raw,
            _NATIVE_AUDIO_VOICE,
        )
    return _NATIVE_AUDIO_VOICE


# ── Session factory ──────────────────────────────────────────────────────────

def _google_cloud_credentials_kwargs() -> dict:
    """Credentials for Google Cloud STT/TTS (NOT Gemini Live — that uses GOOGLE_API_KEY).

    Azure Container Apps: prefer inline JSON secret GOOGLE_APPLICATION_CREDENTIALS_JSON.
    Local/dev: GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_APPLICATION_CREDENTIALS_PATH file path.
    """
    raw_json = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS_JSON is set but is not valid JSON. "
                "Paste the full GCP service-account key JSON (minified one-line is fine)."
            ) from exc
        if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS_JSON must be a GCP service-account key "
                "object including at least client_email and private_key."
            )
        return {"credentials_info": info}

    path = (
        (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_PATH") or "").strip()
        or (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    )
    if path:
        if not os.path.isfile(path):
            raise RuntimeError(
                f"Google credentials file not found: {path}. "
                "On Azure set GOOGLE_APPLICATION_CREDENTIALS_JSON instead of a file path."
            )
        return {"credentials_file": path}

    raise RuntimeError(
        "Pipeline mode needs GCP credentials for Google Cloud STT/TTS "
        "(GOOGLE_API_KEY alone is not enough). Set GOOGLE_APPLICATION_CREDENTIALS_JSON "
        "to the service-account key JSON in Azure Library, or set "
        "GOOGLE_APPLICATION_CREDENTIALS to a local key file path. "
        "Enable Cloud Speech-to-Text + Cloud Text-to-Speech APIs on that project. "
        "Optional: DEEPGRAM_API_KEY for STT only (TTS still needs the GCP key)."
    )


def _build_mercury_llm():
    """OpenAI-compatible client pointed at Inception Mercury."""
    if _openai_llm is None:
        raise RuntimeError("Cascade voice needs livekit-plugins-openai")
    api_key = (config.get_env("INCEPTION_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Cascade voice needs INCEPTION_API_KEY")
    model = (config.get_env("INCEPTION_MODEL") or "mercury-2.5").strip()
    base_url = (config.get_env("INCEPTION_BASE_URL") or _INCEPTION_BASE_URL).strip()
    effort = (config.get_env("INCEPTION_REASONING_EFFORT") or "low").strip()
    # Lower temperature keeps facts and scripted steps steady; a spoken turn needs few tokens,
    # and long answers delay the first audio and turn into monologues.
    temperature = float(config.get_env("INCEPTION_TEMPERATURE") or "0.5")
    max_tokens = int(config.get_env("INCEPTION_MAX_COMPLETION_TOKENS") or "100")
    return _openai_llm(
        model=model,
        api_key=api_key,
        base_url=base_url,
        # LiveKit's OpenAI plugin defaults to a 5s HTTP read timeout. Mercury
        # can take longer than that to stream its first token for a call prompt.
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        temperature=temperature,
        max_completion_tokens=max_tokens,
        reasoning_effort=effort,
        # Mercury voice guidance: stream the answer as soon as it is ready.
        extra_body={"realtime": True},
        _strict_tool_schema=False,
    )


def _stt_provider() -> str:
    """Speech-to-text for the cascade: Sarvam Saaras (default) or ElevenLabs Scribe v2 Realtime.

    Sarvam is the default: on the same audio its final text came ~0.45s after the lead stopped,
    ElevenLabs Scribe ~0.85s (2026-10-12).
    """
    provider = (config.get_env("STT_PROVIDER") or "sarvam").strip().lower()
    return "elevenlabs" if provider == "elevenlabs" else "sarvam"


def _stt_label() -> str:
    return "sarvam saaras:v3-realtime" if _stt_provider() == "sarvam" else "elevenlabs scribe_v2_realtime"


def _build_stt(call_language: str):
    if _stt_provider() == "sarvam":
        return _build_sarvam_stt(call_language, realtime=True)
    return _build_elevenlabs_stt(call_language)


def _build_elevenlabs_stt(call_language: str):
    """ElevenLabs Scribe v2 Realtime with server VAD, so it ends the lead's turn itself."""
    if _elevenlabs_stt is None:
        raise RuntimeError("Cascade voice needs livekit-plugins-elevenlabs")
    api_key = (config.get_env("ELEVEN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Cascade voice needs ELEVEN_API_KEY (or set STT_PROVIDER=sarvam)")
    english = str(call_language or "").strip().lower() == "english"
    # Silence that ends the lead's turn. ElevenLabs defaults to 1.5s; 0.3s is its fastest,
    # and still measured ~0.85s from end of speech to the final text.
    silence = _bounded_float("ELEVEN_VAD_SILENCE_SECS", 0.3, 0.3, 3.0)
    return _elevenlabs_stt(
        api_key=api_key,
        model="scribe_v2_realtime",
        language_code="en" if english else "hi",
        sample_rate=16000,
        server_vad={
            "vad_silence_threshold_secs": silence,
            "vad_threshold": 0.4,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": int(silence * 1000),
        },
    )


def _build_sarvam_stt(call_language: str, *, realtime: bool = True):
    if _sarvam_stt_realtime is None:
        raise RuntimeError("Cascade voice needs livekit-plugins-sarvam")
    api_key = (config.get_env("SARVAM_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Cascade voice needs SARVAM_API_KEY")
    language = (config.get_env("SARVAM_STT_LANGUAGE") or "").strip() or _voice_locale(call_language)
    mode = _stt_mode(call_language)
    if realtime and _sarvam_stt_realtime is not None:
        return _sarvam_stt_realtime(
            language=language,
            stream_type="fast",
            mode=mode,
            endpointing="vad",
            sample_rate=16000,
            api_key=api_key,
            # 200ms ended turns at natural Hinglish pauses ("haan… actually…") and the
            # agent talked over the lead; 300ms keeps replies inside the latency budget.
            vad_min_silence_ms=int(_bounded_float("SARVAM_VAD_MIN_SILENCE_MS", 220, 150, 1000)),
        )
    if _sarvam_stt is None:
        raise RuntimeError("Sarvam STT plugin is unavailable")
    return _sarvam_stt(
        language=language,
        model="saaras:v3",
        mode=mode,
        sample_rate=16000,
        high_vad_sensitivity=True,
        api_key=api_key,
    )


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    raw = (config.get_env(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r — expected a number", name, raw)
        return default
    if value < low or value > high:
        logger.warning("Clamping %s=%s into %.2f–%.2f", name, value, low, high)
        return min(high, max(low, value))
    return value


def _build_tts(call_language: str):
    """Call TTS: Cartesia Sonic with the voice saved on the dashboard (or .env / default).

    Built before dialing, so a missing key stops the job before the lead's phone rings
    instead of placing a call the agent cannot speak on.
    """
    if _cartesia_tts is None:
        raise RuntimeError("Cascade voice needs livekit-plugins-cartesia")
    api_key = (config.get_env("CARTESIA_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Cascade voice needs CARTESIA_API_KEY")
    language = _voice_locale(call_language)
    # Read per call: a voice saved on the dashboard applies from the next call, no restart.
    voice, source = voice_settings.load()
    logger.info("TTS voice: %s (from %s)", voice_settings.describe(voice), source)
    return _cartesia_tts(**voice_settings.cartesia_kwargs(voice, language, api_key))


# Latency budget (lead stops -> first audio, target 1.3–1.5s): end of turn ~0.5s
# (Sarvam VAD silence + endpointing) + Mercury first token ~0.2–0.4s + Cartesia first audio
# ~0.1–0.2s + network/telephony ~0.15s. Preemptive generation overlaps the LLM with the
# end of the turn. Tune with CASCADE_MIN_ENDPOINTING / CASCADE_MAX_ENDPOINTING and watch
# the TURN_LATENCY log lines.
def usage_event(metrics) -> Optional[dict]:
    """Real per-request usage from LiveKit metrics for the dashboard token monitor."""
    if isinstance(metrics, LLMMetrics):
        if metrics.cancelled and not metrics.total_tokens:
            return None
        return {"type": "usage", "kind": "llm", "prompt": int(metrics.prompt_tokens or 0),
                "cached": int(metrics.prompt_cached_tokens or 0),
                "completion": int(metrics.completion_tokens or 0), "total": int(metrics.total_tokens or 0)}
    if isinstance(metrics, TTSMetrics):
        if not metrics.characters_count:
            return None
        return {"type": "usage", "kind": "tts", "chars": int(metrics.characters_count),
                "audio_s": round(float(metrics.audio_duration or 0.0), 3)}
    if isinstance(metrics, STTMetrics):
        if not metrics.audio_duration:
            return None
        return {"type": "usage", "kind": "stt", "audio_s": round(float(metrics.audio_duration), 3)}
    return None


class TurnLatencyTracker:
    """Per-turn reply latency from LiveKit metrics, joined by speech_id:
    end of turn (EOU) + LLM time to first token + TTS time to first audio.

    With preemptive generation the LLM can start before the turn ends, so the sum is an
    upper bound of what the lead hears. Fixed ``say`` lines have no LLM part and are skipped.
    """

    _MAX_OPEN_TURNS = 50

    def __init__(self, report) -> None:
        self._report = report  # report(speech_id, total_seconds, {"eou", "llm", "tts"})
        self._turns: dict[str, dict[str, float]] = {}

    def on_metrics(self, metrics) -> None:
        speech_id = getattr(metrics, "speech_id", None)
        if not speech_id:
            return
        if isinstance(metrics, EOUMetrics):
            key, value = "eou", metrics.end_of_utterance_delay
            stt = metrics.transcription_delay
            if stt is not None and stt >= 0:
                self._turns.setdefault(speech_id, {}).setdefault("stt", float(stt))
        elif isinstance(metrics, LLMMetrics):
            key, value = "llm", metrics.ttft
        elif isinstance(metrics, TTSMetrics):
            key, value = "tts", metrics.ttfb
        else:
            return
        if value is None or value < 0:  # cancelled or unmeasured
            return
        parts = self._turns.setdefault(speech_id, {})
        parts.setdefault(key, float(value))
        if {"eou", "llm", "tts"} <= parts.keys():
            del self._turns[speech_id]
            self._report(speech_id, parts["eou"] + parts["llm"] + parts["tts"], parts)
        while len(self._turns) > self._MAX_OPEN_TURNS:
            self._turns.pop(next(iter(self._turns)))


_CASCADE_TURN_HANDLING: TurnHandlingOptions = {
    "endpointing": {
        "min_delay": _bounded_float("CASCADE_MIN_ENDPOINTING", 0.1, 0.0, 2.0),
        "max_delay": _bounded_float("CASCADE_MAX_ENDPOINTING", 0.6, 0.1, 3.0),
    },
    "interruption": {
        "enabled": True,
        "mode": "adaptive",
        "min_duration": 0.5,
        "false_interruption_timeout": 1.0,
        "resume_false_interruption": True,
        "discard_audio_if_uninterruptible": True,
    },
    "preemptive_generation": {"enabled": True},
}


def _build_cascade_session(tools: list, *, call_language: str) -> AgentSession:
    """Sarvam Saaras (or ElevenLabs Scribe) STT → Mercury 2.5 → Cartesia Sonic. The STT's VAD ends each turn."""
    language = _voice_locale(call_language)
    logger.info(
        "SESSION MODE: cascade (stt=%s %s, llm=%s, tts=cartesia, see TTS voice line)",
        _stt_label(),
        language,
        (config.get_env("INCEPTION_MODEL") or "mercury-2.5").strip(),
    )
    return AgentSession(
        stt=_build_stt(call_language),
        llm=_build_mercury_llm(),
        tts=_build_tts(call_language),
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(max_retry=1, retry_interval=0.5, timeout=30.0),
        ),
        tools=tools,
        turn_detection="stt",
        turn_handling=_CASCADE_TURN_HANDLING,
        aec_warmup_duration=_AEC_WARMUP_SECONDS,
    )


def _build_pipeline_session(
    tools: list,
    system_prompt: str,
    gemini_voice: str,
    *,
    api_key: Optional[str] = None,
) -> AgentSession:
    """STT + LLM + TTS — avoids Gemini Live AUDIO modality (no 1007 CONTENT_TYPE_AUDIO)."""
    if _google_llm is None or _google_tts is None:
        raise RuntimeError("Google LLM/TTS required for pipeline mode")

    # Fail fast with a clear message before STT/TTS constructors hit ADC.
    gcp_creds = _google_cloud_credentials_kwargs()

    if _deepgram_stt is not None and config.get_env("DEEPGRAM_API_KEY"):
        stt = _deepgram_stt(model="nova-3", language="multi")
        logger.info("PIPELINE STT: Deepgram nova-3")
    elif _google_stt is not None:
        stt = _google_stt(
            languages=["hi-IN", "en-IN"],
            detect_language=True,
            **gcp_creds,
        )
        logger.info("PIPELINE STT: Google Cloud STT (hi-IN/en-IN)")
    else:
        raise RuntimeError("Need DEEPGRAM_API_KEY or livekit-plugins-google STT for pipeline mode")

    # Cloud TTS voice names differ from Gemini Live voices (Leda/Puck). Prefer Cloud Neural2.
    tts_voice = gemini_voice
    if tts_voice in ("Leda", "Puck", "Kore", "Aoede", "Charon", "Fenrir", "Orus"):
        tts_voice = "hi-IN-Neural2-A"
        logger.info(
            "PIPELINE TTS: mapping Gemini Live voice %r → Cloud TTS %s",
            gemini_voice,
            tts_voice,
        )
    try:
        tts = _google_tts(voice_name=tts_voice, language="hi-IN", **gcp_creds)
    except TypeError:
        tts = _google_tts(**gcp_creds)

    llm_kwargs: dict = {"model": "gemini-2.0-flash"}
    if api_key:
        llm_kwargs["api_key"] = api_key
    try:
        llm = _google_llm(**llm_kwargs)
    except TypeError:
        llm = _google_llm(model="gemini-2.0-flash")

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=silero.VAD.load(),
        tools=tools,
        turn_handling=_PIPELINE_TURN_HANDLING,
        aec_warmup_duration=_AEC_WARMUP_SECONDS,
    )


def _build_session(
    tools: list,
    system_prompt: str,
    *,
    voice: Optional[str] = None,
    model_override: Optional[str] = None,
    api_key: Optional[str] = None,
    call_language: str = "hinglish",
) -> AgentSession:
    """Build AgentSession with the premium cascade, Gemini Live (native audio) or pipeline fallback."""
    if _cascade_enabled():
        return _build_cascade_session(tools, call_language=call_language)
    gemini_model = _resolve_gemini_model(model_override)
    gemini_voice = _resolve_gemini_voice(voice)
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() == "true"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    # Pipeline mode: no Gemini Live WebSocket → no 1007; needs GCP STT/TTS billing.
    if not use_realtime:
        logger.info("SESSION MODE: pipeline (USE_GEMINI_REALTIME=false)")
        return _build_pipeline_session(tools, system_prompt, gemini_voice, api_key=api_key)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        from google.genai import types as _gt

        # Live captions need Gemini input/output audio transcription. Passing None
        # disables user_input_transcribed / agent text events → empty live_transcript.
        # Kill-switch: ENABLE_LIVE_TRANSCRIPTION=false (only if mid-call 1007 returns).
        live_tx_on = os.getenv("ENABLE_LIVE_TRANSCRIPTION", "true").lower() not in (
            "0", "false", "no", "off",
        )
        tx_cfg = _gt.AudioTranscriptionConfig() if live_tx_on else None
        realtime_kwargs: dict = dict(
            model=gemini_model,
            voice=gemini_voice,
            instructions=system_prompt,
            # Native AUDIO only (NOT Modality.TEXT / half-cascade). AI Studio path.
            # Gemini 3.8 Live supports the public generate_reply greeting path.
            modalities=[_gt.Modality.AUDIO],
            input_audio_transcription=tx_cfg,
            output_audio_transcription=tx_cfg,
            # Gemini is the sole turn-detection owner for realtime calls.
            realtime_input_config=_gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    disabled=False,
                    # HIGH picks up short opening replies ("haan", "ji") faster.
                    # LOW was leaving a long pause after the identity check.
                    start_of_speech_sensitivity=(
                        _gt.StartSensitivity.START_SENSITIVITY_HIGH
                    ),
                    end_of_speech_sensitivity=(
                        _gt.EndSensitivity.END_SENSITIVITY_HIGH
                    ),
                    prefix_padding_ms=40,
                    # Short endpointing so opening affirmatives commit quickly.
                    silence_duration_ms=_GEMINI_SILENCE_MS,
                ),
                # Stop current agent audio when the caller begins speaking so
                # Gemini keeps listening naturally throughout the conversation.
                activity_handling=_gt.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turn_coverage=_gt.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            ),
            conn_options=APIConnectOptions(max_retry=5, retry_interval=1.0, timeout=15.0),
        )
        if api_key:
            realtime_kwargs["api_key"] = api_key
        thinking = "default"
        if (
            _GEMINI_THINKING_LEVEL
            and _GEMINI_THINKING_LEVEL != "OFF"
            and "thinking" not in gemini_model.lower()
        ):
            # Plain gemini-3.8-live rejects thinking_level and the job dies before
            # dialing — never let a stray env value take every call down.
            logger.warning(
                "Ignoring GEMINI_THINKING_LEVEL=%s — model %s does not support thinking_level",
                _GEMINI_THINKING_LEVEL,
                gemini_model,
            )
        elif _GEMINI_THINKING_LEVEL and _GEMINI_THINKING_LEVEL != "OFF":
            try:
                realtime_kwargs["thinking_config"] = _gt.ThinkingConfig(
                    thinking_level=_gt.ThinkingLevel(_GEMINI_THINKING_LEVEL)
                )
                thinking = _GEMINI_THINKING_LEVEL.lower()
            except ValueError:
                logger.warning("Ignoring unknown GEMINI_THINKING_LEVEL=%r", _GEMINI_THINKING_LEVEL)
        if _GEMINI_CONTEXT_TRIGGER_TOKENS:
            realtime_kwargs["context_window_compression"] = _gt.ContextWindowCompressionConfig(
                trigger_tokens=_GEMINI_CONTEXT_TRIGGER_TOKENS,
                sliding_window=_gt.SlidingWindow(target_tokens=_GEMINI_CONTEXT_TARGET_TOKENS),
            )

        logger.info(
            "Native-audio locked: model=%s modalities=AUDIO transcription=%s "
            "turn_detection=gemini silence_ms=%s thinking=%s context_compression=%s "
            "prompt_chars=%d retries=5 aec_warmup=%.1fs key_bound=%s",
            gemini_model,
            "ON" if live_tx_on else "OFF",
            _GEMINI_SILENCE_MS,
            thinking,
            _GEMINI_CONTEXT_TRIGGER_TOKENS or "off",
            len(system_prompt or ""),
            _AEC_WARMUP_SECONDS,
            bool(api_key),
        )

        return AgentSession(
            llm=RealtimeClass(**realtime_kwargs),
            tools=tools,
            aec_warmup_duration=_AEC_WARMUP_SECONDS,
        )

    logger.warning("Realtime unavailable — falling back to pipeline session")
    return _build_pipeline_session(tools, system_prompt, gemini_voice, api_key=api_key)


_AFFIRMATIVE_IDENTITY_WORDS = {
    "haan", "han", "ha", "yes", "ji", "speaking", "correct", "right",
    "हाँ", "हां", "जी",
}
_TRUST_CONCERN_PHRASES = (
    "number kaha", "number kahan", "number kahaan", "data kaha", "data kahan",
    "information kaha", "information kahan", "query kaha", "query kahan",
    "source", "data theft", "data chori", "chori", "privacy", "consent",
    "call kyu", "call kyun", "phone kyu", "phone kyun", "repeat call",
    "another call", "dusri call", "doosri call", "previous call", "pichli call",
    "कहां से", "कहाँ से", "नंबर कहां", "नंबर कहाँ", "डेटा चोरी", "दूसरी कॉल",
)


def _spoken_lead_name(lead_name: str) -> str:
    """Compatibility wrapper — canonical implementation lives in prompting.speech."""
    return spoken_lead_name(lead_name)


def _normalized_turn_text(message) -> str:
    text = getattr(message, "text_content", None) or ""
    if not text and hasattr(message, "content"):
        parts = []
        for part in getattr(message, "content", None) or []:
            part_text = getattr(part, "text", None) or getattr(part, "transcript", None)
            if part_text:
                parts.append(str(part_text))
        text = " ".join(parts)
    return " ".join(str(text).lower().strip().split())


def _has_trust_concern(text: str) -> bool:
    return any(phrase in text for phrase in _TRUST_CONCERN_PHRASES)


def _is_explicit_affirmative(text: str) -> bool:
    """Conservative consent check: a short, clear yes—not a question or concern."""
    if not text or _has_trust_concern(text) or "?" in text:
        return False
    words = text.replace(",", " ").replace(".", " ").split()
    if len(words) > 7:
        return False
    if any(word in _AFFIRMATIVE_IDENTITY_WORDS for word in words):
        return True
    return any(
        phrase in text
        for phrase in ("main hi", "mai hi", "bol raha", "bol rahi", "i am", "that's me")
    )


def _build_permission_intro(
    *,
    lead_name: str,
    business_name: str,
    lead_source: Optional[str],
    language: str = "hinglish",
    agent_name: str = "Riya",
) -> str:
    """Compatibility wrapper — canonical implementation lives in prompting.speech."""
    return build_permission_intro(
        lead_name=lead_name,
        business_name=business_name,
        lead_source=lead_source,
        language=language,
        agent_name=agent_name,
    )


# Spoken instantly (Cartesia, no LLM) when the lead answers the reconnect check with a yes;
# Mercury's continuation follows right after it.
_RECONNECT_BRIDGE = "Ji, shukriya."

_BREVITY_REMINDER = (
    "REPLY LENGTH (strict): at most 2 short sentences, about 25 words, then stop and let the "
    "lead speak. One fact or one question per turn; continue on the next turn if needed."
)


class OutboundAssistant(Agent):
    def __init__(
        self,
        instructions: str,
        *,
        lead_name: str,
        business_name: str,
        lead_source: Optional[str],
        enforce_fresh_call_gate: bool,
        call_language: str = "hinglish",
        additional_instructions: Optional[str] = None,
        agent_name: str = "Riya",
        reconnect: bool = False,
    ) -> None:
        super().__init__(instructions=instructions)
        # Opening fast path (cascade): the first reply after the greeting is spoken from the
        # final STT text, without waiting for turn-end or the LLM. See speak_opening_early().
        self._reconnect_pending = bool(reconnect)
        self._early_reply_pending = False
        self._conversation_gate = "identity" if enforce_fresh_call_gate else "ready"
        self._call_language = call_language
        # Mercury (OpenAI-compatible) rejects role="developer" with HTTP 400 — the call goes
        # silent. Gemini Live accepts it, so keep "developer" there.
        self._instruction_role = "system" if _cascade_enabled() else "developer"
        self._agent_name = (agent_name or "Riya").strip() or "Riya"
        self._campaign_permission = additional_instructions_has_permission(additional_instructions)
        self._permission_intro = _build_permission_intro(
            lead_name=lead_name,
            business_name=business_name,
            lead_source=lead_source,
            language=call_language,
            agent_name=self._agent_name,
        )
        self._permission_audio: Optional[list] = None
        self._permission_audio_task: Optional[asyncio.Task] = None
        self._greeting_text: Optional[str] = None
        self._greeting_audio: Optional[list] = None
        self._greeting_audio_task: Optional[asyncio.Task] = None

    def prefetch_greeting_audio(self, text: str, *, voice: str, api_key: Optional[str]) -> None:
        """Render the exact opening line while the phone rings so it plays on answer."""
        if not _GREETING_AUDIO_PREFETCH or _gemini_tts is None or not text:
            return
        self._greeting_text = text

        async def _render() -> None:
            self._greeting_audio = await self._render_line_audio(
                text, voice=voice, api_key=api_key, label="Greeting"
            )

        self._greeting_audio_task = asyncio.create_task(_render())

    def greeting_audio_for(self, text: str) -> Optional[list]:
        """Pre-rendered frames for ``text`` if ready now; None means generate live."""
        if self._greeting_audio and self._greeting_text == text:
            return self._greeting_audio
        return None

    def prefetch_permission_audio(self, *, voice: str, api_key: Optional[str]) -> None:
        """Start rendering the default permission line so a realtime call can play it verbatim."""
        if (
            not _PERMISSION_AUDIO_PREFETCH
            or _gemini_tts is None
            or self._conversation_gate != "identity"
            or self._campaign_permission
        ):
            return
        self._permission_audio_task = asyncio.create_task(
            self._render_permission_audio(voice=voice, api_key=api_key)
        )

    async def _render_permission_audio(self, *, voice: str, api_key: Optional[str]) -> None:
        self._permission_audio = await self._render_line_audio(
            self._permission_intro, voice=voice, api_key=api_key, label="Permission line"
        )

    async def _render_line_audio(
        self, text: str, *, voice: str, api_key: Optional[str], label: str
    ) -> Optional[list]:
        """Gemini TTS ``text`` in the call voice; None on failure (caller falls back to LLM)."""
        tts = None
        t0 = time.time()
        try:
            kwargs: dict = {"voice_name": voice, "instructions": _PERMISSION_TTS_STYLE}
            if api_key:
                kwargs["api_key"] = api_key
            tts = _gemini_tts(**kwargs)

            async def _collect() -> list:
                frames = []
                async with tts.synthesize(text) as stream:
                    async for chunk in stream:
                        frames.append(chunk.frame)
                return frames

            frames = await asyncio.wait_for(_collect(), timeout=_PERMISSION_AUDIO_TIMEOUT_SEC)
            if frames:
                await _log(
                    "info",
                    f"{label} pre-rendered ({len(frames)} frames, voice={voice}, "
                    f"{time.time() - t0:.2f}s)",
                )
                return frames
        except Exception as exc:
            await _log("warning", f"{label} pre-render failed (LLM fallback): {exc}")
        finally:
            if tts is not None:
                try:
                    await tts.aclose()
                except Exception:
                    pass
        return None

    def _play_permission_audio(self) -> None:
        """Play the pre-rendered permission line, then tell Gemini Live it was spoken."""
        handle = self.play_pre_rendered(self._permission_intro, self._permission_audio)
        asyncio.create_task(
            self.sync_spoken_line(handle, self._permission_intro, label="Permission line")
        )

    def play_pre_rendered(self, text: str, frames: list):
        """Play pre-rendered frames for ``text`` through the session; returns the handle."""

        async def _replay():
            for frame in frames:
                yield frame

        return self.session.say(text, audio=_replay(), allow_interruptions=True)

    async def sync_spoken_line(self, handle, text: str, *, label: str) -> None:
        """After playout, add ``text`` to Gemini Live history.

        say(audio=...) bypasses Gemini, so without this the model does not know the
        line was spoken (it would repeat the greeting / permission ask).
        """
        try:
            await handle.wait_for_playout()
            chat_ctx = self.chat_ctx.copy()
            last = next(
                (m for m in reversed(chat_ctx.messages()) if m.role == "assistant"), None
            )
            if last is None or text not in (last.text_content or ""):
                chat_ctx.add_message(role="assistant", content=text)
            await self.update_chat_ctx(chat_ctx)
        except Exception as exc:
            await _log("warning", f"{label} context sync failed: {exc}")

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        await self._apply_conversation_gate(turn_ctx, new_message)
        if _cascade_enabled() and self._conversation_gate == "ready":
            # Mercury returns the whole reply at once, so reply length is reply latency.
            # A reminder right before generation holds better than one rule deep in the prompt.
            # Not during identity/permission: those turns may require an exact longer sentence.
            turn_ctx.add_message(role=self._instruction_role, content=_BREVITY_REMINDER)

    def _speaking_session(self):
        try:
            session = self.session
        except RuntimeError:
            return None
        return session if getattr(session, "tts", None) is not None else None

    def speak_opening_early(self, transcript: str) -> bool:
        """Answer the lead's first reply the moment the final transcript arrives.

        Saves the turn-end wait (~0.5-0.8s) and, for the fixed lines, the LLM (~1s):
        identity yes -> permission line; reconnect yes -> short bridge, then the LLM continues.
        Returns True when it spoke.
        """
        if not _cascade_enabled():
            return False
        session = self._speaking_session()
        text = " ".join(str(transcript or "").lower().strip().split())
        if session is None or not _is_explicit_affirmative(text):
            return False
        if self._conversation_gate == "identity" and not self._campaign_permission:
            self._conversation_gate = "permission"
            self._early_reply_pending = True  # the matching turn must not also reach the LLM
            session.say(self._permission_intro, allow_interruptions=True)
            return True
        if self._reconnect_pending:
            self._reconnect_pending = False
            session.say(_RECONNECT_BRIDGE, allow_interruptions=True)
            return True
        return False

    async def _apply_conversation_gate(self, turn_ctx, new_message) -> None:
        """Enforce identity → introduction/permission → project flow before each LLM reply.

        On identity yes: speak the permission line immediately via generate_reply and
        raise StopResponse so we do not wait on a second slow default generation.
        """
        text = _normalized_turn_text(new_message)

        if self._early_reply_pending:
            # speak_opening_early() already answered this turn with the fixed line.
            self._early_reply_pending = False
            raise StopResponse()

        if self._reconnect_pending and _is_explicit_affirmative(text):
            # Final transcript did not arrive first: bridge now, then let the LLM continue.
            self._reconnect_pending = False
            session = self._speaking_session()
            if session is not None:
                session.say(_RECONNECT_BRIDGE, allow_interruptions=True)
                turn_ctx.add_message(
                    role=self._instruction_role,
                    content=f'You just said "{_RECONNECT_BRIDGE}" — do not thank again; continue directly.',
                )
            return
        if self._conversation_gate == "identity":
            if _is_explicit_affirmative(text):
                self._conversation_gate = "permission"
                # Immediate permission kickoff — avoids the long haan→permission dead air.
                if self._campaign_permission:
                    self.session.generate_reply(
                        instructions=(
                            "RUNTIME CONVERSATION GATE: Identity is now confirmed. Campaign notes "
                            "customize the permission/intro — follow those notes for this turn "
                            f"(language={self._call_language}). Still no project name or pitch. "
                            "Then STOP and wait. "
                            f'Default fallback if notes are unclear: "{self._permission_intro}"'
                        ),
                        allow_interruptions=True,
                    )
                elif getattr(self.session, "tts", None) is not None:
                    # Default line is spoken as fixed text via TTS, so the LLM cannot
                    # paraphrase it into its own time-check question.
                    self.session.say(self._permission_intro, allow_interruptions=True)
                elif self._permission_audio:
                    # Gemini Live: play the line pre-rendered with the same voice.
                    self._play_permission_audio()
                else:
                    # Pre-render not ready or failed: ask the LLM for the exact line.
                    self.session.generate_reply(
                        instructions=(
                            "RUNTIME CONVERSATION GATE: Identity is now confirmed. Speak EXACTLY "
                            "the quoted default permission line below — word-for-word, no "
                            "paraphrasing, no extra sentences, no project name or pitch — then "
                            f"STOP and wait. Keep call language={self._call_language}. "
                            f"{indian_accent_runtime_reminder(self._agent_name)} "
                            f'Exact line: "{self._permission_intro}"'
                        ),
                        allow_interruptions=True,
                    )
                raise StopResponse()
            if _has_trust_concern(text):
                turn_ctx.add_message(
                    role=self._instruction_role,
                    content=(
                        "RUNTIME CONVERSATION GATE: Identity is not yet confirmed and the lead raised "
                        "a trust/privacy/duplicate-call concern. Address that concern honestly using "
                        "LEAD DATA PROVENANCE, do not mention any project, then ask once whether this "
                        "is the named lead and STOP."
                    ),
                )
                return
            turn_ctx.add_message(
                role=self._instruction_role,
                content=(
                    "RUNTIME CONVERSATION GATE: Identity is not clearly confirmed. Do not introduce "
                    "a project or treat this as permission. Briefly clarify and ask the identity "
                    "confirmation again, then STOP."
                ),
            )
            return

        if self._conversation_gate == "permission":
            if _has_trust_concern(text):
                turn_ctx.add_message(
                    role=self._instruction_role,
                    content=(
                        "RUNTIME CONVERSATION GATE: The lead raised a trust/privacy/duplicate-call "
                        "concern before granting sales permission. Pause sales, answer the concern "
                        "honestly, do not name any project, and ask whether they want to continue."
                    ),
                )
            elif _is_explicit_affirmative(text):
                self._conversation_gate = "ready"
                turn_ctx.add_message(
                    role=self._instruction_role,
                    content=(
                        "RUNTIME CONVERSATION GATE: Explicit permission to discuss property is now "
                        "confirmed. Continue warmly and naturally (HUMAN FIRST): listen, mirror "
                        "briefly, then help — do not dump a brochure or rush a checklist."
                    ),
                )
            else:
                turn_ctx.add_message(
                    role=self._instruction_role,
                    content=(
                        "RUNTIME CONVERSATION GATE: Explicit permission to discuss property has NOT "
                        "been confirmed. Do not name, describe, or recommend a project. Handle a clear "
                        "busy/refusal normally; otherwise clarify the permission question and STOP."
                    ),
                )


async def entrypoint(ctx: agents.JobContext) -> None:
    """Lease a Gemini API key for this job, run the call, always release the key."""
    from key_pool import KeyPoolExhausted, assign_job_key, redact_secrets
    from db import finalize_call, update_call_gemini_usage

    call_id_early: Optional[str] = None
    preferred_slot: str = ""
    client_gemini_key: Optional[str] = None
    dispatched_at: Optional[float] = None
    if ctx.job.metadata:
        try:
            meta_early = json.loads(ctx.job.metadata)
            call_id_early = meta_early.get("call_id")
            preferred_slot = (meta_early.get("gemini_key_slot") or "").strip()
            client_gemini_key = (meta_early.get("gemini_api_key") or "").strip() or None
            raw_dispatched = meta_early.get("dispatched_at")
            dispatched_at = float(raw_dispatched) if raw_dispatched else None
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            call_id_early = None

    room_name_early = ctx.room.name if ctx.room else None
    if dispatched_at is not None:
        job_age = time.time() - dispatched_at
        if job_age > _DISPATCH_MAX_AGE_SEC:
            await _log(
                "error",
                f"STALE_JOB room={room_name_early} age={job_age:.0f}s > {_DISPATCH_MAX_AGE_SEC:.0f}s — "
                "not dialing (no worker was available when the call was dispatched)",
            )
            await _fail_call_before_dial(
                call_id_early,
                f"stale dispatch: worker picked up job {job_age:.0f}s late — not dialed",
                room_name_early,
            )
            ctx.shutdown()
            return

    lease = None
    tool_ctx_ref: list = []
    try:
        if _cascade_enabled() and not client_gemini_key:
            # Premium cascade speaks with Sarvam + Mercury + Cartesia: no Gemini key needed,
            # so an empty Gemini pool must not fail the call.
            await _log("info", f"Premium cascade — no Gemini key needed for call_id={call_id_early or '-'}")
            await _run_outbound_job(ctx, api_key="", key_slot="cascade", tool_ctx_ref=tool_ctx_ref)
            return
        if client_gemini_key:
            from key_pool import create_client_lease
            lease = create_client_lease(client_gemini_key, call_id=call_id_early)
        else:
            # Fallback to pool if client key not provided
            lease = assign_job_key(call_id_early or "", preferred_slot=preferred_slot)
        if call_id_early:
            try:
                await update_call_gemini_usage(call_id_early, key_slot=lease.slot_id)
            except Exception:
                pass
        await _log("info", f"Gemini key leased slot={lease.slot_id} for call_id={call_id_early or '-'}")
        await _run_outbound_job(
            ctx,
            api_key=lease.api_key,
            key_slot=lease.slot_id,
            tool_ctx_ref=tool_ctx_ref,
        )
    except KeyPoolExhausted as exc:
        await _log("error", f"Gemini key pool exhausted: {exc}")
        if call_id_early:
            try:
                await finalize_call(
                    call_id_early,
                    outcome="failed",
                    reason=f"gemini_key_pool_exhausted: {exc}",
                    duration_seconds=0,
                    room_name=ctx.room.name if ctx.room else None,
                )
            except Exception:
                pass
        ctx.shutdown()
    except Exception as exc:
        await _log("error", f"Job failed: {redact_secrets(str(exc))}")
        tool_ctx = tool_ctx_ref[0] if tool_ctx_ref else None
        if not getattr(tool_ctx, "sip_dial_started", False):
            await _fail_call_before_dial(
                call_id_early,
                f"agent job failed before dialing: {redact_secrets(str(exc))}",
                room_name_early,
            )
        raise
    finally:
        # Persist token estimate (transcript-based; Gemini Live rarely exposes usage).
        if call_id_early:
            try:
                from db import estimate_gemini_tokens
                tin = tout = total = 0
                duration = 0
                transcript_json = None
                live_transcript = None
                if tool_ctx_ref:
                    tool_ctx = tool_ctx_ref[0]
                    tracker = getattr(tool_ctx, "transcript_tracker", None)
                    duration = int(time.time() - getattr(tool_ctx, "_call_start_time", time.time()))
                    if tracker is not None:
                        try:
                            await tracker.flush()
                        except Exception:
                            pass
                        transcript_json = tracker.json_text()
                        live_transcript = tracker.live_text()
                tin, tout, total = estimate_gemini_tokens(
                    transcript_json=transcript_json,
                    live_transcript=live_transcript,
                    duration_seconds=duration,
                )
                if total > 0 or (lease and lease.slot_id):
                    await update_call_gemini_usage(
                        call_id_early,
                        key_slot=lease.slot_id if lease else None,
                        tokens_in=tin if total > 0 else None,
                        tokens_out=tout if total > 0 else None,
                        tokens_total=total if total > 0 else None,
                    )
                    await _log(
                        "info",
                        f"Gemini tokens saved call_id={call_id_early} in={tin} out={tout} total={total}",
                    )
            except Exception as tok_exc:
                await _log("warning", f"Token estimate save failed: {tok_exc}")
        # Hash-assigned keys are not held in a shared pool — nothing to release.


async def _run_outbound_job(
    ctx: agents.JobContext,
    *,
    api_key: str,
    key_slot: str,
    tool_ctx_ref: list,
) -> None:
    """
    Main outbound job. Reads metadata JSON from ctx.job.metadata.

    LATENCY PATTERN:
    1. Build the FINAL prompt (script + CRM) before warm-up — loaded into Gemini Live ONCE.
    2. Warm up the session during ring so it's ready the instant the lead answers.
    3. Greet immediately; no mid-call prompt swap, so no gap when the lead replies.
    """
    _agent_name = config.livekit_agent_name()
    # Confirm THIS worker (local test vs prod) accepted the LiveKit job — not the other pool.
    await _log(
        "info",
        f"JOB ACCEPTED by local/registered agent_name={_agent_name} room={ctx.room.name} key_slot={key_slot}",
    )
    await _log("info", f"Job started — room: {ctx.room.name}")

    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt: Optional[str] = None
    additional_instructions: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None
    organization_id: Optional[str] = None
    lead_id: Optional[str] = None
    call_id: Optional[str] = None
    campaign_id: Optional[str] = None
    project_id: Optional[str] = None
    project_details: Optional[str] = None
    projects: Optional[list] = None
    lead_source: Optional[str] = None
    lead_source_date: Optional[str] = None
    preferred_from_number: Optional[str] = None
    trunk_id_override: Optional[str] = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number   = data.get("phone_number")
            lead_name      = data.get("lead_name", lead_name)
            business_name  = data.get("business_name", business_name)
            service_type   = data.get("service_type", service_type)
            custom_prompt  = data.get("system_prompt")
            additional_instructions = (
                data.get("additional_instructions")
                or data.get("additionalInstructions")
            )
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
            tools_override = data.get("tools_override")
            organization_id = data.get("organization_id")
            lead_id = data.get("lead_id")
            call_id = data.get("call_id")
            campaign_id = data.get("campaign_id") or data.get("campaignId")
            project_id = data.get("project_id")
            project_details = data.get("project_details") or data.get("projectDetails")
            projects = data.get("projects")
            lead_source = (
                data.get("lead_source")
                or data.get("leadSource")
                or data.get("enquiry_source")
                or data.get("enquirySource")
            )
            lead_source_date = (
                data.get("lead_source_date")
                or data.get("leadSourceDate")
                or data.get("enquiry_date")
                or data.get("enquiryDate")
            )
            trunk_id_override = data.get("trunk_id")
            preferred_from_number = (
                data.get("from_number")
                or data.get("sip_number")
                or data.get("caller_id")
                or data.get("calling_number")
            )
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    is_campaign_call = bool(campaign_id) or ctx.room.name.startswith("camp-")

    if phone_number and (not organization_id or not lead_id):
        await _log(
            "error",
            "Call rejected: organization_id and lead_id are required for tenant-safe CRM context",
        )
        if call_id:
            try:
                from db import finalize_call
                await finalize_call(
                    call_id,
                    outcome="failed",
                    reason="Missing organization_id or lead_id",
                    duration_seconds=0,
                )
            except Exception:
                pass
        ctx.shutdown()
        return

    normalized_projects = normalize_projects(
        projects=projects if isinstance(projects, list) else None,
        project_id=project_id,
        project_details=project_details,
    )
    if not normalized_projects and project_id:
        normalized_projects = [{
            "project_id": str(project_id).strip(),
            "project_details": "",
            "additional_instructions": "",
        }]
    if normalized_projects:
        try:
            normalized_projects = await resolve_organization_projects(
                str(organization_id or ""),
                normalized_projects,
                register_missing=False,
            )
            if call_id:
                await bind_call_projects(call_id, str(organization_id or ""), normalized_projects)
        except ProjectScopeError as exc:
            await _log("error", f"Project scope validation failed: {exc}")
            if call_id:
                try:
                    from db import finalize_call
                    await finalize_call(
                        call_id,
                        outcome="failed",
                        reason=f"Project scope validation failed: {exc}",
                        duration_seconds=0,
                    )
                except Exception:
                    pass
            ctx.shutdown()
            return
        except Exception as exc:
            await _log("error", f"Project ownership lookup failed: {exc}")
            await _fail_call_before_dial(
                call_id, f"Project ownership lookup failed: {exc}", ctx.room.name
            )
            ctx.shutdown()
            return
    if len(normalized_projects) >= 2:
        projects = normalized_projects
        project_details = None
        project_id = normalized_projects[0].get("project_id") or project_id
    elif len(normalized_projects) == 1:
        # Preserve the normalized item so verified per-project notes reach the prompt.
        projects = normalized_projects
        project_details = normalized_projects[0]["project_details"]
        project_id = normalized_projects[0].get("project_id") or project_id
    else:
        projects = None

    _proj_count = len(projects) if isinstance(projects, list) else (1 if (project_details or "").strip() else 0)
    if isinstance(projects, list) and len(projects) >= 2:
        await _log("info", f"Prompt mode=MULTI-PROJECT ({len(projects)} projects, primary_id={project_id or 'none'})")
    elif (project_details or "").strip():
        await _log(
            "info",
            f"Prompt mode=SINGLE-PROJECT ({len((project_details or '').strip())} chars, id={project_id or 'none'})",
        )
    else:
        await _log("info", "Prompt mode=DEFAULT (no project details in metadata)")

    await _log("info", f"Call job received — phone={phone_number} lead={lead_name} biz={business_name} projects={_proj_count}")

    base_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        custom_prompt=custom_prompt,
        project_details=project_details,
        project_id=project_id,
        projects=projects if isinstance(projects, list) else None,
        additional_instructions=(additional_instructions or "").strip() or None,
        lead_source=(str(lead_source).strip() if lead_source else None),
        lead_source_date=(str(lead_source_date).strip() if lead_source_date else None),
    )
    call_language = resolve_call_language(additional_instructions)
    agent_name = resolve_agent_name(additional_instructions)
    await _log("info", f"Call language mode={call_language} agent_name={agent_name}")
    allowed_project_ids = [
        str(project.get("project_id") or "").strip()
        for project in normalized_projects
        if str(project.get("project_id") or "").strip()
    ]
    tool_ctx = AppointmentTools(
        ctx,
        phone_number,
        lead_name,
        organization_id=organization_id,
        lead_id=lead_id,
        project_id=project_id,
        allowed_project_ids=allowed_project_ids,
        call_language=call_language,
    )
    tool_ctx_ref.append(tool_ctx)
    if call_id:
        tool_ctx.call_id = call_id

    # Per-job voice/model overrides — pass into session builders only (never mutate os.environ).
    gemini_model = _resolve_gemini_model(model_override)
    gemini_voice = _resolve_gemini_voice(voice_override)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LATENCY-CRITICAL SECTION — DO NOT ADD BLOCKING AWAITS HERE
    #
    # ARCHITECTURE INVARIANT (hardcoded — survives feature additions):
    #   session_warmup must be a RUNNING task BEFORE sip.create_sip_participant.
    #   By the time the lead answers, Gemini Live is already connected and the
    #   full system prompt is loaded. generate_reply fires with ZERO prep delay.
    #
    # RULES — violating any of these re-introduces the Hello→greeting gap:
    #   1. NO await between connect+tools and session_warmup creation.
    #   2. NO update_agent / update_session after generate_reply(greeting).
    #   3. CRM must be baked INTO the prompt BEFORE warmup — never injected mid-call.
    #   4. CRM wait timeout ≤ 0.5 s here because the prefetch has been running
    #      concurrently since BEFORE ctx.connect() — it already had connect time.
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # ── Start CRM prefetch immediately (runs in background during connect+tools) ──
    prefetch_task: Optional[asyncio.Task] = None
    if phone_number:
        prefetch_task = asyncio.create_task(
            _fetch_contact_context(
                phone_number,
                organization_id=organization_id,
                lead_id=lead_id,
                exclude_call_id=call_id,
            )
        )

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
        await ctx.connect()
    else:
        tools_task = asyncio.create_task(get_enabled_tools())
        await asyncio.gather(ctx.connect(), tools_task)
        enabled_tools = tools_task.result()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[getattr(t, '__name__', str(t)) for t in active_tools]}")

    # Resolve CRM/reconnect context before session warmup. It is part of Gemini's
    # immutable prompt, and the phone has not been dialed yet.
    contact_block = ""
    is_reconnect = False
    if prefetch_task:
        try:
            # This result is part of Gemini's immutable prompt, and the phone has
            # not been dialed yet. Wait for it instead of silently converting a
            # slow reconnect lookup into a fresh call.
            contact_block, is_reconnect = await _await_prefetched_contact_context(prefetch_task)
        except _CallHistoryLookupError as exc:
            await _log("error", f"Cannot determine reconnect state before dial: {exc}")
            raise
        except Exception as exc:
            contact_block = ""
            is_reconnect = False
            await _log("warning", f"Contact context unavailable before dial: {exc}")
    full_prompt = f"{base_prompt}\n\n{contact_block}" if contact_block else base_prompt
    reconnect_opening = build_reconnect_opening(
        lead_name=lead_name,
        business_name=business_name,
        language=call_language,
        agent_name=agent_name,
    )
    if is_reconnect:
        from prompts import reconnect_override_block

        override = reconnect_override_block(lead_name=lead_name, opening_line=reconnect_opening)
        full_prompt = f"{override}\n\n{full_prompt}"
        await _log("info", "Reconnect override prepended — fresh opening/identity rules suppressed")
    await _log(
        "info",
        f"System prompt ready ({len(full_prompt)} chars, CRM={'yes' if contact_block else 'no'}, "
        f"same_number_reconnect={is_reconnect})",
    )

    # ── Build session kwargs ONCE with the FINAL prompt ───────────────────────
    # RULE 2 & 3: this is the ONLY place instructions are set. Never update after this.
    # close_on_disconnect=False: SIP ring/answer can emit brief disconnect events that would
    # otherwise close the AgentSession before greeting (seen as "AgentSession is closing").
    # We hang up ourselves when the SIP participant leaves.
    _sip_identity = f"sip_{phone_number}" if phone_number else None
    _room_input = RoomInputOptions(
        noise_cancellation=noise_cancellation.BVCTelephony(),
        close_on_disconnect=False,
    )
    if _sip_identity:
        _room_input.participant_identity = _sip_identity
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(
                instructions=full_prompt,
                lead_name=lead_name,
                business_name=business_name,
                lead_source=(str(lead_source).strip() if lead_source else None),
                enforce_fresh_call_gate=not is_reconnect,
                call_language=call_language,
                additional_instructions=additional_instructions,
                agent_name=agent_name,
                reconnect=is_reconnect,
            ),
            room_options=_RO(input_options=_room_input),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(
                instructions=full_prompt,
                lead_name=lead_name,
                business_name=business_name,
                lead_source=(str(lead_source).strip() if lead_source else None),
                enforce_fresh_call_gate=not is_reconnect,
                call_language=call_language,
                additional_instructions=additional_instructions,
                agent_name=agent_name,
                reconnect=is_reconnect,
            ),
            room_input_options=_room_input,
        )

    # Pre-build session with the FINAL prompt (loaded once, started AFTER answer).
    session = _build_session(
        tools=active_tools,
        system_prompt=full_prompt,
        voice=gemini_voice,
        model_override=model_override,
        api_key=api_key,
        call_language=call_language,
    )
    # Exact opening line, when one is known before dialing. It is rendered while the
    # phone rings so the lead hears it right on answer. None → Gemini composes it live
    # (reconnect on a campaign salutation, or notes without a quoted Greeting: line).
    _opening_line: Optional[str] = None
    if additional_instructions_has_greeting(additional_instructions):
        if not is_reconnect:
            _opening_line = build_campaign_opening_line(
                lead_name, additional_instructions, language=call_language
            )
    else:
        _opening_line = (
            reconnect_opening
            if is_reconnect
            else build_identity_opening(lead_name, language=call_language)
        )
    if getattr(session, "tts", None) is None:
        # Rendered while the phone rings, ready by the time identity is confirmed.
        _session_kwargs["agent"].prefetch_permission_audio(voice=gemini_voice, api_key=api_key)
        if _opening_line:
            _session_kwargs["agent"].prefetch_greeting_audio(
                _opening_line, voice=gemini_voice, api_key=api_key
            )

    # Attach transcript listeners before session.start and before any greeting.
    # This guarantees that the opening exchange is observable. Calls dispatched
    # by the API already have an id; direct jobs create one here before dialing.
    if phone_number:
        try:
            if not call_id:
                call_id = await create_active_call(
                    phone_number,
                    lead_name,
                    room_name=ctx.room.name,
                    organization_id=organization_id,
                    lead_id=lead_id,
                    project_id=project_id,
                )
            tool_ctx.call_id = call_id
            if normalized_projects:
                await bind_call_projects(
                    call_id,
                    str(organization_id or ""),
                    normalized_projects,
                )
            tool_ctx.transcript_tracker = CallTranscriptTracker(call_id)
            tool_ctx.requirements_collector = ClientRequirementsCollector(
                call_id,
                phone_number,
                organization_id=organization_id,
                lead_id=lead_id,
            )
            tool_ctx.transcript_tracker.on_user_transcript = tool_ctx.requirements_collector.submit
            attach_transcript_handlers(session, tool_ctx.transcript_tracker)
            await mark_transcript_capturing(call_id)
            asyncio.create_task(replay_pending_transcript_writes())
            await _log("info", f"Live transcript tracking ready before dial — call_id={call_id}")
        except Exception as _tx_exc:
            await _log("error", f"Cannot initialize transcript tracking before dial: {_tx_exc}")
            await _fail_call_before_dial(
                call_id, f"transcript tracking init failed: {_tx_exc}", ctx.room.name
            )
            ctx.shutdown()
            return

    # When the Gemini session dies (bad key, rejected config, Google outage) the
    # lead would otherwise hear silence until they hang up: the greeting fails and
    # the silence watchdog can never speak. Hang up cleanly and report the call.
    call_state = {"ending": False, "ai_dead": False, "ai_dead_reason": "", "failsafe": None}

    def _handle_dead_ai_session(why: str) -> None:
        if call_state["ai_dead"] or call_state["ending"] or tool_ctx._hangup_done:
            return
        call_state["ai_dead"] = True
        call_state["ai_dead_reason"] = why
        if not getattr(tool_ctx, "sip_dial_started", False):
            # Not dialed yet — the pre-dial check fails the call without ringing.
            return
        failsafe = call_state["failsafe"]
        if failsafe is not None:
            failsafe.stop()
        asyncio.create_task(_finalize_and_hangup_session(
            ctx=ctx,
            session=session,
            tool_ctx=tool_ctx,
            call_id=tool_ctx.call_id,
            outcome="failed",
            reason=f"AI voice session stopped mid-call: {why}"[:500],
            log_label="AI_SESSION_DEAD",
        ))

    # Do NOT call generate_reply from error handlers — mid-session
    # send_client_content after a Google blip causes the fatal 1007:
    # "CONTENT_TYPE_AUDIO is not supported for this model configuration" (recoverable=False).
    # LiveKit RealtimeModel already auto-reconnects on recoverable 1011.
    def _on_session_close(ev) -> None:
        reason = getattr(ev, "reason", "?")
        error = getattr(ev, "error", None)
        asyncio.create_task(_log(
            "warning",
            f"AgentSession CLOSED — reason={reason} error={error}",
        ))
        _handle_dead_ai_session(f"session closed (reason={reason} error={error})")

    def _on_session_error(ev) -> None:
        err = getattr(ev, "error", None)
        recoverable = getattr(err, "recoverable", None)
        err_text = str(err)
        level = "warning" if recoverable else "error"
        tip = ""
        if "CONTENT_TYPE_AUDIO" in err_text or "1007" in err_text:
            tip = (
                " | KNOWN_BUG_1007_CONTENT_TYPE_AUDIO: Gemini Live rejected AUDIO mid-call "
                f"(model={_NATIVE_AUDIO_MODEL}). Set USE_GEMINI_REALTIME=false for pipeline fallback."
            )
            if "send_client_content" in err_text.lower() or "invalid argument" in err_text.lower():
                tip += (
                    " | HINT: Gemini Live rejected a client-content update; "
                    "tools use the separate send_tool_response path."
                )
        elif (
            "generate_reply is not compatible" in err_text.lower()
            or "send_client_content" in err_text.lower()
        ):
            tip = " | HINT: Gemini Live rejected a client-content update."
        elif "1011" in err_text or "Internal error" in err_text:
            tip = " | HINT: Google Live preview server blip — LiveKit will auto-reconnect (do not generate_reply)."
        asyncio.create_task(_log(
            level,
            f"AgentSession realtime ERROR — source={type(getattr(ev, 'source', None)).__name__} "
            f"recoverable={recoverable} error={err}{tip}",
        ))
        if recoverable is False:
            _handle_dead_ai_session(f"unrecoverable realtime error: {err_text[:300]}")

    session.on("close", _on_session_close)
    session.on("error", _on_session_error)

    async def _hangup_after_silence_failsafe() -> None:
        await _finalize_and_hangup_session(
            ctx=ctx,
            session=session,
            tool_ctx=tool_ctx,
            call_id=tool_ctx.call_id,
            outcome="disconnect_in_between",
            reason="no reply after silence failsafe hear-checks",
            log_label="SILENCE_FAILSAFE_HANGUP",
        )

    silence_failsafe = SilenceFailsafe(
        session,
        model=gemini_model,
        lead_name=lead_name,
        on_exhausted=_hangup_after_silence_failsafe,
    )
    call_state["failsafe"] = silence_failsafe

    # Single silence watchdog (SilenceFailsafe). Armed only after the answered-call
    # greeting finishes so it cannot speak into a ringing call. After max hear-checks
    # with no reply → hang up and finalize disconnect_in_between.
    if _SILENCE_FAILSAFE_ENABLED:
        session.on("agent_state_changed", silence_failsafe.on_agent_state_changed)
        session.on("user_state_changed", silence_failsafe.on_user_state_changed)
        session.on("user_input_transcribed", silence_failsafe.on_user_input_transcribed)

    # ── Dashboard Latency tab: transcript + per-turn model timing, written live ──
    _latency_id = str(getattr(tool_ctx, "call_id", None) or ctx.room.name)  # one file per call

    def _latency_call_id() -> str:
        return _latency_id

    _voice_now, _ = voice_settings.load()
    latency_log.record(_latency_call_id(), {
        "type": "call",
        "lead": lead_name,
        "phone_tail": (phone_number or "")[-4:],
        "room": ctx.room.name,
        "target": _LATENCY_TARGET_SECONDS,
        "models": {
            "stt": _stt_label(),
            "llm": (config.get_env("INCEPTION_MODEL") or "mercury-2.5").strip(),
            "tts": voice_settings.describe(_voice_now),
        } if _cascade_enabled() else {"realtime": "gemini live"},
    })

    def _report_model_latency(speech_id: str, total: float, parts: dict) -> None:
        slow = total > _LATENCY_TARGET_SECONDS
        asyncio.ensure_future(_log(
            "warning" if slow else "info",
            f"{'TURN_LATENCY_SLOW' if slow else 'TURN_LATENCY'} total={total:.2f}s "
            f"(eou={parts['eou']:.2f} llm={parts['llm']:.2f} tts={parts['tts']:.2f}) "
            f"target={_LATENCY_TARGET_SECONDS:.2f}s",
        ))
        latency_log.record(_latency_call_id(), {
            "type": "turn", "speech_id": speech_id, "total": round(total, 3),
            **{k: round(v, 3) for k, v in parts.items()},
        })

    def _on_conversation_item_for_latency(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = (getattr(item, "text_content", None) or "").strip()
        if role in ("user", "assistant") and text:
            latency_log.record(_latency_call_id(), {
                "type": "message", "role": "lead" if role == "user" else "agent",
                "text": text, "interrupted": bool(getattr(item, "interrupted", False)),
            })

    session.on("conversation_item_added", _on_conversation_item_for_latency)
    _model_latency = TurnLatencyTracker(_report_model_latency)

    def _on_metrics(ev) -> None:
        metrics = ev.metrics
        _model_latency.on_metrics(metrics)
        usage = usage_event(metrics)
        if usage:
            latency_log.record(_latency_call_id(), usage)

    session.on("metrics_collected", _on_metrics)

    # Per-turn reply latency: lead stops speaking → agent starts speaking.
    # (Gemini's end-of-speech silence, GEMINI_SILENCE_MS, comes on top of this.)
    _turn_latency = {"user_stopped_at": 0.0, "turn": 0}

    def _on_user_state_for_latency(ev) -> None:
        old_state = getattr(ev, "old_state", "")
        new_state = getattr(ev, "new_state", "")
        if old_state == "speaking" and new_state != "speaking":
            _turn_latency["user_stopped_at"] = time.time()
        elif new_state == "speaking":
            _turn_latency["user_stopped_at"] = 0.0

    def _on_agent_state_for_latency(ev) -> None:
        stopped_at = _turn_latency["user_stopped_at"]
        if getattr(ev, "new_state", "") != "speaking" or not stopped_at:
            return
        _turn_latency["user_stopped_at"] = 0.0
        _turn_latency["turn"] += 1
        _heard = time.time() - stopped_at
        logger.info("LATENCY_METRIC turn_reply=%.3fs turn=%d", _heard, _turn_latency["turn"])
        # What the lead actually waits (lead stops -> agent audio), fixed lines included.
        latency_log.record(_latency_call_id(), {"type": "heard", "s": round(_heard, 3),
                                                "turn": _turn_latency["turn"]})

    session.on("user_state_changed", _on_user_state_for_latency)
    session.on("agent_state_changed", _on_agent_state_for_latency)

    def _on_final_transcript_for_opening(ev) -> None:
        if getattr(ev, "is_final", False):
            if _session_kwargs["agent"].speak_opening_early(getattr(ev, "transcript", "")):
                asyncio.ensure_future(_log("info", "OPENING_FAST_PATH fixed reply spoken from final transcript"))

    session.on("user_input_transcribed", _on_final_transcript_for_opening)

    # ── AMD requires session.start BEFORE create_sip_participant ──────────────
    # Tradeoff vs older "start after answer" pattern: Gemini Live may sit open
    # during ring. Bound with ringing_timeout (see RINGING_TIMEOUT_SEC).
    try:
        await session.start(**_session_kwargs)
        deadline = time.time() + 10.0
        while session._activity is None and time.time() < deadline:
            await asyncio.sleep(0.05)
        if session._activity is None:
            raise RuntimeError("session activity did not become active")
    except Exception as _start_exc:
        await _log("error", f"Session start failed — cannot dial/greet: {_start_exc}")
        await _fail_call_before_dial(
            call_id or tool_ctx.call_id, f"agent session start failed: {_start_exc}", ctx.room.name
        )
        ctx.shutdown()
        return

    proceed_to_greeting = True
    _answer_t0 = time.time()

    if phone_number:
        trunk_id = trunk_id_override or os.getenv("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "No SIP trunk configured (trunk_id missing in metadata and OUTBOUND_TRUNK_ID unset) — cannot place outbound call")
            await _fail_call_before_dial(
                call_id or tool_ctx.call_id, "no SIP trunk configured", ctx.room.name
            )
            try:
                await session.aclose()
            except Exception:
                pass
            ctx.shutdown()
            return

        # Lease a pool FROM number (caller ID). No hardcoded default —
        # numbers come from Vobiz sync (phone_numbers pool) or per-call from_number.
        from_number: Optional[str] = None
        lease_call_id = call_id or tool_ctx.call_id
        if not lease_call_id:
            try:
                lease_call_id = await create_active_call(
                    phone_number, lead_name, room_name=ctx.room.name,
                    organization_id=organization_id, lead_id=lead_id,
                )
                call_id = lease_call_id
                tool_ctx.call_id = lease_call_id
            except Exception as _cid_exc:
                await _log("warning", f"Could not create call_id before number lease: {_cid_exc}")

        if lease_call_id:
            try:
                preferred = (preferred_from_number or "").strip() or None
                if preferred:
                    from_number = await acquire_specific_number(
                        lease_call_id,
                        preferred,
                        room_name=ctx.room.name,
                        assigned_agent=_agent_name,
                    )
                    await _log("info", f"Using assigned calling number {from_number}")
                else:
                    from_number = await get_available_number(
                        lease_call_id,
                        room_name=ctx.room.name,
                        organization_id=organization_id,
                    )
            except NoNumbersAvailable as _pool_exc:
                await _log("error", f"Phone pool unavailable: {_pool_exc}")
                try:
                    from db import finalize_call
                    await finalize_call(
                        lease_call_id,
                        outcome="failed",
                        reason=str(_pool_exc),
                        duration_seconds=0,
                        notify_webhook=True,
                    )
                except Exception:
                    pass
                try:
                    await session.aclose()
                except Exception:
                    pass
                ctx.shutdown()
                return
            except Exception as _pool_exc:
                await _log("error", f"Phone pool acquire failed: {_pool_exc}")
                try:
                    from db import finalize_call
                    await finalize_call(
                        lease_call_id,
                        outcome="failed",
                        reason=f"Phone pool acquire failed: {_pool_exc}",
                        duration_seconds=0,
                        notify_webhook=True,
                    )
                except Exception:
                    pass
                try:
                    await session.aclose()
                except Exception:
                    pass
                ctx.shutdown()
                return

        if not from_number:
            await _log(
                "error",
                "No FROM number — sync Numbers from Vobiz (no default DID configured)",
            )
            if lease_call_id:
                try:
                    from db import finalize_call
                    await finalize_call(
                        lease_call_id,
                        outcome="failed",
                        reason="No outbound phone numbers available in pool — sync from Vobiz",
                        duration_seconds=0,
                    )
                except Exception:
                    pass
            try:
                await session.aclose()
            except Exception:
                pass
            ctx.shutdown()
            return

        gemini_model = _resolve_gemini_model(model_override)
        await _log(
            "info",
            f"Dialing {phone_number} via SIP trunk {trunk_id} from={from_number} "
            f"(model={gemini_model} voice={gemini_voice} amd={'on' if _AMD_ENABLED else 'off'} "
            f"amd_timeout={_AMD_TIMEOUT_SEC}s ringing_timeout={_RINGING_TIMEOUT_SEC}s)",
        )
        # Never ring the lead into a dead AI session (bad key, rejected config):
        # they would answer and hear silence.
        if call_state["ai_dead"] or not _session_can_speak(session):
            why = call_state["ai_dead_reason"] or "AI voice session not ready"
            await _log("error", f"AI session unusable before dial — not dialing: {why}")
            await _fail_call_before_dial(
                call_id or tool_ctx.call_id, f"AI voice session failed before dial: {why}", ctx.room.name
            )
            try:
                await session.aclose()
            except Exception:
                pass
            ctx.shutdown()
            return

        # Start egress during ringing so the recording is ready before the
        # greeting without adding answer-to-greeting latency.
        asyncio.create_task(_start_recording(ctx, tool_ctx))
        _dial_t0 = time.time()
        # From here on the phone may ring: failures must go through hangup paths,
        # never the "failed before dial" finalize in entrypoint.
        tool_ctx.sip_dial_started = True

        async def _dial_and_wait_answer() -> None:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    sip_number=from_number,
                    participant_identity=_sip_identity,
                    wait_until_answered=True,
                    ringing_timeout={"seconds": _RINGING_TIMEOUT_SEC},
                )
            )
            await ctx.wait_for_participant(identity=_sip_identity)

        try:
            if _AMD_ENABLED:
                # AMD locks speech playout until execute() returns — keep timeout short
                # so a stuck classifier cannot leave the lead in silence for ~15–20s.
                amd_kwargs: dict = {
                    "participant_identity": _sip_identity,
                    "interrupt_on_machine": True,
                    "ivr_detection": False,  # hang up on IVR — do not navigate menus
                    "detection_options": {
                        "prompt": _AMD_HINGLISH_PROMPT,
                        "timeout": _AMD_TIMEOUT_SEC,
                        # Faster human lock so answer→greeting dead air stays short.
                        "human_speech_threshold": 0.7,
                        "human_silence_threshold": 0.25,
                        "machine_silence_threshold": 0.8,
                        "no_speech_threshold": min(2.0, _AMD_TIMEOUT_SEC),
                    },
                }
                if _google_llm is not None:
                    # RealtimeModel cannot classify text; use a normal Gemini LLM for AMD.
                    amd_kwargs["llm"] = _google_llm(model="gemini-2.0-flash", api_key=api_key)
                if _deepgram_stt is not None and config.get_env("DEEPGRAM_API_KEY"):
                    amd_kwargs["stt"] = _deepgram_stt(model="nova-3", language="multi")

                async with AMD(session, **amd_kwargs) as detector:
                    await _dial_and_wait_answer()
                    ring_time = time.time() - _dial_t0
                    tool_ctx.mark_call_connected()
                    if call_id or tool_ctx.call_id:
                        try:
                            await set_call_answered(call_id or tool_ctx.call_id)
                        except Exception as _ans_exc:
                            await _log("warning", f"set_call_answered failed: {_ans_exc}")
                    await _log("info", f"Call ANSWERED in {ring_time:.1f}s — running AMD")
                    _answer_t0 = time.time()
                    amd_exec_t0 = time.time()
                    amd_result = await detector.execute()
                    amd_exec_ms = (time.time() - amd_exec_t0) * 1000.0

                cat = _amd_category_str(amd_result.category)
                await _log(
                    "info",
                    f"AMD_RESULT={cat} delay_s={amd_result.delay:.3f} "
                    f"speech_s={amd_result.speech_duration:.3f} execute_ms={amd_exec_ms:.0f} "
                    f"reason={amd_result.reason!r} transcript={amd_result.transcript[:160]!r}",
                )

                if cat in (
                    AMDCategory.MACHINE_VM.value,
                    AMDCategory.MACHINE_IVR.value,
                    AMDCategory.MACHINE_UNAVAILABLE.value,
                    "machine-vm",
                    "machine-ivr",
                    "machine-unavailable",
                ):
                    await _finalize_amd_machine_and_hangup(
                        ctx=ctx,
                        session=session,
                        tool_ctx=tool_ctx,
                        call_id=call_id,
                        category=cat,
                        reason=amd_result.reason or "",
                        transcript=amd_result.transcript or "",
                        business_name=business_name,
                        language=call_language,
                        agent_name=agent_name,
                    )
                    return

                # human | uncertain → greet (AMD unlocks speech in execute()).
                await _log("info", f"AMD_RESULT={cat} — proceeding to greeting kickoff")
            else:
                await _dial_and_wait_answer()
                ring_time = time.time() - _dial_t0
                tool_ctx.mark_call_connected()
                if call_id or tool_ctx.call_id:
                    try:
                        await set_call_answered(call_id or tool_ctx.call_id)
                    except Exception as _ans_exc:
                        await _log("warning", f"set_call_answered failed: {_ans_exc}")
                _answer_t0 = time.time()
                await _log(
                    "info",
                    f"Call ANSWERED in {ring_time:.1f}s — AMD disabled, greeting immediately",
                )
            proceed_to_greeting = True
        except Exception as exc:
            # Dial outcome (busy / no answer / failed) is handled below; the
            # session close it triggers is not an AI failure.
            call_state["ending"] = True
            if prefetch_task and not prefetch_task.done():
                prefetch_task.cancel()
            err_l = str(exc).lower()
            # SIP busy / rejected → finalize busy so batch/campaign queue advances to next lead.
            if any(tok in err_l for tok in ("busy", "user busy", "486", "call rejected", "declined")):
                await _log("warning", f"SIP BUSY for {phone_number}: {exc}")
                if call_id:
                    try:
                        from db import finalize_call
                        await finalize_call(
                            call_id,
                            outcome="busy",
                            reason=f"SIP busy / rejected: {exc}",
                            duration_seconds=int(time.time() - tool_ctx._call_start_time),
                            room_name=ctx.room.name if ctx.room else None,
                        )
                    except Exception as _fin_exc:
                        await _log("warning", f"Failed to finalize busy call: {_fin_exc}")
                try:
                    await session.aclose()
                except Exception:
                    pass
                ctx.shutdown()
                return
            if "timeout" in err_l or "no answer" in err_l or "canceled" in err_l or "480" in err_l:
                await _log("warning", f"SIP dial ended without answer for {phone_number}: {exc}")
                if call_id:
                    try:
                        from db import finalize_call
                        await finalize_call(
                            call_id,
                            outcome="no_answer",
                            reason=f"SIP no answer / ring timeout: {exc}",
                            duration_seconds=int(time.time() - tool_ctx._call_start_time),
                            room_name=ctx.room.name if ctx.room else None,
                        )
                    except Exception as _fin_exc:
                        await _log("warning", f"Failed to finalize no_answer call: {_fin_exc}")
                try:
                    await session.aclose()
                except Exception:
                    pass
                ctx.shutdown()
                return
            await _log("error", f"SIP dial / AMD FAILED for {phone_number}: {exc}")
            if call_id:
                try:
                    from db import finalize_call
                    await finalize_call(
                        call_id,
                        outcome="failed",
                        reason=f"SIP dial/AMD failed: {exc}",
                        duration_seconds=int(time.time() - tool_ctx._call_start_time),
                        room_name=ctx.room.name if ctx.room else None,
                    )
                except Exception as _fin_exc:
                    await _log("warning", f"Failed to finalize call after SIP/AMD error: {_fin_exc}")
            try:
                await session.aclose()
            except Exception:
                pass
            ctx.shutdown()
            return

    audio_ok = await _wait_for_audio_subscribed(session, timeout=1.0)
    if not audio_ok:
        await _log("warning", "Audio not subscribed yet — greeting anyway")

    ready_secs = time.time() - _answer_t0
    await _log(
        "info",
        f"Agent session ready — {ready_secs:.2f}s after answer/AMD "
        f"(speakable={_session_can_speak(session)} closing={getattr(session, '_closing', None)})",
    )

    # Greeting ONLY after AMD human/uncertain (or non-SIP jobs). Never race AMD speech lock.
    if proceed_to_greeting:
        spoken_name = _spoken_lead_name(lead_name)
        if is_reconnect:
            greeting_text = reconnect_opening
            await _log(
                "info",
                f"Opening mode=RECONNECT language={call_language} — cut apology + continue ask: "
                f"{greeting_text!r}",
            )
        elif additional_instructions_has_greeting(additional_instructions):
            greeting_text = build_campaign_opening_instructions(
                lead_name,
                additional_instructions,
                language=call_language,
                agent_name=agent_name,
            )
            await _log(
                "info",
                f"Opening mode=CAMPAIGN language={call_language} agent={agent_name} — "
                + (
                    f"exact line: {_opening_line!r}"
                    if _opening_line
                    else "salutation + identity composed live (no quoted Greeting: line)"
                ),
            )
        else:
            greeting_text = build_identity_opening(lead_name, language=call_language)
            await _log(
                "info",
                f"Opening mode=DEFAULT language={call_language} — exact identity line: {greeting_text!r}",
            )
        # Answer → first agent audio: the silence the lead actually hears.
        def _on_greeting_audio_start(ev) -> None:
            if getattr(ev, "new_state", "") != "speaking":
                return
            session.off("agent_state_changed", _on_greeting_audio_start)
            _first_word = time.time() - _answer_t0
            latency_log.record(_latency_call_id(), {"type": "greeting", "first_word": round(_first_word, 3)})
            asyncio.create_task(_log("info", f"GREETING_START {_first_word:.2f}s after answer"))

        session.on("agent_state_changed", _on_greeting_audio_start)
        _assistant = _session_kwargs["agent"]
        greeting_frames = _assistant.greeting_audio_for(_opening_line) if _opening_line else None
        try:
            if greeting_frames and _session_can_speak(session):
                _greet_t0 = time.time()
                handle = _assistant.play_pre_rendered(_opening_line, greeting_frames)
                await _assistant.sync_spoken_line(handle, _opening_line, label="Greeting")
                await _log(
                    "info",
                    f"Greeting completed in {time.time() - _greet_t0:.2f}s (pre-rendered)",
                )
            elif _opening_line and getattr(session, "tts", None) is not None and _session_can_speak(session):
                # Premium cascade: speak the exact line through the call TTS right away —
                # no LLM round trip and no paraphrase before the first word.
                _greet_t0 = time.time()
                handle = session.say(_opening_line, allow_interruptions=True)
                await handle.wait_for_playout()
                await _log(
                    "info",
                    f"Greeting completed in {time.time() - _greet_t0:.2f}s (fixed line via TTS)",
                )
            else:
                if _assistant._greeting_audio_task is not None:
                    await _log("warning", "Greeting audio not ready at answer — generating live")
                greet_elapsed = await _speak_greeting(
                    session,
                    greeting_text,
                    model=gemini_model,
                    lead_name=spoken_name or "the person on the line",
                    additional_instructions=additional_instructions,
                    reconnect=is_reconnect,
                    campaign=is_campaign_call,
                )
                await _log("info", f"Greeting completed in {greet_elapsed:.2f}s")
        except Exception as _gr_exc:
            await _log("warning", f"Greeting failed: {_gr_exc}")

    # Start silence monitoring only now: the SIP call is answered and the
    # opening greeting has finished, so it cannot speak into a ringing call.
    if _SILENCE_FAILSAFE_ENABLED:
        silence_failsafe.arm_and_start()
        await _log(
            "info",
            f"SILENCE_FAILSAFE armed ({_SILENCE_FAILSAFE_SEC:.0f}s, max={_SILENCE_FAILSAFE_MAX})",
        )

    # ── Keep session alive until SIP participant actually leaves ─────────────
    # Without this block, the entrypoint returns and the process spins down.
    # Prefer SIP identity leave. Room-level disconnect alone can false-trigger on
    # glitches and finalize/webhook while audio is still up → Node redials.
    if phone_number:
        _disconnect_event = asyncio.Event()
        _sip_left = False

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            nonlocal _sip_left
            if participant.identity == _sip_identity:
                _sip_left = True
                _disconnect_event.set()

        def _on_disconnected():
            # Room drop is a backup only — confirm SIP identity is gone before ending.
            # Fail-closed on unknown local cache state (do not finalize).
            try:
                still_there = any(
                    (p.identity == _sip_identity)
                    for p in (ctx.room.remote_participants or {}).values()
                )
            except Exception:
                still_there = True
            if still_there:
                return
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        # Lead may have hung up before handlers registered — check once now.
        try:
            if not any(
                (p.identity == _sip_identity)
                for p in (ctx.room.remote_participants or {}).values()
            ):
                _sip_left = True
                _disconnect_event.set()
        except Exception:
            pass

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")
        latency_log.record(_latency_call_id(), {"type": "end"})

        await _log(
            "info",
            f"SIP session ending for {phone_number} "
            f"(sip_left={_sip_left})",
        )
        # Normal end: the session close that follows is not an AI failure.
        call_state["ending"] = True
        silence_failsafe.stop()
        room_name = ctx.room.name if ctx.room else None
        # Hang up / confirm SIP gone BEFORE webhook so CRM cannot redial a live line.
        hangup_ok = bool(getattr(tool_ctx, "_hangup_done", False))
        if phone_number and not hangup_ok:
            try:
                hangup_ok = bool(await tool_ctx._hangup_call())
            except Exception as _hang_exc:
                await _log("warning", f"Safety hangup at session end failed: {_hang_exc}")
            if not hangup_ok:
                try:
                    hangup_ok = bool(await tool_ctx._hangup_call())
                except Exception as _hang_exc:
                    await _log("warning", f"Safety hangup retry failed: {_hang_exc}")
        if not hangup_ok:
            await _log(
                "error",
                "Skipping auto-finalize — SIP not confirmed absent at session end",
            )
            try:
                await session.aclose()
            except Exception:
                pass
            return
        if tool_ctx.call_id:
            try:
                from db import finalize_call, get_call_by_id
                from transcripts import run_post_call_transcription
                existing = await get_call_by_id(tool_ctx.call_id)
                if existing and existing.get("status") in ("ringing", "in_progress"):
                    live_transcript = None
                    transcript_json = None
                    if tool_ctx.requirements_collector:
                        await tool_ctx.requirements_collector.flush()
                    if tool_ctx.transcript_tracker:
                        await tool_ctx.transcript_tracker.flush()
                        live_transcript = tool_ctx.transcript_tracker.live_text()
                        transcript_json = tool_ctx.transcript_tracker.json_text()
                    duration = int(time.time() - tool_ctx._call_start_time)
                    from client_requirements import infer_hangup_outcome
                    hangup_outcome = infer_hangup_outcome(
                        live_transcript=live_transcript,
                        transcript_json=transcript_json,
                        property_data=existing.get("property_data"),
                    ) or "disconnect_in_between"
                    hangup_reason = (
                        f"call disconnected after lead said {hangup_outcome}"
                        if hangup_outcome != "disconnect_in_between"
                        else "call disconnected mid-conversation"
                    )
                    await finalize_call(
                        tool_ctx.call_id,
                        outcome=hangup_outcome,
                        reason=hangup_reason,
                        duration_seconds=duration,
                        recording_url=tool_ctx.recording_url,
                        live_transcript=live_transcript,
                        transcript_json=transcript_json,
                        room_name=room_name,
                    )
                    if tool_ctx.recording_url and os.getenv("ENABLE_POST_CALL_TRANSCRIPT", "").lower() == "true":
                        asyncio.create_task(run_post_call_transcription(tool_ctx.call_id, tool_ctx.recording_url))
            except Exception as _fin_exc:
                await _log("warning", f"Auto-finalize call failed: {_fin_exc}")
        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass



