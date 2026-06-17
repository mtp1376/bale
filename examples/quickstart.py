#!/usr/bin/env python3
"""Connect, identify yourself, list dialogs, send a message.

    export BALE_TOKEN="eyJ..."
    python examples/quickstart.py
"""
import asyncio
import os

from bale import BaleClient


async def main() -> None:
    token = os.environ["BALE_TOKEN"]
    async with BaleClient(token) as client:
        me = await client.get_me()
        print("Logged in as:", me.title, f"(id={me.peer.id})")

        print("\nRecent conversations:")
        for dialog in await client.get_dialogs(limit=10):
            flag = f"{dialog.unread_count} unread" if dialog.unread_count else ""
            print(f"  - {dialog.title or dialog.id}  {flag}")

        # Send yourself a note in Saved Messages.
        await client.send_message(me.peer, "Hello from the bale Python client 👋")
        print("\nSent a message to Saved Messages.")


if __name__ == "__main__":
    asyncio.run(main())
