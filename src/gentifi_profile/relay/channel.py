"""How the relay talks to business owners.

The relay only ever uses this interface, so the owner's messaging app is a
configuration choice: Telegram now, WhatsApp later, a mock in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # opaque payload returned when pressed (Telegram allows up to 64 bytes)


Keyboard = list[list[Button]]  # rows of buttons


@dataclass(frozen=True)
class Incoming:
    """Something the owner did: pressed a button, or sent text (including /commands)."""
    kind: str                    # "button", "text", "photo", or "file" (one the bot can't read)
    chat_id: str
    value: str                   # button data, the message text, a photo's file id, or a file's type
    message_id: str | None = None   # the message the button belongs to
    ack_id: str | None = None       # channel-specific id to acknowledge a button press
    name: str | None = None         # the sender's display name, when known
    mime: str = "image/jpeg"        # a photo's type (Telegram's own photos are JPEG)


class OwnerChannel(Protocol):
    def send(self, chat_id: str, text: str, buttons: Keyboard | None = None) -> str:
        """Send a message; returns its id so it can be edited later."""

    def edit(self, chat_id: str, message_id: str, text: str, buttons: Keyboard | None = None) -> None:
        """Replace a message's text and buttons (no buttons removes them)."""

    def poll(self, timeout: int) -> list[Incoming]:
        """Wait up to `timeout` seconds for owner activity."""

    def ack(self, incoming: Incoming, note: str | None = None) -> None:
        """Acknowledge a button press (stops the app's loading spinner)."""

    def send_photo(self, chat_id: str, photo: str, caption: str = "") -> str:
        """Pass on a photo the channel received (by its id); returns the message id."""

    def download(self, file_id: str) -> bytes:
        """The bytes of a photo an owner sent."""


@dataclass
class Sent:
    chat_id: str
    message_id: str
    text: str
    buttons: Keyboard | None
    photo: str | None = None


@dataclass
class MockChannel:
    """In-memory channel for tests and dry runs. `press` and `type` play the owner."""
    messages: dict[str, Sent] = field(default_factory=dict)
    inbox: list[Incoming] = field(default_factory=list)
    acks: list[tuple[Incoming, str | None]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    _next: int = 1

    def send(self, chat_id: str, text: str, buttons: Keyboard | None = None) -> str:
        mid = str(self._next)
        self._next += 1
        self.messages[mid] = Sent(chat_id, mid, text, buttons)
        return mid

    def edit(self, chat_id: str, message_id: str, text: str, buttons: Keyboard | None = None) -> None:
        self.messages[message_id] = Sent(chat_id, message_id, text, buttons)

    def poll(self, timeout: int) -> list[Incoming]:
        out, self.inbox = self.inbox, []
        return out

    def ack(self, incoming: Incoming, note: str | None = None) -> None:
        self.acks.append((incoming, note))

    def send_photo(self, chat_id: str, photo: str, caption: str = "") -> str:
        mid = self.send(chat_id, caption)
        self.messages[mid].photo = photo
        return mid

    def download(self, file_id: str) -> bytes:
        return self.files[file_id]

    # ---- helpers for tests

    def last(self, chat_id: str | None = None) -> Sent:
        msgs = [m for m in self.messages.values() if chat_id is None or m.chat_id == chat_id]
        return max(msgs, key=lambda m: int(m.message_id))

    def labels(self, message_id: str) -> list[str]:
        return [b.label for row in (self.messages[message_id].buttons or []) for b in row]

    def press(self, message_id: str, label: str) -> None:
        msg = self.messages[message_id]
        button = next(b for row in (msg.buttons or []) for b in row if b.label == label)
        self.inbox.append(Incoming("button", msg.chat_id, button.data, message_id, ack_id=button.data))

    def type(self, chat_id: str, text: str, name: str | None = "Owner") -> None:
        self.inbox.append(Incoming("text", chat_id, text, name=name))

    def photo(self, chat_id: str, data: bytes, name: str | None = "Owner") -> str:
        file_id = f"file{len(self.files) + 1}"
        self.files[file_id] = data
        self.inbox.append(Incoming("photo", chat_id, file_id, name=name))
        return file_id
