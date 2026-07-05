from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from utils.retry import exponential_backoff


async def discord_call_with_retry(
    coro_factory: Callable[[], Awaitable[object]],
    /,
    *,
    label: str = "discord call",
    logger: logging.Logger,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Run a Discord HTTP call with retry/backoff on 429 responses."""
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            await coro_factory()
            return
        except discord.HTTPException as exc:
            last = exc
            if exc.status == 429:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after and isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = min(float(retry_after), max_delay)
                else:
                    delay = exponential_backoff(
                        attempt,
                        base=base_delay,
                        cap=max_delay,
                    )
                if attempt < max_attempts:
                    logger.warning(
                        "%s: rate-limited (attempt %d/%d); sleeping %.1fs",
                        label, attempt, max_attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
            raise
    if last is not None:
        raise last
