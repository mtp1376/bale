"""Normalized representation of Bale message history.

This module converts the protobuf-generated `History` / `Message` types in
`bale.bale_pb2` into plain Python dataclasses that are easier to
consume in higher layers. It is intentionally self-contained: it does not
depend on any Peer abstraction and uses plain ints for peer ids.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MediaInfo:
    kind: str
    mime: str
    name: str
    size: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    caption: Optional[str] = None
    file_id: int = 0
    access_hash: int = 0


@dataclass
class Content:
    kind: str
    text: Optional[str] = None
    media: Optional[MediaInfo] = None
    raw_variant_name: Optional[str] = None


@dataclass
class HistoryEntry:
    rid: int
    date: int
    sender_id: int
    content: Content
    is_forward: bool = False
    forward_from_peer_id: Optional[int] = None
    forward_from_peer_type: Optional[int] = None
    forward_from_sender_id: Optional[int] = None
    forward_orig_message_id: Optional[int] = None
    forward_orig_date: Optional[int] = None
    is_reply: bool = False
    reply_to_message_id: Optional[int] = None
    reply_to_sender_id: Optional[int] = None
    reply_to_content: Optional[Content] = None
    reactions: List[Tuple[str, int]] = field(default_factory=list)
    edited_at: Optional[int] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Field names for the Media.ext (T_LnR) oneof-style wrapper. We map each
# possible ext field to a logical media kind.
_EXT_KINDS = (
    ("documentExPhoto", "photo"),
    ("documentExVideo", "video"),
    ("documentExVoice", "voice"),
    ("documentExAudio", "audio"),
    ("documentExGif", "gif"),
)

# Field names for the Message oneof-style variants we explicitly support.
_MESSAGE_VARIANTS = (
    "textMessage",
    "longTextMessage",
    "documentMessage",
    "stickerMessage",
    "animatedStickerMessage",
    "serviceMessage",
    "emptyMessage",
)


def _has_field(msg, name: str) -> bool:
    """Safely call HasField; return False if the field name is unknown."""
    if msg is None:
        return False
    try:
        return msg.HasField(name)
    except (ValueError, AttributeError):
        return False


def _get_wrapped_int(msg, name: str) -> Optional[int]:
    if not _has_field(msg, name):
        return None
    try:
        return int(getattr(msg, name).value)
    except (AttributeError, TypeError, ValueError):
        return None


def _which_variant(msg, names) -> Optional[str]:
    """Return the (single) populated variant field name from `names`, or None."""
    if msg is None:
        return None
    found = None
    for n in names:
        if _has_field(msg, n):
            if found is not None:
                # More than one populated; ambiguous.
                return None
            found = n
    return found


def _any_populated_message_field(msg) -> Optional[str]:
    """Return the name of any populated sub-message field on `msg`.

    Used as a last-ditch attempt to label an unknown Message variant.
    """
    if msg is None:
        return None
    try:
        fields = msg.DESCRIPTOR.fields
    except AttributeError:
        return None
    found = None
    for f in fields:
        # Only consider sub-message fields (type 11 = TYPE_MESSAGE).
        if getattr(f, "type", None) != 11:
            continue
        if _has_field(msg, f.name):
            if found is not None:
                return None
            found = f.name
    return found


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------

def parse_media(d) -> MediaInfo:
    """Convert a pb.Media (a.k.a. documentMessage) to a MediaInfo.

    The Media.ext wrapper (proto type T_LnR) is inspected to determine the
    logical kind. If none of the known ext sub-fields is populated, the
    media is treated as a generic "document".
    """
    kind = "document"
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None

    ext = None
    if _has_field(d, "ext"):
        try:
            ext = d.ext
        except AttributeError:
            ext = None

    if ext is not None:
        for fname, k in _EXT_KINDS:
            if _has_field(ext, fname):
                kind = k
                sub = getattr(ext, fname, None)
                if sub is not None:
                    width = getattr(sub, "w", None) or None
                    height = getattr(sub, "h", None) or None
                    duration = getattr(sub, "duration", None) or None
                break

    # Caption: Media.caption is a TextMessage (has .text), not a StringValue.
    caption: Optional[str] = None
    if _has_field(d, "caption"):
        try:
            cap_msg = d.caption
            cap_text = getattr(cap_msg, "text", "") or ""
            caption = cap_text if cap_text else None
        except AttributeError:
            caption = None

    return MediaInfo(
        kind=kind,
        mime=getattr(d, "mimeType", "") or "",
        name=getattr(d, "name", "") or "",
        size=int(getattr(d, "fileSize", 0) or 0),
        width=width,
        height=height,
        duration=duration,
        caption=caption,
        file_id=int(getattr(d, "fileId", 0) or 0),
        access_hash=int(getattr(d, "accessHash", 0) or 0),
    )


def parse_message(m) -> Content:
    """Convert a pb.Message to a Content dataclass.

    Recognizes textMessage, documentMessage, stickerMessage, serviceMessage,
    and emptyMessage. Anything else yields kind="unknown"; if exactly one
    sub-message field is populated, its name is preserved in
    `raw_variant_name`.
    """
    if m is None:
        return Content(kind="empty")

    variant = _which_variant(m, _MESSAGE_VARIANTS)

    if variant in ("textMessage", "longTextMessage"):
        # longTextMessage carries the same `.text` field (the body may be
        # offloaded to a file for very long messages; the inline text is
        # recovered here).
        try:
            tm = getattr(m, variant)
            text = getattr(tm, "text", "") or ""
        except AttributeError:
            text = ""
        return Content(kind="text", text=text, raw_variant_name=variant)

    if variant == "documentMessage":
        try:
            media = parse_media(m.documentMessage)
        except AttributeError:
            media = None
        return Content(
            kind="media", media=media, raw_variant_name="documentMessage"
        )

    if variant in ("stickerMessage", "animatedStickerMessage"):
        return Content(kind="sticker", raw_variant_name=variant)

    if variant == "serviceMessage":
        text = getattr(getattr(m, "serviceMessage", None), "text", "") or None
        return Content(kind="service", text=text, raw_variant_name="serviceMessage")

    if variant == "emptyMessage":
        return Content(kind="empty", raw_variant_name="emptyMessage")

    # No (or ambiguous) variant among the supported set. If exactly one
    # sub-message field of any kind is set, surface its name.
    other = _any_populated_message_field(m)
    if other is None:
        # Truly nothing populated -> treat as empty for ergonomic reasons.
        return Content(kind="empty")
    return Content(kind="unknown", raw_variant_name=other)


def parse_history(h, current_peer_id: int) -> HistoryEntry:
    """Build a HistoryEntry from a pb.History.

    Applies the forward-vs-reply heuristic:
      * No quotedMessage           -> plain own message.
      * quotedMessage + quotedPeer.id == current_peer_id  -> reply.
      * quotedMessage + quotedPeer.id != current_peer_id  -> forward.
        (When the empty-message placeholder is set, the forwarded payload
        lives in quotedMessage.quotedMessageContent.)
    """
    rid = int(getattr(h, "rid", 0) or 0)
    date = int(getattr(h, "date", 0) or 0)
    sender_id = int(getattr(h, "senderUid", 0) or 0)

    # Default content: parse the top-level message.
    inner_msg = getattr(h, "message", None) if _has_field(h, "message") else None
    content = parse_message(inner_msg)

    is_forward = False
    is_reply = False
    forward_from_peer_id: Optional[int] = None
    forward_from_peer_type: Optional[int] = None
    forward_from_sender_id: Optional[int] = None
    forward_orig_message_id: Optional[int] = None
    forward_orig_date: Optional[int] = None
    reply_to_message_id: Optional[int] = None
    reply_to_sender_id: Optional[int] = None
    reply_to_content: Optional[Content] = None

    if _has_field(h, "quotedMessage"):
        try:
            qm = h.quotedMessage
        except AttributeError:
            qm = None

        if qm is not None:
            quoted_peer_id: Optional[int] = None
            quoted_peer_type: Optional[int] = None
            if _has_field(qm, "quotedPeer"):
                try:
                    qp = qm.quotedPeer
                    quoted_peer_id = int(getattr(qp, "id", 0) or 0)
                    quoted_peer_type = int(getattr(qp, "type", 0) or 0)
                except AttributeError:
                    quoted_peer_id = None
                    quoted_peer_type = None

            # Distinguish forward vs reply based on quotedPeer.id.
            if quoted_peer_id is not None and quoted_peer_id != current_peer_id:
                # Forward.
                is_forward = True
                forward_from_peer_id = quoted_peer_id
                forward_from_peer_type = quoted_peer_type
                forward_from_sender_id = int(
                    getattr(qm, "senderUserId", 0) or 0
                ) or None
                forward_orig_message_id = _get_wrapped_int(qm, "messageId")
                msg_date = int(getattr(qm, "messageDate", 0) or 0)
                forward_orig_date = msg_date or None

                # For forwards, the user-visible payload is the quoted content.
                if _has_field(qm, "quotedMessageContent"):
                    try:
                        content = parse_message(qm.quotedMessageContent)
                    except AttributeError:
                        pass
            else:
                # Reply (quotedPeer absent OR points at the current peer).
                is_reply = True
                reply_to_sender_id = int(
                    getattr(qm, "senderUserId", 0) or 0
                ) or None
                reply_to_message_id = _get_wrapped_int(qm, "messageId")
                if _has_field(qm, "quotedMessageContent"):
                    try:
                        reply_to_content = parse_message(qm.quotedMessageContent)
                    except AttributeError:
                        reply_to_content = None

    # Reactions.
    reactions: List[Tuple[str, int]] = []
    raw_reactions = getattr(h, "reactions", None)
    if raw_reactions is not None:
        for r in raw_reactions:
            code = getattr(r, "code", "") or ""
            cardinality = _get_wrapped_int(r, "cardinality")
            if cardinality is None:
                try:
                    cardinality = len(r.users)
                except (AttributeError, TypeError):
                    cardinality = 0
            reactions.append((code, int(cardinality)))

    edited_at = _get_wrapped_int(h, "editedAt")

    return HistoryEntry(
        rid=rid,
        date=date,
        sender_id=sender_id,
        content=content,
        is_forward=is_forward,
        forward_from_peer_id=forward_from_peer_id,
        forward_from_peer_type=forward_from_peer_type,
        forward_from_sender_id=forward_from_sender_id,
        forward_orig_message_id=forward_orig_message_id,
        forward_orig_date=forward_orig_date,
        is_reply=is_reply,
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id=reply_to_sender_id,
        reply_to_content=reply_to_content,
        reactions=reactions,
        edited_at=edited_at,
    )


def parse_history_list(hist_iterable, current_peer_id: int) -> List[HistoryEntry]:
    """Map `parse_history` over an iterable of pb.History messages."""
    return [parse_history(h, current_peer_id) for h in hist_iterable]
