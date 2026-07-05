from __future__ import annotations

import discord
from discord import app_commands


def build_client_tree():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    return client, tree
