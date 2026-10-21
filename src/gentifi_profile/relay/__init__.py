"""Owner confirmation relay: business owners answer agents' requests from a chat app.

Telegram first (free Bot API, one-tap buttons); WhatsApp is a later adapter behind
the same OwnerChannel interface. See docs/telegram-merchant-bot-spec.md.
"""

from .channel import Button, Incoming, MockChannel, OwnerChannel
from .links import Links
from .relay import OwnerRelay

__all__ = ["Button", "Incoming", "Links", "MockChannel", "OwnerChannel", "OwnerRelay"]
