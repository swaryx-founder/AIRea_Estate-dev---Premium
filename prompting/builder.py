"""Public prompt builder and project-payload normalization."""

import hashlib
import json
import re

from .flows import (
    CAMPAIGN_DEFAULT_FLOW,
    CAMPAIGN_MULTI_PROJECT_FLOW,
    CAMPAIGN_SINGLE_PROJECT_FLOW,
    DEFAULT_FLOW,
    MULTI_PROJECT_FLOW,
    SINGLE_PROJECT_FLOW,
)
from .language import language_policy_block, resolve_call_language
from .policies import MANDATORY_CORE_MARKER, MANDATORY_RULES
from .speech import (
    build_identity_opening,
    build_permission_intro,
    resolve_agent_name,
    spoken_lead_name,
)
from .templates import campaign_block, name_mode_block, opening_mode_block, provenance_block

# Compatibility constants used by the dashboard/profile API. They remain full
# templates, while build_prompt() composes the same blocks per call.
_DASHBOARD_LANGUAGE = language_policy_block("hinglish").strip()
_DASHBOARD_MANDATORY = MANDATORY_RULES.replace("{language_policy}", _DASHBOARD_LANGUAGE)
DEFAULT_SYSTEM_PROMPT = f"{DEFAULT_FLOW.rstrip()}\n\n{_DASHBOARD_MANDATORY}"
PROJECT_SYSTEM_PROMPT = f"{SINGLE_PROJECT_FLOW.rstrip()}\n\n{_DASHBOARD_MANDATORY}"
MULTI_PROJECT_SYSTEM_PROMPT = f"{MULTI_PROJECT_FLOW.rstrip()}\n\n{_DASHBOARD_MANDATORY}"


def _extract_project_name(project_details: str) -> str:
    if not project_details:
        return "yeh project"
    text = project_details.strip()
    match = re.search(r"(?im)^Project Name:\s*(.+)$", text)
    if match:
        return match.group(1).strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        name = payload.get("projectName") or payload.get("project_name") or payload.get("name")
        if name:
            return str(name).strip()
    match = re.search(r'["\'](?:projectName|project_name)["\']\s*:\s*["\']([^"\']+)', text)
    return match.group(1).strip() if match else "yeh project"


def _format_prompt(template: str, **kwargs: object) -> str:
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


def _join_list(values: object, *, limit: int = 8) -> str:
    if not isinstance(values, list):
        return ""
    parts = [str(v).strip() for v in values if str(v or "").strip()]
    if not parts:
        return ""
    if len(parts) > limit:
        parts = parts[:limit] + ["…"]
    return ", ".join(parts)


def _format_broker_project_object(payload: dict) -> str:
    """Turn VoiceArt/broker project JSON into speech-friendly catalog lines."""
    name = payload.get("projectName") or payload.get("project_name") or payload.get("name")
    developer = payload.get("developer")
    location = payload.get("location")
    project_type = payload.get("projectType") or payload.get("project_type")
    status = payload.get("projectStatus") or payload.get("project_status")
    price = payload.get("startingPrice") or payload.get("starting_price")
    possession = payload.get("possessionDate") or payload.get("possession_date")
    rera = payload.get("reraNumber") or payload.get("rera_number")

    desc = payload.get("projectDescription") or payload.get("project_description") or {}
    if not isinstance(desc, dict):
        desc = {"text": desc}
    blurb = str(
        desc.get("text")
        or payload.get("description")
        or payload.get("project_description_text")
        or ""
    ).strip()
    specs = desc.get("specifications") if isinstance(desc.get("specifications"), dict) else {}
    if not specs and isinstance(payload.get("specifications"), dict):
        specs = payload["specifications"]

    lines: list[str] = []
    if name:
        lines.append(f"Project Name: {name}")
    if developer:
        lines.append(f"Developer: {developer}")
    if location:
        lines.append(f"Location: {location}")
    if project_type:
        lines.append(f"Type: {project_type}")
    if status:
        lines.append(f"Status: {status}")
    if price not in (None, ""):
        lines.append(f"Starting Price: {price}")
    if possession:
        lines.append(f"Possession: {possession}")
    if rera:
        lines.append(f"RERA: {rera}")
    if blurb:
        lines.append(f"Description: {blurb}")

    # Legacy top-level BHK list (older broker shape).
    bhk_top = _join_list(payload.get("bhkConfig") or payload.get("bhk_config"))
    if bhk_top:
        lines.append(f"BHK: {bhk_top}")

    configs = specs.get("configurations") if isinstance(specs, dict) else None
    if isinstance(configs, list) and configs:
        cfg_bits = []
        for cfg in configs[:6]:
            if not isinstance(cfg, dict):
                continue
            bhk = str(cfg.get("bhk") or "").strip()
            sqft = str(cfg.get("sqft") or "").strip()
            cfg_price = str(cfg.get("price") or "").strip()
            bit = bhk or "config"
            if sqft:
                bit += f" ({sqft} sqft)"
            if cfg_price:
                bit += f" @ {cfg_price}"
            cfg_bits.append(bit)
        if cfg_bits:
            lines.append(f"Configurations: {', '.join(cfg_bits)}")

    for label, key in (
        ("Amenities", "amenities"),
        ("Parking", "parking"),
        ("Security", "security"),
        ("Utilities", "utilities"),
        ("Connectivity", "connectivity"),
        ("Highlights", "highlights"),
        ("Furnishing", "furnishing"),
    ):
        joined = _join_list((specs or {}).get(key)) or _join_list(payload.get(key))
        if joined:
            lines.append(f"{label}: {joined}")

    return "\n".join(lines).strip()


def _looks_like_broker_project(payload: dict) -> bool:
    keys = {str(k) for k in payload.keys()}
    markers = {
        "projectName",
        "project_name",
        "developer",
        "location",
        "startingPrice",
        "starting_price",
        "projectDescription",
        "project_description",
        "reraNumber",
        "rera_number",
        "projectStatus",
        "project_status",
    }
    return bool(keys & markers)


def _project_details_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(parsed, dict) and _looks_like_broker_project(parsed):
            formatted = _format_broker_project_object(parsed)
            return formatted or text
        return text
    if isinstance(value, dict):
        if _looks_like_broker_project(value):
            formatted = _format_broker_project_object(value)
            if formatted:
                return formatted
        return json.dumps(value, ensure_ascii=False, indent=2, default=str).strip()
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str).strip()
    return str(value).strip()


def _derived_project_id(parent_project_id: str | None, item: object, index: int) -> str | None:
    """Create a stable child ID for the broker's legacy projectDetails[] shape.

    That payload supplies one top-level project_id for a catalog but no IDs on
    its individual project objects. Keep the parent ID for the first project and
    namespace deterministic IDs for the remaining projects under it.
    """
    parent = str(parent_project_id or "").strip()
    if not parent:
        return None
    if index == 0:
        return parent

    if isinstance(item, dict):
        identity_fields = (
            item.get("reraNumber") or item.get("rera_number"),
            item.get("projectName") or item.get("project_name") or item.get("name"),
            item.get("developer"),
            item.get("location"),
        )
        identity = "|".join(
            " ".join(str(value or "").strip().casefold().split())
            for value in identity_fields
        ).strip("|")
        if not identity:
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    else:
        identity = str(item).strip().casefold()

    digest = hashlib.sha256(f"{parent}\0{identity}".encode("utf-8")).hexdigest()[:16]
    return f"{parent}--{digest}"


def normalize_projects(
    *,
    projects: list | None = None,
    project_id: str | None = None,
    project_details: object | None = None,
) -> list[dict]:
    """Normalize broker payloads into one internal project list.

    Supported shapes:
    - ``projects: [{project_id, projectDetails}, ...]``
    - top-level ``projectDetails: [ {projectName, location, ...}, ... ]`` + optional parent ``project_id``
    - single ``projectDetails`` string/object
    """
    out: list[dict] = []
    if projects:
        for idx, item in enumerate(projects):
            if isinstance(item, dict):
                item_pid = item.get("project_id") or item.get("projectId") or item.get("id")
                name = item.get("project_name") or item.get("projectName") or item.get("name")
                pid = str(item_pid).strip() if item_pid else None
                if not pid and name:
                    pid = re.sub(r"[^a-zA-Z0-9_-]", "-", str(name).strip().lower()).strip("-")
                if not pid and project_id:
                    pid = _derived_project_id(project_id, item, idx)
                if not pid:
                    pid = f"proj-{idx + 1}"
                nested = item.get("projectDetails") or item.get("project_details")
                # Broker catalog objects put fields on the item itself (no nested projectDetails).
                details = _project_details_to_text(
                    nested if nested not in (None, "") else item
                )
                notes = str(
                    item.get("additional_instructions")
                    or item.get("additionalInstructions")
                    or item.get("notes")
                    or ""
                ).strip()
            else:
                pid = str(getattr(item, "project_id", None) or getattr(item, "projectId", None) or getattr(item, "id", None) or "").strip() or None
                if not pid and project_id:
                    pid = _derived_project_id(project_id, item, idx)
                if not pid:
                    pid = f"proj-{idx + 1}"
                details = _project_details_to_text(getattr(item, "project_details", None))
                notes = str(
                    getattr(item, "additional_instructions", None)
                    or getattr(item, "notes", None)
                    or ""
                ).strip()
            if details:
                out.append(
                    {"project_id": pid, "project_details": details, "additional_instructions": notes}
                )

    if not out and isinstance(project_details, list):
        for index, item in enumerate(project_details):
            if isinstance(item, dict):
                item_pid = item.get("project_id") or item.get("projectId")
                pid = (
                    str(item_pid).strip()
                    if item_pid
                    else _derived_project_id(project_id, item, index)
                )
                nested = item.get("projectDetails") or item.get("project_details")
                details = _project_details_to_text(nested if nested not in (None, "") else item)
                notes = str(
                    item.get("additional_instructions")
                    or item.get("additionalInstructions")
                    or item.get("notes")
                    or ""
                ).strip()
            else:
                pid = _derived_project_id(project_id, item, index)
                details = _project_details_to_text(item)
                notes = ""
            if details:
                out.append(
                    {"project_id": pid, "project_details": details, "additional_instructions": notes}
                )

    if not out:
        details = _project_details_to_text(project_details)
        if details:
            out.append(
                {
                    "project_id": (project_id or "").strip() or None,
                    "project_details": details,
                    "additional_instructions": "",
                }
            )
    return out


def _format_projects_catalog(projects: list[dict]) -> str:
    lines: list[str] = []
    for index, project in enumerate(projects, 1):
        details = (project.get("project_details") or "").strip()
        if not details:
            continue
        lines.append(f"--- PROJECT {index}: {_extract_project_name(details)} ---")
        if project.get("project_id"):
            lines.append(f"Project ID: {project['project_id']}")
        lines.append(details)
        notes = (project.get("additional_instructions") or "").strip()
        if notes:
            lines.append(f"Additional notes for this project: {notes}")
        lines.append("")
    return "\n".join(lines).strip()


def _custom_supports_multi(custom: str) -> bool:
    return "{projects_catalog}" in custom or "MULTI-PROJECT CALL FLOW" in custom.upper()


def _custom_supports_single(custom: str) -> bool:
    return "{project_details}" in custom or "PROJECT DETAILS — SOURCE OF TRUTH" in custom


def _ensure_mandatory_rules(
    body: str,
    *,
    lead_name: str,
    business_name: str,
    service_type: str,
    lead_source: str | None = None,
    additional_instructions: str | None = None,
) -> str:
    """Always attach the full current mandatory ruleset.

    Custom prompts that paste a partial ``MANDATORY CORE`` block used to skip this
    append and could bypass trust/closing/tool rules. Strip any pasted mandatory
    section from the marker onward, then append the complete rules.
    """
    body = (body or "").strip()
    if MANDATORY_CORE_MARKER in body:
        body = body.split(MANDATORY_CORE_MARKER)[0].rstrip()
    call_language = resolve_call_language(additional_instructions)
    agent_name = resolve_agent_name(additional_instructions)
    spoken_name = spoken_lead_name(lead_name) or "(no usable first name — do not invent one)"
    rules = _format_prompt(
        MANDATORY_RULES,
        lead_name=lead_name,
        spoken_lead_name=spoken_name,
        business_name=business_name,
        service_type=service_type,
        agent_name=agent_name,
        identity_opening=build_identity_opening(lead_name, language=call_language),
        permission_intro=build_permission_intro(
            lead_name=lead_name,
            business_name=business_name,
            lead_source=lead_source,
            language=call_language,
            agent_name=agent_name,
        ),
        language_policy=language_policy_block(
            call_language, agent_name=agent_name
        ).strip(),
    )
    if not body:
        return rules
    return f"{body}\n\n{rules}"


def _assemble(
    body: str,
    *,
    lead_name: str,
    business_name: str,
    service_type: str,
    additional_instructions: str | None,
    lead_source: str | None,
    lead_source_date: str | None,
) -> str:
    call_language = resolve_call_language(additional_instructions)
    agent_name = resolve_agent_name(additional_instructions)
    identity_opening = build_identity_opening(lead_name, language=call_language)
    permission_intro = build_permission_intro(
        lead_name=lead_name,
        business_name=business_name,
        lead_source=lead_source,
        language=call_language,
        agent_name=agent_name,
    )
    body = _ensure_mandatory_rules(
        body,
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        lead_source=lead_source,
        additional_instructions=additional_instructions,
    )
    # Order: facts → names → opening → campaign notes → call flow → mandatory rules.
    # Each rule family appears once; language/accent lives inside the mandatory rules.
    # Gemini Live re-reads this prompt every turn, so its size adds reply latency.
    blocks = [
        provenance_block(lead_source, lead_source_date),
        name_mode_block(lead_name, agent_name=agent_name),
        opening_mode_block(
            lead_name,
            additional_instructions,
            identity_opening=identity_opening,
            permission_intro=permission_intro,
            agent_name=agent_name,
        ),
        campaign_block(
            additional_instructions,
            call_language=call_language,
            agent_name=agent_name,
        ),
        body,
    ]
    return "\n\n".join(block.strip() for block in blocks if block.strip())


def build_prompt(
    lead_name: str = "Name",
    business_name: str = "company name",
    service_type: str = "our service",
    custom_prompt: str = None,
    project_details: object = None,
    project_id: str = None,
    projects: list | None = None,
    additional_instructions: str | None = None,
    lead_source: str | None = None,
    lead_source_date: str | None = None,
) -> str:
    """Build a compact prompt from the active call flow plus mandatory shared rules."""
    normalized = normalize_projects(
        projects=projects,
        project_id=project_id,
        project_details=project_details,
    )
    custom = (custom_prompt or "").strip() or None
    spoken_name = spoken_lead_name(lead_name) or "(no usable first name — do not invent one)"
    agent_name = resolve_agent_name(additional_instructions)
    common = {
        "lead_name": lead_name,
        "spoken_lead_name": spoken_name,
        "business_name": business_name,
        "service_type": service_type,
        "agent_name": agent_name,
    }
    # Campaign calls send additional_instructions: those notes ARE the call flow, so the
    # built-in step lists are dropped (one flow only — fewer tokens, no mixed steps).
    # Single calls send no notes and keep the built-in pitch-first flow.
    campaign = bool((additional_instructions or "").strip())

    if len(normalized) >= 2:
        catalog = _format_projects_catalog(normalized)
        names = [_extract_project_name(item["project_details"]) for item in normalized]
        project_names = ", ".join(names[:-1]) + f" aur {names[-1]}"
        # Project calls always use the verified standard flow. A caller-supplied
        # system prompt may contain stale or cross-organization project facts and
        # must never replace the organization-scoped catalog.
        template = CAMPAIGN_MULTI_PROJECT_FLOW if campaign else MULTI_PROJECT_FLOW
        body = _format_prompt(
            template,
            **common,
            project_count=len(normalized),
            project_names=project_names,
            projects_catalog=catalog,
            project_details=catalog,
            project_name=project_names,
        )
    elif len(normalized) == 1:
        details = normalized[0]["project_details"]
        project_notes = (normalized[0].get("additional_instructions") or "").strip()
        if project_notes:
            details = f"{details}\n\nAdditional verified notes for this project: {project_notes}"
        project_name = _extract_project_name(details)
        template = CAMPAIGN_SINGLE_PROJECT_FLOW if campaign else SINGLE_PROJECT_FLOW
        body = _format_prompt(
            template,
            **common,
            project_name=project_name,
            project_details=details,
        )
    elif campaign:
        body = _format_prompt(CAMPAIGN_DEFAULT_FLOW, **common)
    else:
        body = _format_prompt(custom or DEFAULT_FLOW, **common)

    return _assemble(
        body,
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        additional_instructions=additional_instructions,
        lead_source=lead_source,
        lead_source_date=lead_source_date,
    )
