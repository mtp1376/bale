#!/usr/bin/env python3
"""Live smoke test against YOUR OWN account.

Exercises the most-used read endpoints plus a self-contained write round-trip
(send to your own Saved Messages, then delete it). Read-only except for that
self message; never touches anyone else's data.

    export BALE_TOKEN="eyJ..."
    python examples/smoke_test.py
"""
import asyncio
import os
import random
import time

from bale import BaleClient, pb


async def main() -> None:
    async with BaleClient(os.environ["BALE_TOKEN"]) as client:
        me = await client.get_me()
        print(f"[me]          {me.title} (id={me.peer.id})")

        params = await client.configs.GetParameters()
        print(f"[config]      {len(params.parameters)} parameters")

        dialogs = await client.get_dialogs(limit=10)
        print(f"[dialogs]     {len(dialogs)} (e.g. {dialogs[0].title if dialogs else '-'})")

        if dialogs:
            msgs = await client.get_messages(dialogs[0].peer, limit=5)
            print(f"[messages]    {len(msgs)} from first dialog")

        # write round-trip on self only
        rid = random.getrandbits(63)
        sr = await client.send_message(me.peer, f"smoke test {time.strftime('%H:%M:%S')}", rid=rid)
        print(f"[send]        ok (seq={sr.seq})")
        await client.delete_messages(me.peer, [rid], revoke=True)
        print("[delete]      ok (cleaned up)")

    print("\nSmoke test passed ✓")


if __name__ == "__main__":
    asyncio.run(main())
