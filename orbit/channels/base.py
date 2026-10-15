"""Channel protocol: anything that can receive messages from the user and push messages back."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Incoming:
    channel: str
    chat_id: str
    text: str


MessageHandler = Callable[[Incoming], Awaitable[str]]


class Channel(Protocol):
    name: str

    async def start(self, handler: MessageHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_id: str, text: str) -> bool:
        """Deliver a proactive message. Returns False if this channel cannot deliver it."""
        ...

    def default_chat_id(self) -> str | None:
        """Where unsolicited pushes (briefings, fallbacks) go on this channel, if anywhere."""
        ...
