"""Dashboard voice tuning: storage, API and the worker picking it up (no Cartesia calls)."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")

import voice_settings  # noqa: E402

ARUSHI = "95d51f79-c397-46f9-b49a-23763d3eaa2d"
SIYA = "4459a9a5-69d6-4680-b970-e13dc51845b6"


@pytest.fixture(autouse=True)
def voice_file(tmp_path, monkeypatch):
    path = tmp_path / "voice.json"
    monkeypatch.setenv("VOICE_SETTINGS_FILE", str(path))
    for key in ("CARTESIA_VOICE_ID", "CARTESIA_TTS_SPEED", "CARTESIA_TTS_VOLUME",
                "CARTESIA_TTS_MODEL", "CARTESIA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return path


def test_defaults_then_env_then_dashboard(monkeypatch):
    assert voice_settings.load() == (voice_settings.CARTESIA_DEFAULTS, "default")
    monkeypatch.setenv("CARTESIA_VOICE_ID", SIYA.upper())
    monkeypatch.setenv("CARTESIA_TTS_SPEED", "0.95")
    voice, source = voice_settings.load()
    assert source == "env" and voice["voice_id"] == SIYA and voice["speed"] == 0.95
    voice_settings.save({"voice_id": ARUSHI, "speed": 1.05, "volume": 1.2})
    voice, source = voice_settings.load()
    assert source == "dashboard" and voice == {"provider": "cartesia", "voice_id": ARUSHI, "model": "sonic-3.6",
                                               "speed": 1.05, "volume": 1.2}
    voice_settings.reset()
    assert voice_settings.load()[1] == "env"


@pytest.mark.parametrize("bad", [
    {"voice_id": "not-a-listed-voice", "speed": 1.0, "volume": 1.0},
    {"voice_id": ARUSHI, "speed": 2.5, "volume": 1.0},
    {"voice_id": ARUSHI, "speed": 1.0, "volume": 0.1},
    {"voice_id": ARUSHI, "speed": "fast", "volume": 1.0},
    {"voice_id": ARUSHI, "speed": 1.0, "volume": 1.0, "model": "other"},
    {"provider": "sarvam", "voice_id": ARUSHI, "speed": 1.0, "volume": 1.0},
])
def test_invalid_values_are_rejected(bad):
    with pytest.raises(voice_settings.VoiceSettingsError):
        voice_settings.save(bad)


def test_corrupt_or_old_bulbul_file_falls_back_instead_of_breaking_a_call(voice_file):
    voice_file.write_text("{not json", encoding="utf-8")
    assert voice_settings.load() == (voice_settings.CARTESIA_DEFAULTS, "default")
    voice_file.write_text('{"speaker": "priya", "pace": 1.0, "temperature": 0.6}', encoding="utf-8")
    assert voice_settings.load() == (voice_settings.CARTESIA_DEFAULTS, "default")


def test_worker_builds_tts_from_saved_voice(monkeypatch):
    import agent_runtime

    monkeypatch.setenv("CARTESIA_API_KEY", "test-key-not-used")
    voice_settings.save({"voice_id": ARUSHI, "speed": 1.05, "volume": 1.0})
    captured = {}
    monkeypatch.setattr(agent_runtime, "_cartesia_tts", lambda **kw: captured.update(kw) or "cartesia-tts")
    assert agent_runtime._build_tts("hinglish") == "cartesia-tts"
    assert captured["voice"] == ARUSHI and captured["language"] == "hi" and captured["speed"] == 1.05
    assert captured["model"] == "sonic-3.6" and captured["word_timestamps"] is False
    agent_runtime._build_tts("english")
    assert captured["language"] == "en"


def test_missing_key_stops_the_job_before_dialing(monkeypatch):
    """The TTS is built before the SIP dial, so no lead is called on a voice that cannot speak."""
    import agent_runtime

    monkeypatch.setattr(agent_runtime, "_cartesia_tts", lambda **kw: pytest.fail("must not build without a key"))
    with pytest.raises(RuntimeError, match="CARTESIA_API_KEY"):
        agent_runtime._build_tts("hinglish")


def test_api_get_save_reset_and_preview_validation(monkeypatch):
    from fastapi.testclient import TestClient

    import server

    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    client = TestClient(server.app)
    data = client.get("/api/voice").json()
    ids = {v["id"] for v in data["options"]["cartesia_voices"]}
    assert ARUSHI in ids and len(ids) == len(voice_settings.CARTESIA_VOICES)
    assert data["source"] == "default" and data["cartesia_configured"] is False

    body = {"voice_id": SIYA, "speed": 1.0, "volume": 1.0, "model": "sonic-3.6"}
    saved = client.post("/api/voice", json=body)
    assert saved.status_code == 200 and saved.json()["voice"]["voice_id"] == SIYA
    data = client.get("/api/voice").json()
    assert data["source"] == "dashboard" and "Siya" in data["label"]
    assert client.post("/api/voice", json={**body, "voice_id": "x"}).status_code == 422

    # Preview validates before reaching Cartesia: no network call happens here.
    assert client.post("/api/voice/preview", json={**body, "voice_id": "x", "text": "hello"}).status_code == 422
    bad_lang = {**body, "text": "hello", "language": "fr-FR"}
    assert client.post("/api/voice/preview", json=bad_lang).status_code == 422
    no_key = client.post("/api/voice/preview", json={**body, "text": "hello"})
    assert no_key.status_code == 400 and "CARTESIA_API_KEY" in no_key.json()["detail"]

    assert client.delete("/api/voice").json()["source"] == "default"
