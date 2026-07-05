from __future__ import annotations

import asyncio
import unittest

from gpbot.concurrency import get_or_create_lock


class GetOrCreateLockTests(unittest.TestCase):
    def test_returns_same_lock_for_same_key(self):
        store = {}
        first = get_or_create_lock(store, 7)
        second = get_or_create_lock(store, 7)
        self.assertIs(first, second)
        self.assertIsInstance(first, asyncio.Lock)
