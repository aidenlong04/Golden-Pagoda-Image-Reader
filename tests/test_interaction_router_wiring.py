from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord


class InteractionRouterWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_interaction_delegates_to_custom_id_router(self):
        import bot

        interaction = Mock()
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": "status:0"}

        with patch.object(bot._CUSTOM_ID_ROUTER, "dispatch", new=AsyncMock()) as dispatch:
            await bot.on_interaction(interaction)
            dispatch.assert_awaited_once_with(interaction, "status:0")
