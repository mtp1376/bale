"""
High-level Bale client.

Sends an RPC by:
  1. Serializing the request protobuf to bytes.
  2. Wrapping in an Envelope { RpcCall { service, method, request, headers }, seq }.
  3. Sending over the WS transport.
  4. Reading subsequent frames; correlating by the seq embedded in the response.

Wire layout of server frames (confirmed against 1174 captured pairs):
    server_frame  = { f1 = inner_response }   // top-level
    inner_response = { f3 = varint seq,       // matches client's RpcCall.f5
                       f2 = bytes  response } // serialized response message
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import random
import time
from typing import (
    Any, AsyncIterator, Callable, List, Optional, Tuple, Type, TypeVar, Union,
)

from google.protobuf.message import Message as PbMessage

from . import bale_pb2 as pb
from . import envelope as env
from . import events as events_mod
from .bale_methods import METHODS
from .normalize import HistoryEntry, parse_history, parse_history_list
from .peer import Peer, PeerCache, PeerInfo, Resolver
from .transport import Transport
from .types import Dialog, Message

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=PbMessage)

PeerRef = Union[Peer, PeerInfo, str, int]


def _uid_from_token(token: str) -> Optional[int]:
    """Best-effort decode of this account's own user id from the JWT payload.

    Returns ``None`` if the token can't be parsed (the client still works; only
    :meth:`BaleClient.get_me` needs it)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return None

    def find(obj: Any) -> Optional[int]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("user_id", "userId", "uid") and isinstance(value, int):
                    return value
                found = find(value)
                if found is not None:
                    return found
        return None

    return find(claims)


class BaleClient:
    def __init__(self, access_token: str):
        self.transport = Transport(access_token)
        self.cache = PeerCache()
        self.resolver = Resolver(self, self.cache)
        self._me_id: Optional[int] = _uid_from_token(access_token)
        self._event_handlers: List[Tuple[Callable, "events_mod.EventBuilder"]] = []
        self._update_handler_installed = False
        self._bg_tasks: set = set()
        # Attach a namespace for every Bale web service: client.messaging.SendMessage(...),
        # client.users.LoadUsers(...), client.groups.GetFullGroup(...), etc.
        from . import services
        services.install(self)

    async def connect(self) -> None:
        await self.transport.connect()

    async def __aenter__(self) -> "BaleClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def is_connected(self) -> bool:
        return self.transport.connected

    async def disconnect(self) -> None:
        """Alias for :meth:`close` (Telethon-style naming)."""
        await self.close()

    async def close(self) -> None:
        await self.transport.close()

    # ------------------------------------------------------------------ #
    # High-level methods.  Each accepts a `PeerRef` (Peer/PeerInfo/str/int)
    # and resolves it through the cache + Resolver.
    # ------------------------------------------------------------------ #

    async def resolve(self, ref: PeerRef) -> PeerInfo:
        return await self.resolver.resolve(ref)

    async def get_full(self, ref: PeerRef) -> PeerInfo:
        info = await self.resolve(ref)
        return await self.resolver.get_full(info.peer)

    async def get_history(
        self,
        ref: PeerRef,
        limit: int = 20,
        before_date: int = -1,
        load_mode: int = 2,
    ) -> List[HistoryEntry]:
        info = await self.resolve(ref)
        req = pb.LoadHistoryRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.date = before_date
        req.loadMode = load_mode
        req.limit = limit
        resp = await self.call(
            "bale.messaging.v2.Messaging", "LoadHistory", req, timeout=10.0,
        )
        self._stash_entities(resp)
        return parse_history_list(resp.history, info.peer.id)

    def _stash_entities(self, resp) -> None:
        """Cache any User/Group entities that came along on a response so we can
        resolve sender_id -> display name later without extra RPCs."""
        if resp is None:
            return
        for u in getattr(resp, "users", []) or []:
            self.cache.put(PeerInfo(
                peer=Peer.user(u.id, getattr(u, "accessHash", 0) or 0),
                title=getattr(u, "name", "") or None,
            ))
        for g in getattr(resp, "groups", []) or []:
            self.cache.put(PeerInfo(
                peer=Peer.channel(g.id, getattr(g, "accessHash", 1) or 1),
                title=getattr(g, "title", "") or None,
            ))

    def name_of(self, peer_type: int, peer_id: int) -> Optional[str]:
        """Lookup a cached display name for (type, id). Returns None if unknown."""
        info = self.cache.by_id(peer_type, peer_id)
        return info.title if info and info.title else None

    async def load_users(self, ids):
        """Bulk-resolve a set/list of user ids. Names land in the peer cache."""
        ids = [i for i in dict.fromkeys(int(x) for x in ids) if i]
        if not ids:
            return
        req = pb.LoadUsersRequest()
        for i in ids:
            up = req.peers.add()
            up.uid = i
            up.accessHash = 0   # placeholder; server resolves by uid alone
        resp = await self.call(
            "bale.users.v1.Users", "LoadUsers", req, timeout=8.0,
        )
        self._stash_entities(resp)

    async def load_groups(self, ids):
        """Bulk-resolve a set/list of group/channel ids."""
        ids = [i for i in dict.fromkeys(int(x) for x in ids) if i]
        if not ids:
            return
        req = pb.LoadGroupsRequest()
        for i in ids:
            gp = req.peers.add()
            gp.groupId = i
            gp.accessHash = 1   # public-channel sentinel
        resp = await self.call(
            "bale.groups.v1.Groups", "LoadGroups", req, timeout=8.0,
        )
        self._stash_entities(resp)

    async def resolve_names_for_history(self, history) -> None:
        """Given an iterable of HistoryEntry, fetch any sender / forward / reply
        names that aren't already in the cache."""
        unknown_users = set()
        unknown_groups = set()
        for h in history:
            if h.sender_id and self.name_of(1, h.sender_id) is None:
                unknown_users.add(h.sender_id)
            if h.forward_from_sender_id and self.name_of(1, h.forward_from_sender_id) is None:
                unknown_users.add(h.forward_from_sender_id)
            if h.reply_to_sender_id and self.name_of(1, h.reply_to_sender_id) is None:
                unknown_users.add(h.reply_to_sender_id)
            if (h.forward_from_peer_id
                    and (h.forward_from_peer_type or 2) == 2
                    and self.name_of(2, h.forward_from_peer_id) is None):
                unknown_groups.add(h.forward_from_peer_id)
        await self.load_users(unknown_users)
        await self.load_groups(unknown_groups)

    async def send_message(
        self,
        ref: PeerRef,
        text: str,
        *,
        reply_to: Optional[int] = None,
        rid: Optional[int] = None,
    ):
        """Send a text message to ``ref`` (user/channel/username/id).

        ``reply_to`` quotes an existing message by its rid. Returns the raw
        ``SendMessage`` response (``seq`` / ``date``)."""
        info = await self.resolve(ref)
        req = pb.SendMessageRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.exPeer.CopyFrom(info.peer.to_out_proto())   # exPeer is OutPeer, not Peer
        req.rid = rid if rid is not None else random.getrandbits(63)
        req.message.textMessage.text = text
        if reply_to is not None:
            req.quotedMessageReference.peer.CopyFrom(info.peer.to_proto())
            req.quotedMessageReference.rid = reply_to
        return await self.call(
            "bale.messaging.v2.Messaging", "SendMessage", req, timeout=8.0,
        )

    async def delete_message(self, ref: PeerRef, rid: int, just_mine: bool = True):
        info = await self.resolve(ref)
        req = pb.DeleteMessageRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.rids.append(rid)
        req.justMine.value = just_mine
        return await self.call(
            "bale.messaging.v2.Messaging", "DeleteMessage", req, timeout=8.0,
        )

    async def edit_message(self, ref: PeerRef, rid: int, new_text: str):
        info = await self.resolve(ref)
        req = pb.UpdateMessageRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.rid = rid
        req.updatedMessage.textMessage.text = new_text
        return await self.call(
            "bale.messaging.v2.Messaging", "UpdateMessage", req, timeout=8.0,
        )

    async def react(self, ref: PeerRef, message_rid: int, code: str = "❤"):
        info = await self.resolve(ref)
        req = pb.MessageSetReactionRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.rid = message_rid
        req.code = code
        req.date = int(time.time() * 1000)
        return await self.call(
            "bale.abacus.v1.Abacus", "MessageSetReaction", req, timeout=8.0,
        )

    # ================================================================== #
    # Telethon-style high-level convenience API
    # ================================================================== #

    # -- entities --------------------------------------------------------- #

    async def get_me(self) -> PeerInfo:
        """Return the logged-in account as a :class:`PeerInfo`."""
        if self._me_id is None:
            raise RuntimeError("could not determine own user id from the access token")
        return await self.get_full(Peer.user(self._me_id))

    @property
    def me_id(self) -> Optional[int]:
        """This account's own user id (decoded from the token), if known."""
        return self._me_id

    async def get_entity(self, ref: PeerRef) -> PeerInfo:
        """Resolve ``ref`` to a :class:`PeerInfo`.

        ``ref`` may be a ``"@username"``, a :class:`Peer`/:class:`PeerInfo`, or a
        bare ``int``. Note a bare int is interpreted as a **channel/group** id;
        for a user id pass ``Peer.user(uid)`` explicitly."""
        return await self.resolve(ref)

    async def get_input_entity(self, ref: PeerRef) -> Peer:
        """Resolve ``ref`` to a bare :class:`Peer` (type / id / access_hash)."""
        info = await self.resolve(ref)
        return info.peer

    async def get_peer_id(self, ref: PeerRef) -> int:
        info = await self.resolve(ref)
        return info.peer.id

    # -- messages --------------------------------------------------------- #

    async def iter_messages(
        self,
        ref: PeerRef,
        limit: Optional[int] = 100,
        *,
        offset_date: int = -1,
        load_mode: int = 2,
        page_size: int = 50,
    ) -> AsyncIterator[Message]:
        """Iterate a chat's messages, newest first, paginating ``LoadHistory``.

        ``limit=None`` yields until the history is exhausted."""
        info = await self.resolve(ref)
        peer_id = info.peer.id
        date = offset_date
        seen: set = set()
        yielded = 0
        while True:
            want = page_size if limit is None else min(page_size, limit - yielded)
            if want <= 0:
                return
            req = pb.LoadHistoryRequest()
            req.peer.CopyFrom(info.peer.to_proto())
            req.date = date
            req.loadMode = load_mode
            req.limit = want
            resp = await self.call(
                "bale.messaging.v2.Messaging", "LoadHistory", req, timeout=10.0,
            )
            self._stash_entities(resp)
            history = list(getattr(resp, "history", []) or [])
            if not history:
                return
            progressed = False
            for h in history:
                if h.rid in seen:
                    continue
                seen.add(h.rid)
                progressed = True
                yield parse_history(h, peer_id)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not progressed:
                return
            oldest = min(int(getattr(h, "date", 0) or 0) for h in history)
            if oldest <= 0 or oldest == date:
                return
            date = oldest

    async def get_messages(
        self, ref: PeerRef, limit: Optional[int] = 100, **kwargs: Any
    ) -> List[Message]:
        """Eager variant of :meth:`iter_messages` returning a list."""
        return [m async for m in self.iter_messages(ref, limit, **kwargs)]

    async def delete_messages(self, ref: PeerRef, rids, *, revoke: bool = True):
        """Delete messages by rid. ``revoke=True`` deletes for everyone."""
        info = await self.resolve(ref)
        req = pb.DeleteMessageRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        for r in rids:
            req.rids.append(int(r))
        req.justMine.value = not revoke
        return await self.call(
            "bale.messaging.v2.Messaging", "DeleteMessage", req, timeout=8.0,
        )

    async def forward_messages(self, to_ref: PeerRef, rids, from_ref: PeerRef):
        """Forward messages (by rid) from ``from_ref`` to ``to_ref``."""
        src = await self.resolve(from_ref)
        dst = await self.resolve(to_ref)
        req = pb.ForwardMessagesRequest()
        req.peer.CopyFrom(dst.peer.to_out_proto())
        for r in rids:
            fwd = req.forwardedMessages.add()
            fwd.peer.CopyFrom(src.peer.to_proto())
            fwd.rid = int(r)
            req.rid.append(random.getrandbits(63))
        return await self.call(
            "bale.messaging.v2.Messaging", "ForwardMessages", req, timeout=10.0,
        )

    async def mark_read(self, ref: PeerRef, max_date: Optional[int] = None):
        """Mark a chat read up to ``max_date`` (epoch ms; defaults to now)."""
        info = await self.resolve(ref)
        req = pb.MessageReadRequest()
        req.peer.CopyFrom(info.peer.to_proto())
        req.date = int(max_date if max_date is not None else time.time() * 1000)
        return await self.call(
            "bale.messaging.v2.Messaging", "MessageRead", req, timeout=8.0,
        )

    send_read_acknowledge = mark_read  # Telethon-compatible alias

    # -- dialogs ---------------------------------------------------------- #

    async def _load_named_peers(self, user_peers, group_peers) -> None:
        """Resolve display names for the given UserPeer/GroupPeer refs into the
        cache (LoadDialogs returns peer refs but not the named entities)."""
        if user_peers:
            req = pb.LoadUsersRequest()
            for up in user_peers:
                p = req.peers.add()
                p.uid = up.uid
                p.accessHash = up.accessHash
            self._stash_entities(
                await self.call("bale.users.v1.Users", "LoadUsers", req, timeout=8.0)
            )
        if group_peers:
            req = pb.LoadGroupsRequest()
            for gp in group_peers:
                p = req.peers.add()
                p.groupId = gp.groupId
                p.accessHash = gp.accessHash
            self._stash_entities(
                await self.call("bale.groups.v1.Groups", "LoadGroups", req, timeout=8.0)
            )

    async def iter_dialogs(
        self, limit: Optional[int] = 100, *, page_size: int = 50, resolve_names: bool = True
    ) -> AsyncIterator[Dialog]:
        """Iterate the dialog list (open chats), most-recent first.

        ``resolve_names`` (default ``True``) bulk-resolves the title of each
        dialog's peer so :attr:`Dialog.title` is populated."""
        min_date = int(time.time() * 1000)
        seen: set = set()
        yielded = 0
        while True:
            want = page_size if limit is None else min(page_size, limit - yielded)
            if want <= 0:
                return
            req = pb.LoadDialogsRequest()
            req.minDate = min_date
            req.limit = want
            resp = await self.call(
                "bale.messaging.v2.Messaging", "LoadDialogs", req, timeout=10.0,
            )
            self._stash_entities(resp)
            if resolve_names:
                await self._load_named_peers(
                    list(getattr(resp, "userPeers", []) or []),
                    list(getattr(resp, "groupPeers", []) or []),
                )
            dialogs = list(getattr(resp, "dialogs", []) or [])
            if not dialogs:
                return
            progressed = False
            for d in dialogs:
                key = (int(d.peer.type), int(d.peer.id))
                if key in seen:
                    continue
                seen.add(key)
                progressed = True
                yield self._make_dialog(d)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not progressed:
                return
            oldest = min(int(getattr(d, "sortDate", 0) or 0) for d in dialogs)
            if oldest <= 0 or oldest == min_date:
                return
            min_date = oldest

    async def get_dialogs(self, limit: Optional[int] = 100, **kwargs: Any) -> List[Dialog]:
        """Eager variant of :meth:`iter_dialogs` returning a list."""
        return [d async for d in self.iter_dialogs(limit, **kwargs)]

    def _make_dialog(self, d) -> Dialog:
        peer = Peer.from_proto(d.peer)
        title = self.name_of(peer.type, peer.id)
        last = None
        try:
            if d.HasField("message"):
                last = parse_history(d, peer.id)
        except Exception:  # pragma: no cover - defensive
            last = None
        return Dialog(
            peer=peer,
            title=title,
            unread_count=int(getattr(d, "unreadCount", 0) or 0),
            date=int(getattr(d, "date", 0) or 0),
            is_muted=bool(getattr(d, "isMute", False)),
            last_message=last,
            raw=d,
        )

    # ================================================================== #
    # Events (Telethon-style)
    # ================================================================== #

    def add_event_handler(self, callback: Callable, event: Any = None) -> None:
        """Register ``callback`` for ``event`` (an :mod:`bale.events` builder/class).

        ``event=None`` registers a raw handler that fires for every update."""
        builder = events_mod.coerce_builder(event)
        self._event_handlers.append((callback, builder))
        if not self._update_handler_installed:
            self.transport.add_update_handler(self._on_update_frame)
            self._update_handler_installed = True

    def on(self, event: Any = None) -> Callable:
        """Decorator form of :meth:`add_event_handler`."""
        def decorator(func: Callable) -> Callable:
            self.add_event_handler(func, event)
            return func
        return decorator

    def remove_event_handler(self, callback: Callable, event: Any = None) -> None:
        self._event_handlers = [
            (cb, b) for (cb, b) in self._event_handlers if cb is not callback
        ]
        if not self._event_handlers and self._update_handler_installed:
            self.transport.remove_update_handler(self._on_update_frame)
            self._update_handler_installed = False

    async def run_until_disconnected(self) -> None:
        """Block until the WebSocket disconnects, dispatching events meanwhile."""
        await self.transport.disconnected.wait()

    def _on_update_frame(self, frame: bytes) -> None:
        for update in _iter_updates(frame):
            for callback, builder in list(self._event_handlers):
                try:
                    event = builder.build(self, update, frame)
                except Exception:  # pragma: no cover - builder isolation
                    logger.exception("event builder failed")
                    continue
                if event is None:
                    continue
                try:
                    result = callback(event)
                except Exception:  # pragma: no cover - handler isolation
                    logger.exception("event handler raised")
                    continue
                if asyncio.iscoroutine(result):
                    self._spawn(result)

    def _spawn(self, coro) -> None:
        """Schedule a coroutine event handler, keeping a strong reference so it
        isn't garbage-collected mid-await, and logging exceptions."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("async event handler failed", exc_info=t.exception())

        task.add_done_callback(_done)

    async def call(
        self,
        service: str,
        method: str,
        request: Optional[PbMessage] = None,
        *,
        request_bytes: Optional[bytes] = None,
        response_type: Optional[Type[T]] = None,
        timeout: float = 10.0,
    ) -> Optional[T]:
        """Send a unary RPC. Response type is auto-resolved from METHODS registry
        unless explicitly given.

        ``request`` is a typed protobuf message; alternatively ``request_bytes``
        may be passed for cases where the proto schema is incomplete (some empty
        message types in our static extract require manually-encoded fields).
        """
        if response_type is None:
            info = METHODS.get((service, method))
            if info:
                response_type = info[1]
        if request_bytes is not None:
            req_bytes = request_bytes
        elif request is not None:
            req_bytes = request.SerializeToString()
        else:
            raise ValueError("either request or request_bytes is required")
        my_seq = self.transport.next_seq()
        fut = self.transport.expect(my_seq)
        rpc = env.encode_rpc_call(service, method, req_bytes, my_seq)
        frame = env.encode_envelope(rpc)
        logger.debug("→ %s.%s seq=%d (%dB)", service, method, my_seq, len(frame))
        try:
            await self.transport.send_envelope(frame)
        except Exception:
            self.transport.cancel(my_seq)  # don't leak the pending future
            raise

        try:
            resp_bytes = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.transport.cancel(my_seq)
            raise TimeoutError(
                f"no response for {service}.{method} within {timeout}s"
            ) from None
        if response_type is None:
            return resp_bytes  # type: ignore[return-value]
        msg = response_type()
        msg.ParseFromString(resp_bytes)
        return msg


def _iter_updates(frame: bytes):
    """Yield each :class:`pb.Update` decoded from a server-pushed frame.

    Wire layout (reverse-engineered from live capture against web.bale.ai):
    a push frame is ``outer{ f2 = box{ f1 = seqbox{ f1 = Update } } }`` — i.e.
    updates live under outer field 2 (RPC responses use field 1), nested two
    length-delimited levels deep. Both inner levels may repeat (batched updates).
    """
    try:
        outer = env.parse_top_fields(frame)
    except ValueError:
        return
    for f, _w, box_bytes in outer:
        if f != 2 or not isinstance(box_bytes, (bytes, bytearray)):
            continue
        try:
            box = env.parse_top_fields(box_bytes)
        except ValueError:
            continue
        for bf, _bw, seqbox_bytes in box:
            if bf != 1 or not isinstance(seqbox_bytes, (bytes, bytearray)):
                continue
            try:
                seqbox = env.parse_top_fields(seqbox_bytes)
            except ValueError:
                continue
            for sf, _sw, upd_bytes in seqbox:
                if sf != 1 or not isinstance(upd_bytes, (bytes, bytearray)):
                    continue
                update = pb.Update()
                try:
                    update.ParseFromString(bytes(upd_bytes))
                except Exception:
                    continue
                if update.ListFields():
                    yield update
