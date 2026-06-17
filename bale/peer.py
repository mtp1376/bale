"""Peer abstraction for the Bale messenger client.

A high-level wrapper over the raw ``pb.Peer`` / ``pb.GroupPeer`` / ``pb.Bot``
messages, with a lookup cache and a resolver that hides the
SearchPeer/GetFullGroup/GetFullUser dance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from . import bale_pb2 as pb

if TYPE_CHECKING:
    from .client import BaleClient


# Wire-format type codes used by pb.Peer / pb.Bot.
_TYPE_USER = 1
_TYPE_CHANNEL = 2  # also covers groups; the wire field is shared.

_KIND_CHANNEL_ALIASES = ("channel", "group")


def _decode_bytes_or_str(x: Any) -> Optional[str]:
    """Decode a protobuf bytes/str field to str, or return None for empties."""
    if x is None:
        return None
    if isinstance(x, (bytes, bytearray)):
        if not x:
            return None
        return bytes(x).decode("utf-8", "replace")
    if isinstance(x, str):
        return x or None
    return None


def _normalize_kind(kind: str) -> int:
    """Map a kind string ('user' / 'channel' / 'group') to a wire peerType."""
    k = (kind or "").lower()
    if k == "user":
        return _TYPE_USER
    if k in _KIND_CHANNEL_ALIASES:
        return _TYPE_CHANNEL
    raise ValueError(f"unknown peer kind: {kind!r}")


class Peer:
    """A logical reference to a Bale peer (user or channel/group).

    Equality and hashing are keyed by (type, id) only; ``access_hash`` is treated
    as opaque metadata (``1`` is a sentinel for public channels).
    """

    __slots__ = ("id", "type", "access_hash")

    def __init__(self, id: int, type: int, access_hash: int = 1) -> None:
        self.id = int(id)
        self.type = int(type)
        self.access_hash = int(access_hash)

    # --- factories -------------------------------------------------------

    @classmethod
    def user(cls, id: int, access_hash: int = 0) -> "Peer":
        return cls(id=id, type=_TYPE_USER, access_hash=access_hash)

    @classmethod
    def channel(cls, id: int, access_hash: int = 1) -> "Peer":
        return cls(id=id, type=_TYPE_CHANNEL, access_hash=access_hash)

    # --- kind helpers ----------------------------------------------------

    @property
    def kind(self) -> str:
        if self.type == _TYPE_USER:
            return "user"
        if self.type == _TYPE_CHANNEL:
            return "channel"
        return f"unknown({self.type})"

    @property
    def is_user(self) -> bool:
        return self.type == _TYPE_USER

    @property
    def is_channel(self) -> bool:
        return self.type == _TYPE_CHANNEL

    # --- dunder ----------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Peer):
            return NotImplemented
        return self.type == other.type and self.id == other.id

    def __hash__(self) -> int:
        return hash((self.type, self.id))

    def __repr__(self) -> str:
        return f"Peer({self.kind} id={self.id} ah={self.access_hash})"

    # --- proto conversion ------------------------------------------------

    def to_proto(self) -> pb.Peer:
        p = pb.Peer()
        p.type = self.type
        p.id = self.id
        p.accessHash = self.access_hash
        return p

    def to_out_proto(self) -> pb.OutPeer:
        op = pb.OutPeer()
        op.type = self.type
        op.id = self.id
        op.accessHash = self.access_hash
        return op

    def to_group_proto(self) -> pb.GroupPeer:
        if not self.is_channel:
            raise ValueError(
                f"to_group_proto() requires a channel/group peer; got {self!r}"
            )
        gp = pb.GroupPeer()
        gp.groupId = self.id
        gp.accessHash = self.access_hash
        return gp

    @classmethod
    def from_proto(cls, pb_obj: Any) -> "Peer":
        # pb.Peer has 'type', 'id', 'accessHash'; pb.Bot has just 'type', 'id'.
        ptype = int(getattr(pb_obj, "type", 0))
        pid = int(getattr(pb_obj, "id", 0))
        ah = getattr(pb_obj, "accessHash", None)
        if ah is None:
            # Match the factory conventions: users default to the 0 placeholder
            # (server resolves by uid), channels to the public sentinel 1.
            access_hash = 0 if ptype == _TYPE_USER else 1
        else:
            access_hash = int(ah)
        return cls(id=pid, type=ptype, access_hash=access_hash)


@dataclass
class PeerInfo:
    """Lightweight display metadata associated with a :class:`Peer`."""

    peer: Peer
    title: Optional[str] = None
    username: Optional[str] = None
    description: Optional[str] = None
    members_count: Optional[int] = None
    is_public: Optional[bool] = None
    is_joined: Optional[bool] = None
    owner_uid: Optional[int] = None


def _merge_info(old: PeerInfo, new: PeerInfo) -> PeerInfo:
    """Return a PeerInfo combining old and new (new wins on non-None)."""
    # Preserve any access_hash that isn't the sentinel.
    peer = new.peer
    if peer.access_hash in (0, 1) and old.peer.access_hash not in (0, 1):
        peer = Peer(id=peer.id, type=peer.type, access_hash=old.peer.access_hash)
    return PeerInfo(
        peer=peer,
        title=new.title if new.title is not None else old.title,
        username=new.username if new.username is not None else old.username,
        description=new.description if new.description is not None else old.description,
        members_count=new.members_count if new.members_count is not None else old.members_count,
        is_public=new.is_public if new.is_public is not None else old.is_public,
        is_joined=new.is_joined if new.is_joined is not None else old.is_joined,
        owner_uid=new.owner_uid if new.owner_uid is not None else old.owner_uid,
    )


class PeerCache:
    """In-memory cache of :class:`PeerInfo` keyed by (type, id) and username."""

    def __init__(self) -> None:
        self._by_key: Dict[Tuple[int, int], PeerInfo] = {}
        self._by_username: Dict[str, Tuple[int, int]] = {}

    def put(self, info: PeerInfo) -> None:
        key = (info.peer.type, info.peer.id)
        existing = self._by_key.get(key)
        merged = _merge_info(existing, info) if existing is not None else info
        self._by_key[key] = merged
        # Drop a stale username alias if the peer was renamed.
        if existing is not None and existing.username:
            old = existing.username.lower()
            if not merged.username or merged.username.lower() != old:
                if self._by_username.get(old) == key:
                    self._by_username.pop(old, None)
        if merged.username:
            self._by_username[merged.username.lower()] = key

    def by_id(self, type: int, id: int) -> Optional[PeerInfo]:
        return self._by_key.get((int(type), int(id)))

    def by_username(self, username: str) -> Optional[PeerInfo]:
        if not username:
            return None
        key_name = username.lstrip("@").lower()
        key = self._by_username.get(key_name)
        if key is None:
            return None
        return self._by_key.get(key)

    def peers(self) -> List[PeerInfo]:
        return list(self._by_key.values())


def _info_from_search_result(result: Any) -> PeerInfo:
    """Build a PeerInfo from a SearchPeer T_I_ result item."""
    peer = Peer.from_proto(result.peer)
    title = _decode_bytes_or_str(result.title)
    description = (
        result.description.value
        if result.HasField("description")
        else None
    )
    members_count = (
        result.membersCount.value if result.HasField("membersCount") else None
    )
    is_public = result.isPublic.value if result.HasField("isPublic") else None
    is_joined = result.isJoined.value if result.HasField("isJoined") else None
    owner_uid = result.creator.value if result.HasField("creator") else None
    return PeerInfo(
        peer=peer,
        title=title,
        username=None,
        description=description,
        members_count=members_count,
        is_public=is_public,
        is_joined=is_joined,
        owner_uid=owner_uid,
    )


def _info_from_full_group(peer: Peer, full: Any) -> PeerInfo:
    """Build a PeerInfo from a FullGroup proto (response payload)."""
    fg = full.fullGroup
    access_hash = peer.access_hash
    if getattr(fg, "accessHash", 0):
        access_hash = int(fg.accessHash)
    resolved_peer = Peer.channel(id=int(fg.id) or peer.id, access_hash=access_hash)
    title = _decode_bytes_or_str(getattr(fg, "title", None))
    username = None
    if fg.HasField("nick"):
        nick = fg.nick.value
        if nick:
            username = nick.lstrip("@").lower() or None
    description = None
    if fg.HasField("about"):
        about = fg.about.value
        description = about or None
    members_count = (
        fg.membersCount.value if fg.HasField("membersCount") else None
    )
    is_member = fg.isMember.value if fg.HasField("isMember") else None
    owner_uid = int(fg.ownerUid) if getattr(fg, "ownerUid", 0) else None
    return PeerInfo(
        peer=resolved_peer,
        title=title,
        username=username,
        description=description,
        members_count=members_count,
        is_public=None,
        is_joined=is_member,
        owner_uid=owner_uid,
    )


def _info_from_full_user(peer: Peer, full: Any) -> PeerInfo:
    """Build a PeerInfo from a GetFullUserResponse.

    The wire shape here (T_n4) carries the user 'name' and 'nick' fields rather
    than the FullGroup-style title/about pair. Map what we can; leave the rest
    as None.
    """
    fu = full.fullUser
    access_hash = peer.access_hash
    if getattr(fu, "accessHash", 0):
        access_hash = int(fu.accessHash)
    resolved_peer = Peer.user(id=int(fu.id) or peer.id, access_hash=access_hash)
    title = _decode_bytes_or_str(getattr(fu, "name", None))
    username = None
    if fu.HasField("nick"):
        nick = fu.nick.value
        if nick:
            username = nick.lstrip("@").lower() or None
    description = None
    if fu.HasField("about"):
        about = fu.about.value
        description = about or None
    return PeerInfo(
        peer=resolved_peer,
        title=title,
        username=username,
        description=description,
        members_count=None,
        is_public=None,
        is_joined=None,
        owner_uid=None,
    )


Ref = Union[Peer, PeerInfo, str, int]


class Resolver:
    """High-level peer resolution on top of :class:`BaleClient`.

    Wraps the raw SearchPeer / GetFullGroup / GetFullUser RPCs and keeps results
    in a :class:`PeerCache` so subsequent lookups for the same peer are free.
    """

    def __init__(self, client: "BaleClient", cache: PeerCache) -> None:
        self.client = client
        self.cache = cache

    async def resolve(self, ref: Ref) -> PeerInfo:
        if isinstance(ref, PeerInfo):
            self.cache.put(ref)
            return ref
        if isinstance(ref, Peer):
            cached = self.cache.by_id(ref.type, ref.id)
            if cached is not None:
                return cached
            info = PeerInfo(peer=ref)
            self.cache.put(info)
            return info
        if isinstance(ref, str):
            username = ref.lstrip("@").strip()
            if not username:
                raise ValueError("empty username")
            cached = self.cache.by_username(username)
            if cached is not None:
                return cached
            target = username.lower()
            # SearchPeer results carry no username field (the wire type has
            # none), so we can't exact-match on the search results directly.
            # Gather candidates (channels first — the common public case), then
            # verify each candidate's real nick via GetFull and accept only an
            # exact match. This avoids silently returning/caching the wrong peer
            # under a requested username.
            candidates = list(await self.search_peer(username, kind="channel", limit=5))
            candidates += list(await self.search_peer(username, kind="user", limit=5))
            for cand in candidates[:4]:
                try:
                    full = await self.get_full(cand.peer)
                except Exception:
                    continue
                if full.username and full.username.lower() == target:
                    return full  # get_full() already cached it (incl. username)
            # No verified exact match. Return the top-ranked hit as a best-effort
            # result, but do NOT stamp the requested username onto it — a fuzzy
            # guess must not become a sticky (wrong) cache alias.
            if not candidates:
                raise LookupError(f"no peer found for {ref!r}")
            picked = candidates[0]
            self.cache.put(picked)
            return picked
        if isinstance(ref, int):
            peer = Peer.channel(ref)
            cached = self.cache.by_id(peer.type, peer.id)
            if cached is not None:
                return cached
            info = PeerInfo(peer=peer)
            self.cache.put(info)
            return info
        raise TypeError(f"unsupported peer reference type: {type(ref).__name__}")

    async def search_peer(
        self, query: str, kind: str = "channel", limit: int = 5
    ) -> List[PeerInfo]:
        peer_type = _normalize_kind(kind)
        req = pb.SearchPeerRequest()
        req.query.add().searchPeerTypeCondition.peerType = peer_type
        req.query.add().searchPieceText.query = query.encode("utf-8")
        resp = await self.client.call("bale.search.v1.Search", "SearchPeer", req)
        out: List[PeerInfo] = []
        if resp is None:
            return out
        for r in resp.searchResults:
            info = _info_from_search_result(r)
            # SearchPeer doesn't return accessHash; preserve sentinel defaults.
            self.cache.put(info)
            out.append(info)
        return out[:limit]

    async def get_full(self, peer: Peer) -> PeerInfo:
        if peer.is_channel:
            req = pb.GetFullGroupRequest()
            req.peer.groupId = peer.id
            req.peer.accessHash = peer.access_hash
            resp = await self.client.call(
                "bale.groups.v1.Groups", "GetFullGroup", req
            )
            if resp is None:
                raise LookupError(f"GetFullGroup returned no response for {peer!r}")
            info = _info_from_full_group(peer, resp)
            self.cache.put(info)
            return info
        if peer.is_user:
            req = pb.GetFullUserRequest()
            req.peer.uid = peer.id
            req.peer.accessHash = peer.access_hash
            resp = await self.client.call(
                "bale.users.v1.Users", "GetFullUser", req
            )
            if resp is None:
                raise LookupError(f"GetFullUser returned no response for {peer!r}")
            info = _info_from_full_user(peer, resp)
            self.cache.put(info)
            return info
        raise ValueError(f"cannot get_full for peer kind {peer.kind!r}")
