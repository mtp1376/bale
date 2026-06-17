#!/usr/bin/env python3
"""Send a text message via the high-level API.

    export BALE_TOKEN="eyJ..."
    python examples/send_message.py "hello"            # to Saved Messages
    python examples/send_message.py "hi" @some_channel  # to a username
"""
import asyncio
import os
import sys

from bale import BaleClient


async def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "hello from the bale client"
    target = sys.argv[2] if len(sys.argv) > 2 else None

    async with BaleClient(os.environ["BALE_TOKEN"]) as client:
        dest = target if target is not None else (await client.get_me()).peer
        resp = await client.send_message(dest, text)
        print(f"sent (seq={resp.seq}, date={resp.date})")


if __name__ == "__main__":
    asyncio.run(main())
