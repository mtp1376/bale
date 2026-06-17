#!/usr/bin/env python3
"""A tiny echo bot built on the event system.

Replies "pong" to "/ping" and echoes anything else. Run it, then message the
account from another chat.

    export BALE_TOKEN="eyJ..."
    python examples/echo_bot.py
"""
import asyncio
import os

from bale import BaleClient, events

client = BaleClient(os.environ["BALE_TOKEN"])


@client.on(events.NewMessage(pattern="/ping"))
async def ping(event):
    await event.reply("pong")


@client.on(events.NewMessage)
async def echo(event):
    if event.text and not event.text.startswith("/"):
        await event.respond(f"you said: {event.text}")


async def main() -> None:
    async with client:
        me = await client.get_me()
        print(f"Echo bot running as {me.title}. Press Ctrl+C to stop.")
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
