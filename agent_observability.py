"""Logging helpers shared by outbound-agent modules."""

import asyncio
import logging

from db import log_error


logger = logging.getLogger("outbound-agent")


async def log_event(level: str, message: str, detail: str = "") -> None:
    """Write to process logs immediately and persist asynchronously."""
    if level == "info":
        logger.info(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.error(message)

    async def persist() -> None:
        try:
            await log_error("agent", message, detail, level)
        except Exception:
            pass

    asyncio.create_task(persist())


_log = log_event
