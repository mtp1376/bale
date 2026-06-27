# bale

**Unofficial async Python client for [Bale messenger](https://web.bale.ai)'s web RPC API.**

Reverse-engineered from the Bale web client, with a high-level API modeled on
[Telethon](https://docs.telethon.dev/): an async, context-managed client,
ergonomic message/dialog/entity helpers, and an `@client.on(...)` event system —
plus typed access to **all 607 raw RPCs** (52 services) when you need them.

> ⚠️ **Unofficial & proprietary.** This library is not affiliated with or
> endorsed by Bale. It talks to a private API recovered from the web client, so
> behavior may change without notice. Use it only with accounts and data you are
> authorized to access, and in accordance with Bale's Terms of Service. See
> [LICENSE](LICENSE) — all rights reserved.

---

## Install

```bash
pip install bale-sdk
```

The distribution is named `bale-sdk`, but you **import it as `bale`** (like
`beautifulsoup4` → `import bs4`). Requires Python 3.9+. Dependencies:
`websockets` and `protobuf`.

## Authentication

The client authenticates with an **access token** (a JWT) — the same
`access_token` cookie the web client uses. Provide it however you like; the
examples read it from the `BALE_TOKEN` environment variable:

```bash
export BALE_TOKEN="eyJ..."
```

You can also obtain one programmatically via phone OTP:

```python
from bale import auth

session = auth.start_phone_auth(989123456789)        # sends an SMS code
result = auth.validate_code(session.transaction_hash, input("code: "))
print(result.access_token)                            # store this securely
```

## Quickstart

```python
import asyncio
import os
from bale import BaleClient

async def main():
    async with BaleClient(os.environ["BALE_TOKEN"]) as client:
        me = await client.get_me()
        print("Logged in as:", me.title)

        # Your most recent conversations (titles auto-resolved)
        for dialog in await client.get_dialogs(limit=10):
            print(f"{dialog.unread_count:>3} unread  {dialog.title}")

        # Send a message (accepts a username, numeric id, or Peer)
        await client.send_message("@some_channel", "Hello from Python 👋")

asyncio.run(main())
```

## High-level API

`BaleClient` provides Telethon-style conveniences. Entities can be referenced by
**username** (`"@name"`), **numeric id**, or a `Peer`/`PeerInfo`:

| Method | Description |
|---|---|
| `await client.get_me()` | The logged-in account (as `PeerInfo`). |
| `await client.get_entity(ref)` | Resolve a username / id / peer to `PeerInfo`. |
| `await client.send_message(ref, text, reply_to=None)` | Send a text message (optionally quoting). |
| `await client.edit_message(ref, rid, text)` | Edit a message. |
| `await client.delete_messages(ref, rids, revoke=True)` | Delete messages. |
| `await client.forward_messages(to, messages, from_)` | Forward messages (pass `Message` objects or `(rid, date)` pairs — Bale needs both). |
| `await client.react(ref, rid, "❤")` | React to a message. |
| `await client.mark_read(ref)` | Mark a chat read. |
| `await client.get_messages(ref, limit=100)` | Fetch messages (list). |
| `async for m in client.iter_messages(ref)` | Iterate messages (paginated). |
| `await client.get_dialogs(limit=100)` | Fetch the dialog list. |
| `async for d in client.iter_dialogs()` | Iterate dialogs (paginated). |

```python
async with BaleClient(token) as client:
    chat = await client.get_entity("@my_channel")

    async for msg in client.iter_messages(chat, limit=50):
        if msg.content.kind == "text":
            print(msg.sender_id, msg.content.text)
```

## Events

React to incoming updates in real time, just like Telethon:

```python
import asyncio
from bale import BaleClient, events

client = BaleClient(token)

@client.on(events.NewMessage(pattern="/ping"))
async def on_ping(event):
    await event.reply("pong")

@client.on(events.NewMessage)
async def on_message(event):
    print(f"{event.sender_id} in {event.chat_id}: {event.text}")

async def main():
    async with client:
        await client.run_until_disconnected()

asyncio.run(main())
```

A `NewMessage` event exposes `text`, `sender_id`, `chat_id`, `peer`, `id`,
`date`, `content`, `is_private`/`is_group`, and the coroutines `reply()`,
`respond()`, `delete()`, `mark_read()`.

Need something other than new messages? `events.Raw` fires for **every** update;
`event.update` is the decoded `pb.Update` and `event.variant` names which of
Bale's ~140 update types it is (e.g. `messageRead`, `reactionsUpdate`, `typing`):

```python
@client.on(events.Raw)
async def on_raw(event):
    if event.variant == "reactionsUpdate":
        print("reaction:", event.update.reactionsUpdate)
```

## Raw RPC access

Every Bale web RPC is available as a typed async method, grouped by service. The
namespace is the snake_case of the service's final segment:

```python
# bale.messaging.v2.Messaging -> client.messaging
resp = await client.messaging.SendMessage(
    peer={"type": 1, "id": uid, "accessHash": ah},
    rid=rid,
    message={"textMessage": {"text": "hi"}},
)

# Pass a pre-built protobuf instead of kwargs, if you prefer:
from bale import pb
req = pb.LoadHistoryRequest(peer=peer, date=-1, loadMode=2, limit=20)
hist = await client.messaging.LoadHistory(req)
```

Discover the full surface:

```python
from bale import ALL_RPCS, SERVICE_CLASSES
print(len(ALL_RPCS), "RPCs across", len(SERVICE_CLASSES), "services")
```

Full reference (method names, inputs, outputs, inferred usage) is in
[`docs/API_COVERAGE.md`](docs/API_COVERAGE.md) (English) and
[`docs/API_FA.md`](docs/API_FA.md) (Persian / فارسی).

## Project layout

```
bale/            the package (high-level client, transport, events, generated RPCs)
docs/            API reference (English + Persian)
examples/        runnable scripts (read BALE_TOKEN from the environment)
proto/           the recovered .proto schemas (reference)
tests/           offline tests
```

## Disclaimer

This is an independent, unofficial project for interoperability and research.
"Bale" and related marks belong to their respective owners. You are responsible
for how you use it.
