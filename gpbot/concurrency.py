from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def run_heavy_job(
    semaphore: asyncio.Semaphore,
    metrics: Any,
    func: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    enqueue_ts = metrics.record_enqueue()
    async with semaphore:
        metrics.record_acquire(enqueue_ts)
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        finally:
            metrics.record_release()


def spawn_bg_task(
    coro: Awaitable[Any],
    *,
    task_set: set[asyncio.Task],
    on_done: Callable[[asyncio.Task], None],
) -> asyncio.Task:
    task = asyncio.create_task(coro)
    task_set.add(task)
    task.add_done_callback(on_done)
    return task


def get_or_create_lock(lock_map: dict[Any, asyncio.Lock], key: Any) -> asyncio.Lock:
    lock = lock_map.get(key)
    if lock is None:
        lock = asyncio.Lock()
        lock_map[key] = lock
    return lock


def prune_lock_if_unused(lock_map: dict[Any, asyncio.Lock], key: Any) -> None:
    """Drop ``key``'s lock from ``lock_map`` when nothing holds or awaits it.

    Call after releasing a lock obtained via ``get_or_create_lock`` so the map
    stays bounded instead of accumulating one idle ``asyncio.Lock`` per key
    forever. Skips locks that are held or have queued waiters — a waiter still
    holds a reference to the mapped lock, and pruning it would let a later
    caller mint a second lock for the same key (breaking mutual exclusion).
    """
    lock = lock_map.get(key)
    if lock is None or lock.locked():
        return
    if getattr(lock, "_waiters", None):
        return
    del lock_map[key]
