from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable


class MessageProcessingError(RuntimeError):
    """A temporary processing failure has been reported to the sender."""


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    external_user_id: str
    message_id: str
    text: str
    timestamp: datetime
    kind: str = "text"

    def __post_init__(self):
        if not all(isinstance(value, str) and value.strip() for value in (
            self.channel, self.external_user_id, self.message_id,
        )):
            raise ValueError("Channel, external user ID and message ID are required")
        if not isinstance(self.text, str):
            raise ValueError("Message text must be a string")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Message timestamp must include a timezone")
        if self.kind not in {"text", "action"}:
            raise ValueError("Unsupported message kind")


@dataclass(frozen=True)
class Button:
    text: str
    value: str


class ChannelAdapter(ABC):
    name: str

    def __init__(self, on_message: Callable[[IncomingMessage], Awaitable[None]]):
        self.on_message = on_message

    @abstractmethod
    async def start(self) -> None:
        """Receive messages until stopped; propagate startup failures."""

    @abstractmethod
    async def stop(self) -> None:
        """Release transport resources. Safe to call more than once."""

    @abstractmethod
    async def send_message(self, user_id: str, text: str, *,
                           buttons: tuple[tuple[Button, ...], ...] = ()) -> None:
        """Deliver to an external recipient ID in this channel's namespace."""

    async def show_typing(self, user_id: str) -> None:
        """Optional cosmetic capability; must not block message processing."""
