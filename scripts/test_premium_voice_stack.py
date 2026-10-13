"""Premium voice stack (VOICE_STACK=cascade): Sarvam STT + Mercury 2.5 + Cartesia TTS.

No network and no call: model constructors and the job runner are replaced with fakes.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")

import agent_runtime  # noqa: E402
from agent_runtime import EOUMetrics, LLMMetrics, TTSMetrics, TurnLatencyTracker  # noqa: E402


def _assistant(gate: bool = True):
    return agent_runtime.OutboundAssistant(
        instructions="test", lead_name="Karan Kataria", business_name="Swaryx",
        lead_source="MagicBricks", enforce_fresh_call_gate=gate,
    )


def test_cascade_stack_builds_sarvam_mercury_cartesia_session(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    built = {}
    monkeypatch.setattr(agent_runtime, "_build_cascade_session",
                        lambda tools, call_language: built.update(language=call_language) or "cascade")
    assert agent_runtime._build_session([], "prompt", call_language="english") == "cascade"
    assert built == {"language": "english"}


def test_gemini_stays_default_without_voice_stack(monkeypatch):
    monkeypatch.delenv("VOICE_STACK", raising=False)
    assert agent_runtime._cascade_enabled() is False


def test_cascade_session_wires_all_three_models(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    monkeypatch.setattr(agent_runtime, "_build_stt", lambda lang: f"stt:{lang}")
    monkeypatch.setattr(agent_runtime, "_build_mercury_llm", lambda: "mercury")
    monkeypatch.setattr(agent_runtime, "_build_tts", lambda lang: f"tts:{lang}")
    captured = {}
    monkeypatch.setattr(agent_runtime, "AgentSession", lambda **kw: captured.update(kw) or "session")
    assert agent_runtime._build_cascade_session([], call_language="hinglish") == "session"
    assert captured["stt"] == "stt:hinglish" and captured["llm"] == "mercury" and captured["tts"] == "tts:hinglish"
    assert captured["turn_detection"] == "stt"
    assert captured["turn_handling"]["endpointing"]["min_delay"] == 0.1


def test_mercury_defaults_short_replies(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "test-key-not-used")
    monkeypatch.delenv("INCEPTION_MAX_COMPLETION_TOKENS", raising=False)
    captured = {}
    monkeypatch.setattr(agent_runtime, "_openai_llm", lambda **kw: captured.update(kw) or "llm")
    agent_runtime._build_mercury_llm()
    assert captured["max_completion_tokens"] == 100 and captured["model"] == "mercury-2.5"
    assert captured["base_url"].startswith("https://api.inceptionlabs.ai")


@pytest.mark.parametrize("stack, role", [("cascade", "system"), ("", "developer")])
def test_gate_messages_use_a_role_the_llm_accepts(monkeypatch, stack, role):
    """Mercury rejects role="developer" with HTTP 400 (silent call); Gemini Live accepts it."""
    monkeypatch.setenv("VOICE_STACK", stack)
    assistant = _assistant()
    added = []
    asyncio.run(assistant.on_user_turn_completed(SimpleNamespace(add_message=lambda **m: added.append(m)),
                                                 SimpleNamespace(text_content="hello")))
    assert added and all(m["role"] == role for m in added)


@pytest.mark.parametrize("stack, reminded", [("cascade", True), ("", False)])
def test_two_sentence_reminder_after_permission_only(monkeypatch, stack, reminded):
    monkeypatch.setenv("VOICE_STACK", stack)
    added = []
    ctx = SimpleNamespace(add_message=lambda **m: added.append(m))
    asyncio.run(_assistant(gate=False).on_user_turn_completed(ctx, SimpleNamespace(text_content="budget 2 crore")))
    assert any("at most 2 short sentences" in m["content"] for m in added) is reminded


def test_no_reminder_while_identity_is_unconfirmed(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    added = []
    asyncio.run(_assistant().on_user_turn_completed(SimpleNamespace(add_message=lambda **m: added.append(m)),
                                                    SimpleNamespace(text_content="kaun?")))
    assert not any("at most 2 short sentences" in m["content"] for m in added)


def test_cascade_job_runs_without_a_gemini_key(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    ran = {}

    async def fake_job(ctx, *, api_key, key_slot, tool_ctx_ref):
        ran.update(api_key=api_key, key_slot=key_slot)

    monkeypatch.setattr(agent_runtime, "_run_outbound_job", fake_job)
    monkeypatch.setattr(agent_runtime, "assign_job_key", lambda *a, **k: pytest.fail("no Gemini key needed"),
                        raising=False)
    import key_pool
    monkeypatch.setattr(key_pool, "assign_job_key", lambda *a, **k: pytest.fail("no Gemini key needed"))
    ctx = SimpleNamespace(job=SimpleNamespace(metadata=json.dumps({})), room=SimpleNamespace(name="call-test"),
                          shutdown=lambda: None)
    asyncio.run(agent_runtime.entrypoint(ctx))
    assert ran == {"api_key": "", "key_slot": "cascade"}


def test_api_accepts_calls_without_gemini_key_on_cascade(monkeypatch):
    import server

    monkeypatch.setenv("VOICE_STACK", "cascade")
    assert server._gemini_key_required() is False
    server._validate_client_credentials({})  # no exception
    assert server._validate_call_initiation_requirements(organization_id="org", lead_id="lead", creds={}) == (
        "org", "lead")
    monkeypatch.delenv("VOICE_STACK")
    with pytest.raises(server.HTTPException):
        server._validate_client_credentials({})


def test_latency_tracker_sums_model_stages_per_turn():
    reports = []
    tracker = TurnLatencyTracker(lambda sid, total, parts: reports.append((sid, round(total, 3), parts)))
    for m in (
        EOUMetrics(timestamp=0, end_of_utterance_delay=0.5, transcription_delay=0.1,
                   on_user_turn_completed_delay=0.0, speech_id="a"),
        LLMMetrics(label="mercury", request_id="r", timestamp=0, duration=0.5, ttft=0.8, cancelled=False,
                   completion_tokens=20, prompt_tokens=100, prompt_cached_tokens=0, total_tokens=120,
                   tokens_per_second=200.0, speech_id="a"),
        TTSMetrics(label="cartesia", request_id="r", timestamp=0, ttfb=0.1, duration=1.0, audio_duration=2.0,
                   cancelled=False, characters_count=60, streamed=True, speech_id="a"),
    ):
        tracker.on_metrics(m)
    assert reports == [("a", 1.4, {"eou": 0.5, "stt": 0.1, "llm": 0.8, "tts": 0.1})]


def test_elevenlabs_scribe_when_selected(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVEN_API_KEY", "test-key-not-used")
    monkeypatch.delenv("ELEVEN_VAD_SILENCE_SECS", raising=False)
    captured = {}
    monkeypatch.setattr(agent_runtime, "_elevenlabs_stt", lambda **kw: captured.update(kw) or "scribe")
    assert agent_runtime._build_stt("hinglish") == "scribe"
    assert captured["model"] == "scribe_v2_realtime" and captured["language_code"] == "hi"
    assert captured["server_vad"]["vad_silence_threshold_secs"] == 0.3  # ElevenLabs default 1.5s is too slow
    agent_runtime._build_stt("english")
    assert captured["language_code"] == "en"
    assert agent_runtime._stt_label() == "elevenlabs scribe_v2_realtime"


def test_sarvam_is_the_default_stt(monkeypatch):
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    monkeypatch.setattr(agent_runtime, "_build_sarvam_stt", lambda lang, realtime=True: "saaras")
    assert agent_runtime._build_stt("hinglish") == "saaras"
    assert agent_runtime._stt_label() == "sarvam saaras:v3-realtime"


def test_missing_eleven_key_stops_the_job_before_dialing(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "elevenlabs")
    monkeypatch.delenv("ELEVEN_API_KEY", raising=False)
    monkeypatch.setattr(agent_runtime.config, "get_env",
                        lambda k, d="": "" if k == "ELEVEN_API_KEY" else os.getenv(k, d))
    with pytest.raises(RuntimeError, match="ELEVEN_API_KEY"):
        agent_runtime._build_stt("hinglish")


def test_usage_events_come_from_real_livekit_metrics():
    from agent_runtime import STTMetrics, usage_event

    llm = LLMMetrics(label="mercury", request_id="r", timestamp=0, duration=0.5, ttft=0.8, cancelled=False,
                     completion_tokens=35, prompt_tokens=6000, prompt_cached_tokens=4000, total_tokens=6035,
                     tokens_per_second=200.0, speech_id="a")
    assert usage_event(llm) == {"type": "usage", "kind": "llm", "prompt": 6000, "cached": 4000,
                                "completion": 35, "total": 6035}
    tts = TTSMetrics(label="cartesia", request_id="r", timestamp=0, ttfb=0.1, duration=1.0, audio_duration=3.2,
                     cancelled=False, characters_count=80, streamed=True, speech_id="a")
    assert usage_event(tts) == {"type": "usage", "kind": "tts", "chars": 80, "audio_s": 3.2}
    stt = STTMetrics(label="sarvam", request_id="r", timestamp=0, duration=0.0, audio_duration=5.0, streamed=True)
    assert usage_event(stt) == {"type": "usage", "kind": "stt", "audio_s": 5.0}


class _SaySession:
    def __init__(self):
        self.tts = object()
        self.said = []

    def say(self, text, *, allow_interruptions=True):
        self.said.append(text)


def _with_session(assistant, session):
    assistant._activity = SimpleNamespace(session=session)
    return assistant


def test_identity_yes_speaks_permission_from_final_transcript_without_llm(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    session = _SaySession()
    assistant = _with_session(_assistant(), session)
    assert assistant.speak_opening_early("हाँ, बोलो।") is True
    assert session.said == [assistant._permission_intro] and assistant._conversation_gate == "permission"
    # The same turn then completes: it must not also go to Mercury or be gated twice.
    added = []
    with pytest.raises(agent_runtime.StopResponse):
        asyncio.run(assistant.on_user_turn_completed(SimpleNamespace(add_message=lambda **m: added.append(m)),
                                                     SimpleNamespace(text_content="हाँ बोलो")))
    assert added == [] and session.said == [assistant._permission_intro]
    assert assistant._conversation_gate == "permission"  # next "haan" is the permission answer


def test_reconnect_yes_speaks_bridge_then_lets_mercury_continue(monkeypatch):
    monkeypatch.setenv("VOICE_STACK", "cascade")
    session = _SaySession()
    assistant = _with_session(agent_runtime.OutboundAssistant(
        instructions="test", lead_name="Karan", business_name="Swaryx", lead_source=None,
        enforce_fresh_call_gate=False, reconnect=True), session)
    assert assistant.speak_opening_early("haan bolo") is True
    assert session.said == [agent_runtime._RECONNECT_BRIDGE]
    added = []
    asyncio.run(assistant.on_user_turn_completed(SimpleNamespace(add_message=lambda **m: added.append(m)),
                                                 SimpleNamespace(text_content="haan bolo")))
    assert session.said == [agent_runtime._RECONNECT_BRIDGE]  # bridge spoken once only
    assert any("at most 2 short sentences" in m["content"] for m in added)  # Mercury continues


@pytest.mark.parametrize("stack, text", [("", "haan"), ("cascade", "kaun bol raha hai?"), ("cascade", "number kahan se mila")])
def test_fast_path_skips_gemini_and_unclear_replies(monkeypatch, stack, text):
    monkeypatch.setenv("VOICE_STACK", stack)
    session = _SaySession()
    assistant = _with_session(_assistant(), session)
    assert assistant.speak_opening_early(text) is False and session.said == []
    assert assistant._conversation_gate == "identity"
