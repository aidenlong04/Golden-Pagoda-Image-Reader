from __future__ import annotations

from collections.abc import Awaitable, Callable


class CustomIDRouter:
    def __init__(self):
        self._routes: list[tuple[str, Callable]] = []
        self._default_handler: Callable | None = None

    def register_prefix(self, prefix: str, handler: Callable):
        self._routes.append((prefix, handler))

    def register_default(self, handler: Callable):
        self._default_handler = handler

    async def dispatch(self, interaction, custom_id: str):
        for prefix, handler in self._routes:
            if custom_id.startswith(prefix):
                return await handler(interaction, custom_id)
        if self._default_handler is not None:
            return await self._default_handler(interaction, custom_id)
        return None
