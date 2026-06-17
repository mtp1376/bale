#!/usr/bin/env python3
"""Read message history from a chat.

    export BALE_TOKEN="eyJ..."
    python examples/messages.py @some_channel
"""
import asyncio
import os
import sys

from bale import BaleClient


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    async with BaleClient(os.environ["BALE_TOKEN"]) as client:
        if target is None:
            # default to the most recent dialog
            dialogs = await client.get_dialogs(limit=1)
            if not dialogs:
                print("no dialogs")
                return
            entity = dialogs[0].peer
            print("Reading:", dialogs[0].title or entity.id)
        else:
            info = await client.get_entity(target)
            entity = info.peer
            print("Reading:", info.title or entity.id)

        async for msg in client.iter_messages(entity, limit=20):
            who = msg.sender_id
            if msg.content.kind == "text":
                print(f"  [{who}] {msg.content.text}")
            else:
                print(f"  [{who}] <{msg.content.kind}>")


if __name__ == "__main__":
    asyncio.run(main())
