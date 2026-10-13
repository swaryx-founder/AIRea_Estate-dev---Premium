"""CRM / Node project payload adapters.

Ported from morning production work: accept rich projectDetails objects/arrays
from Node and turn them into pitch strings the agent can speak from.
"""

from __future__ import annotations

from typing import Any

from .builder import (
    _format_broker_project_object,
    _looks_like_broker_project,
    _project_details_to_text,
)


def _is_crm_project_object(item: dict) -> bool:
    """True when payload looks like Node CRM project JSON (not our pitch wrapper)."""
    return isinstance(item, dict) and _looks_like_broker_project(item)


def format_crm_project_pitch(project: dict | Any) -> str:
    """Turn Node CRM project object into agent pitch text (Project Name: … lines)."""
    if not isinstance(project, dict):
        return str(project or "").strip()

    raw_pd = project.get("projectDetails", project.get("project_details"))
    if isinstance(raw_pd, str) and raw_pd.strip() and not _is_crm_project_object(project):
        return raw_pd.strip()

    if _is_crm_project_object(project):
        formatted = _format_broker_project_object(project)
        if formatted:
            return formatted

    if isinstance(raw_pd, dict):
        return _project_details_to_text(raw_pd)

    return _project_details_to_text(project)


def coerce_project_details_value(value: Any) -> str:
    """Accept string pitch OR CRM object → single pitch string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return format_crm_project_pitch(value)
    return str(value).strip()


def adapt_call_project_payload(data: dict) -> dict:
    """Rewrite Node call body so projectDetails array/objects become projects[] + pitch strings.

    Node may send:
      projectDetails: [ { projectName, developer, projectDescription: {…} }, … ]
    We convert to:
      projects: [ { project_id, projectDetails: "Project Name: …\\n…" }, … ]
      projectDetails removed (or left as string for single-project legacy).
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)

    raw_projects = out.get("projects")
    if isinstance(raw_projects, list) and raw_projects:
        normalized = []
        for item in raw_projects:
            if isinstance(item, dict) and _is_crm_project_object(item):
                pid = (
                    str(
                        item.get("project_id")
                        or item.get("id")
                        or item.get("projectName")
                        or item.get("project_name")
                        or ""
                    ).strip()
                    or None
                )
                normalized.append(
                    {
                        "project_id": pid,
                        "projectDetails": format_crm_project_pitch(item),
                        "additional_instructions": item.get("additional_instructions")
                        or item.get("notes"),
                    }
                )
            elif isinstance(item, dict):
                pd = item.get("projectDetails", item.get("project_details"))
                item2 = dict(item)
                item2["projectDetails"] = coerce_project_details_value(pd)
                item2.pop("project_details", None)
                normalized.append(item2)
            else:
                normalized.append(item)
        out["projects"] = normalized

    pd = out.get("projectDetails", out.get("project_details"))

    # Keep top-level projectDetails arrays as-is so normalize_projects can derive
    # stable child IDs from parent project_id (existing multi-project contract).
    # Only coerce a single CRM object into a pitch string for the legacy field.
    if isinstance(pd, dict):
        out["projectDetails"] = format_crm_project_pitch(pd)
        out.pop("project_details", None)

    return out
