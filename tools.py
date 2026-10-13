import asyncio
import logging
import os
import time
from time import perf_counter
from typing import Optional

from livekit import agents, api
from livekit.agents import llm
from livekit.agents.voice.events import RunContext

from db import (
    check_slot, get_next_available, insert_appointment, log_call, log_error,
    get_calls_by_phone, get_appointments_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
    parse_property_insight, update_call_property_data,
    SlotUnavailableError,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


_GOODBYE_MARKERS = (
    "thank you",
    "thanks",
    "shukriya",
    "dhanyavaad",
    "goodbye",
    "good bye",
    "have a good",
    "have a great",
    "din shubh",
    "phir baat",
    "धन्यवाद",
    "शुक्रिया",
    "दिन शुभ",
)


def _recent_agent_has_goodbye(transcript_tracker) -> bool:
    """Return true when the latest agent turn already contains a real farewell."""
    if not transcript_tracker:
        return False
    lines = getattr(transcript_tracker, "lines", []) or []
    for line in reversed(lines):
        if line.get("role") != "agent":
            continue
        text = " ".join(str(line.get("text", "")).lower().split())
        return any(marker in text for marker in _GOODBYE_MARKERS)
    return False


from prompting.language import resolve_active_call_language


def _farewell_instruction(outcome: str, language: str = "hinglish") -> str:
    if outcome == "callback_requested":
        meaning = "confirm the agreed callback and thank the lead for their time"
    elif outcome == "booked":
        meaning = "confirm that the booking is complete, thank the lead, and wish them well"
    else:
        meaning = "politely acknowledge the decision, thank the lead, and wish them well"

    lang = (language or "hinglish").strip().lower()
    if lang == "english":
        lang_rule = (
            "Speak the closing in clear warm English only "
            '(for example: "Thank you so much, have a great day."). '
            "Do not switch into Hindi or Hinglish for the goodbye."
        )
    elif lang == "hindi":
        lang_rule = (
            "Speak the closing in warm conversational Hindi "
            '(for example: "Bahut dhanyavaad, aapka din shubh ho."). '
            "Do not switch into full English for the goodbye."
        )
    else:
        lang_rule = (
            "Speak the closing in natural Hinglish "
            '(for example: "Bahut shukriya, aapka din shubh ho."). '
            "Do not switch into pure English just for the goodbye."
        )

    return (
        "Before ending the call, say exactly one short closing aloud and nothing else. "
        f"{lang_rule} "
        "Keep the configured Leda speaker and the same warm professional Riya Indian accent, "
        "pitch, pace, and energy — including any English words; never a foreign/US/UK accent. "
        f"The closing should {meaning}. Do not invoke any tool."
    )


async def _await_goodbye_before_hangup(
    ctx: RunContext,
    transcript_tracker=None,
    outcome: str = "",
    language: str = "hinglish",
) -> None:
    """Ensure a farewell is heard, then wait for speech before disconnecting."""
    try:
        await ctx.wait_for_playout()
    except Exception as exc:
        logger.warning("wait_for_playout before hangup failed: %s", exc)

    session = ctx.session
    # A prompt instruction alone is not a reliable call-lifecycle guarantee. If the
    # model invoked end_call without saying goodbye, generate the closing here while
    # the SIP participant is still connected.
    if not _recent_agent_has_goodbye(transcript_tracker):
        try:
            active_language = resolve_active_call_language(
                language,
                transcript_tracker,
            )
            farewell = session.generate_reply(
                instructions=_farewell_instruction(outcome, language=active_language),
                allow_interruptions=False,
            )
            await asyncio.wait_for(
                farewell,
                timeout=float(os.getenv("END_CALL_FAREWELL_TIMEOUT_SEC", "15")),
            )
            logger.info(
                "Fallback farewell completed before hangup (outcome=%s language=%s)",
                outcome,
                active_language,
            )
        except Exception as exc:
            # Never leave a completed call stuck forever if synthesis/provider fails.
            logger.error("Fallback farewell failed before hangup: %s", exc)

    deadline = time.time() + float(os.getenv("END_CALL_SPEECH_WAIT_SEC", "20"))
    while time.time() < deadline:
        state = getattr(session, "agent_state", "listening")
        if state != "speaking":
            await asyncio.sleep(0.25)
            if getattr(session, "agent_state", "listening") != "speaking":
                break
        await asyncio.sleep(0.1)

    await asyncio.sleep(float(os.getenv("END_CALL_HANGUP_DELAY_SEC", "1.2")))


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        *,
        organization_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        project_id: Optional[str] = None,
        allowed_project_ids: Optional[list[str]] = None,
        call_language: str = "hinglish",
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.organization_id = organization_id
        self.lead_id = lead_id
        self.project_id = project_id
        self.call_language = (call_language or "hinglish").strip().lower() or "hinglish"
        self.allowed_project_ids = tuple(
            str(value).strip()
            for value in (allowed_project_ids or [])
            if str(value).strip()
        )
        # Reset via mark_call_connected() at SIP answer so duration ≈ Vobiz billsec
        # (not job-start → hangup, which incorrectly includes ring/AMD time).
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self.call_id: Optional[str] = None
        self.transcript_tracker = None
        self.requirements_collector = None
        self._hangup_done = False
        # Set by the runtime right before SIP dialing starts.
        self.sip_dial_started = False
        super().__init__(tools=[])

    def mark_call_connected(self) -> None:
        """Start billable timer when the callee answers (matches Vobiz connected time)."""
        self._call_start_time = time.time()

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
        ]
        if not enabled:
            return all_methods
        name_map: dict[str, object] = {}
        for m in all_methods:
            api_name = getattr(getattr(m, "info", None), "name", None) or m.__name__
            name_map[api_name] = m
            if m.__name__ != api_name:
                name_map[m.__name__] = m
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """
        Check whether a date/time slot is available for booking.
        Use this only when the lead is exploring possible times before final confirmation.
        Do NOT call this immediately before book_appointment: booking performs its own
        atomic availability check to avoid a duplicate database round trip.
        date format: YYYY-MM-DD  |  time format: HH:MM (24-hour)
        Returns 'available' or 'unavailable: next available slot is <slot>'.
        """
        try:
            if await check_slot(date, time):
                return "available"
            next_slot = await get_next_available(date, time)
            return f"unavailable: next available slot is {next_slot}"
        except Exception as exc:
            return "Unable to check availability right now — please suggest a date and I will confirm."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """
        Atomically check and book after the lead has verbally confirmed date, time, service,
        and booking under their remembered name.
        Call ONLY after the lead confirms all details.
        name: use the remembered/CRM lead name already known on this call — NEVER ask the lead
        for their name again at booking time | phone: with country code | date: YYYY-MM-DD |
        time: HH:MM | service: type
        """
        started = perf_counter()
        try:
            scoped_phone = self.phone_number or phone
            booking_id = await insert_appointment(
                name,
                scoped_phone,
                date,
                time,
                service,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
                project_id=self.project_id,
                call_id=self.call_id,
            )
            logger.info(
                "Atomic booking completed date=%s time=%s elapsed_ms=%d",
                date,
                time,
                int((perf_counter() - started) * 1000),
            )
            return f"Slot booked for {date} at {time}. Tell the lead it is confirmed — do not read IDs aloud."
        except SlotUnavailableError as exc:
            logger.info(
                "Atomic booking unavailable date=%s time=%s elapsed_ms=%d",
                date,
                time,
                int((perf_counter() - started) * 1000),
            )
            next_slot = exc.next_slot or "another time"
            return (
                f"unavailable: that slot was just taken. "
                f"Next available is {next_slot}. Offer that or ask for another preference."
            )
        except Exception as exc:
            logger.warning(
                "Atomic booking failed date=%s time=%s elapsed_ms=%d error=%s",
                date,
                time,
                int((perf_counter() - started) * 1000),
                exc,
            )
            return "Technical issue saving the booking. Our team will confirm shortly."

    @llm.function_tool(
        name="end_call",
        description=(
            "Internal only: log call outcome and disconnect. Invoke ONLY after you have fully "
            "spoken your final goodbye aloud (e.g. 'Bahut shukriya, aapka din shubh ho!'). "
            "Never call this before the goodbye sentence is complete. The caller must hear "
            "NOTHING about this step — no tool names, no English system words. "
            "Outcomes: booked | interested | not_interested | wrong_number | voicemail | "
            "no_answer | callback_requested."
        ),
    )
    async def end_call(
        self,
        ctx: RunContext,
        outcome: str,
        reason: str = "",
        scheduled_at: str = "",
        callback_urgency: str = "",
    ) -> str:
        """
        Log outcome and disconnect. Invoke ONLY after goodbye is already spoken aloud.
        outcome: booked | interested | not_interested | wrong_number | voicemail | no_answer | callback_requested
        reason: brief note for CRM
        scheduled_at: REQUIRED when outcome=callback_requested and lead gave a time —
            prefer ISO-8601 UTC, or clear relative text like "tomorrow 5pm IST" / "in 5 minutes".
            Omit when lead only said "later" with no usable time.
            For off-topic scope-lock closes: omit scheduled_at entirely.
        callback_urgency: now | later | specific_time | none (for callback_requested).
            Use "none" for off-topic closes so Node does not auto-dial.
        """
        await _await_goodbye_before_hangup(
            ctx,
            self.transcript_tracker,
            outcome,
            language=self.call_language,
        )

        duration = int(time.time() - self._call_start_time)
        live_transcript = None
        transcript_json = None
        if self.requirements_collector:
            await self.requirements_collector.flush()
        if self.transcript_tracker:
            await self.transcript_tracker.flush()
            live_transcript = self.transcript_tracker.live_text()
            transcript_json = self.transcript_tracker.json_text()
        room_name = self.ctx.room.name if self.ctx.room else None
        # Hang up SIP/room and confirm absence BEFORE CRM webhook.
        hangup_ok = False
        try:
            hangup_ok = await self._hangup_call()
        except Exception as exc:
            logger.error("Hangup failed after end_call: %s", exc)
            await _log("error", f"Hangup failed after end_call: {exc}", level="error")
        if not hangup_ok:
            # One retry — never finalize/release while SIP state is unknown/live.
            try:
                hangup_ok = await self._hangup_call()
            except Exception as exc:
                logger.error("Hangup retry failed after end_call: %s", exc)
        if not hangup_ok:
            logger.error(
                "Skipping log_call/webhook — SIP line not confirmed clear after end_call"
            )
            await _log(
                "error",
                "Skipping log_call — SIP not confirmed absent after end_call",
                level="error",
            )
            return ""

        try:
            call_id = await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name, outcome=outcome, reason=reason,
                duration_seconds=duration, recording_url=self.recording_url,
                call_id=self.call_id, live_transcript=live_transcript,
                transcript_json=transcript_json,
                room_name=room_name,
                scheduled_at=(scheduled_at or "").strip() or None,
                callback_urgency=(callback_urgency or "").strip() or None,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
                project_id=self.project_id,
            )
            if self.recording_url and os.getenv("ENABLE_POST_CALL_TRANSCRIPT", "").lower() == "true":
                from transcripts import run_post_call_transcription
                asyncio.create_task(run_post_call_transcription(call_id, self.recording_url))
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)

        return ""

    def _sip_identity(self, room=None) -> Optional[str]:
        if self.phone_number:
            return f"sip_{self.phone_number}"
        room = room or (self.ctx.room if self.ctx else None)
        if not room:
            return None
        try:
            for p in (room.remote_participants or {}).values():
                ident = getattr(p, "identity", "") or ""
                if ident.startswith("sip_"):
                    return ident
        except Exception:
            pass
        return None

    async def _hangup_call(self) -> bool:
        """Tear down SIP/room and confirm the phone line is clear.

        Returns True only when LiveKit confirms the room is gone or the SIP
        participant is absent. Agent-only disconnect is NOT success.
        """
        if self._hangup_done:
            return True
        room = self.ctx.room
        if not room or not getattr(room, "name", None):
            logger.warning("Hangup skipped — no LiveKit room")
            return False
        room_name = room.name
        lk_api = getattr(self.ctx, "api", None)
        if not lk_api:
            logger.warning("Hangup skipped — LiveKit API client unavailable")
            return False

        from sip_hangup import force_delete_room_and_confirm, sip_line_confirmed_clear

        sip_identity = self._sip_identity(room)

        # Prefer DeleteRoom (forcibly disconnects all participants).
        try:
            await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
            logger.info("delete_room issued (%s) — confirming SIP absent", room_name)
            await _log("info", f"delete_room issued ({room_name})")
        except Exception as exc:
            if "not found" in str(exc).lower() or "404" in str(exc).lower():
                logger.info("Room already deleted (%s)", room_name)
            else:
                logger.warning("delete_room failed (%s), trying remove_participant", exc)
                if sip_identity:
                    try:
                        await lk_api.room.remove_participant(
                            api.RoomParticipantIdentity(
                                room=room_name, identity=sip_identity
                            )
                        )
                        logger.info("remove_participant issued (%s)", sip_identity)
                        await _log(
                            "info",
                            f"remove_participant issued ({sip_identity})",
                        )
                    except Exception as rem_exc:
                        logger.warning("remove_participant failed (%s)", rem_exc)

        confirmed = await sip_line_confirmed_clear(
            lk_api,
            room_name=room_name,
            sip_identity=sip_identity,
        )
        if not confirmed:
            # One forced delete+confirm pass before giving up.
            confirmed = await force_delete_room_and_confirm(
                lk_api,
                room_name=room_name,
                sip_identity=sip_identity,
            )

        if confirmed:
            self._hangup_done = True
            logger.info(
                "Call hung up — SIP confirmed clear (room=%s identity=%s)",
                room_name,
                sip_identity,
            )
            await _log(
                "info",
                f"Call hung up — SIP confirmed clear ({room_name})",
            )
            try:
                await room.disconnect()
            except Exception:
                pass
            return True

        # Fail-closed: do not pretend the line is free. Agent disconnect only
        # for local cleanup — callers must NOT finalize/release/webhook.
        try:
            await room.disconnect()
            logger.warning(
                "Hangup incomplete — agent disconnected only; phone line may still be open"
            )
            await _log(
                "warning",
                "Hangup incomplete — SIP not confirmed absent",
                level="warning",
            )
        except Exception:
            pass
        return False

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Call when lead requests a human, is angry, or has a complex issue.
        reason: why you're transferring
        """
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination, play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception as exc:
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send SMS confirmation after a successful booking. Skips silently if Twilio not configured.
        phone: lead's phone | message: text to send
        """
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        # Only ever text the lead on this call — the model-supplied number is
        # ignored so a caller cannot redirect messages to a third party.
        to_number = (self.phone_number or "").strip()
        if not to_number:
            return "SMS skipped: no lead number on this call."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=to_number))
            return "SMS sent to the lead."
        except Exception as exc:
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Only use mid-call if PRIOR CONTACT DATA was not provided.
        phone: the lead's phone number with country code
        Returns call history, appointments, and remembered details.
        """
        try:
            scoped_phone = self.phone_number or phone
            if self.phone_number and phone and phone.strip() != self.phone_number.strip():
                return "Unable to retrieve a different contact during this call."
            calls = await get_calls_by_phone(
                scoped_phone,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            appointments = await get_appointments_by_phone(
                scoped_phone,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            memories = await get_contact_memory(
                scoped_phone,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            if not calls and not appointments and not memories:
                return f"No history for {scoped_phone} in this organization. First-time contact."
            lines = [f"Contact history for {scoped_phone} in this organization:"]
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:10]:
                    lines.append(f"  • {m['insight']}")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  • {ts} — {c.get('outcome','?')}: {c.get('reason','')}")
                    from callback_schedule import format_callback_for_agent
                    callback_schedule = format_callback_for_agent(c)
                    if callback_schedule:
                        lines.append(f"    CALLBACK SCHEDULE: {callback_schedule}")
                    elif (c.get("outcome") or "").strip().lower() == "callback_requested":
                        lines.append("    CALLBACK SCHEDULE: exact date/time was not saved; do not guess.")
            if appointments:
                lines.append(f"\nAPPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(f"  • {a.get('date')} {a.get('time')} — {a.get('service')} [{a.get('status')}]")
            return "\n".join(lines)
        except Exception as exc:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        Examples: "Prefers morning calls", "Has 2 kids, interested in family plan", "Callback in 2 weeks"
        insight: the detail to remember
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(
                self.phone_number,
                insight,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            parsed = parse_property_insight(insight)
            if parsed and self.call_id:
                field, value = parsed
                await update_call_property_data(self.call_id, field, value)
            memories = await get_contact_memory(
                self.phone_number,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return "saved"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(
                self.phone_number,
                organization_id=self.organization_id,
                lead_id=self.lead_id,
            )
            if len(memories) < 5:
                return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these notes about a sales contact into 3-5 concise bullets. Keep all key facts.\n\n{bullet_list}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(
                    self.phone_number,
                    response.text.strip(),
                    organization_id=self.organization_id,
                    lead_id=self.lead_id,
                )
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """
        Book in Cal.com calendar after book_appointment succeeds.
        name: full name | email: lead's email | date: YYYY-MM-DD | start_time: HH:MM | notes: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone,
                          "responses": {"name": name, "email": email, "notes": notes},
                          "metadata": {"source": "OutboundAI"}, "language": "en"},
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            uid = data.get("uid", "")
            return f"Cal.com booked. UID: {uid}"
        except Exception as exc:
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """
        Cancel a Cal.com booking by UID.
        booking_uid: from book_calcom | reason: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {},
                )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
