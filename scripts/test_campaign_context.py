"""Campaign prompt-context propagation tests."""

from prompting import build_prompt
from server import (
    CampaignRequest,
    _apply_campaign_context_to_contact,
    _campaign_default_projects,
    _merge_instruction_text,
    _projects_from_contact,
)


def _request(**overrides) -> CampaignRequest:
    payload = {
        "name": "October launch",
        "contacts": [{"phone": "+919876543210"}],
        **overrides,
    }
    return CampaignRequest.model_validate(payload)


def test_campaign_root_single_project_and_instructions_reach_prompt() -> None:
    req = _request(
        additionalInstructions="Speak in English and focus on end-use buyers.",
        projectId="green-valley",
        projectDetails={
            "projectName": "Green Valley",
            "location": "Sector 150, Noida",
            "startingPrice": "80 lakh",
        },
    )
    defaults = _campaign_default_projects(req)
    contact = _apply_campaign_context_to_contact(
        req.contacts[0],
        additional_instructions=req.additional_instructions,
        default_projects=defaults,
    )
    projects = _projects_from_contact(contact)
    prompt = build_prompt(
        lead_name="Rahul",
        projects=projects,
        additional_instructions=contact["additional_instructions"],
    )

    assert projects[0]["project_id"] == "green-valley"
    assert "Green Valley" in prompt
    assert "Sector 150, Noida" in prompt
    assert "80 lakh" in prompt
    assert "focus on end-use buyers" in prompt
    assert "CAMPAIGN LANGUAGE" in prompt


def test_campaign_root_multi_project_defaults_and_contact_override() -> None:
    req = _request(
        projects=[
            {"projectId": "alpha", "projectDetails": "Project Name: Alpha\nLocation: Noida"},
            {"projectId": "beta", "projectDetails": "Project Name: Beta\nLocation: Gurgaon"},
        ]
    )
    defaults = _campaign_default_projects(req)
    inherited = _apply_campaign_context_to_contact({}, default_projects=defaults)
    overridden = _apply_campaign_context_to_contact(
        {"project_id": "gamma", "projectDetails": "Project Name: Gamma"},
        default_projects=defaults,
    )

    assert [p["project_id"] for p in _projects_from_contact(inherited)] == ["alpha", "beta"]
    assert [p["project_id"] for p in _projects_from_contact(overridden)] == ["gamma"]


def test_campaign_and_contact_instructions_are_merged_once() -> None:
    assert _merge_instruction_text("Speak slowly", "Mention the launch offer") == (
        "Speak slowly\n\nMention the launch offer"
    )
    assert _merge_instruction_text("Speak slowly", " speak slowly ") == "Speak slowly"
    assert _merge_instruction_text(
        "Speak slowly",
        "Speak slowly\n\nMention the launch offer",
    ) == "Speak slowly\n\nMention the launch offer"


def test_single_project_notes_reach_prompt() -> None:
    prompt = build_prompt(
        lead_name="Rahul",
        projects=[{
            "project_id": "alpha",
            "projectDetails": "Project Name: Alpha\nLocation: Noida",
            "additionalInstructions": "Mention that weekend visits need advance confirmation.",
        }],
    )
    assert "weekend visits need advance confirmation" in prompt
