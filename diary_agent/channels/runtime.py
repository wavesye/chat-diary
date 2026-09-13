"""Compose enabled adapters, identity storage, and the existing diary services."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from ..config import Settings
from ..message_router import MessageRouter
from ..users import ServicePool, UserRegistry
from .config import CHANNEL_NAMES, ChannelsConfig
from .line import LineAdapter
from .qq import QQAdapter
from .telegram import TelegramAPI, TelegramAdapter
from .webhook import WebhookChannelAdapter, create_webhook_app
from .wechat import WeChatAdapter
from .whatsapp import WhatsAppAdapter


class ChannelRuntime:
    def __init__(self, settings: Settings, config: ChannelsConfig, *, registry=None,
                 services=None):
        self.settings = settings
        self.config = config
        path = Path(os.getenv("CHANNEL_BINDINGS_DB") or
                    settings.database_path.parent / "channel_users.sqlite")
        self.registry = registry if registry is not None else UserRegistry(path)
        self.services = services if services is not None else ServicePool(settings, self.registry)
        self.router = MessageRouter(self.registry, self.services)
        self.adapters = self.router.adapters

    def build(self) -> None:
        for name, config in self.config.channels.items():
            if not config.enabled:
                continue
            credentials = config.credentials
            if name == "telegram":
                allowed_user_id = credentials["allowed_user_id"]
                local_id = self.registry.local_user_id()
                if allowed_user_id:
                    self.registry.bind(name, allowed_user_id, local_id)
                legacy_store = self.services.get(local_id).store
                adapter = TelegramAdapter(
                    self.router.route, api=TelegramAPI(credentials["bot_token"]),
                    state_store=self.registry,
                    legacy_state=legacy_store,
                    is_authorized=lambda external_id: self.router.is_bound("telegram", external_id),
                    on_batch=self.router.route_many,
                    on_tick=lambda: self.router.remind("telegram"),
                    pairing_mode=not bool(allowed_user_id),
                    message_debounce_seconds=float(credentials["message_debounce_seconds"]),
                )
            else:
                factory = {"line": LineAdapter, "whatsapp": WhatsAppAdapter,
                           "wechat": WeChatAdapter, "qq": QQAdapter}[name]
                adapter = factory(self.router.route, **credentials)
            self.router.register(adapter)
        if not self.adapters:
            raise ValueError("No chat channels are enabled")

    async def run(self) -> None:
        tasks = []
        server = None
        try:
            # Fail before starting any live transport if an unfinished adapter is enabled.
            for name, factory in (("wechat", WeChatAdapter), ("qq", QQAdapter)):
                config = self.config.channels.get(name)
                if config and config.enabled:
                    await factory(self.router.route, **config.credentials).start()
            self.build()
            webhooks = [adapter for adapter in self.adapters.values()
                        if isinstance(adapter, WebhookChannelAdapter)]
            if webhooks:
                try:
                    port = int(os.getenv("CHANNEL_WEBHOOK_PORT", "8080"))
                except ValueError:
                    raise ValueError("CHANNEL_WEBHOOK_PORT must be an integer") from None
                if not 1 <= port <= 65535:
                    raise ValueError("CHANNEL_WEBHOOK_PORT must be between 1 and 65535")
                server = uvicorn.Server(uvicorn.Config(
                    create_webhook_app(webhooks),
                    host=os.getenv("CHANNEL_WEBHOOK_HOST", "127.0.0.1"),
                    port=port, log_level="info", access_log=False,
                ))
                tasks.append(asyncio.create_task(server.serve(), name="channel-webhooks"))
            tasks.extend(asyncio.create_task(adapter.start(), name=f"channel-{name}")
                         for name, adapter in self.adapters.items())
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            if server:
                server.should_exit = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(adapter.stop() for adapter in self.adapters.values()),
                                 return_exceptions=True)
            self.services.close()
            self.registry.close()


async def run_channels(*, only: str | None = None) -> None:
    env = dict(os.environ)
    if only is not None:
        if only not in CHANNEL_NAMES:
            raise ValueError("Unknown channel")
        for name in CHANNEL_NAMES:
            if name != only:
                env[f"CHANNEL_{name.upper()}_ENABLED"] = "false"
    config = ChannelsConfig.from_env(env)
    await ChannelRuntime(Settings.from_env(), config).run()
