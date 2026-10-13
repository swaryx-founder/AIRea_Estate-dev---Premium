"""Generate temporary signed URLs for private Azure Blob recordings."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import config


def azure_recording_configured() -> bool:
    return bool(
        config.get_env("AZURE_STORAGE_ACCOUNT") and config.get_env("AZURE_STORAGE_KEY")
    )


def is_azure_recording_url(recording_url: Optional[str]) -> bool:
    if not recording_url:
        return False
    return _parse_blob_path(recording_url) is not None


def _parse_blob_path(recording_url: str) -> Optional[tuple[str, str]]:
    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    if not account:
        return None
    parsed = urlparse(recording_url)
    if parsed.netloc.lower() != f"{account}.blob.core.windows.net":
        return None
    path = parsed.path.lstrip("/")
    if "/" not in path:
        return None
    container, blob_name = path.split("/", 1)
    return container, blob_name


def recording_blob_exists(recording_url: Optional[str]) -> bool:
    """True only when the blob is actually present in Azure (egress succeeded)."""
    if not recording_url:
        return False
    key = config.get_env("AZURE_STORAGE_KEY")
    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    if not (account and key):
        return False

    parsed = _parse_blob_path(recording_url)
    if not parsed:
        return False  # not an Azure Blob URL (e.g. old Supabase S3 paths)

    container, blob_name = parsed
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return False

    try:
        client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
        return client.get_blob_client(container=container, blob=blob_name).exists()
    except Exception:
        return False


def signed_recording_url(recording_url: Optional[str], *, expiry_hours: int = 1) -> Optional[str]:
    if not recording_url:
        return None

    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    key = config.get_env("AZURE_STORAGE_KEY")
    if not (account and key):
        return None

    parsed = _parse_blob_path(recording_url)
    if not parsed:
        return None  # only Azure Blob URLs get a signed play link
    container, blob_name = parsed

    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    except ImportError:
        return None

    expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=blob_name,
        account_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"{recording_url}?{sas}"
