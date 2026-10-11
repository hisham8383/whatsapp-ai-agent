"""The seam where the bot's reply is produced.

``InboundHandler`` does the plumbing (state machine, window, sending). It asks
a ``Replier`` only for the reply. ``AgentReplier`` (``app/conversation/agent.py``)
is the production replier: LLM + tools. ``EchoReplier`` stays as the minimal
replier that proves the webhook → queue → worker → send slice in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.db.models import Conversation, Customer
from app.whatsapp.client import ListRow


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """Everything a replier needs to produce the next message."""

    session: Session  # open; the worker commits
    conversation: Conversation
    customer: Customer
    language: str  # "ar" | "en", detected from this message
    text: str | None  # message text, or the title of the picked list option
    reply_id: str | None  # id of the interactive option the customer picked, if any
    inbound_message_id: int | None  # our ``messages.id`` for this inbound message


@dataclass(frozen=True, slots=True)
class Reply:
    """What to send (§4).

    With ``rows`` it goes out as an interactive list (time slots); with
    ``buttons`` as reply buttons ("Confirm / Change"); otherwise as plain text.
    """

    text: str
    rows: tuple[ListRow, ...] = ()
    button: str = ""  # label of the button that opens the list
    buttons: tuple[ListRow, ...] = ()


class Replier(Protocol):
    """Produces the bot's reply."""

    def reply(self, ctx: ReplyContext) -> Reply | None:
        """Return the reply, or ``None`` to send nothing."""
        ...


class EchoReplier:
    """Minimal replier: echoes the message back in the customer's language."""

    def reply(self, ctx: ReplyContext) -> Reply | None:
        """Echo ``ctx.text`` with a short prefix, or ``None`` if there is no text."""
        if not ctx.text:
            return None
        prefix = "وصلتني رسالتك: " if ctx.language == "ar" else "You said: "
        return Reply(prefix + ctx.text)
