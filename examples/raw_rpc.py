#!/usr/bin/env python3
"""Call raw RPCs directly when the high-level API doesn't cover something.

Every Bale web RPC is a typed async method grouped by service namespace
(snake_case of the service's final segment). Requests accept keyword fields
(nested dicts/lists auto-assembled) or a pre-built protobuf message.

    export BALE_TOKEN="eyJ..."
    python examples/raw_rpc.py
"""
import asyncio
import os

from bale import ALL_RPCS, SERVICE_CLASSES, BaleClient, pb


async def main() -> None:
    print(f"{len(ALL_RPCS)} RPCs across {len(SERVICE_CLASSES)} services\n")

    async with BaleClient(os.environ["BALE_TOKEN"]) as client:
        # bale.v1.Configs -> client.configs
        params = await client.configs.GetParameters()
        print("server parameters:", len(params.parameters))

        # kwargs form (nested dict auto-assembled into the request proto)
        contacts = await client.users.GetContacts(contactsHash="")
        print("contacts:", len(contacts.users))

        # pre-built protobuf form
        me_id = client.me_id
        req = pb.LoadHistoryRequest(
            peer=pb.Peer(type=1, id=me_id), date=-1, loadMode=2, limit=5
        )
        hist = await client.messaging.LoadHistory(req)
        print("saved-messages history:", len(hist.history))


if __name__ == "__main__":
    asyncio.run(main())
