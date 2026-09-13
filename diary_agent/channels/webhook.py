"""Verified webhook ingress; deliberately separate from the personal web API."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .base import Button, ChannelAdapter, IncomingMessage


class WebhookSignatureError(ValueError):
    """Missing or invalid platform authentication."""


class WebhookChannelAdapter(ChannelAdapter):
    def __init__(self, on_message, *, client: httpx.AsyncClient | None = None):
        super().__init__(on_message)
        self.client = client or httpx.AsyncClient(timeout=30)
        self._stop_event = asyncio.Event()
        self.running = False

    async def start(self) -> None:
        self.running = True
        try:
            await self._stop_event.wait()
        finally:
            self.running = False

    async def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        await self.client.aclose()

    @abstractmethod
    def parse_request(self, raw_body: bytes, headers: Mapping[str, str]) -> list[IncomingMessage]:
        """Verify the unmodified request body before decoding messages."""

    async def _post(self, url: str, *, token: str, payload: dict) -> None:
        try:
            response = await self.client.post(
                url, headers={"Authorization": f"Bearer {token}"}, json=payload,
            )
        except httpx.HTTPError:
            raise RuntimeError(f"{self.name} message delivery failed; check network connectivity") from None
        if not response.is_success:
            raise RuntimeError(f"{self.name} message delivery failed (HTTP {response.status_code})")


def header_value(headers: Mapping[str, str], name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def event_timestamp(value, *, milliseconds: bool = False) -> datetime:
    try:
        return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        raise ValueError("Invalid webhook message timestamp") from None


def button_text(text: str, buttons: tuple[tuple[Button, ...], ...]) -> str:
    """Render shared Todo actions as commands on text-only transports."""
    hints = []
    for row in buttons:
        for button in row:
            parts = button.value.split(":")
            if len(parts) != 3 or parts[0] != "todo" or not parts[2].isdigit():
                continue
            action, todo_id = parts[1:]
            command = {
                "confirm": f"/todo confirm {todo_id}",
                "done": f"/todo done {todo_id}",
                "cancel": f"/todo cancel {todo_id}",
                "tomorrow": f"/todo postpone {todo_id} 明天",
                "day2": f"/todo postpone {todo_id} 后天",
                "edit": f"/todo edit {todo_id} 新标题",
            }.get(action)
            if command:
                hints.append(f"{button.text}：{command}")
    return text + ("\n\n" + "\n".join(hints) if hints else "")


def create_webhook_app(adapters: Mapping[str, ChannelAdapter] | Iterable[ChannelAdapter]) -> FastAPI:
    """Mount only adapters passed by the enabled-channel runtime.

    Successful handling is acknowledged synchronously. TODO: a durable inbound
    queue is needed before acknowledging early for slow model calls at scale.
    Router deduplication protects against ordinary platform webhook redelivery.
    """
    values = adapters.values() if isinstance(adapters, Mapping) else adapters
    registered = {adapter.name: adapter for adapter in values
                  if isinstance(adapter, WebhookChannelAdapter)}
    app = FastAPI(title="Chat Diary Channel Webhooks", docs_url=None, redoc_url=None,
                  openapi_url=None)

    def adapter_for(name: str) -> WebhookChannelAdapter:
        adapter = registered.get(name)
        if adapter is None:
            raise HTTPException(404, "Channel webhook is not enabled")
        return adapter

    @app.get("/webhooks/{channel}")
    async def verify(channel: str, request: Request):
        adapter = adapter_for(channel)
        verify_challenge = getattr(adapter, "verify_challenge", None)
        if verify_challenge is None:
            raise HTTPException(405, "This channel does not use GET verification")
        try:
            challenge = verify_challenge(request.query_params)
        except ValueError:
            raise HTTPException(403, "Invalid webhook verification") from None
        return PlainTextResponse(challenge)

    @app.post("/webhooks/{channel}")
    async def receive(channel: str, request: Request):
        adapter = adapter_for(channel)
        # Bound memory even when Content-Length is absent or untrusted.
        raw_body = bytearray()
        async for chunk in request.stream():
            raw_body.extend(chunk)
            if len(raw_body) > 1_048_576:
                raise HTTPException(413, "Webhook payload is too large")
        try:
            messages = adapter.parse_request(bytes(raw_body), request.headers)
        except WebhookSignatureError:
            raise HTTPException(403, "Invalid webhook signature") from None
        except ValueError:
            raise HTTPException(400, "Invalid webhook payload") from None
        try:
            for message in messages:
                await adapter.on_message(message)
        except Exception:
            raise HTTPException(503, "Message processing failed; delivery can be retried") from None
        return {"ok": True}

    return app
