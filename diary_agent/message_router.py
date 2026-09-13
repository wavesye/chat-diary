"""Resolve external identities before any diary data is accessed."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from .channels.base import ChannelAdapter, IncomingMessage
from .chat_handler import ChatHandler


class MessageRouter:
    def __init__(self, registry, services):
        self.registry = registry
        self.services = services
        self.adapters: dict[str, ChannelAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        if adapter.name in self.adapters:
            raise ValueError(f"Channel already registered: {adapter.name}")
        self.adapters[adapter.name] = adapter

    def is_bound(self, channel: str, external_user_id: str) -> bool:
        return self.registry.resolve(channel, external_user_id) is not None

    @staticmethod
    def _receipt_key(message: IncomingMessage) -> str:
        identity = json.dumps([message.channel, message.external_user_id,
                               message.kind, message.message_id])
        return "message_receipt:" + hashlib.sha256(identity.encode()).hexdigest()

    async def route(self, message: IncomingMessage) -> bool:
        return await self.route_many([message])

    async def route_many(self, messages: list[IncomingMessage]) -> bool:
        # to_thread work cannot be cancelled. Finish the in-flight workflow before
        # runtime shutdown closes its SQLite connection or releases the user lock.
        task = asyncio.create_task(self._route_many(messages))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _route_many(self, messages: list[IncomingMessage]) -> bool:
        if not messages:
            return False
        first = messages[0]
        if any((item.channel, item.external_user_id) !=
               (first.channel, first.external_user_id) for item in messages):
            raise ValueError("A message batch must belong to one channel identity")
        adapter = self.adapters.get(first.channel)
        user_id = self.registry.resolve(first.channel, first.external_user_id)
        if adapter is None or user_id is None:
            return False  # No public self-registration or implicit account merging.
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            # An administrator may have removed the binding while this waited.
            if self.registry.resolve(first.channel, first.external_user_id) != user_id:
                return False
            unique = {}
            for message in messages:
                key = self._receipt_key(message)
                if not self.registry.get_state(key):
                    unique.setdefault(key, message)
            if not unique:
                return True
            pending = list(unique.values())
            handler = ChatHandler(self.services.get(user_id), adapter,
                                  user_id, first.external_user_id,
                                  binding_is_current=lambda: self.registry.resolve(
                                      first.channel, first.external_user_id) == user_id)
            if len(pending) > 1 and all(
                item.kind == "text" and not item.text.lstrip().startswith("/")
                for item in pending
            ):
                await handler.handle_texts([item.text for item in pending])
            else:
                for item in pending:
                    if item.kind == "action":
                        await handler.handle_action(item.text)
                    else:
                        await handler.handle_text(item.text)
            for key, item in unique.items():
                self.registry.set_state(key, item.timestamp.isoformat())
            return True

    async def remind(self, channel: str) -> None:
        adapter = self.adapters[channel]
        for binding in self.registry.bindings(channel):
            user_id = binding["user_id"]
            async with self._locks.setdefault(user_id, asyncio.Lock()):
                if self.registry.resolve(channel, binding["external_user_id"]) != user_id:
                    continue
                handler = ChatHandler(self.services.get(user_id), adapter, user_id,
                                      binding["external_user_id"],
                                      binding_is_current=lambda: self.registry.resolve(
                                          channel, binding["external_user_id"]) == user_id)
                try:
                    await handler.maybe_send_reminder()
                except Exception as error:
                    # A blocked bot or unreachable recipient must not bring down
                    # every user's channel. Do not mark a failed reminder sent.
                    logging.getLogger(__name__).warning(
                        "Reminder delivery failed: %s", type(error).__name__)
