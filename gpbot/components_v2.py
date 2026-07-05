from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiohttp
import discord


DISCORD_API_BASE = "https://discord.com/api/v10"
V2_USER_AGENT = "GoldenPagoda (https://github.com/aidenlong04, 1.0)"


async def v2_multipart_request(
    session: aiohttp.ClientSession,
    *,
    method: str,
    url: str,
    bot_token: str,
    payload: dict[str, Any],
    file_bytes: bytes,
    file_name: str,
    file_content_type: str = "image/png",
    extra_files: list[tuple[bytes, str]] | None = None,
) -> dict[str, Any] | None:
    form = aiohttp.FormData()
    form.add_field(
        "payload_json", json.dumps(payload),
        content_type="application/json",
    )
    form.add_field(
        "files[0]", file_bytes,
        filename=file_name, content_type=file_content_type,
    )
    for idx, (extra_bytes, extra_name) in enumerate(extra_files or [], start=1):
        form.add_field(
            f"files[{idx}]", extra_bytes,
            filename=extra_name, content_type="image/png",
        )
    headers = {
        "Authorization": f"Bot {bot_token}",
        "User-Agent": V2_USER_AGENT,
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with session.request(
        method, url, data=form, headers=headers, timeout=timeout
    ) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise discord.HTTPException(resp, text)  # type: ignore[arg-type]
        try:
            return await resp.json()
        except (aiohttp.ContentTypeError, ValueError):
            return None


async def interaction_callback(
    *,
    http_client: Any,
    interaction: discord.Interaction,
    callback_type: int,
    components: list[dict],
    flags: int,
    allowed_mentions: Mapping[str, Any],
) -> None:
    from discord.http import Route

    route = Route(
        "POST",
        "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id,
        interaction_token=interaction.token,
    )
    await http_client.request(
        route,
        json={
            "type": callback_type,
            "data": {
                "flags": flags,
                "components": components,
                "allowed_mentions": allowed_mentions,
            },
        },
    )


async def interaction_edit_original_v2(
    *,
    http_client: Any,
    interaction: discord.Interaction,
    components: list[dict],
    allowed_mentions: Mapping[str, Any],
) -> None:
    from discord.http import Route

    route = Route(
        "PATCH",
        "/webhooks/{application_id}/{interaction_token}/messages/@original",
        application_id=interaction.application_id,
        interaction_token=interaction.token,
    )
    await http_client.request(
        route,
        json={
            "components": components,
            "allowed_mentions": allowed_mentions,
        },
    )
