"""Deploy Container App image + Library env vars in one atomic update.

Secrets and env-specific URLs are NEVER baked into the Docker image.
They come from the Azure DevOps Library variable group bound to the deploy stage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# Non-secret + secret keys accepted from the agent environment (Library group).
# Values are injected at deploy time only.
LIBRARY_KEYS = (
    "PORT",
    "CONFIG_FROM_ENV_ONLY",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "GEMINI_API_KEYS",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "VOBIZ_SIP_DOMAIN",
    "VOBIZ_USERNAME",
    "VOBIZ_PASSWORD",
    "VOBIZ_OUTBOUND_NUMBER",
    "OUTBOUND_TRUNK_ID",
    "VOBIZ_AUTH_ID",
    "VOBIZ_AUTH_TOKEN",
    "VOBIZ_RATE_PER_MINUTE",
    "VOBIZ_BILLING_CURRENCY",
    "PHONE_NUMBER_SYNC_INTERVAL_MINUTES",
    "PHONE_NUMBER_BUSY_MAX_MINUTES",
    "PHONE_NUMBER_JOB_LOCK_TTL_SEC",
    "GEMINI_MODEL",
    "GEMINI_TTS_VOICE",
    "USE_GEMINI_REALTIME",
    "VOICE_STACK",
    "INCEPTION_API_KEY",
    "INCEPTION_MODEL",
    "INCEPTION_REASONING_EFFORT",
    "STT_PROVIDER",
    "ELEVEN_API_KEY",
    "SARVAM_API_KEY",
    "CARTESIA_API_KEY",
    "CARTESIA_VOICE_ID",
    "CARTESIA_TTS_MODEL",
    "CARTESIA_TTS_SPEED",
    "CARTESIA_TTS_VOLUME",
    "AZURE_STORAGE_ACCOUNT",
    "AZURE_STORAGE_KEY",
    "AZURE_STORAGE_CONTAINER",
    "CALL_COMPLETED_WEBHOOK_URL",
    "CALLING_SERVICE_BASE_URL",
    "API_AUTH_TOKEN",
    "ACS_CONNECTION_STRING",
    "ACS_SENDER_EMAIL",
    "ACS_EMAIL_ENABLED",
    "ACS_FOLLOWUP_TO",
    # Not AGENT_NAME: Azure DevOps predefines it as the build agent name
    # ("Azure Pipelines 1"). The worker name is written by _build_pairs below.
    "GEMINI_THINKING_LEVEL",
    "ENABLE_LIVE_TRANSCRIPTION",
    "META_WHATSAPP_TOKEN",
    "META_WHATSAPP_PHONE_NUMBER_ID",
    "META_WHATSAPP_ENABLED",
)

REQUIRED_KEYS = frozenset({
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
})


def _build_pairs() -> tuple[list[str], int]:
    build_id = os.environ.get("ACA_BUILD_ID", "").strip()
    pairs: list[str] = []
    if build_id:
        pairs.append(f"BUILD_ID={build_id}")

    set_count = 0
    for key in LIBRARY_KEYS:
        val = os.environ.get(key, "").strip()
        if not val:
            continue
        # Skip unresolved Azure DevOps macros so they never land on ACA.
        if val.startswith("$(") or val.startswith("${"):
            print(f"skip unresolved library value for {key}")
            continue
        pairs.append(f"{key}={val}")
        set_count += 1

    # API and worker must share one LiveKit agent name. Prefer AI_AGENT_NAME —
    # Azure DevOps predefines AGENT_NAME as the build agent ("Azure Pipelines 1").
    worker_name = (
        os.environ.get("LIVEKIT_AGENT_NAME", "").strip()
        or os.environ.get("AI_AGENT_NAME", "").strip()
        or "outbound-caller"
    )
    if (
        worker_name.startswith("$(")
        or worker_name.lower() == "outbound-caller-sarvam"
        or " " in worker_name
        or worker_name.lower().startswith("azure")
    ):
        worker_name = "outbound-caller"
    pairs.append(f"LIVEKIT_AGENT_NAME={worker_name}")
    pairs.append(f"AI_AGENT_NAME={worker_name}")
    pairs.append(f"AGENT_NAME={worker_name}")
    print(f"LiveKit worker name: {worker_name}")
    return pairs, set_count


def _verify_template(app: str, rg: str, image: str, build_id: str) -> int:
    proc = subprocess.run(
        ["az", "containerapp", "show", "--name", app, "--resource-group", rg, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    container = data["properties"]["template"]["containers"][0]
    configured_image = container.get("image", "")
    env_names = {item.get("name") for item in container.get("env", []) if item.get("name")}

    print(f"Template image: {configured_image}")
    print(f"Template env count: {len(env_names)}")

    if image and image not in configured_image:
        print(f"ERROR: expected image '{image}' on template, got '{configured_image}'", file=sys.stderr)
        return 1

    missing = sorted(REQUIRED_KEYS - env_names)
    if missing:
        print(f"ERROR: required env vars missing on template: {', '.join(missing)}", file=sys.stderr)
        return 1

    if build_id:
        build_val = next(
            (item.get("value") for item in container.get("env", []) if item.get("name") == "BUILD_ID"),
            None,
        )
        if build_val != build_id:
            print(
                f"ERROR: BUILD_ID on template is '{build_val}', expected '{build_id}'",
                file=sys.stderr,
            )
            return 1

    active = data["properties"].get("latestRevisionName")
    print(f"Latest revision: {active}")
    return 0


def main() -> int:
    app = os.environ["ACA_APP_NAME"]
    rg = os.environ["ACA_RESOURCE_GROUP"]
    image = os.environ.get("ACA_IMAGE", "").strip()
    build_id = os.environ.get("ACA_BUILD_ID", "").strip()
    # Replicas are owned by the deploy template / Terraform — do not hard-code here.
    min_replicas = os.environ.get("ACA_MIN_REPLICAS", "").strip()
    max_replicas = os.environ.get("ACA_MAX_REPLICAS", "").strip()

    pairs, set_count = _build_pairs()
    if set_count == 0:
        print(
            "ERROR: No library env vars found in the agent environment. "
            "Authorize the stage's Azure DevOps Library variable group for this pipeline.",
            file=sys.stderr,
        )
        return 1

    cmd = [
        "az",
        "containerapp",
        "update",
        "--name",
        app,
        "--resource-group",
        rg,
        "--replace-env-vars",
        *pairs,
    ]
    if min_replicas:
        cmd.extend(["--min-replicas", min_replicas])
    if max_replicas:
        cmd.extend(["--max-replicas", max_replicas])
    if image:
        cmd.extend(["--image", image, "--revision-suffix", f"b{build_id or 'deploy'}"])

    print(f"Deploying {app} with image={image or '(unchanged)'} and {set_count} env vars (+ BUILD_ID) ...")
    subprocess.run(cmd, check=True)
    return _verify_template(app, rg, image, build_id)


if __name__ == "__main__":
    sys.exit(main())
