"""Per-call latency + transcript timeline for the dashboard Latency tab (worker -> server).

The worker appends one JSON line per event to runtime/latency/<call_id>.jsonl:
  {"type": "call", ...models...}            once, when the session starts
  {"type": "message", "role", "text"}       each transcript line (lead / agent)
  {"type": "turn", "eou", "stt", "llm", "tts", "total"}   each measured reply
  {"type": "greeting", "first_word"}         pickup -> first greeting word
  {"type": "usage", "kind": "llm"|"tts"|"stt", ...}   real model usage per request
  {"type": "heard", "s"}                     what the lead waited: lead stops -> agent audio
The server reads the files and the dashboard polls them, so latency shows live while a
call runs. Local files (not Supabase) keep debug data out of the production database;
on Azure with separate worker/server containers this page sees only its own machine.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

_MAX_FILES = 200
_lock = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.+-]{1,120}$")


def log_dir() -> Path:
    raw = (os.getenv("LATENCY_LOG_DIR") or "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent / "runtime" / "latency"


def _path(call_id: str) -> Path:
    if not _SAFE_ID.match(call_id or ""):
        raise ValueError("invalid call id")
    return log_dir() / f"{call_id}.jsonl"


def record(call_id: str, event: dict[str, Any]) -> None:
    """Append one event. Never raises: latency logging must not break a call."""
    try:
        path = _path(call_id)
        line = json.dumps({"t": round(time.time(), 3), **event}, ensure_ascii=False)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if new_file:
            _prune()
    except Exception:
        pass


def _prune() -> None:
    files = sorted(log_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[_MAX_FILES:]:
        old.unlink(missing_ok=True)


def read(call_id: str) -> list[dict[str, Any]]:
    events = []
    with _path(call_id).open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue  # a line being written right now
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [e for e in events if e.get("type") == "turn"]
    info = next((e for e in events if e.get("type") == "call"), {})

    def avg(key: str):
        vals = [t[key] for t in turns if isinstance(t.get(key), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    greeting = next((e.get("first_word") for e in events if e.get("type") == "greeting"), None)
    heard = [float(e["s"]) for e in events if e.get("type") == "heard" and isinstance(e.get("s"), (int, float))]
    return {
        "heard": {"first": heard[0] if heard else None,
                  "avg": round(sum(heard) / len(heard), 3) if heard else None,
                  "count": len(heard)},
        "usage": usage_totals(events),
        "turns": len(turns),
        "avg": {k: avg(k) for k in ("total", "eou", "stt", "llm", "tts")},
        "max_total": max((t["total"] for t in turns), default=None),
        "slow": sum(1 for t in turns if t["total"] > info.get("target", 1.5)),
        "greeting_first_word": greeting,
        "ended": any(e.get("type") == "end" for e in events),
    }


def usage_totals(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Mercury tokens, Cartesia characters and Sarvam audio seconds for one call."""
    out = {"llm_requests": 0, "llm_prompt": 0, "llm_cached": 0, "llm_completion": 0, "llm_total": 0,
           "tts_chars": 0, "tts_audio_s": 0.0, "stt_audio_s": 0.0}
    for e in events:
        if e.get("type") != "usage":
            continue
        kind = e.get("kind")
        if kind == "llm":
            out["llm_requests"] += 1
            out["llm_prompt"] += int(e.get("prompt") or 0)
            out["llm_cached"] += int(e.get("cached") or 0)
            out["llm_completion"] += int(e.get("completion") or 0)
            out["llm_total"] += int(e.get("total") or 0)
        elif kind == "tts":
            out["tts_chars"] += int(e.get("chars") or 0)
            out["tts_audio_s"] += float(e.get("audio_s") or 0.0)
        elif kind == "stt":
            out["stt_audio_s"] += float(e.get("audio_s") or 0.0)
    out["tts_audio_s"] = round(out["tts_audio_s"], 1)
    out["stt_audio_s"] = round(out["stt_audio_s"], 1)
    return out


def today_summary() -> dict[str, Any]:
    """Usage across all calls whose log changed today (local time)."""
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    keys = ("llm_requests", "llm_prompt", "llm_cached", "llm_completion", "llm_total",
            "tts_chars", "tts_audio_s", "stt_audio_s")
    total = {k: 0 for k in keys}
    calls = turns = 0
    reply_sum = 0.0
    for path in log_dir().glob("*.jsonl"):
        try:
            if path.stat().st_mtime < start:
                continue
            events = read(path.stem)
        except (OSError, ValueError):
            continue
        calls += 1
        for k, v in usage_totals(events).items():
            total[k] += v
        for t in (e for e in events if e.get("type") == "turn"):
            turns += 1
            reply_sum += float(t.get("total") or 0.0)
    total["tts_audio_s"] = round(total["tts_audio_s"], 1)
    total["stt_audio_s"] = round(total["stt_audio_s"], 1)
    return {"calls": calls, "turns": turns,
            "avg_reply": round(reply_sum / turns, 3) if turns else None, **total}


def list_calls(limit: int = 30) -> list[dict[str, Any]]:
    out = []
    files = sorted(log_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        try:
            events = read(path.stem)
        except (OSError, ValueError):
            continue
        info = next((e for e in events if e.get("type") == "call"), {})
        out.append({
            "call_id": path.stem,
            "started": info.get("t"),
            "updated": path.stat().st_mtime,
            "lead": info.get("lead"),
            "phone_tail": info.get("phone_tail"),
            **summarize(events),
        })
    return out
