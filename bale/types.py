"""Friendly high-level result types returned by the convenience API.

``Message`` is an alias for :class:`bale.normalize.HistoryEntry` (the normalized
message dataclass). :class:`Dialog` is a lightweight view over a conversation in
the dialog list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .normalize import HistoryEntry
from .peer import Peer

# A normalized message. Alias kept so high-level code reads like Telethon.
Message = HistoryEntry


@dataclass
class Dialog:
    """One conversation in the dialog list (open chat / channel / group)."""

    peer: Peer
    title: Optional[str]
    unread_count: int
    date: int
    is_muted: bool
    last_message: Optional[HistoryEntry]
    raw: Any = None  # the underlying pb.Dialog

    @property
    def id(self) -> int:
        return self.peer.id

    @property
    def name(self) -> Optional[str]:
        return self.title

    @property
    def is_user(self) -> bool:
        return self.peer.is_user

    @property
    def is_group(self) -> bool:
        """True for any non-user peer. Wire type 2 covers groups and channels."""
        return self.peer.is_channel

    # alias: wire type 2 is shared by groups and broadcast channels
    is_channel = is_group

    def __repr__(self) -> str:
        return (f"Dialog({self.peer.kind} id={self.id} title={self.title!r} "
                f"unread={self.unread_count})")
