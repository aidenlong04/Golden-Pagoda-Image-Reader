from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from gpbot.routing import CustomIDRouter


class CustomIDRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_first_matching_prefix(self):
        router = CustomIDRouter()
        manage = AsyncMock()
        status = AsyncMock()
        router.register_prefix("manage:", manage)
        router.register_prefix("status:", status)

        await router.dispatch(object(), "manage:1:p:0")

        manage.assert_awaited_once()
        status.assert_not_awaited()

    async def test_dispatches_to_default_when_no_prefix_matches(self):
        router = CustomIDRouter()
        default = AsyncMock()
        router.register_default(default)

        await router.dispatch(object(), "unknown:thing")

        default.assert_awaited_once()
