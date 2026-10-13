"""Live and post-call transcription helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger("transcripts")


class CallTranscriptTracker:
    """Collect transcript lines during a call and flush to Supabase periodically."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.lines: list[dict] = []
        self._flush_lock = asyncio.Lock()
        self._pending_flush: Optional[asyncio.Task] = None
        self._version = 0
        self._saved_version = 0
        self._queued = False

    def add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        now = datetime.now(timezone.utc).isoformat()
        if self.lines and self.lines[-1]["role"] == role:
            prev = self.lines[-1]["text"]
            if text == prev:
                return
            if role == "user" or text.startswith(prev) or prev.startswith(text):
                self.lines[-1] = {"role": role, "text": text, "ts": now}
                self._version += 1
                self._schedule_flush()
                return
        self.lines.append({"role": role, "text": text, "ts": now})
        self._version += 1
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._pending_flush and not self._pending_flush.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending_flush = loop.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        try:
            await asyncio.sleep(2)
            await self.flush()
        finally:
            self._pending_flush = None
            # Lines can arrive while a database write is in flight. Ensure that
            # change receives its own later snapshot instead of waiting until
            # call shutdown.
            if self._version > self._saved_version:
                self._schedule_flush()

    async def flush(self) -> bool:
        async with self._flush_lock:
            if not self.lines:
                return True
            if self._saved_version >= self._version:
                return True
            snapshot_version = self._version
            from db import (
                complete_queued_transcript_write,
                enqueue_transcript_write,
                update_call_transcript,
            )
            live_text = "\n".join(
                f"{'Lead' if line['role'] == 'user' else 'Agent'}: {line['text']}"
                for line in self.lines
            )
            transcript_json = json.dumps(self.lines)
            last_error: Optional[Exception] = None
            for attempt, delay in enumerate((0.0, 0.5, 1.5), start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await update_call_transcript(self.call_id, transcript_json, live_text)
                    self._saved_version = max(self._saved_version, snapshot_version)
                    if self._queued:
                        await complete_queued_transcript_write(self.call_id)
                        self._queued = False
                    return True
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Transcript flush attempt %d/3 failed for %s: %s",
                        attempt,
                        self.call_id,
                        exc,
                    )

            try:
                await enqueue_transcript_write(
                    self.call_id,
                    transcript_json,
                    live_text,
                    attempts=3,
                    last_error=str(last_error or "unknown transcript write error"),
                )
                self._queued = True
                logger.warning("Transcript snapshot queued for retry: %s", self.call_id)
            except Exception as queue_exc:
                # Supabase may be entirely unreachable. Keep the in-memory lines
                # intact so a later add/final shutdown flush can try again.
                logger.error(
                    "Transcript queue write failed for %s: %s",
                    self.call_id,
                    queue_exc,
                )
            return False

    def live_text(self) -> str:
        return "\n".join(
            f"{'Lead' if line['role'] == 'user' else 'Agent'}: {line['text']}"
            for line in self.lines
        )

    def json_text(self) -> str:
        return json.dumps(self.lines)


def attach_transcript_handlers(
    session,
    tracker: CallTranscriptTracker,
    on_user_transcript: Optional[Callable[[str], None]] = None,
) -> None:
    """Hook LiveKit session events for user + agent speech."""
    user_callback = on_user_transcript or getattr(tracker, "on_user_transcript", None)

    @session.on("user_input_transcribed")
    def _on_user_input(ev) -> None:
        transcript = getattr(ev, "transcript", "") or ""
        if transcript:
            tracker.add("user", transcript)
            # Partial hypotheses change as the caller speaks and must not
            # overwrite a later confirmed CRM value.
            if user_callback and getattr(ev, "is_final", True):
                user_callback(transcript)

    @session.on("conversation_item_added")
    def _on_conversation_item(ev) -> None:
        item = getattr(ev, "item", None)
        if not item:
            return
        role = getattr(item, "role", "")
        text = getattr(item, "text_content", None) or ""
        if not text and hasattr(item, "content"):
            parts = []
            for part in item.content or []:
                part_text = getattr(part, "text", None) or getattr(part, "transcript", None) or ""
                if part_text:
                    parts.append(part_text)
            text = " ".join(parts)
        if role == "assistant" and text:
            tracker.add("agent", text)
        elif role == "user" and text:
            tracker.add("user", text)
            # Some session configurations emit only conversation items. The
            # collector de-duplicates this against user_input_transcribed.
            if user_callback:
                user_callback(text)

    @session.on("agent_speech_committed")
    def _on_agent_speech(ev) -> None:
        text = getattr(ev, "text", None) or getattr(ev, "transcript", None) or ""
        if text:
            tracker.add("agent", text)


async def replay_pending_transcript_writes(limit: int = 25) -> None:
    """Best-effort replay of snapshots left by earlier failed flushes."""
    try:
        from db import process_transcript_write_queue

        result = await process_transcript_write_queue(limit=limit)
        if result.get("scanned"):
            logger.info(
                "Transcript queue replay: scanned=%d saved=%d failed=%d",
                result.get("scanned", 0),
                result.get("saved", 0),
                result.get("failed", 0),
            )
    except Exception as exc:
        logger.warning("Transcript queue replay unavailable: %s", exc)


async def _download_azure_recording(recording_url: str) -> Optional[bytes]:
    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    key = config.get_env("AZURE_STORAGE_KEY")
    if not (account and key):
        return None

    parsed = urlparse(recording_url)
    if parsed.netloc.lower() != f"{account}.blob.core.windows.net":
        return None

    path = parsed.path.lstrip("/")
    if "/" not in path:
        return None
    container, blob_name = path.split("/", 1)

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        logger.warning("azure-storage-blob not installed — post-call transcript skipped")
        return None

    client = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )
    blob = client.get_blob_client(container=container, blob=blob_name)
    return blob.download_blob().readall()


async def transcribe_recording(recording_url: Optional[str]) -> Optional[str]:
    """Post-call transcription via Deepgram (Hindi + English)."""
    if not recording_url:
        return None

    api_key = config.get_env("DEEPGRAM_API_KEY")
    if not api_key:
        logger.info("DEEPGRAM_API_KEY not set — post-call transcript skipped")
        return None

    audio = await _download_azure_recording(recording_url)
    if not audio:
        logger.warning("Could not download recording for transcription: %s", recording_url)
        return None

    import httpx

    params = {
        "model": "nova-3",
        "language": "multi",
        "smart_format": "true",
        "punctuate": "true",
        "diarize": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/ogg",
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=params,
                headers=headers,
                content=audio,
            )
            resp.raise_for_status()
            data = resp.json()
            transcript = (
                data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
            )
            return transcript.strip() or None
    except Exception as exc:
        logger.warning("Deepgram transcription failed: %s", exc)
        return None


async def run_post_call_transcription(call_id: str, recording_url: Optional[str]) -> None:
    from db import update_final_transcript

    final = await transcribe_recording(recording_url)
    if not final:
        return
    try:
        await update_final_transcript(call_id, final)
        logger.info("Post-call transcript saved for call %s", call_id)
        from call_webhook import schedule_call_completed_webhook
        schedule_call_completed_webhook(call_id)
    except Exception as exc:
        logger.warning("Failed to save final transcript: %s", exc)
