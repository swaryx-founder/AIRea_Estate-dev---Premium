"""Verify Azure Blob credentials — run before debugging LiveKit egress.

Usage (local):
  set LOAD_DOTENV=true
  python scripts/test_azure_storage.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def main() -> int:
    account = config.get_env("AZURE_STORAGE_ACCOUNT")
    key = config.get_env("AZURE_STORAGE_KEY")
    container = config.get_env("AZURE_STORAGE_CONTAINER", "recordings")

    if not account or not key:
        print("FAIL: AZURE_STORAGE_ACCOUNT and AZURE_STORAGE_KEY must be set")
        print("  Add them to Azure Container App env vars (not only Supabase S3 vars).")
        return 1

    print(f"Account:   {account}")
    print(f"Container: {container}")

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        print("FAIL: pip install azure-storage-blob")
        return 1

    client = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )

    # 1) Container exists?
    try:
        cc = client.get_container_client(container)
        cc.get_container_properties()
        print(f"OK: Container '{container}' exists")
    except Exception as exc:
        print(f"FAIL: Container '{container}' — {exc}")
        print("  Fix: Azure Portal → swaryxbucket → Containers → create 'recordings'")
        return 1

    # 2) Can we write?
    blob_name = f"test-upload-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.txt"
    try:
        blob = cc.get_blob_client(blob_name)
        blob.upload_blob(b"OutboundAI storage test OK", overwrite=True)
        print(f"OK: Uploaded test blob {blob_name}")
        blob.delete_blob()
        print("OK: Deleted test blob")
    except Exception as exc:
        print(f"FAIL: Upload test — {exc}")
        print("  Fix: Storage account → Networking → Public network access: Enabled")
        return 1

    print("\nAzure Blob credentials work. If recordings still missing:")
    print("  1) Same 3 env vars on Azure Container App (not only local .env)")
    print("  2) LiveKit Cloud -> Egress -> check failed jobs after a call")
    print("  3) Container App logs: search 'Recording egress'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
