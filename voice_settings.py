"""Live TTS voice settings for the cascade stack (dashboard -> worker).

The agent speaks with Cartesia Sonic. The dashboard saves the voice here and the worker
reads it when it builds each call's TTS, so a change applies from the next call with no
restart. A call already in progress keeps its voice. Values come from (highest first): the
saved file, the CARTESIA_* environment variables, the defaults below.

Stored in a local JSON file (not Supabase), so tuning never writes to the shared
production database. The file is per machine/container: on Azure with several replicas,
copy the final values into the app settings.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import config

# ── Cartesia Sonic ───────────────────────────────────────────────────────────
# Female voices with a native Indian accent: Hindi/Hinglish ("hi") and Indian English ("en").
# (voice id, name, language) — from the Cartesia voice library, 2026-10-12.
CARTESIA_VOICES: tuple[tuple[str, str, str], ...] = (
    ("f91ab3e6-5071-4e15-b016-cde6f2bcd222", "Aadhya - Soother", "hi"),
    ("9cebb910-d4b7-4a4a-85a4-12c79137724c", "Aarti - Conversationalist", "hi"),
    ("ffa0d297-8cf9-4ec9-8e4e-99a59e939b02", "Ananya - Process Facilitator", "hi"),
    ("00218a9c-4b3e-49a1-8c88-f30daf6929f3", "Anaya - Keen Support", "hi"),
    ("32b0f12b-67c3-421d-8850-b46c019ced91", "Anika - System Facilitator", "hi"),
    ("209d9a43-03eb-40d8-a7b7-51a6d54c052f", "Anita - Meditation Guide", "hi"),
    ("95d51f79-c397-46f9-b49a-23763d3eaa2d", "Arushi - Hinglish Speaker", "hi"),
    ("d2d3584d-1b44-428e-aab1-30255d28d978", "Diya - Service Specialist", "hi"),
    ("72656902-fb4b-4c31-af52-c3b68e2cae26", "Esha - Calm Guide", "hi"),
    ("14008c51-fbf4-418e-ae23-9316a03dcfa2", "Ishani - Thoughtful Responder", "hi"),
    ("bc4324cb-08c4-4869-8fed-58436d02fdc2", "Kaira - Operational Specialist", "hi"),
    ("56e35e2d-6eb6-4226-ab8b-9776515a7094", "Kavita - Customer Care Agent", "hi"),
    ("ce45ddca-5f4b-4047-ada3-3fce2bc78acd", "Kavya - Clear Briefing", "hi"),
    ("5283efe8-07d1-4e3a-b615-2ae4a81c1b73", "Kriti - Clear Explainer", "hi"),
    ("c6bbc7d5-4b35-4d49-b1c6-4417019a61c1", "Lavanya - Friendly Assistant", "hi"),
    ("a81fccdc-5595-4dfc-ae76-4de6a515b8a2", "Meera - Bright Companion", "hi"),
    ("911d97fc-e9f5-4249-bf11-50d4edb7b8d8", "Mehak - Reschedule Host", "hi"),
    ("47f3bbb1-e98f-4e0c-92c5-5f0325e1e206", "Neha - Virtual Assistant", "hi"),
    ("0f14d8cb-f039-41fe-a813-a9b4bee7eed8", "Nisha - Elegant Speaker", "hi"),
    ("28ca2041-5dda-42df-8123-f58ea9c3da00", "Palak - Presenter", "hi"),
    ("bec003e2-3cb3-429c-8468-206a393c67ad", "Parvati - Friendly Supporter", "hi"),
    ("92da9281-7cf3-4c61-be0f-face03a3312f", "Preeti - Steady Advisor", "hi"),
    ("a631bc8b-ea1c-49bb-8dab-7a118afd11b8", "Riva - Service Coordinator", "hi"),
    ("faf0731e-dfb9-4cfc-8119-259a79b27e12", "Riya - College Roommate", "hi"),
    ("e6b71342-48f1-4c70-a13a-d197b176ff24", "Sana - Friendly Concierge", "hi"),
    ("432fc642-6a83-4975-b77a-c605903b5ba6", "Sanya - Modern Communicator", "hi"),
    ("b3610b4c-0fd6-4912-89d1-ace6cb6054dd", "Shailika - Clear Explainer", "hi"),
    ("4459a9a5-69d6-4680-b970-e13dc51845b6", "Siya - Bright Conversationalist", "hi"),
    ("6b02ffe5-e3cb-48c0-a023-c72f85953375", "Sneha - Empathetic Voice", "hi"),
    ("126a0835-beea-4e77-a883-f66eabcf6dd4", "Swati - Bright Professional", "hi"),
    ("7ea5e9c2-b719-4dc3-b870-5ba5f14d31d8", "Janvi - Steady Agent", "en"),
    ("f8f5f1b2-f02d-4d8e-a40d-fd850a487b3d", "Kiara - Joyful Woman", "en"),
    ("f6141af3-5f94-418c-80ed-a45d450e7e2e", "Priya - Trusted Operator", "en"),
    ("3b554273-4299-48b9-9aaf-eefd438e3941", "Simi - Support Specialist", "en"),
)
CARTESIA_VOICE_IDS = frozenset(v[0] for v in CARTESIA_VOICES)
CARTESIA_MODELS: tuple[str, ...] = ("sonic-3.6", "sonic-3")
SPEED_RANGE = (0.6, 1.5)
VOLUME_RANGE = (0.5, 2.0)
CARTESIA_DEFAULTS: dict[str, Any] = {
    "provider": "cartesia",
    "voice_id": "95d51f79-c397-46f9-b49a-23763d3eaa2d",  # Arushi - Hinglish Speaker
    "speed": 1.0,
    "volume": 1.0,
    "model": "sonic-3.6",
}

_CARTESIA_ENV_KEYS = {"voice_id": "CARTESIA_VOICE_ID", "speed": "CARTESIA_TTS_SPEED",
                      "volume": "CARTESIA_TTS_VOLUME", "model": "CARTESIA_TTS_MODEL"}
_lock = threading.Lock()


class VoiceSettingsError(ValueError):
    pass


def settings_file() -> Path:
    raw = (os.getenv("VOICE_SETTINGS_FILE") or "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent / "runtime" / "voice_settings.json"


def _number(data: dict[str, Any], key: str, low: float, high: float) -> float:
    try:
        value = float(data.get(key))
    except (TypeError, ValueError):
        raise VoiceSettingsError(f"{key} must be a number") from None
    if not low <= value <= high:
        raise VoiceSettingsError(f"{key} must be between {low} and {high}")
    return round(value, 2)


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Return a clean settings dict or raise VoiceSettingsError with a readable message."""
    provider = str(data.get("provider") or "cartesia").strip().lower()
    if provider != "cartesia":
        raise VoiceSettingsError("provider must be cartesia")
    voice_id = str(data.get("voice_id", "")).strip().lower()
    if voice_id not in CARTESIA_VOICE_IDS:
        raise VoiceSettingsError("voice_id must be one of the listed Cartesia Indian voices")
    model = str(data.get("model") or CARTESIA_DEFAULTS["model"]).strip()
    if model not in CARTESIA_MODELS:
        raise VoiceSettingsError(f"model must be one of: {', '.join(CARTESIA_MODELS)}")
    return {"provider": provider, "voice_id": voice_id, "model": model,
            "speed": _number(data, "speed", *SPEED_RANGE),
            "volume": _number(data, "volume", *VOLUME_RANGE)}


def _env_values(defaults: dict[str, Any], keys: dict[str, str]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, env_key in keys.items():
        raw = (config.get_env(env_key) or "").strip()
        if raw:
            merged[key] = raw.lower() if key == "voice_id" else raw
    try:
        return validate(merged)
    except VoiceSettingsError:
        return dict(defaults)


def _from_env() -> tuple[dict[str, Any], bool]:
    has_env = any((config.get_env(k) or "").strip() for k in _CARTESIA_ENV_KEYS.values())
    return _env_values(CARTESIA_DEFAULTS, _CARTESIA_ENV_KEYS), has_env


def load() -> tuple[dict[str, Any], str]:
    """Current voice and where it came from: "dashboard", "env" or "default"."""
    path = settings_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return validate(saved), "dashboard"
    except FileNotFoundError:
        pass
    except (OSError, ValueError, VoiceSettingsError):
        pass  # unreadable or invalid file: fall back rather than break a call
    env_values, has_env = _from_env()
    return env_values, "env" if has_env else "default"


def save(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and write atomically, so a worker never reads a half-written file."""
    clean = validate(data)
    path = settings_file()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".voice_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return clean


def cartesia_voice_name(voice_id: str) -> str:
    return next((name for vid, name, _ in CARTESIA_VOICES if vid == voice_id), voice_id)


def describe(voice: dict[str, Any]) -> str:
    """One-line label for logs and the dashboard."""
    return (f"cartesia {voice['model']} voice={cartesia_voice_name(voice['voice_id'])} "
            f"speed={voice['speed']:.2f} volume={voice['volume']:.2f}")


def cartesia_kwargs(voice: dict[str, Any], language: str, api_key: str) -> dict[str, Any]:
    """Arguments for livekit.plugins.cartesia.TTS — shared by phone calls and dashboard preview,
    so the preview sounds exactly like a call. `language` is the call
    locale (hi-IN / en-IN); Hinglish is sent as Hindi so Hindi words are pronounced right."""
    return dict(
        model=voice["model"],
        voice=voice["voice_id"],
        language="en" if language.lower().startswith("en") else "hi",
        speed=voice["speed"],
        volume=voice["volume"],
        sample_rate=24000,
        word_timestamps=False,  # only supported for a few languages; Hindi would warn
        api_key=api_key,
    )


def reset() -> None:
    """Forget dashboard values; the .env / defaults apply again."""
    with _lock:
        settings_file().unlink(missing_ok=True)
