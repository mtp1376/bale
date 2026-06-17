"""
Bale WebSocket transport.

Handles: connect (with cookie auth), the 6-byte client/server hello, an outbound
seq counter, and demultiplexing of incoming frames into:

  * **RPC responses** — routed to the waiting caller via a per-seq future
    (so concurrent ``call()``s never steal each other's frames), and
  * **server-pushed updates** — handed to registered update handlers, which
    is what powers the high-level event system (see :mod:`bale.events`).

A single background reader task owns the socket; callers register a future for
their seq with :meth:`expect` and await it.
"""
from __future__ import annotations

import asyncio
import logging
from itertools import count
from typing import Awaitable, Callable, Dict, List, Optional, Union

import websockets

from . import envelope as env

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://next-ws.bale.ai/ws/"
DEFAULT_ORIGIN = "https://web.bale.ai"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

#: An update handler receives the raw bytes of a server-pushed frame. It may be
#: a plain function or a coroutine function (awaited on the event loop).
UpdateHandler = Callable[[bytes], Union[None, Awaitable[None]]]


class Transport:
    def __init__(
        self,
        access_token: str,
        url: str = DEFAULT_WS_URL,
        origin: str = DEFAULT_ORIGIN,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.access_token = access_token
        self.url = url
        self.origin = origin
        self.user_agent = user_agent
        self.ws: Optional["websockets.ClientConnection"] = None
        self._seq = count(1)
        self._pending: Dict[int, "asyncio.Future[bytes]"] = {}
        self._update_handlers: List[UpdateHandler] = []
        self._reader_task: Optional[asyncio.Task] = None
        self._bg_tasks: set = set()
        #: Set once the reader stops (socket closed). Created lazily inside the
        #: running loop by :meth:`connect` — building it in ``__init__`` would
        #: bind it to the wrong event loop on Python 3.9 (the client is usually
        #: constructed before ``asyncio.run`` creates the loop).
        self.disconnected: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        # Create/refresh the disconnect Event under the running loop.
        self.disconnected = asyncio.Event()
        ws = await websockets.connect(
            self.url,
            additional_headers={"Cookie": f"access_token={self.access_token}"},
            user_agent_header=self.user_agent,
            origin=self.origin,
            max_size=8 * 1024 * 1024,
        )
        try:
            await ws.send(env.CLIENT_HELLO)
            ack = await asyncio.wait_for(ws.recv(), timeout=10)
        except BaseException:
            # Don't leak the socket if the hello handshake fails.
            await ws.close()
            self.disconnected.set()
            raise
        if ack != env.SERVER_HELLO:
            logger.warning("unexpected server hello: %s", ack.hex())
        self.ws = ws
        self._reader_task = asyncio.create_task(self._reader())

    @property
    def connected(self) -> bool:
        return (
            self.ws is not None
            and self.disconnected is not None
            and not self.disconnected.is_set()
        )

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws is not None:
            await self.ws.close()
        self._fail_pending(ConnectionError("transport closed"))
        if self.disconnected is not None:
            self.disconnected.set()

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #

    def next_seq(self) -> int:
        return next(self._seq)

    def expect(self, seq: int) -> "asyncio.Future[bytes]":
        """Register interest in the response for ``seq`` and return its future.

        Always called from within a running event loop (by ``BaleClient.call``)."""
        fut: "asyncio.Future[bytes]" = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        return fut

    def cancel(self, seq: int) -> None:
        """Drop a pending response future (e.g. on caller timeout)."""
        self._pending.pop(seq, None)

    async def send_envelope(self, envelope_bytes: bytes) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        assert self.ws is not None
        await self.ws.send(envelope_bytes)

    # ------------------------------------------------------------------ #
    # Updates
    # ------------------------------------------------------------------ #

    def add_update_handler(self, handler: UpdateHandler) -> None:
        self._update_handlers.append(handler)

    def remove_update_handler(self, handler: UpdateHandler) -> None:
        try:
            self._update_handlers.remove(handler)
        except ValueError:
            pass

    # ------------------------------------------------------------------ #
    # Reader / demux
    # ------------------------------------------------------------------ #

    async def _reader(self) -> None:
        try:
            assert self.ws is not None
            async for frame in self.ws:
                if isinstance(frame, str):
                    frame = frame.encode()
                self._handle_frame(frame)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("reader crashed: %s", e)
        finally:
            self._fail_pending(ConnectionError("connection closed"))
            if self.disconnected is not None:
                self.disconnected.set()

    def _handle_frame(self, frame: bytes) -> None:
        parsed = env.parse_response_frame(frame)
        if parsed is not None:
            seq, resp = parsed
            fut = self._pending.pop(seq, None)
            if fut is not None:
                if not fut.done():
                    fut.set_result(resp)
                return
            # Parsed like a response but nobody is waiting on this seq -> it is a
            # server-pushed frame; fall through to update dispatch.
        self._dispatch_update(frame)

    def _dispatch_update(self, frame: bytes) -> None:
        for handler in list(self._update_handlers):
            try:
                result = handler(frame)
            except Exception:  # pragma: no cover - handler isolation
                logger.exception("update handler raised")
                continue
            if asyncio.iscoroutine(result):
                self._spawn(result)

    def _spawn(self, coro) -> None:
        """Schedule a coroutine, keeping a strong reference so it isn't GC'd
        mid-await, and logging any exception instead of dropping it."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("update handler task failed", exc_info=t.exception())

        task.add_done_callback(_done)

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
