"""One-shot connectivity check against .env — run from repo root."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["LOAD_DOTENV"] = "true"

import config  # noqa: E402 — loads .env when LOAD_DOTENV=true

OK = 0
FAIL = 0
WARN = 0


def _mark(ok: bool, label: str, detail: str = "", warn: bool = False) -> None:
    global OK, FAIL, WARN
    if warn:
        WARN += 1
        status = "WARN"
    elif ok:
        OK += 1
        status = "OK  "
    else:
        FAIL += 1
        status = "FAIL"
    extra = f" — {detail}" if detail else ""
    print(f"[{status}] {label}{extra}")


def check_env_keys() -> None:
    required = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID",
        "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_KEY", "AZURE_STORAGE_CONTAINER",
    ]
    optional = [
        "CALL_COMPLETED_WEBHOOK_URL", "ACS_CONNECTION_STRING", "ACS_SENDER_EMAIL",
        "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN", "API_AUTH_TOKEN", "DEEPGRAM_API_KEY",
        "META_WHATSAPP_TOKEN",
    ]
    print("\n=== Env vars ===")
    for k in required:
        v = config.get_env(k)
        _mark(bool(v), k, "set" if v else "MISSING (required)")
    for k in optional:
        v = config.get_env(k)
        if v:
            _mark(True, k, "set")
        else:
            _mark(True, k, "not set (optional)", warn=True)


async def check_supabase() -> None:
    print("\n=== Supabase ===")
    try:
        from db import _adb, init_db
        init_db()
        db = await _adb()
        r = await db.table("call_logs").select("id").limit(1).execute()
        _mark(True, "Supabase connect", f"call_logs readable (sample={len(r.data or [])})")
        for table in ("appointments", "campaigns", "settings", "agent_profiles", "error_logs"):
            try:
                await db.table(table).select("*").limit(1).execute()
                _mark(True, f"table {table}", "ok")
            except Exception as exc:
                _mark(False, f"table {table}", str(exc)[:120])
    except Exception as exc:
        _mark(False, "Supabase connect", str(exc)[:200])


async def check_azure_blob() -> None:
    print("\n=== Azure Blob ===")
    try:
        from azure.storage.blob import BlobServiceClient
        account = config.get_env("AZURE_STORAGE_ACCOUNT")
        key = config.get_env("AZURE_STORAGE_KEY")
        container = config.get_env("AZURE_STORAGE_CONTAINER", "recordings")
        client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
        cc = client.get_container_client(container)
        exists = cc.exists()
        _mark(exists, "Azure container", f"{account}/{container} exists={exists}")
        if exists:
            blobs = list(cc.list_blobs(results_per_page=3))
            _mark(True, "Azure list blobs", f"ok (listed up to {len(blobs)})")
    except Exception as exc:
        _mark(False, "Azure Blob", str(exc)[:200])


async def check_livekit() -> None:
    print("\n=== LiveKit ===")
    try:
        import ssl
        import aiohttp
        from livekit import api as lk_api

        url = config.get_env("LIVEKIT_URL")
        key = config.get_env("LIVEKIT_API_KEY")
        secret = config.get_env("LIVEKIT_API_SECRET")
        ssl_ctx = ssl.create_default_context()
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_ctx, limit=5),
        )
        try:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            rooms = await lk.room.list_rooms(lk_api.ListRoomsRequest())
            n = len(rooms.rooms or [])
            _mark(True, "LiveKit API", f"list_rooms ok (rooms={n})")
        finally:
            await session.close()
    except Exception as exc:
        _mark(False, "LiveKit API", str(exc)[:200])


async def check_vobiz() -> None:
    print("\n=== Vobiz ===")
    try:
        from vobiz_api import vobiz_api_configured, fetch_account_rate_hint, get_cached_cdrs_for_calls
        _mark(vobiz_api_configured(), "Vobiz Auth ID/Token", "configured" if vobiz_api_configured() else "missing")
        if vobiz_api_configured():
            hint = await fetch_account_rate_hint()
            _mark(True, "Vobiz /auth/me rate hint", str(hint) if hint is not None else "no tier (ok)")
            cdrs, meta = await get_cached_cdrs_for_calls([
                {"phone_number": config.get_env("VOBIZ_OUTBOUND_NUMBER"), "timestamp": None, "duration_seconds": 60}
            ])
            err = (meta or {}).get("error")
            if err:
                _mark(False, "Vobiz CDR fetch", str(err)[:160])
            else:
                _mark(True, "Vobiz CDR fetch", f"cdrs={meta.get('cdr_count', len(cdrs))}")
    except Exception as exc:
        _mark(False, "Vobiz", str(exc)[:200])


async def check_webhook() -> None:
    print("\n=== Call-completed webhook ===")
    try:
        import httpx
        from call_webhook import webhook_url, webhook_enabled, webhook_status
        st = webhook_status()
        _mark(webhook_enabled(), "Webhook config", f"host={st.get('host')} enabled={st.get('enabled')}")
        url = webhook_url()
        if not url:
            _mark(False, "Webhook URL", "empty")
            return
        # OPTIONS/GET may 404 — try a lightweight HEAD/GET without posting call data
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url.rstrip("/").rsplit("/api/", 1)[0] + "/api/health")
                _mark(resp.status_code < 500, "Node gateway health", f"HTTP {resp.status_code}")
            except Exception:
                # Fall back: POST would mutate — just TCP/TLS reachability via GET of webhook URL
                resp = await client.get(url)
                _mark(
                    resp.status_code < 500,
                    "Webhook URL reachable",
                    f"HTTP {resp.status_code} (endpoint may require POST)",
                    warn=resp.status_code >= 400,
                )
    except Exception as exc:
        _mark(False, "Webhook", str(exc)[:200], warn=True)


async def check_acs_email() -> None:
    print("\n=== ACS Email ===")
    try:
        from email_followup import email_enabled
        enabled = email_enabled() if hasattr(__import__("email_followup"), "email_enabled") else False
    except Exception:
        enabled = False
    conn = bool(config.get_env("ACS_CONNECTION_STRING"))
    sender = bool(config.get_env("ACS_SENDER_EMAIL"))
    flag = config.get_env("ACS_EMAIL_ENABLED", "").lower() in ("1", "true", "yes")
    _mark(conn and sender and flag, "ACS email config", f"enabled_flag={flag} conn={conn} sender={sender}")


async def check_google_key() -> None:
    print("\n=== Google API key ===")
    key = config.get_env("GOOGLE_API_KEY")
    if not key:
        _mark(False, "GOOGLE_API_KEY", "missing")
        return
    try:
        import httpx
        # Lightweight Generative Language API ping (may 400 without model — still proves key shape)
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                n = len(data.get("models") or [])
                _mark(True, "Google API key", f"models list ok ({n} models)")
            elif resp.status_code in (400, 403):
                _mark(False, "Google API key", f"HTTP {resp.status_code}: {(resp.text or '')[:120]}")
            else:
                _mark(False, "Google API key", f"HTTP {resp.status_code}")
    except Exception as exc:
        _mark(False, "Google API key", str(exc)[:200])


async def main() -> int:
    print(f"Working dir: {ROOT}")
    print(
        f"LOAD_DOTENV={os.getenv('LOAD_DOTENV')} "
        f"AI_AGENT_NAME={config.livekit_agent_name()}"
    )
    check_env_keys()
    await check_supabase()
    await check_azure_blob()
    await check_livekit()
    await check_vobiz()
    await check_webhook()
    await check_acs_email()
    await check_google_key()
    print(f"\n=== Summary: OK={OK} FAIL={FAIL} WARN={WARN} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # email_enabled helper
    try:
        import email_followup as ef
        if not hasattr(ef, "email_enabled"):
            def _email_enabled():
                if config.get_env("ACS_EMAIL_ENABLED", "").lower() in ("0", "false", "no"):
                    return False
                return bool(config.get_env("ACS_CONNECTION_STRING") and config.get_env("ACS_SENDER_EMAIL"))
            ef.email_enabled = _email_enabled  # type: ignore
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
