"""Atomic Gemini API key leasing for parallel outbound calls.

Keys stay in process memory only. Public APIs return opaque slot ids (key_1, …),
never raw secrets. Optional Redis backend for multi-worker coordination.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import config

logger = logging.getLogger("key-pool")


class KeyPoolExhausted(Exception):
    """No API key became available before the lease timeout."""

    def __init__(self, message: str = "All Gemini API keys are currently leased"):
        super().__init__(message)


@dataclass
class KeyLease:
    """Opaque lease handle — never log or return ``api_key`` outside the agent."""

    slot_id: str
    api_key: str
    lease_id: str
    leased_at: float = field(default_factory=time.monotonic)

    def redacted(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "lease_id": self.lease_id,
            "leased_at_age_sec": round(time.monotonic() - self.leased_at, 2),
        }


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def load_gemini_api_keys() -> list[tuple[str, str]]:
    """Load unique Gemini keys from env.

    Accepts:
      - GEMINI_API_KEYS / GOOGLE_API_KEYS — comma-separated
      - GEMINI_API_KEY_1 … GEMINI_API_KEY_N / GOOGLE_API_KEY_1 …
      - GOOGLE_API_KEY / GEMINI_API_KEY — single fallback
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(slot: str, raw: str) -> None:
        key = (raw or "").strip()
        if not key:
            return
        if key in seen:
            logger.warning("Duplicate Gemini API key ignored for slot=%s", slot)
            return
        seen.add(key)
        pairs.append((slot, key))

    csv = (
        config.get_env("GEMINI_API_KEYS")
        or config.get_env("GOOGLE_API_KEYS")
        or ""
    ).strip()
    if csv:
        for i, part in enumerate(csv.split(","), start=1):
            _add(f"key_{i}", part.strip())

    # Numbered keys (scan a reasonable range)
    for i in range(1, 33):
        for prefix in ("GEMINI_API_KEY_", "GOOGLE_API_KEY_"):
            val = (os.getenv(f"{prefix}{i}") or "").strip()
            if val:
                _add(f"key_{i}", val)

    # Legacy single key
    single = (
        config.get_env("GOOGLE_API_KEY")
        or config.get_env("GEMINI_API_KEY")
        or ""
    ).strip()
    if single and not pairs:
        _add("key_1", single)
    elif single and single not in seen:
        # Prefer pool; also allow single as extra only if not already listed
        _add(f"key_{len(pairs) + 1}", single)

    return pairs


def lease_timeout_sec() -> float:
    raw = (config.get_env("GEMINI_KEY_LEASE_TIMEOUT_SEC") or "30").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


class InMemoryKeyPool:
    """Single-process atomic key pool (asyncio.Lock + Condition)."""

    def __init__(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            raise ValueError("Gemini key pool requires at least one API key")
        self._keys = list(keys)  # (slot_id, api_key)
        self._available: list[tuple[str, str]] = list(keys)
        self._leases: dict[str, KeyLease] = {}  # lease_id -> lease
        # Lazily bound to the running event loop (LiveKit job subprocesses each
        # have their own loop; creating Lock/Condition in __init__ breaks).
        self._lock: Optional[asyncio.Lock] = None
        self._cond: Optional[asyncio.Condition] = None
        self._total_leases = 0
        self._total_releases = 0
        self._total_wait_timeouts = 0

    def _sync(self) -> asyncio.Condition:
        if self._cond is None:
            self._lock = asyncio.Lock()
            self._cond = asyncio.Condition(self._lock)
        return self._cond

    @property
    def size(self) -> int:
        return len(self._keys)

    async def status(self) -> dict[str, Any]:
        async with self._sync():
            return {
                "backend": "memory",
                "total_keys": len(self._keys),
                "available": len(self._available),
                "leased": len(self._leases),
                "slots": [s for s, _ in self._keys],
                "leased_slots": [lease.slot_id for lease in self._leases.values()],
                "total_leases": self._total_leases,
                "total_releases": self._total_releases,
                "wait_timeouts": self._total_wait_timeouts,
                "lease_timeout_sec": lease_timeout_sec(),
            }

    async def available_count(self) -> int:
        async with self._sync():
            return len(self._available)

    async def lease(self, *, timeout: Optional[float] = None, call_id: str = "") -> KeyLease:
        """Lease one key. Waits up to timeout if pool is empty."""
        wait_for = lease_timeout_sec() if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait_for

        async with self._sync():
            while not self._available:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._total_wait_timeouts += 1
                    raise KeyPoolExhausted(
                        f"No Gemini API key available within {wait_for:.0f}s "
                        f"(pool size={len(self._keys)}, all leased)"
                    )
                try:
                    await asyncio.wait_for(self._sync().wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    self._total_wait_timeouts += 1
                    raise KeyPoolExhausted(
                        f"No Gemini API key available within {wait_for:.0f}s "
                        f"(pool size={len(self._keys)}, all leased)"
                    ) from None

            slot_id, api_key = self._available.pop(0)
            lease = KeyLease(
                slot_id=slot_id,
                api_key=api_key,
                lease_id=str(uuid.uuid4()),
            )
            self._leases[lease.lease_id] = lease
            self._total_leases += 1
            logger.info(
                "KEY_LEASED slot=%s lease=%s call_id=%s available=%d/%d",
                slot_id,
                lease.lease_id[:8],
                (call_id or "")[:12] or "-",
                len(self._available),
                len(self._keys),
            )
            return lease

    async def release(self, lease: Optional[KeyLease]) -> None:
        if lease is None:
            return
        cond = self._sync()
        async with cond:
            held = self._leases.pop(lease.lease_id, None)
            if held is None:
                return
            # Return key to pool (by slot identity)
            for slot_id, api_key in self._keys:
                if slot_id == held.slot_id:
                    self._available.append((slot_id, api_key))
                    break
            self._total_releases += 1
            logger.info(
                "KEY_RELEASED slot=%s lease=%s available=%d/%d",
                held.slot_id,
                held.lease_id[:8],
                len(self._available),
                len(self._keys),
            )
            cond.notify(1)

    @asynccontextmanager
    async def lease_context(
        self,
        *,
        timeout: Optional[float] = None,
        call_id: str = "",
    ) -> AsyncIterator[KeyLease]:
        lease = await self.lease(timeout=timeout, call_id=call_id)
        try:
            yield lease
        finally:
            await self.release(lease)


class RedisKeyPool:
    """Multi-worker key pool using Redis SET + BLMOVE-style lease (best-effort).

    Falls back semantics: available keys stored as Redis set members ``slot_id``.
    Secrets never stored in Redis — only slot ids; secrets resolved from local env map.
    """

    def __init__(self, keys: list[tuple[str, str]], redis_url: str) -> None:
        if not keys:
            raise ValueError("Gemini key pool requires at least one API key")
        self._by_slot = {slot: key for slot, key in keys}
        self._slots = [slot for slot, _ in keys]
        self._redis_url = redis_url
        self._prefix = (config.get_env("GEMINI_KEY_POOL_REDIS_PREFIX") or "gemini_key_pool").strip()
        self._client = None
        self._init_lock = asyncio.Lock()
        self._total_leases = 0
        self._total_releases = 0
        self._total_wait_timeouts = 0

    async def _redis(self):
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            import redis.asyncio as redis

            self._client = redis.from_url(self._redis_url, decode_responses=True)
            # Seed available set once (SADD is idempotent)
            avail_key = f"{self._prefix}:available"
            await self._client.sadd(avail_key, *self._slots)
            return self._client

    @property
    def size(self) -> int:
        return len(self._slots)

    async def status(self) -> dict[str, Any]:
        r = await self._redis()
        avail = int(await r.scard(f"{self._prefix}:available") or 0)
        leased = int(await r.scard(f"{self._prefix}:leased") or 0)
        return {
            "backend": "redis",
            "total_keys": len(self._slots),
            "available": avail,
            "leased": leased,
            "slots": list(self._slots),
            "total_leases": self._total_leases,
            "total_releases": self._total_releases,
            "wait_timeouts": self._total_wait_timeouts,
            "lease_timeout_sec": lease_timeout_sec(),
        }

    async def available_count(self) -> int:
        r = await self._redis()
        return int(await r.scard(f"{self._prefix}:available") or 0)

    async def lease(self, *, timeout: Optional[float] = None, call_id: str = "") -> KeyLease:
        wait_for = lease_timeout_sec() if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait_for
        r = await self._redis()
        avail_key = f"{self._prefix}:available"
        leased_key = f"{self._prefix}:leased"

        while True:
            slot_id = await r.spop(avail_key)
            if slot_id:
                api_key = self._by_slot.get(slot_id)
                if not api_key:
                    # Stale slot in Redis — discard and retry
                    continue
                lease = KeyLease(slot_id=slot_id, api_key=api_key, lease_id=str(uuid.uuid4()))
                await r.sadd(leased_key, slot_id)
                await r.setex(
                    f"{self._prefix}:lease:{lease.lease_id}",
                    int(max(3600, wait_for + 3600)),
                    slot_id,
                )
                self._total_leases += 1
                logger.info(
                    "KEY_LEASED slot=%s lease=%s call_id=%s backend=redis",
                    slot_id,
                    lease.lease_id[:8],
                    (call_id or "")[:12] or "-",
                )
                return lease

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._total_wait_timeouts += 1
                raise KeyPoolExhausted(
                    f"No Gemini API key available within {wait_for:.0f}s (redis pool)"
                )
            await asyncio.sleep(min(0.25, remaining))

    async def release(self, lease: Optional[KeyLease]) -> None:
        if lease is None:
            return
        r = await self._redis()
        pipe = r.pipeline()
        pipe.delete(f"{self._prefix}:lease:{lease.lease_id}")
        pipe.srem(f"{self._prefix}:leased", lease.slot_id)
        pipe.sadd(f"{self._prefix}:available", lease.slot_id)
        await pipe.execute()
        self._total_releases += 1
        logger.info("KEY_RELEASED slot=%s lease=%s backend=redis", lease.slot_id, lease.lease_id[:8])

    @asynccontextmanager
    async def lease_context(
        self,
        *,
        timeout: Optional[float] = None,
        call_id: str = "",
    ) -> AsyncIterator[KeyLease]:
        lease = await self.lease(timeout=timeout, call_id=call_id)
        try:
            yield lease
        finally:
            await self.release(lease)


_pool_by_loop: dict[int, Any] = {}
_redis_pool: Optional[Any] = None


def _running_loop_id() -> int:
    return id(asyncio.get_running_loop())


def create_job_key_pool() -> InMemoryKeyPool:
    """Fresh in-memory pool for one LiveKit job (no cross-process global cache)."""
    return InMemoryKeyPool(load_gemini_api_keys())


def assign_job_key(call_id: str = "", preferred_slot: str = "") -> KeyLease:
    """Pick a Gemini key for one LiveKit job without shared in-memory leasing.

    LiveKit runs each call in an isolated subprocess, so an in-memory pool always
    starts full and every job would lease ``key_1``. Prefer an explicit slot from
    the server (unique per parallel dial); otherwise hash ``call_id``.
    """
    keys = load_gemini_api_keys()
    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Set GOOGLE_API_KEY or GEMINI_API_KEYS."
        )
    by_slot = {slot: key for slot, key in keys}
    if preferred_slot and preferred_slot in by_slot:
        slot_id, api_key = preferred_slot, by_slot[preferred_slot]
    elif call_id:
        idx = int(hashlib.sha256(call_id.encode("utf-8")).hexdigest(), 16) % len(keys)
        slot_id, api_key = keys[idx]
    else:
        idx = int(time.time_ns()) % len(keys)
        slot_id, api_key = keys[idx]
    lease = KeyLease(
        slot_id=slot_id,
        api_key=api_key,
        lease_id=str(uuid.uuid4()),
    )
    logger.info(
        "KEY_ASSIGNED slot=%s lease=%s call_id=%s preferred=%s keys=%d",
        slot_id,
        lease.lease_id[:8],
        (call_id or "")[:12] or "-",
        preferred_slot or "-",
        len(keys),
    )
    return lease


def list_key_slots() -> list[str]:
    """Opaque slot ids only — safe for logs / metadata."""
    return [slot for slot, _ in load_gemini_api_keys()]


async def get_key_pool():
    """Lazy key pool — one in-memory pool per asyncio event loop (LiveKit job subprocesses)."""
    global _redis_pool
    redis_url = (config.get_env("REDIS_URL") or config.get_env("GEMINI_KEY_POOL_REDIS_URL") or "").strip()
    keys = load_gemini_api_keys()
    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Set GOOGLE_API_KEY or GEMINI_API_KEYS."
        )

    if redis_url:
        if _redis_pool is None:
            try:
                _redis_pool = RedisKeyPool(keys, redis_url)
                await _redis_pool.status()
                logger.info("Gemini key pool ready backend=redis keys=%d", len(keys))
            except Exception as exc:
                logger.warning("Redis key pool unavailable (%s) — using in-memory pool", exc)
                _redis_pool = None
        if _redis_pool is not None:
            return _redis_pool

    loop_id = _running_loop_id()
    pool = _pool_by_loop.get(loop_id)
    if pool is not None:
        return pool

    pool = InMemoryKeyPool(keys)
    _pool_by_loop[loop_id] = pool
    logger.info(
        "Gemini key pool ready backend=memory keys=%d slots=%s loop=%s",
        len(keys),
        [s for s, _ in keys],
        loop_id,
    )
    return pool


async def key_pool_public_status() -> dict[str, Any]:
    """Safe status for HTTP — no secrets."""
    try:
        pool = await get_key_pool()
        return await pool.status()
    except Exception as exc:
        return {
            "backend": "unavailable",
            "total_keys": 0,
            "available": 0,
            "leased": 0,
            "error": str(exc)[:200],
        }


def create_client_lease(client_api_key: str, call_id: Optional[str] = None) -> KeyLease:
    """Create a dedicated KeyLease for client-provided Gemini API key (no pool consumption)."""
    clean_key = (client_api_key or "").strip()
    if not clean_key:
        raise KeyPoolExhausted("Client Gemini API key is missing or empty")
    lease = KeyLease(
        slot_id="client_provided",
        api_key=clean_key,
        lease_id=str(uuid.uuid4()),
    )
    logger.info(
        "CLIENT_KEY_LEASE lease=%s call_id=%s slot=client_provided",
        lease.lease_id[:8],
        (call_id or "")[:12] or "-",
    )
    return lease


def redact_secrets(value: Any) -> Any:
    """Best-effort redaction of configured and dynamic API keys / credentials."""
    import re
    if value is None:
        return value
    if isinstance(value, dict):
        redacted = {}
        for k, v in value.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("key", "secret", "password", "token", "auth")):
                redacted[k] = "***REDACTED***"
            else:
                redacted[k] = redact_secrets(v)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    # Redact Google/Gemini API key patterns (AIzaSy...)
    out = re.sub(r"AIzaSy[A-Za-z0-9_\-]{10,}", "***REDACTED_GEMINI_KEY***", out)
    for _, key in load_gemini_api_keys():
        if key and key in out:
            out = out.replace(key, "***REDACTED_KEY***")
    return out
