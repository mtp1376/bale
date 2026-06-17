"""bale — an unofficial async Python client for Bale messenger's web RPC API.

Reverse-engineered from the web.bale.ai client. The high-level API is modeled on
Telethon: an async context-managed client, ergonomic ``send_message`` /
``get_messages`` / ``get_dialogs`` / ``get_entity`` helpers, and an
``@client.on(events.NewMessage)`` event system — plus typed access to all ~600
raw RPCs via per-service namespaces (``client.messaging.SendMessage(...)``).

    import asyncio
    from bale import BaleClient, events

    client = BaleClient(token)

    @client.on(events.NewMessage(pattern="/ping"))
    async def _(event):
        await event.reply("pong")

    async def main():
        async with client:
            me = await client.get_me()
            print("logged in as", me.title)
            await client.run_until_disconnected()

    asyncio.run(main())
"""
from ._version import __version__
from .client import BaleClient
from .normalize import Content, HistoryEntry, MediaInfo, parse_history, parse_history_list
from .peer import Peer, PeerCache, PeerInfo, Resolver
from .transport import Transport
from .types import Dialog, Message
from . import bale_pb2 as pb
from . import auth
from . import events
from . import services
from .bale_methods import METHODS
from .services import ALL_RPCS, SERVICE_CLASSES

__all__ = [
    "__version__",
    "BaleClient", "Transport", "pb", "auth", "events", "METHODS",
    "Peer", "PeerInfo", "PeerCache", "Resolver",
    "MediaInfo", "Content", "HistoryEntry", "Message", "Dialog",
    "parse_history", "parse_history_list",
    "services", "ALL_RPCS", "SERVICE_CLASSES",
]
