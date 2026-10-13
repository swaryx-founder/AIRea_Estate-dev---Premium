"""Dashboard Latency tab: per-call event log and its API (no call, no network)."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")

import latency_log  # noqa: E402


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LATENCY_LOG_DIR", str(tmp_path))
    return tmp_path


def _call(cid="call-1"):
    latency_log.record(cid, {"type": "call", "lead": "Prashant", "phone_tail": "1734", "target": 1.5,
                             "models": {"stt": "sarvam", "llm": "mercury-2.5", "tts": "cartesia"}})
    latency_log.record(cid, {"type": "greeting", "first_word": 0.1})
    latency_log.record(cid, {"type": "message", "role": "lead", "text": "haan ji"})
    latency_log.record(cid, {"type": "turn", "total": 1.2, "eou": 0.5, "stt": 0.1, "llm": 0.6, "tts": 0.1})
    latency_log.record(cid, {"type": "message", "role": "agent", "text": "Ji, boliye"})
    latency_log.record(cid, {"type": "turn", "total": 2.0, "eou": 0.7, "llm": 1.1, "tts": 0.2})


def test_summary_averages_each_model_and_counts_slow_replies():
    _call()
    s = latency_log.summarize(latency_log.read("call-1"))
    assert s["turns"] == 2 and s["slow"] == 1 and s["max_total"] == 2.0
    assert s["avg"] == {"total": 1.6, "eou": 0.6, "stt": 0.1, "llm": 0.85, "tts": 0.15}
    assert s["greeting_first_word"] == 0.1 and s["ended"] is False
    latency_log.record("call-1", {"type": "end"})
    assert latency_log.summarize(latency_log.read("call-1"))["ended"] is True


def test_unsafe_ids_are_refused_and_recording_never_raises(log_dir):
    latency_log.record("../../etc/passwd", {"type": "turn"})  # silently ignored
    assert list(log_dir.iterdir()) == []
    with pytest.raises(ValueError):
        latency_log.read("../secret")


def test_api_lists_calls_and_returns_timeline(monkeypatch):
    from fastapi.testclient import TestClient

    import server

    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    _call("f75cecce-f01f-4332-824d-dd1eab068a3a")
    client = TestClient(server.app)
    calls = client.get("/api/latency/calls").json()["calls"]
    assert calls[0]["call_id"].startswith("f75cecce") and calls[0]["lead"] == "Prashant"
    data = client.get("/api/latency/calls/f75cecce-f01f-4332-824d-dd1eab068a3a").json()
    assert [e["type"] for e in data["events"]][:3] == ["call", "greeting", "message"]
    assert data["summary"]["avg"]["total"] == 1.6
    assert client.get("/api/latency/calls/nope").status_code == 404
    assert client.get("/api/latency/calls/bad%20id").status_code == 422


def test_usage_totals_and_today_summary():
    latency_log.record("call-u", {"type": "call", "lead": "A", "target": 1.5})
    latency_log.record("call-u", {"type": "usage", "kind": "llm", "prompt": 6000, "cached": 0, "completion": 40, "total": 6040})
    latency_log.record("call-u", {"type": "usage", "kind": "llm", "prompt": 6100, "cached": 5000, "completion": 30, "total": 6130})
    latency_log.record("call-u", {"type": "usage", "kind": "tts", "chars": 120, "audio_s": 7.5})
    latency_log.record("call-u", {"type": "usage", "kind": "stt", "audio_s": 5.0})
    latency_log.record("call-u", {"type": "turn", "total": 1.6, "eou": 0.5, "llm": 1.0, "tts": 0.1})
    u = latency_log.summarize(latency_log.read("call-u"))["usage"]
    assert u == {"llm_requests": 2, "llm_prompt": 12100, "llm_cached": 5000, "llm_completion": 70,
                 "llm_total": 12170, "tts_chars": 120, "tts_audio_s": 7.5, "stt_audio_s": 5.0}
    today = latency_log.today_summary()
    assert today["calls"] == 1 and today["llm_total"] == 12170 and today["avg_reply"] == 1.6
