"""Azure-backed recording startup for outbound calls."""

from livekit import agents, api

import config
from agent_observability import log_event
from tools import AppointmentTools


async def start_recording(ctx: agents.JobContext, tool_ctx: AppointmentTools) -> None:
    """Start non-blocking room egress recording to Azure Blob Storage."""
    recording_path = f"{ctx.room.name}.ogg"
    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    account_key = config.get_env("AZURE_STORAGE_KEY")
    container = config.get_env("AZURE_STORAGE_CONTAINER", "recordings")

    if not (account and account_key):
        await log_event(
            "warning",
            "Recording SKIPPED — Azure Blob required. Set AZURE_STORAGE_ACCOUNT, "
            "AZURE_STORAGE_KEY, AZURE_STORAGE_CONTAINER (not Supabase S3).",
        )
        return

    pending_url = f"https://{account}.blob.core.windows.net/{container}/{recording_path}"
    try:
        upload = api.EncodedFileOutput(
            file_type=api.EncodedFileType.OGG,
            filepath=recording_path,
            azure=api.AzureBlobUpload(
                account_name=account,
                account_key=account_key,
                container_name=container,
            ),
        )
        await log_event(
            "info",
            f"Recording config: azure account={account} container={container} "
            f"path={recording_path}",
        )
        request = api.RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            audio_only=True,
            file_outputs=[upload],
        )
        egress = await ctx.api.egress.start_room_composite_egress(request)
        tool_ctx.recording_url = pending_url
        await log_event(
            "info",
            f"Recording egress STARTED: id={egress.egress_id} backend=azure "
            f"url={tool_ctx.recording_url} — check LiveKit Cloud -> Egress if file missing",
        )
    except Exception as exc:
        tool_ctx.recording_url = None
        await log_event(
            "warning",
            f"Recording egress FAILED (azure): {exc} — "
            "Common fixes: (1) LiveKit egress quota/plan (2) AZURE_STORAGE_KEY correct "
            "(3) container name matches (4) Storage Networking = all networks "
            "(5) LiveKit Cloud -> Egress tab for error details",
            detail=str(exc),
        )


_start_recording = start_recording
