from __future__ import annotations

from collections.abc import Awaitable, Callable


# Keep this module discord-agnostic; caller passes framework-specific interaction objects.
InteractionHandler = Callable[[object, str], Awaitable[object | None]]


class CustomIDRouter:
    def __init__(self):
        self._routes: list[tuple[str, InteractionHandler]] = []
        self._default_handler: InteractionHandler | None = None

    def register_prefix(self, prefix: str, handler: InteractionHandler):
        self._routes.append((prefix, handler))

    def register_default(self, handler: InteractionHandler):
        self._default_handler = handler

    async def dispatch(self, interaction, custom_id: str):
        for prefix, handler in self._routes:
            if custom_id.startswith(prefix):
                return await handler(interaction, custom_id)
        if self._default_handler is not None:
            return await self._default_handler(interaction, custom_id)
        return None
