"""CI smoke checks — no API keys or network required."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_phone_normalization():
    from phone_utils import normalize_e164_phone

    assert normalize_e164_phone("9876543210") == "+919876543210"
    assert normalize_e164_phone("919876543210") == "+919876543210"
    assert normalize_e164_phone("+919876543210") == "+919876543210"
    assert normalize_e164_phone("09876543210") == "+919876543210"
    assert normalize_e164_phone("+91 98765 43210") == "+919876543210"
    assert normalize_e164_phone("987654321") is None
    assert normalize_e164_phone("") is None
    print("OK phone normalization")


def test_whatsapp_helpers():
    from whatsapp import should_send_whatsapp, whatsapp_enabled

    os.environ["WHATSAPP_BACKEND_ENABLED"] = "true"
    os.environ["WHATSAPP_BACKEND_SEND_URL"] = "https://gateway.example/api/calls/whatsapp/send"
    assert whatsapp_enabled() is True
    assert should_send_whatsapp("booked") is True
    assert should_send_whatsapp("no_answer") is False
    assert should_send_whatsapp("callback_requested", {"whatsapp_confirmed": True}) is True
    assert should_send_whatsapp("callback_requested", {"whatsapp_confirmed": False}) is False
    assert should_send_whatsapp("callback_requested", {"whatsapp_number": "nahi"}) is False
    os.environ["WHATSAPP_BACKEND_ENABLED"] = "false"
    assert whatsapp_enabled() is False
    print("OK whatsapp helpers")


def test_call_request_ignores_whatsapp_auth_tokens():
    """WhatsApp authentication belongs to the backend, not the AI service."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import CallRequest

    req = CallRequest(**{
        "phone": "+919876543210",
        "whatsapp_auth_token": "legacy-token",
        "credentials": {
            "gemini_api_key": "test-key",
            "whatsapp_auth_token": "legacy-nested-token",
        },
    })
    assert not hasattr(req, "whatsapp_auth_token")
    assert req.credentials is not None
    assert not hasattr(req.credentials, "whatsapp_auth_token")
    print("OK WhatsApp auth tokens are ignored by AI request models")


def test_email_followup_helpers():
    from email_followup import (
        build_followup_template,
        is_valid_email,
        should_send_email_followup,
    )

    assert is_valid_email("agent@example.com") is True
    assert is_valid_email("not-an-email") is False
    assert should_send_email_followup("completed", "booked") is True
    assert should_send_email_followup("completed", "no_answer") is False
    assert should_send_email_followup("failed", "failed") is False
    html = build_followup_template(
        lead_name="Rahul",
        lead_phone="+919876543210",
        duration=81,
        outcome="booked",
        summary="Interested in Green Valley",
    )
    assert "Rahul" in html
    assert "Call summary" in html
    assert "Green Valley" in html
    print("OK email follow-up helpers")


def test_imports():
    import config  # noqa: F401
    import call_webhook  # noqa: F401
    import whatsapp  # noqa: F401
    import email_followup  # noqa: F401
    import recordings  # noqa: F401
    import transcripts  # noqa: F401
    print("OK core module imports")


def test_gemini_38_model_is_consistent():
    from agent import _resolve_gemini_model, _resolve_gemini_voice
    from config import DEFAULTS
    from server import AgentProfileRequest

    expected = "gemini-3.8-live"
    assert DEFAULTS["GEMINI_MODEL"] == expected
    assert AgentProfileRequest(name="test").model == expected
    assert _resolve_gemini_model("gemini-3.1-flash-live-preview") == expected
    assert _resolve_gemini_voice(None) == "Leda"
    assert _resolve_gemini_voice("Puck") == "Leda"
    print("OK Gemini 3.8 Live model + Leda voice consistency")


def test_hinglish_leda_voice_continuity_in_every_prompt_mode():
    from prompts import build_prompt

    prompts = [
        build_prompt(lead_name="Rahul", business_name="99 Acres"),
        build_prompt(
            lead_name="Rahul",
            business_name="99 Acres",
            project_details="Project Name: Green Valley\nLocation: Noida",
        ),
        build_prompt(
            lead_name="Rahul",
            business_name="99 Acres",
            projects=[
                {"projectDetails": "Project Name: Green Valley\nLocation: Noida"},
                {"projectDetails": "Project Name: Sky Towers\nLocation: Delhi"},
            ],
        ),
        build_prompt(
            lead_name="Rahul",
            business_name="99 Acres",
            custom_prompt="Custom call flow for {lead_name}.",
            additional_instructions="Greet with Radhe Radhe.",
        ),
    ]
    for prompt in prompts:
        assert prompt.count("CALL LANGUAGE MODE —") == 1
        assert "ALWAYS the configured Leda voice" in prompt
        assert "CALL LANGUAGE MODE —" in prompt
        assert "Never imitate the lead's voice" in prompt
        assert "INDIAN ACCENT + PITCH LOCK" in prompt
        assert "never slip into US, UK, Australian" in prompt
        assert "Speak EVERY English word or phrase" in prompt
        assert "accent and pitch NEVER" in prompt
    # Default / non-language campaigns stay Hinglish
    assert "CALL LANGUAGE — HINGLISH" in prompts[0] or "CALL LANGUAGE MODE — HINGLISH" in prompts[0]
    assert "Hello… kya main" in prompts[0]
    assert "OPENING EXCEPTION" not in prompts[0]
    print("OK Leda/Riya Indian-tone continuity in all prompt modes")


def test_property_insight_parser():
    from db import parse_property_insight, parse_property_data

    assert parse_property_insight("Budget: 50-70 lakh") == ("budget", "50-70 lakh")
    assert parse_property_insight("City/area: Pune") == ("city", "Pune")
    assert parse_property_insight("Requested callback") is None
    merged = parse_property_data('{"budget":"1Cr","city":"Delhi"}')
    assert merged["budget"] == "1Cr"
    print("OK property_data helpers")


def test_project_context_injection():
    from prompts import build_prompt

    base = build_prompt(lead_name="Rahul", business_name="99 Acres")
    assert "PROJECT CALL FLOW" not in base
    assert "DISCOVERY TRACK" in base
    assert "PHONE TALK-TRACK" in base

    with_project = build_prompt(
        lead_name="Rahul",
        business_name="99 Acres",
        project_details="Project Name: Green Valley\nLocation: Noida\nStarting Price: 80 lakh",
    )
    assert "PROJECT CALL FLOW" in with_project
    assert "PROJECT PITCH TRACK" in with_project
    assert "Green Valley" in with_project
    assert "STEP 3 — PITCH" in with_project
    assert "kis area ya city mein property dekh rahe hain" not in with_project
    print("OK project vs default prompt branching")


def test_default_when_project_details_empty():
    from prompts import build_prompt

    for empty in (None, "", "   "):
        p = build_prompt(lead_name="Rahul", business_name="99 Acres", project_details=empty)
        assert "DISCOVERY TRACK" in p
        assert "STEP 3 — DISCOVERY TRACK" in p
        assert "PROJECT CALL FLOW" not in p
    print("OK empty project_details uses default prompt")


def test_call_request_project_alias():
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import CallRequest

    req = CallRequest(**{
        "phone": "+919876543210",
        "lead_name": "Rahul",
        "from_number": "+918065354603",
        "project_id": "proj_1",
        "projectDetails": "Project Name: Green Valley",
    })
    assert req.project_id == "proj_1"
    assert req.project_details == "Project Name: Green Valley"
    print("OK CallRequest accepts projectDetails alias")


def test_livekit_trunk_id_contract():
    import asyncio

    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import (
        BatchCallRequest,
        CallRequest,
        CampaignRequest,
        _prepare_call_work_items,
        _resolve_sip_trunk,
    )

    snake = CallRequest(
        phone="+919876543210",
        livekit_trunk_id="ST_org_a",
    )
    camel = CallRequest(**{
        "phone": "+919876543210",
        "livekitTrunkId": "ST_org_b",
    })
    batch = BatchCallRequest(
        contacts=[],
        livekit_trunk_id="ST_org_c",
    )
    campaign = CampaignRequest(
        name="Trunk contract",
        contacts=[],
        livekitTrunkId="ST_org_d",
    )

    assert snake.livekit_trunk_id == "ST_org_a"
    assert camel.livekit_trunk_id == "ST_org_b"
    assert batch.livekit_trunk_id == "ST_org_c"
    assert campaign.livekit_trunk_id == "ST_org_d"

    class MustNotBeUsed:
        def __getattr__(self, name):
            raise AssertionError(f"LiveKit API was unexpectedly used: {name}")

    unused = MustNotBeUsed()
    assert asyncio.run(_resolve_sip_trunk(unused, unused, {}, "ST_org_a")) == "ST_org_a"
    assert asyncio.run(_resolve_sip_trunk(unused, unused, {"trunk_id": "ST_legacy"})) == "ST_legacy"
    assert asyncio.run(_resolve_sip_trunk(unused, unused, {})) is None

    work, responses, invalid = asyncio.run(_prepare_call_work_items(
        [{
            "phone": "+919876543210",
            "organization_id": "org_a",
            "lead_id": "lead_a",
        }],
        room_prefix="test",
        default_credentials={"gemini_api_key": "test-key", "vobiz": {}, "livekit": {}},
    ))
    assert work == [] and responses == []
    assert "livekit_trunk_id" in invalid[0]["error"]
    print("OK livekit_trunk_id contract for call, batch, and campaign")


def test_property_insight_aliases_and_budget_parse():
    from db import parse_property_insight, parse_budget_range, enrich_property_data_for_webhook

    assert parse_property_insight("Preferred location: Less pollution") == (
        "location_preference", "Less pollution",
    )
    assert parse_property_insight("Location suit: Yes") == ("location_fit", "Yes")
    assert parse_property_insight("WhatsApp: 9876543210") == ("whatsapp_number", "9876543210")
    assert parse_budget_range("3-4 Lakhs") == (300_000, 400_000)
    assert parse_budget_range("50-70 lakh") == (5_000_000, 7_000_000)
    assert parse_budget_range("1 Cr") == (10_000_000, 10_000_000)
    assert parse_budget_range("3 से 4 लाख") == (300_000, 400_000)
    assert parse_budget_range("Yes") == (None, None)
    enriched = enrich_property_data_for_webhook({
        "budget": "3-4 Lakhs",
        "location_preference": "Less pollution",
        "city": "Ghaziabad",
        "bhk": "3BHK",
    })
    assert enriched["preferred_location"] == "Ghaziabad"
    assert enriched["city"] == "Ghaziabad"
    assert enriched["min_budget"] == 300_000
    assert enriched["max_budget"] == 400_000
    # Soft preference alone still fills preferred_location for Node cards.
    soft = enrich_property_data_for_webhook({"location_preference": "Less pollution"})
    assert soft["preferred_location"] == "Less pollution"
    assert "city" not in soft or soft.get("city") in (None, "")
    print("OK property insight aliases + budget parse")


def test_call_request_from_number_optional():
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import CallRequest

    req = CallRequest(**{"phone": "+919876543210", "lead_name": "Rahul"})
    assert req.resolved_from_number() is None
    print("OK CallRequest allows omitting from_number (agent auto-leases)")


def test_end_call_enforces_goodbye_before_hangup():
    import asyncio
    import inspect
    from tools import (
        AppointmentTools,
        _await_goodbye_before_hangup,
        _farewell_instruction,
        _recent_agent_has_goodbye,
    )

    source = inspect.getsource(AppointmentTools.end_call)
    assert "await self._hangup_call()" in source
    assert "await _await_goodbye_before_hangup(" in source
    assert "language=self.call_language" in source
    assert "Skipping log_call/webhook" in source
    assert "_delayed_hangup" not in source
    assert not _recent_agent_has_goodbye(None)
    assert not _recent_agent_has_goodbye(type("Tracker", (), {"lines": [
        {"role": "agent", "text": "Sure, kis preferred time par call kar sakte hain?"},
    ]})())
    assert _recent_agent_has_goodbye(type("Tracker", (), {"lines": [
        {"role": "agent", "text": "Aapka samay dene ke liye bahut shukriya."},
    ]})())
    callback_farewell = _farewell_instruction("callback_requested", language="hinglish")
    assert "Hinglish" in callback_farewell
    assert "Leda" in callback_farewell
    assert "confirm the agreed callback" in callback_farewell
    assert "Bahut shukriya" in callback_farewell
    english_farewell = _farewell_instruction("booked", language="english")
    assert "English" in english_farewell
    assert "Thank you so much" in english_farewell
    hindi_farewell = _farewell_instruction("not_interested", language="hindi")
    assert "Hindi" in hindi_farewell
    assert "Bahut dhanyavaad" in hindi_farewell

    class FakeSession:
        agent_state = "listening"

        def __init__(self):
            self.generated = []

        async def generate_reply(self, **kwargs):
            self.generated.append(kwargs)

    class FakeContext:
        def __init__(self):
            self.session = FakeSession()

        async def wait_for_playout(self):
            return None

    old_speech_wait = os.environ.get("END_CALL_SPEECH_WAIT_SEC")
    old_hangup_delay = os.environ.get("END_CALL_HANGUP_DELAY_SEC")
    os.environ["END_CALL_SPEECH_WAIT_SEC"] = "0"
    os.environ["END_CALL_HANGUP_DELAY_SEC"] = "0"
    try:
        missing_ctx = FakeContext()
        asyncio.run(_await_goodbye_before_hangup(missing_ctx, None, "callback_requested"))
        assert len(missing_ctx.session.generated) == 1
        assert missing_ctx.session.generated[0]["allow_interruptions"] is False

        existing_ctx = FakeContext()
        existing = type("Tracker", (), {"lines": [
            {"role": "agent", "text": "Bahut shukriya ji, aapka din shubh ho."},
        ]})()
        asyncio.run(_await_goodbye_before_hangup(existing_ctx, existing, "not_interested"))
        assert existing_ctx.session.generated == []
    finally:
        if old_speech_wait is None:
            os.environ.pop("END_CALL_SPEECH_WAIT_SEC", None)
        else:
            os.environ["END_CALL_SPEECH_WAIT_SEC"] = old_speech_wait
        if old_hangup_delay is None:
            os.environ.pop("END_CALL_HANGUP_DELAY_SEC", None)
        else:
            os.environ["END_CALL_HANGUP_DELAY_SEC"] = old_hangup_delay
    print("OK end_call enforces a non-duplicated goodbye before hangup")


def test_server_app():
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from fastapi.testclient import TestClient
    from server import app

    assert app.title == "OutboundAI Dashboard"
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/api/health" in routes
    assert "/health" in routes
    # /api/health must be the detailed readiness handler (not the simple liveness clone).
    client = TestClient(app)
    readiness = client.get("/api/health").json()
    assert readiness.get("status") == "ok"
    assert "recording_storage" in readiness
    assert "gemini_key_pool" in readiness
    liveness = client.get("/health").json()
    assert liveness.get("status") == "ok"
    assert "recording_storage" not in liveness
    print("OK FastAPI app + /api/health readiness + /health liveness")


def test_multi_project_prompt():
    from prompts import build_prompt, normalize_projects

    projects = normalize_projects(
        projects=[
            {"project_id": "p1", "projectDetails": "Project Name: Green Valley\nLocation: Noida\nStarting Price: 80 lakh"},
            {"project_id": "p2", "projectDetails": "Project Name: Sky Towers\nLocation: Delhi\nStarting Price: 1.2 Cr"},
        ],
    )
    assert len(projects) == 2
    prompt = build_prompt(lead_name="Rahul", business_name="99 Acres", projects=projects)
    assert "MULTI-PROJECT CALL FLOW" in prompt
    assert "flexible guide — not a rigid script" in prompt
    assert "HUMAN FIRST" in prompt
    assert "Green Valley" in prompt
    assert "Sky Towers" in prompt
    assert "2 projects" in prompt
    assert "FIRST PROJECT PITCH" in prompt
    assert 'Never call a project "newly launched"' in prompt
    assert "Discuss only one" in prompt
    assert "project at a time" in prompt
    assert "P1→P2" in prompt
    assert "FIRST PROJECT" in prompt
    assert "ANSWER BY FIT" in prompt
    print("OK multi-project prompt")


def test_single_project_from_array():
    from prompts import build_prompt, normalize_projects

    projects = normalize_projects(
        projects=[{"project_id": "p1", "projectDetails": "Project Name: Green Valley\nLocation: Noida"}],
    )
    prompt = build_prompt(lead_name="Rahul", business_name="99 Acres", projects=projects)
    assert "MULTI-PROJECT CALL FLOW" not in prompt
    assert "PROJECT CALL FLOW" in prompt
    print("OK single project from array uses single prompt")


def test_object_project_details_from_backend():
    """The broker sends projectDetails as a nested JSON object, not only text."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from prompts import build_prompt
    from server import CallRequest, _projects_from_call_request

    req = CallRequest(**{
        "phone": "+919876543210",
        "lead_id": "lead-1",
        "organization_id": "org-1",
        "credentials": {
            "gemini_api_key": "test-key",
            "vobiz": {"trunk_id": "ST_test"},
        },
        "lead_name": "Karan",
        "business_name": "Swaryx",
        "project_id": "project-1",
        "project_details": {
            "projectName": "Gaur Sidhartham",
            "developer": "Gaurs Group",
            "location": "Siddharth Vihar, Ghaziabad",
            "startingPrice": "₹70,00,000",
            "possessionDate": "Ready to Move",
            "bhkConfig": ["2 BHK", "3 BHK", "4 BHK"],
            "amenities": ["Swimming Pool", "Club House", "Gym"],
            "description": "Premium high-rise apartments in Siddharth Vihar.",
            "crmDetails": {
                "organizationId": "org-1",
                "leadId": "lead-1",
            },
        },
    })

    projects = _projects_from_call_request(req)
    assert len(projects) == 1
    assert projects[0]["project_id"] == "project-1"
    assert "Gaur Sidhartham" in projects[0]["project_details"]
    assert "₹70,00,000" in projects[0]["project_details"]
    assert "Swimming Pool" in projects[0]["project_details"]
    prompt = build_prompt(
        lead_name=req.lead_name,
        business_name=req.business_name,
        project_id=req.project_id,
        project_details=req.project_details,
    )
    assert "PROJECT CALL FLOW" in prompt
    assert "Gaur Sidhartham" in prompt
    assert "Kya aap k pass 1 minute h?" in prompt
    # Phrase may appear only as a forbidden example in language policy — never as spoken dialogue.
    assert "no corporate lines like" in prompt
    assert 'Exact sentence: "property-related outreach"' not in prompt
    assert "property requirements discuss" not in prompt
    assert "STEP 3 — PITCH" in prompt
    assert "PHONE TALK-TRACK" in prompt
    assert "PROJECT PITCH TRACK" in prompt
    assert "Never invent" in prompt or "never invent" in prompt.lower()
    print("OK object projectDetails from backend")


def test_top_level_project_details_array_from_backend():
    """Broker's new projectDetails[] contract must select multi-project mode."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from prompts import build_prompt
    from server import CallRequest, _projects_from_call_request

    req = CallRequest(**{
        "phone": "+917838080040",
        "lead_id": "lead-1",
        "organization_id": "org-1",
        "project_id": "primary-project-id",
        "projectDetails": [
            {
                "projectName": "Wave City",
                "developer": "Wave Group",
                "location": "NH24, Ghaziabad, UP",
                "startingPrice": "₹40,00,000",
            },
            {
                "projectName": "Gaurs Siddhartham",
                "developer": "Gaurs Homes",
                "location": "Ghaziabad, UP",
                "startingPrice": "₹60,00,000",
            },
        ],
    })

    projects = _projects_from_call_request(req)
    assert len(projects) == 2
    assert projects[0]["project_id"] == "primary-project-id"
    assert projects[1]["project_id"].startswith("primary-project-id--")
    assert projects[1]["project_id"] != projects[0]["project_id"]
    assert "Wave City" in projects[0]["project_details"]
    assert "Gaurs Siddhartham" in projects[1]["project_details"]
    assert "Project Name: Wave City" in projects[0]["project_details"]
    assert projects[0]["project_details"].startswith("Project Name:")

    prompt = build_prompt(
        lead_name=req.lead_name,
        business_name=req.business_name,
        project_id=req.project_id,
        project_details=req.project_details,
    )
    assert "MULTI-PROJECT CALL FLOW" in prompt
    assert "Wave City" in prompt
    assert "Gaurs Siddhartham" in prompt
    print("OK top-level projectDetails[] uses multi-project prompt")


def test_voiceart_rich_project_details_array_formats_catalog():
    """VoiceArt-style projectDetails[] with nested projectDescription becomes readable catalog."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from prompts import build_prompt
    from server import CallRequest, _projects_from_call_request

    req = CallRequest(**{
        "phone": "+919990274564",
        "lead_name": "Prabath",
        "business_name": "Swaryx",
        "lead_id": "lead-1",
        "organization_id": "org-1",
        "project_id": "340b686a-d475-4260-9e4f-99a50c7afd58",
        "projectDetails": [
            {
                "projectName": "DLF Midtown",
                "developer": "DLF",
                "location": "Moti Nagar, New Delhi, Delhi",
                "projectType": "RESIDENTIAL",
                "projectStatus": "NEW_LAUNCH",
                "startingPrice": "₹42,00,000",
                "possessionDate": "2029-03-31",
                "reraNumber": "DLRERA123456",
                "projectDescription": {
                    "text": "Luxury Living",
                    "specifications": {
                        "configurations": [
                            {"bhk": "3 BHK", "sqft": "1650", "price": "4200000"},
                            {"bhk": "4 BHK", "sqft": "2100", "price": "5400000"},
                        ],
                        "amenities": ["Clubhouse", "Indoor Games"],
                    },
                },
            },
            {
                "projectName": "ATS Knightsbridge",
                "developer": "ATS",
                "location": "Sector 124, Noida, UP",
                "startingPrice": "₹95,00,000",
                "projectDescription": {"text": "Luxury Apartments"},
            },
            {
                "projectName": "Gaur City",
                "developer": "Gaursons",
                "location": "Noida Extension, Noida, UP",
                "startingPrice": "₹55,00,000",
            },
            {
                "projectName": "Gaur Siddhartham",
                "developer": "ATS Group",
                "location": "ghaziabad 148, Ghaziabad, UP",
                "startingPrice": None,
                "projectDescription": {"text": "", "specifications": {"configurations": []}},
            },
        ],
    })
    projects = _projects_from_call_request(req)
    assert len(projects) == 4
    assert projects[0]["project_id"] == "340b686a-d475-4260-9e4f-99a50c7afd58"
    assert all(p["project_id"] for p in projects)
    assert "Project Name: DLF Midtown" in projects[0]["project_details"]
    assert "Location: Moti Nagar" in projects[0]["project_details"]
    assert "Configurations:" in projects[0]["project_details"]
    assert "3 BHK" in projects[0]["project_details"]
    assert "Project Name: ATS Knightsbridge" in projects[1]["project_details"]
    assert "Project Name: Gaur City" in projects[2]["project_details"]
    assert "Project Name: Gaur Siddhartham" in projects[3]["project_details"]
    # Catalog text should not dump raw nested JSON blobs.
    assert '"projectDescription"' not in projects[0]["project_details"]

    prompt = build_prompt(
        lead_name=req.lead_name,
        business_name=req.business_name,
        project_id=req.project_id,
        project_details=req.project_details,
    )
    assert "MULTI-PROJECT CALL FLOW" in prompt
    assert "DLF Midtown" in prompt and "ATS Knightsbridge" in prompt
    assert "Gaur City" in prompt and "Gaur Siddhartham" in prompt
    print("OK VoiceArt rich projectDetails[] formats multi-project catalog")


def test_top_level_single_item_project_details_array_from_backend():
    """Broker always sends projectDetails[]; one item must select single-project mode."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from prompts import build_prompt
    from server import CallRequest, _projects_from_call_request

    req = CallRequest(**{
        "phone": "+917838080040",
        "lead_id": "lead-1",
        "organization_id": "org-1",
        "project_id": "wave-city-id",
        "projectDetails": [
            {
                "projectName": "Wave City",
                "developer": "Wave Group",
                "location": "NH24, Ghaziabad, UP",
                "startingPrice": "₹40,00,000",
            },
        ],
    })

    projects = _projects_from_call_request(req)
    assert len(projects) == 1
    assert projects[0]["project_id"] == "wave-city-id"
    assert "Wave City" in projects[0]["project_details"]

    prompt = build_prompt(
        lead_name=req.lead_name,
        business_name=req.business_name,
        project_id=req.project_id,
        project_details=req.project_details,
    )
    assert "PROJECT CALL FLOW" in prompt
    assert "MULTI-PROJECT CALL FLOW" not in prompt
    assert "Wave City" in prompt
    print("OK one-item top-level projectDetails[] uses single-project prompt")


def test_greeting_nudge_accepts_campaign_context():
    from prompts import (
        additional_instructions_has_greeting,
        build_greeting_kickoff_nudge,
        build_identity_opening,
        build_prompt,
    )

    default_nudge = build_greeting_kickoff_nudge("Rahul", reconnect=False, campaign=True)
    assert build_identity_opening("Rahul") in default_nudge
    assert "DEFAULT OPENING MODE" in default_nudge

    campaign_notes = "Greet with Radhe Radhe, then pitch Project Alpha."
    assert additional_instructions_has_greeting(campaign_notes) is True
    campaign_nudge = build_greeting_kickoff_nudge(
        "Rahul",
        campaign_notes,
        reconnect=False,
        campaign=True,
    )
    assert "CAMPAIGN OPENING MODE" in campaign_nudge
    assert "Radhe Radhe" in campaign_nudge
    assert "Rahul ji" in campaign_nudge
    assert "project name or pitch" in campaign_nudge.lower() or "project name" in campaign_nudge

    prompt = build_prompt(
        lead_name="Rahul",
        business_name="Swaryx",
        additional_instructions=campaign_notes,
    )
    assert "OPENING MODE — CAMPAIGN FIRST" in prompt
    assert "Opening if-branch" in prompt or "use the campaign salutation" in prompt

    prompt_default = build_prompt(lead_name="Rahul", business_name="Swaryx")
    assert "OPENING MODE — DEFAULT" in prompt_default
    assert build_identity_opening("Rahul") in prompt_default
    print("OK greeting nudge accepts campaign context")


def test_call_request_projects_array():
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import CallRequest

    req = CallRequest(**{
        "phone": "+919876543210",
        "lead_name": "Rahul",
        "from_number": "+918065354603",
        "projects": [
            {"project_id": "p1", "projectDetails": "Project Name: Green Valley"},
            {"project_id": "p2", "projectDetails": "Project Name: Sky Towers"},
        ],
    })
    assert len(req.projects) == 2
    assert req.projects[0].project_details == "Project Name: Green Valley"
    print("OK CallRequest accepts projects array")


def test_call_request_accepts_node_crm_project_array():
    """Node sends projectDetails as an array of rich CRM objects — we adapt to projects[]."""
    os.environ.setdefault("CONFIG_FROM_ENV_ONLY", "true")
    from server import CallRequest, _projects_from_call_request

    payload = {
        "phone": "+918006077821",
        "from_number": "+918071578722",
        "lead_name": "Tushar",
        "business_name": "Swaryx",
        "organization_id": "fb234086-9ce0-4f27-a97f-ae91f18bce93",
        "lead_id": "ec608a75-d640-493f-a507-7659fa81190c",
        "project_id": "340b686a-d475-4260-9e4f-99a50c7afd58",
        "credentials": {"gemini_api_key": "x", "vobiz": {"sip_domain": "x"}},
        "whatsapp_auth_token": "ignored",
        "projectDetails": [
            {
                "projectName": "DLF Midtown",
                "developer": "DLF",
                "location": "Moti Nagar, New Delhi, Delhi",
                "projectType": "RESIDENTIAL",
                "projectStatus": "NEW_LAUNCH",
                "startingPrice": "₹42,00,000",
                "possessionDate": "2029-03-31",
                "reraNumber": "DLRERA123456",
                "projectDescription": {
                    "text": "Luxury Living",
                    "specifications": {
                        "configurations": [
                            {"bhk": "3 BHK", "sqft": "1650", "price": "4200000"},
                        ],
                        "amenities": ["Clubhouse"],
                        "highlights": ["Smart Home Features"],
                    },
                },
            },
            {
                "projectName": "ATS Knightsbridge",
                "developer": "ATS",
                "location": "Sector 124, Noida, UP",
                "startingPrice": "₹95,00,000",
                "projectDescription": {"text": "Luxury Apartments", "specifications": {}},
            },
        ],
    }
    req = CallRequest(**payload)
    # Array stays on projectDetails so parent project_id derives stable child IDs.
    assert req.projects is None or len(req.projects or []) == 0 or req.project_details is not None
    projects = _projects_from_call_request(req)
    assert len(projects) == 2
    assert projects[0]["project_id"] == "340b686a-d475-4260-9e4f-99a50c7afd58"
    assert "Project Name: DLF Midtown" in projects[0]["project_details"]
    assert "3 BHK" in projects[0]["project_details"]
    assert "Clubhouse" in projects[0]["project_details"]
    assert "Project Name: ATS Knightsbridge" in projects[1]["project_details"]
    print("OK CallRequest accepts Node CRM projectDetails array")


def test_custom_prompt_overrides_project_template():
    """No custom prompt may replace an organization-verified project catalog."""
    from prompts import build_prompt

    # Generic custom (no {project_details}) → dedicated project script wins
    generic = "CUSTOM DASHBOARD PROMPT for {lead_name} at {business_name}."
    p = build_prompt(
        lead_name="Rahul",
        business_name="99 Acres",
        custom_prompt=generic,
        project_details="Project Name: Green Valley\nLocation: Noida",
    )
    assert "PROJECT CALL FLOW" in p
    assert "CUSTOM DASHBOARD PROMPT" not in p
    assert "Green Valley" in p
    print("OK generic custom does not override project template")

    # Explicit project-aware custom → allowed to override
    project_custom = (
        "CUSTOM PROJECT PROMPT for {lead_name}.\n"
        "PROJECT DETAILS — SOURCE OF TRUTH\n{project_details}"
    )
    p2 = build_prompt(
        lead_name="Rahul",
        business_name="99 Acres",
        custom_prompt=project_custom,
        project_details="Project Name: Green Valley\nLocation: Noida",
    )
    assert "CUSTOM PROJECT PROMPT" not in p2
    assert "PROJECT CALL FLOW" in p2
    assert "Green Valley" in p2
    assert "Rahul" in p2
    print("OK verified project template overrides project-aware custom prompt")


# Gemini Live re-reads the system prompt every turn, so its size adds reply latency.
# ~4 chars per token; 14k chars ≈ 3.5k tokens (it was ~3.6k when replies took 1.3–1.5s).
PROMPT_CHAR_BUDGET = 14_000


def test_prompt_size_budget():
    from prompts import build_prompt

    projects = [
        {"project_id": f"p{i}", "project_details": f"Project Name: Tower {i}\nLocation: Noida\n3 BHK"}
        for i in range(1, 3)
    ]
    notes = "Greet with Radhe Radhe. Speak English. Your name is Priya. Focus on ready-to-move units."
    cases = {
        "default": {},
        "custom_dashboard_flow": {"custom_prompt": "STEP 3 — DISCOVER: ask city. STEP 4 — VISIT."},
        "single_project": {"projects": projects[:1]},
        "multi_project": {"projects": projects},
        "campaign_multi_project": {"projects": projects, "additional_instructions": notes},
    }
    for label, kwargs in cases.items():
        prompt = build_prompt(lead_name="Rahul Sharma", business_name="Swaryx", **kwargs)
        assert len(prompt) <= PROMPT_CHAR_BUDGET, (
            f"{label} prompt is {len(prompt)} chars (budget {PROMPT_CHAR_BUDGET}) — "
            "trim rules instead of adding text; size adds per-turn latency"
        )
        for heading in (
            "MANDATORY CORE",
            "CALL LANGUAGE MODE",
            "INDIAN ACCENT + PITCH LOCK",
            "TRUST, PRIVACY",
            "PROJECT FACTS AND MATCHING",
            "CLOSING AND CALLBACKS",
        ):
            assert prompt.count(heading) == 1, f"{label}: {heading} appears {prompt.count(heading)}x"
    print("OK prompt size budget + no duplicated rule sections")


def test_campaign_notes_replace_builtin_flow():
    from prompts import build_prompt

    projects = [
        {"project_id": "p1", "project_details": "Project Name: Sunrise Heights\nLocation: Noida"},
        {"project_id": "p2", "project_details": "Project Name: Green Valley\nLocation: Delhi"},
    ]
    notes = "Greeting: Start with Happy Diwali.\nFlow:\n1. Mention the Diwali offer.\n2. Ask budget."
    for kwargs in ({"projects": projects[:1]}, {"projects": projects}, {}):
        campaign = build_prompt(lead_name="Rahul", business_name="Swaryx", additional_instructions=notes, **kwargs)
        single = build_prompt(lead_name="Rahul", business_name="Swaryx", **kwargs)
        # Campaign: one flow only — the notes. Built-in step lists are dropped.
        assert "CALL FLOW — FROM CAMPAIGN NOTES" in campaign
        assert "Mention the Diwali offer" in campaign
        for builtin in ("PROJECT PITCH TRACK", "PROJECT CALL FLOW", "DISCOVERY TRACK", "DEFAULT CALL FLOW"):
            assert builtin not in campaign, builtin
        # Project data and safety rails stay.
        if kwargs:
            assert "Sunrise Heights" in campaign
        assert "GLOBAL PROJECT-PERMISSION GATE" in campaign
        # Campaign offers may be spoken; project specs still come only from the catalog.
        assert "Campaign info in CAMPAIGN NOTES (offers" in campaign
        assert "come ONLY from PROJECT DETAILS/catalog" in campaign
        # Single call (no notes): built-in flow, no campaign flow.
        assert "CALL FLOW — FROM CAMPAIGN NOTES" not in single
        assert ("PROJECT PITCH TRACK" in single) or ("DISCOVERY TRACK" in single)
    print("OK campaign notes replace the built-in flow (one flow only)")


if __name__ == "__main__":
    test_imports()
    test_gemini_38_model_is_consistent()
    test_hinglish_leda_voice_continuity_in_every_prompt_mode()
    test_property_insight_parser()
    test_project_context_injection()
    test_default_when_project_details_empty()
    test_custom_prompt_overrides_project_template()
    test_call_request_project_alias()
    test_livekit_trunk_id_contract()
    test_call_request_from_number_optional()
    test_property_insight_aliases_and_budget_parse()
    test_call_request_projects_array()
    test_call_request_accepts_node_crm_project_array()
    test_multi_project_prompt()
    test_single_project_from_array()
    test_object_project_details_from_backend()
    test_top_level_project_details_array_from_backend()
    test_voiceart_rich_project_details_array_formats_catalog()
    test_top_level_single_item_project_details_array_from_backend()
    test_greeting_nudge_accepts_campaign_context()
    test_prompt_size_budget()
    test_campaign_notes_replace_builtin_flow()
    test_end_call_enforces_goodbye_before_hangup()
    test_phone_normalization()
    test_whatsapp_helpers()
    test_call_request_ignores_whatsapp_auth_tokens()
    test_email_followup_helpers()
    test_server_app()
    print("\nAll smoke checks passed.")
