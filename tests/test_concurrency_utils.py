from __future__ import annotations

import asyncio
import unittest

from gpbot.concurrency import get_or_create_lock, prune_lock_if_unused


class GetOrCreateLockTests(unittest.TestCase):
    def test_returns_same_lock_for_same_key(self):
        store = {}
        first = get_or_create_lock(store, 7)
        second = get_or_create_lock(store, 7)
        self.assertIs(first, second)
        self.assertIsInstance(first, asyncio.Lock)


class PruneLockIfUnusedTests(unittest.TestCase):
    def test_prunes_released_lock(self):
        store = {}
        get_or_create_lock(store, 7)
        prune_lock_if_unused(store, 7)
        self.assertNotIn(7, store)

    def test_missing_key_is_noop(self):
        prune_lock_if_unused({}, 7)

    def test_keeps_held_lock(self):
        async def scenario():
            store = {}
            lock = get_or_create_lock(store, 7)
            async with lock:
                prune_lock_if_unused(store, 7)
                self.assertIs(store.get(7), lock)

        asyncio.run(scenario())

    def test_keeps_lock_with_waiters(self):
        async def scenario():
            store = {}
            lock = get_or_create_lock(store, 7)
            await lock.acquire()
            waiter = asyncio.ensure_future(lock.acquire())
            await asyncio.sleep(0)  # queue the waiter
            lock.release()
            # Waiter woken but not yet resumed: pruning must keep the lock so
            # a third caller can't mint a second lock for the same key.
            prune_lock_if_unused(store, 7)
            self.assertIs(store.get(7), lock)
            await waiter
            lock.release()
            prune_lock_if_unused(store, 7)
            self.assertNotIn(7, store)

        asyncio.run(scenario())
