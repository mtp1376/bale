"""Telethon-style event system for incoming Bale updates.

Bale's server pushes ``Update`` messages over the same WebSocket. The client
decodes them and dispatches to handlers you register with
:meth:`BaleClient.on` / :meth:`BaleClient.add_event_handler`, then keep the
process alive with :meth:`BaleClient.run_until_disconnected`::

    from bale import BaleClient, events

    client = BaleClient(token)

    @client.on(events.NewMessage)
    async def handler(event):
        if event.text and event.text.lower() == "ping":
            await event.reply("pong")

    async def main():
        async with client:
            await client.run_until_disconnected()

Two builders are provided:

* :class:`NewMessage` — fires for incoming messages (the ``Update.message``
  variant). The event exposes ``text``, ``sender_id``, ``peer``, ``id`` and
  convenience coroutines ``reply`` / ``respond`` / ``delete`` / ``mark_read``.
* :class:`Raw` — fires for *every* update; the event exposes the decoded
  ``pb.Update`` (``event.update``) and its populated ``variant`` name, so you
  can react to any of Bale's ~140 update types.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from . import bale_pb2 as pb
from . import normalize as _norm
from .peer import Peer


def populated_variant(update: "pb.Update") -> Optional[str]:
    """Return the name of the single populated ``Update`` sub-field, or ``None``."""
    fields = update.ListFields()
    return fields[0][0].name if fields else None


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

class EventBuilder:
    """Base class: decides whether an ``Update`` matches and wraps it.

    A builder may be registered as the *class* (``events.NewMessage``) or as a
    configured *instance* (``events.NewMessage(from_users=[123])``).
    """

    def __init__(self, func: Optional[Callable[[Any], bool]] = None) -> None:
        self.filter_func = func

    def match(self, update: "pb.Update") -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _make(self, client: Any, update: "pb.Update", raw: bytes) -> Any:  # pragma: no cover
        raise NotImplementedError

    def build(self, client: Any, update: "pb.Update", raw: bytes) -> Optional[Any]:
        if not self.match(update):
            return None
        event = self._make(client, update, raw)
        if event is None:
            return None
        if self.filter_func is not None:
            verdict = self.filter_func(event)
            if asyncio.iscoroutine(verdict):
                verdict.close()
                raise TypeError(
                    "event filter func must be synchronous (it cannot be a coroutine)"
                )
            if not verdict:
                return None
        return event


class Raw(EventBuilder):
    """Matches every update. ``event.update`` is the decoded ``pb.Update``."""

    def match(self, update: "pb.Update") -> bool:
        return True

    def _make(self, client: Any, update: "pb.Update", raw: bytes) -> "RawEvent":
        return RawEvent(client, update, raw)


class NewMessage(EventBuilder):
    """Fires for incoming messages (the ``Update.message`` / ThreadMessage variant).

    Parameters mirror Telethon where practical:

    * ``func`` — a predicate ``(event) -> bool`` for custom filtering.
    * ``from_users`` — restrict to these sender uids.
    * ``pattern`` — if set, the event matches only when ``event.text`` starts
      with this string (handy for simple commands).
    """

    def __init__(
        self,
        func: Optional[Callable[[Any], bool]] = None,
        *,
        from_users: Optional[Any] = None,
        pattern: Optional[str] = None,
    ) -> None:
        super().__init__(func)
        self.from_users = self._coerce_uids(from_users)
        self.pattern = pattern

    @staticmethod
    def _coerce_uids(from_users: Optional[Any]) -> Optional[set]:
        """Normalize ``from_users`` to a set of int uids.

        ``None`` means "no sender filter"; an empty iterable means "match
        nobody". Accepts ints, digit strings, and Peer/PeerInfo objects."""
        if from_users is None:
            return None
        out = set()
        for x in from_users:
            if isinstance(x, bool):
                raise TypeError("from_users entries must be user ids, not bool")
            if isinstance(x, int):
                out.add(x)
            elif isinstance(x, str) and x.lstrip("-").isdigit():
                out.add(int(x))
            elif hasattr(x, "peer") and hasattr(getattr(x, "peer"), "id"):
                out.add(int(x.peer.id))
            elif hasattr(x, "id"):
                out.add(int(x.id))
            else:
                raise TypeError(f"cannot interpret {x!r} as a user id")
        return out

    def match(self, update: "pb.Update") -> bool:
        return populated_variant(update) == "message"

    def _make(self, client: Any, update: "pb.Update", raw: bytes) -> Optional["NewMessageEvent"]:
        ev = NewMessageEvent(client, update.message, update, raw)
        if self.from_users is not None and ev.sender_id not in self.from_users:
            return None
        if self.pattern is not None and not (ev.text or "").startswith(self.pattern):
            return None
        return ev


# --------------------------------------------------------------------------- #
# Event objects
# --------------------------------------------------------------------------- #

class RawEvent:
    """Wraps any decoded :class:`pb.Update`."""

    def __init__(self, client: Any, update: "pb.Update", raw: bytes) -> None:
        self.client = client
        self.update = update
        self.raw = raw

    @property
    def variant(self) -> Optional[str]:
        return populated_variant(self.update)

    def __repr__(self) -> str:
        return f"RawEvent(variant={self.variant!r})"


class NewMessageEvent:
    """A friendly incoming-message event built from a ``ThreadMessage``."""

    def __init__(self, client: Any, thread_message: Any, update: "pb.Update", raw: bytes) -> None:
        self.client = client
        self.message = thread_message  # pb.ThreadMessage
        self.update = update
        self.raw = raw

    # -- identity / addressing -------------------------------------------- #
    @property
    def peer(self) -> Peer:
        return Peer.from_proto(self.message.peer)

    @property
    def chat_id(self) -> int:
        return int(self.message.peer.id)

    @property
    def sender_id(self) -> int:
        return int(self.message.senderUid)

    @property
    def id(self) -> int:
        return int(self.message.rid)

    rid = id

    @property
    def date(self) -> int:
        return int(self.message.date)

    @property
    def is_private(self) -> bool:
        return int(self.message.peer.type) == 1

    @property
    def is_group(self) -> bool:
        """True for any non-private chat. Wire type 2 covers both groups and
        broadcast channels; the update payload can't distinguish them."""
        return int(self.message.peer.type) == 2

    # -- content ----------------------------------------------------------- #
    @property
    def content(self) -> "_norm.Content":
        return _norm.parse_message(self.message.message)

    @property
    def text(self) -> Optional[str]:
        c = self.content
        return c.text if c and c.kind == "text" else None

    @property
    def is_reply(self) -> bool:
        """True for a reply (quotes a message in the same chat). A forwarded
        message also carries a quote but points at a different peer — that is
        reported as a forward, not a reply."""
        try:
            if not self.message.HasField("quotedMessage"):
                return False
            qm = self.message.quotedMessage
            if qm.HasField("quotedPeer"):
                return int(qm.quotedPeer.id) == self.chat_id
        except (ValueError, AttributeError):
            return False
        return True

    @property
    def is_forward(self) -> bool:
        try:
            if not self.message.HasField("quotedMessage"):
                return False
            qm = self.message.quotedMessage
            if qm.HasField("quotedPeer"):
                return int(qm.quotedPeer.id) != self.chat_id
        except (ValueError, AttributeError):
            return False
        return False

    # -- actions ----------------------------------------------------------- #
    async def reply(self, text: str):
        """Reply to this message (quotes it)."""
        return await self.client.send_message(self.peer, text, reply_to=self.id)

    async def respond(self, text: str):
        """Send a message to the same chat without quoting."""
        return await self.client.send_message(self.peer, text)

    async def delete(self, revoke: bool = True):
        return await self.client.delete_messages(self.peer, [self.id], revoke=revoke)

    async def mark_read(self):
        return await self.client.mark_read(self.peer, self.date)

    def __repr__(self) -> str:
        kind = "private" if self.is_private else "group"
        preview = (self.text or self.content.kind)
        if len(preview) > 30:
            preview = preview[:30] + "…"
        return (f"NewMessageEvent({kind} chat={self.chat_id} from={self.sender_id} "
                f"id={self.id} {preview!r})")


def coerce_builder(event: Any) -> EventBuilder:
    """Normalize whatever the user passed to :meth:`BaleClient.on` into a builder.

    Accepts a builder instance, a builder class, or ``None`` (defaults to Raw).
    """
    if event is None:
        return Raw()
    if isinstance(event, EventBuilder):
        return event
    if isinstance(event, type) and issubclass(event, EventBuilder):
        return event()
    raise TypeError(f"expected an events builder/class, got {event!r}")
