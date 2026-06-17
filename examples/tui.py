"""
Tiny Bale TUI — vim-flavored dialog browser.

    export BALE_TOKEN="eyJ..." && python examples/tui.py    # needs: pip install prompt_toolkit

Layout:
    +-----------------------------+----------------+
    | messages of selected dialog | dialog list    |
    |                             |                |
    | (input at bottom)           |                |
    +-----------------------------+----------------+

Keys:
  NORMAL    (dialog list focused)
    j/k ↑/↓    move in dialog list
    g / G      first / last dialog
    enter      open dialog (load history)
    r          reload dialog list
    m          enter MESSAGES mode (cursor on most-recent msg)
    i          INSERT (compose new message)
    q / ^C     quit

  MESSAGES  (cursor on a specific message)
    j/k ↑/↓    move cursor up/down through history
    g / G      first / last message
    e          EDIT (prefill input with selected text)
    d          delete selected; press d again within 3s to confirm
    esc        back to NORMAL

  INSERT / EDIT
    enter      send / save
    esc        back to NORMAL (discards draft)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import List, Optional, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from bale import BaleClient, Peer
from bale import bale_pb2 as pb


# ---------- formatting helpers ----------

def _fmt_time(epoch_ms: int) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(epoch_ms / 1000.0))
    except Exception:
        return "--:--"


def _fmt_content(c) -> str:
    if c.kind == "text" and c.text:
        return c.text.replace("\n", " ⏎ ")
    if c.kind == "media" and c.media is not None:
        m = c.media
        dims = ""
        if m.width and m.height:
            dims = f" {m.width}x{m.height}"
        dur = f" {m.duration}s" if m.duration else ""
        return f"[{m.kind}{dims}{dur}]"
    return f"<{c.kind}>"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- the app ----------

def _own_user_id_from_jwt(jwt: str) -> int:
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("payload", {}).get("user_id", 0))
    except Exception:
        return 0


class TUI:
    def __init__(self, client: BaleClient, own_user_id: int = 0) -> None:
        self.client = client
        self.own_user_id = own_user_id
        self.dialogs: List[Tuple[Peer, str, int, int]] = []  # (peer, title, date, unread)
        self.selected = 0
        self.scroll = 0
        # history_view is in chronological order (oldest first) so j/k feel natural
        self.history_view = []    # List[HistoryEntry]
        self.current_peer: Optional[Peer] = None
        self.current_title: str = ""
        self.status = "ready"
        self.mode = "NORMAL"      # NORMAL | MESSAGES | INSERT | EDIT
        self.msg_cursor = 0       # index into history_view when mode == MESSAGES
        self.editing_rid: Optional[int] = None   # set while mode == EDIT
        self._pending_delete_at = 0.0            # timestamp of first `d`

        # UI controls
        self.dialog_control = FormattedTextControl(
            self._dialog_text, focusable=True, show_cursor=False, key_bindings=None,
        )
        self.message_control = FormattedTextControl(
            self._message_text, focusable=False, show_cursor=False,
        )
        self.input_buffer = Buffer(multiline=False)
        self.input_control = BufferControl(
            buffer=self.input_buffer,
            input_processors=[BeforeInput("➤ ")],
        )
        self.status_control = FormattedTextControl(self._status_text)

    # ---- text producers ----

    def _dialog_text(self):
        out = []
        for i, (_peer, title, date, unread) in enumerate(self.dialogs):
            sel = i == self.selected
            tag = "class:dialog-sel" if sel else "class:dialog"
            unread_tag = "class:unread" if unread else ""
            t = _fmt_time(date)
            line = f"{title:<22.22} {t}"
            badge = f" ({unread})" if unread else ""
            out.append((tag, line))
            if badge:
                out.append((unread_tag, badge))
            out.append(("", "\n"))
        if not self.dialogs:
            out.append(("class:status", "  (no dialogs yet — press r to load)\n"))
        return out

    def _name(self, peer_type: int, peer_id: int) -> str:
        name = self.client.name_of(peer_type, peer_id)
        return name if name else f"#{peer_id}"

    def _message_text(self):
        out = []
        if not self.current_peer:
            out.append(("class:status", "  select a dialog (j/k + enter)\n"))
            return out
        out.append(("class:header", f"  {self.current_title}\n"))
        out.append(("class:header", "  " + "─" * 60 + "\n"))
        for i, h in enumerate(self.history_view):  # oldest top, newest bottom
            is_sel = (self.mode in ("MESSAGES", "EDIT")) and i == self.msg_cursor
            is_own = self.own_user_id and h.sender_id == self.own_user_id
            tag_marker = []
            if h.is_forward: tag_marker.append("FWD")
            if h.is_reply:   tag_marker.append("REPLY")
            marker = " [" + ",".join(tag_marker) + "]" if tag_marker else ""
            ts = _fmt_time(h.date)
            content = _fmt_content(h.content)
            if h.is_forward and h.forward_from_peer_id:
                src = self._name(h.forward_from_peer_type or 2, h.forward_from_peer_id)
                if h.forward_from_sender_id:
                    sender_name = self._name(1, h.forward_from_sender_id)
                    content = f"(from {src} • {sender_name}) {content}"
                else:
                    content = f"(from {src}) {content}"
            cursor = "▸ " if is_sel else "  "
            own_glyph = "✎ " if is_own else "  "
            line_style = "class:msg-sel" if is_sel else "class:meta"
            text_style = "class:msg-sel" if is_sel else "class:text"
            sender_label = "you" if is_own else self._name(1, h.sender_id)
            out.append((line_style,
                        f"{cursor}{own_glyph}{ts}  {sender_label}{marker}: "))
            out.append((text_style, content + "\n"))
            if h.is_reply and h.reply_to_content is not None:
                ridr = self._name(1, h.reply_to_sender_id) if h.reply_to_sender_id else "?"
                out.append(("class:reply",
                            f"           ↳ {ridr}: {_fmt_content(h.reply_to_content)}\n"))
        return out

    def _status_text(self):
        hints = {
            "NORMAL":   " j/k · enter · m messages · i write · r reload · q quit",
            "MESSAGES": " j/k · e edit · d delete (×2 to confirm) · esc back",
            "INSERT":   " enter send · esc cancel",
            "EDIT":     f" enter save (rid={self.editing_rid}) · esc cancel",
        }
        return [
            ("class:status-bar", f" [{self.mode}] "),
            ("", f" {self.status}    "),
            ("class:hint", hints.get(self.mode, "")),
        ]

    # ---- actions ----

    async def load_dialogs(self) -> None:
        self.status = "loading dialogs…"
        get_app().invalidate()
        req = pb.LoadGroupedDialogsRequest()
        resp = await self.client.call(
            "bale.messaging.v2.Messaging", "LoadGroupedDialogs", req, timeout=10.0,
        )
        # Build (type,id) -> (title, access_hash) map from associated entities
        info = {}
        for u in resp.users:
            info[(1, u.id)] = (u.name or f"user {u.id}", u.accessHash)
        for g in resp.groups:
            info[(2, g.id)] = (g.title or f"group {g.id}", g.accessHash)

        # Flatten folders
        flat = []
        for folder in resp.dialogs:
            for d in folder.dialogs:
                pe = d.peer  # pb.Bot {type,id}
                title, ah = info.get((pe.type, pe.id), (f"peer {pe.id}", 1))
                p = Peer(id=pe.id, type=pe.type, access_hash=ah or 1)
                # cache the access hash + title for later
                from bale.peer import PeerInfo
                self.client.cache.put(PeerInfo(peer=p, title=title))
                flat.append((p, title, d.date, d.counter))
        flat.sort(key=lambda x: -x[2])           # newest first
        self.dialogs = flat
        self.status = f"{len(flat)} dialog(s)"
        if self.selected >= len(flat):
            self.selected = 0
        get_app().invalidate()

    async def open_selected(self) -> None:
        if not self.dialogs:
            return
        peer, title, _date, _unread = self.dialogs[self.selected]
        self.current_peer = peer
        self.current_title = title
        self.status = f"loading history for {title!r}…"
        get_app().invalidate()
        try:
            await self._reload_history()
            self.status = f"{title}: {len(self.history_view)} msgs"
        except Exception as e:
            self.status = f"history error: {e}"
        get_app().invalidate()

    async def _reload_history(self) -> None:
        raw = await self.client.get_history(self.current_peer, limit=30)
        # Resolve any unknown sender / forward / reply names in one batch
        await self.client.resolve_names_for_history(raw)
        self.history_view = list(reversed(raw))   # oldest first
        # keep cursor in range
        if self.history_view:
            self.msg_cursor = max(0, min(self.msg_cursor, len(self.history_view) - 1))
        else:
            self.msg_cursor = 0

    async def send_current(self, text: str) -> None:
        if not self.current_peer or not text.strip():
            return
        self.status = f"sending ({len(text)} chars)…"
        get_app().invalidate()
        try:
            await self.client.send_message(self.current_peer, text)
            self.status = "sent"
            await self._reload_history()
            self.msg_cursor = len(self.history_view) - 1  # jump to newest
        except Exception as e:
            self.status = f"send error: {e}"
        get_app().invalidate()

    async def save_edit(self, rid: int, text: str) -> None:
        if not self.current_peer or not text.strip():
            return
        self.status = f"editing rid={rid}…"
        get_app().invalidate()
        try:
            await self.client.edit_message(self.current_peer, rid, text)
            self.status = "edited"
            await self._reload_history()
        except Exception as e:
            self.status = f"edit error: {e}"
        get_app().invalidate()

    async def delete_selected(self) -> None:
        if not self.current_peer or not self.history_view:
            return
        h = self.history_view[self.msg_cursor]
        self.status = f"deleting rid={h.rid}…"
        get_app().invalidate()
        try:
            await self.client.delete_message(self.current_peer, h.rid, just_mine=True)
            self.status = "deleted"
            await self._reload_history()
            # cursor may now point past end
            if self.msg_cursor >= len(self.history_view):
                self.msg_cursor = max(0, len(self.history_view) - 1)
        except Exception as e:
            self.status = f"delete error: {e}"
        get_app().invalidate()

    # ---- key bindings ----

    def _bindings(self) -> KeyBindings:
        from prompt_toolkit.filters import Condition
        N = Condition(lambda: self.mode == "NORMAL")
        M = Condition(lambda: self.mode == "MESSAGES")
        IE = Condition(lambda: self.mode in ("INSERT", "EDIT"))

        kb = KeyBindings()

        # global
        @kb.add("c-c")
        def _(event): event.app.exit()

        @kb.add("q", filter=N)
        def _(event): event.app.exit()

        # ----- NORMAL: dialog navigation -----
        @kb.add("j", filter=N)
        @kb.add("down", filter=N)
        def _(event):
            if self.dialogs and self.selected < len(self.dialogs) - 1:
                self.selected += 1

        @kb.add("k", filter=N)
        @kb.add("up", filter=N)
        def _(event):
            if self.selected > 0:
                self.selected -= 1

        @kb.add("g", filter=N)
        def _(event): self.selected = 0

        @kb.add("G", filter=N)
        def _(event):
            if self.dialogs: self.selected = len(self.dialogs) - 1

        @kb.add("r", filter=N)
        def _(event): event.app.create_background_task(self.load_dialogs())

        @kb.add("enter", filter=N)
        def _(event): event.app.create_background_task(self.open_selected())

        @kb.add("i", filter=N)
        def _(event):
            if not self.current_peer:
                self.status = "open a dialog first (enter)"
                return
            self.mode = "INSERT"
            self.input_buffer.reset()
            event.app.layout.focus(self.input_control)

        @kb.add("m", filter=N)
        def _(event):
            if not self.history_view:
                self.status = "no messages — open a dialog first"
                return
            self.mode = "MESSAGES"
            self.msg_cursor = len(self.history_view) - 1   # newest

        # ----- MESSAGES: message-cursor navigation + edit/delete -----
        @kb.add("j", filter=M)
        @kb.add("down", filter=M)
        def _(event):
            if self.msg_cursor < len(self.history_view) - 1:
                self.msg_cursor += 1
            self._pending_delete_at = 0.0

        @kb.add("k", filter=M)
        @kb.add("up", filter=M)
        def _(event):
            if self.msg_cursor > 0:
                self.msg_cursor -= 1
            self._pending_delete_at = 0.0

        @kb.add("g", filter=M)
        def _(event): self.msg_cursor = 0

        @kb.add("G", filter=M)
        def _(event):
            if self.history_view: self.msg_cursor = len(self.history_view) - 1

        @kb.add("escape", filter=M)
        def _(event):
            self.mode = "NORMAL"
            self._pending_delete_at = 0.0
            event.app.layout.focus(self.dialog_control)

        @kb.add("e", filter=M)
        def _(event):
            if not self.history_view: return
            h = self.history_view[self.msg_cursor]
            if h.sender_id != self.own_user_id:
                self.status = "can only edit own messages"
                return
            if h.content.kind != "text":
                self.status = "can only edit text messages"
                return
            self.editing_rid = h.rid
            self.input_buffer.text = h.content.text or ""
            self.mode = "EDIT"
            event.app.layout.focus(self.input_control)

        @kb.add("d", filter=M)
        def _(event):
            if not self.history_view: return
            h = self.history_view[self.msg_cursor]
            if h.sender_id != self.own_user_id:
                self.status = "can only delete own messages (justMine=true)"
                self._pending_delete_at = 0.0
                return
            now = time.time()
            if now - self._pending_delete_at <= 3.0:
                self._pending_delete_at = 0.0
                event.app.create_background_task(self.delete_selected())
            else:
                self._pending_delete_at = now
                self.status = f"press d again to confirm delete rid={h.rid}"

        # ----- INSERT / EDIT: text input -----
        @kb.add("escape", filter=IE)
        def _(event):
            self.mode = "NORMAL"
            self.editing_rid = None
            self.input_buffer.reset()
            event.app.layout.focus(self.dialog_control)

        @kb.add("enter", filter=IE)
        def _(event):
            text = self.input_buffer.text
            self.input_buffer.reset()
            if self.mode == "INSERT":
                event.app.create_background_task(self.send_current(text))
            else:  # EDIT
                rid = self.editing_rid
                if rid is not None:
                    event.app.create_background_task(self.save_edit(rid, text))
            self.editing_rid = None
            self.mode = "NORMAL"
            event.app.layout.focus(self.dialog_control)

        return kb

    # ---- layout ----

    def build(self) -> Application:
        msg_pane = Frame(
            Window(self.message_control, wrap_lines=True),
            title="messages",
        )
        dlg_pane = Frame(
            Window(self.dialog_control, wrap_lines=False),
            title="dialogs",
        )
        input_pane = Frame(
            Window(self.input_control, height=Dimension(min=1, max=3), wrap_lines=True),
            title="input",
        )
        status_bar = Window(self.status_control, height=1, style="class:status-bar")

        body = VSplit(
            [
                HSplit([msg_pane, input_pane]),
                Window(width=1, char="│", style="class:divider"),
                dlg_pane,
            ],
        )
        root = HSplit([body, status_bar])

        style = Style.from_dict({
            "dialog":        "fg:#bbbbbb",
            "dialog-sel":    "bg:#2d4a8a fg:#ffffff bold",
            "unread":        "fg:#ffaa00 bold",
            "header":        "fg:#88ddff bold",
            "meta":          "fg:#888888",
            "text":          "fg:#dddddd",
            "reply":         "fg:#888888 italic",
            "msg-sel":       "bg:#4a2d8a fg:#ffffff bold",
            "status":        "fg:#888888",
            "status-bar":    "bg:#222266 fg:#ffffff",
            "hint":          "fg:#88aaff",
            "divider":       "fg:#444444",
        })
        return Application(
            layout=Layout(root, focused_element=self.dialog_control),
            key_bindings=self._bindings(),
            full_screen=True,
            style=style,
        )


async def main():
    jwt = os.environ["BALE_TOKEN"]
    client = BaleClient(jwt)
    await client.connect()
    try:
        tui = TUI(client, own_user_id=_own_user_id_from_jwt(jwt))
        app = tui.build()
        # Preload dialogs once the app starts
        app.create_background_task(tui.load_dialogs())
        await app.run_async()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
