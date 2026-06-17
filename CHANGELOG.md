# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-06-17

First packaged release.

### Added
- **Full RPC surface**: 607 RPCs across 52 services exposed as typed async
  methods via per-service namespaces (`client.messaging.SendMessage(...)`,
  `client.users.LoadUsers(...)`, …). Requests accept a pre-built protobuf
  message *or* keyword fields (nested dicts/lists auto-assembled).
- **Telethon-style high-level API** on `BaleClient`:
  - async context manager (`async with BaleClient(token) as client:`)
  - `get_me`, `get_entity`, `get_input_entity`, `get_peer_id`
  - `send_message` (with `reply_to`), `edit_message`, `delete_messages`,
    `forward_messages`, `react`, `mark_read` / `send_read_acknowledge`
  - `iter_messages` / `get_messages` and `iter_dialogs` / `get_dialogs`
    (paginated; dialog titles auto-resolved)
- **Event system** (`bale.events`): `@client.on(events.NewMessage)` /
  `add_event_handler`, `run_until_disconnected`, with `NewMessage` (incoming
  messages, `.reply()` / `.respond()` / `.delete()` / `.mark_read()`) and `Raw`
  (any of Bale's ~140 update types) builders.
- **Concurrency-safe transport**: a single reader demultiplexes RPC responses
  to per-seq futures and dispatches server-pushed updates to handlers — multiple
  concurrent `call()`s no longer race for frames.
- Phone-OTP login helpers (`bale.auth`), peer resolution + cache, and normalized
  message/history dataclasses.
- Bundled reference docs: English (`docs/API_COVERAGE.md`) and Persian
  (`docs/API_FA.md`) covering every method's inputs, outputs, and inferred usage.

### Verified
- High-traffic endpoints exercised live against a real account: `GetParameters`,
  `GetContacts`, `LoadDialogs`, `GetFullUser`, `LoadUsers`, `LoadHistory`, and a
  full `SendMessage` → `DeleteMessage` round-trip; concurrent calls and the live
  update/event pipeline.
- 16 offline tests (`tests/`) cover the wire codec, kwargs→protobuf builder,
  update extraction, event builders, and peer logic.

### Hardened
After a multi-agent code review (32 confirmed findings), the following were fixed:
- The disconnect `Event` is now created lazily inside the running loop, so a
  module-scope `BaleClient(...)` + `run_until_disconnected()` no longer hits the
  Python 3.9 "Future attached to a different loop" error.
- `@username` resolution now verifies a candidate's real nick before accepting,
  and never caches a fuzzy guess under the requested username.
- `longTextMessage` bodies are decoded as text (previously dropped).
- Background event-handler tasks are tracked (not GC-collectable mid-await) and
  their exceptions are logged; a failed send no longer leaks a pending future;
  the websocket isn't leaked if the hello handshake fails.
- `populate()` rejects a bare `str`/`bytes` for repeated fields; login HTTP calls
  have a timeout; wire decoding bounds-checks malformed frames.
