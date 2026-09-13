"""Backwards-compatible Telegram entry point and constructor.

New integrations should use TelegramAdapter and MessageRouter directly.
"""
from __future__ import annotations

import asyncio

from diary_agent.channels.telegram import TelegramAPI, TelegramAPIError, TelegramAdapter
from diary_agent.chat_handler import ChatHandler, HELP
from diary_agent.message_router import MessageRouter
from diary_agent.service import DiaryService
from diary_agent.users import ServicePool, UserRegistry


class TelegramDiaryBot(TelegramAdapter):
    """Compatibility facade; commands and business logic live in ChatHandler."""

    _friendly_error = staticmethod(ChatHandler._friendly_error)

    def __init__(self, service: DiaryService, api: TelegramAPI,
                 allowed_user_id: int | None,
                 message_debounce_seconds: float = 3.0):
        self.service = service
        self.allowed_user_id = allowed_user_id
        self.registry = UserRegistry(service.settings.database_path.parent / "channel_users.sqlite")
        local_id = self.registry.local_user_id()
        if allowed_user_id is not None:
            self.registry.bind("telegram", str(allowed_user_id), local_id)
        self.services = ServicePool(
            service.settings, self.registry,
            factory=lambda settings: service if settings == service.settings else DiaryService(settings),
        )
        self.router = MessageRouter(self.registry, self.services)
        super().__init__(
            self.router.route, api=api, state_store=service.store,
            is_authorized=lambda external_id: (
                allowed_user_id is not None and external_id == str(allowed_user_id)
            ),
            on_batch=self.router.route_many,
            on_tick=lambda: self.router.remind("telegram"),
            pairing_mode=allowed_user_id is None,
            message_debounce_seconds=message_debounce_seconds,
        )
        self.router.register(self)


async def async_main() -> None:
    from diary_agent.channels.runtime import run_channels
    await run_channels(only="telegram")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nTelegram Bot 已停止")
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
