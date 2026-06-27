"""Offline tests — no network. Exercise the wire codec, request builder,
update extraction, event builders, and peer/normalize logic."""
import pytest

from bale import Peer, pb
from bale import envelope as env
from bale import events
from bale._rpc import populate
from bale.client import _iter_rid_date, _iter_updates, _uid_from_token


# --------------------------------------------------------------------------- #
# envelope wire codec
# --------------------------------------------------------------------------- #

def test_rpc_roundtrip_response_frame():
    body = pb.LoadHistoryRequest(date=-1, limit=10).SerializeToString()
    rpc = env.encode_rpc_call("bale.messaging.v2.Messaging", "LoadHistory", body, 42)
    # rpc body carries service/method/request/headers/seq
    fields = {f: v for f, _w, v in env.parse_top_fields(rpc)}
    assert fields[1] == b"bale.messaging.v2.Messaging"
    assert fields[2] == b"LoadHistory"
    assert fields[5] == 42  # seq lives inside RpcCall.f5

    # build a fake server response frame: outer.f1 = { f2=resp_bytes, f3=seq }
    resp_payload = pb.T_pQ(seq=7).SerializeToString()
    inner = env.f_bytes(2, resp_payload) + env.f_varint(3, 42)
    frame = env.f_bytes(1, inner)
    parsed = env.parse_response_frame(frame)
    assert parsed is not None
    seq, resp = parsed
    assert seq == 42 and resp == resp_payload


def test_parse_response_frame_rejects_non_response():
    assert env.parse_response_frame(env.CLIENT_HELLO) is None
    # an update frame uses outer field 2, not 1 -> not a response
    assert env.parse_response_frame(env.f_bytes(2, b"\x0a\x00")) is None


# --------------------------------------------------------------------------- #
# kwargs -> protobuf builder
# --------------------------------------------------------------------------- #

def test_populate_scalar_nested_repeated_and_map():
    req = pb.SendMessageRequest()
    populate(req, {
        "peer": {"type": 1, "id": 99, "accessHash": 5},
        "rid": 123,
        "message": {"textMessage": {"text": "hi"}},
    })
    assert req.peer.type == 1 and req.peer.id == 99 and req.peer.accessHash == 5
    assert req.rid == 123
    assert req.message.textMessage.text == "hi"


def test_populate_unknown_field_raises():
    with pytest.raises(TypeError):
        populate(pb.SendMessageRequest(), {"not_a_field": 1})


def test_populate_repeated_rejects_bare_str():
    # a bare string to a repeated field must not expand into characters
    with pytest.raises(TypeError):
        populate(pb.GetContactsRequest(), {"optimizations": "abc"})


def test_populate_matches_handbuilt_bytes():
    a = pb.LoadHistoryRequest()
    a.peer.type = 1
    a.peer.id = 7
    a.date = -1
    a.limit = 3
    b = populate(pb.LoadHistoryRequest(), {
        "peer": {"type": 1, "id": 7}, "date": -1, "limit": 3,
    })
    assert a.SerializeToString() == b.SerializeToString()


# --------------------------------------------------------------------------- #
# update extraction (push-frame wire layout)
# --------------------------------------------------------------------------- #

def _wrap_update(update: "pb.Update") -> bytes:
    """outer.f2 -> box.f1 -> seqbox.f1 = Update."""
    u = update.SerializeToString()
    return env.f_bytes(2, env.f_bytes(1, env.f_bytes(1, u)))


def test_iter_updates_extracts_update():
    upd = pb.Update()
    upd.message.peer.type = 1
    upd.message.peer.id = 555
    upd.message.senderUid = 555
    upd.message.rid = 1
    upd.message.message.textMessage.text = "yo"
    frame = _wrap_update(upd)
    out = list(_iter_updates(frame))
    assert len(out) == 1
    assert out[0].message.message.textMessage.text == "yo"


def test_iter_updates_ignores_response_frame():
    # a response frame (outer field 1) yields no updates
    frame = env.f_bytes(1, env.f_bytes(2, b"") + env.f_varint(3, 1))
    assert list(_iter_updates(frame)) == []


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

def test_newmessage_builder_and_event():
    upd = pb.Update()
    upd.message.peer.type = 1
    upd.message.peer.id = 7
    upd.message.senderUid = 7
    upd.message.rid = 11
    upd.message.message.textMessage.text = "hello"
    ev = events.coerce_builder(events.NewMessage).build(None, upd, b"")
    assert ev is not None
    assert ev.text == "hello" and ev.sender_id == 7 and ev.id == 11 and ev.is_private


def test_newmessage_pattern_filter():
    upd = pb.Update()
    upd.message.message.textMessage.text = "/start go"
    assert events.NewMessage(pattern="/start").build(None, upd, b"") is not None
    upd.message.message.textMessage.text = "nope"
    assert events.NewMessage(pattern="/start").build(None, upd, b"") is None


def test_raw_builder_matches_any():
    upd = pb.Update()
    upd.emptyUpdate.SetInParent()
    ev = events.Raw().build(None, upd, b"")
    assert ev is not None and ev.variant == "emptyUpdate"


def test_newmessage_from_users_empty_matches_nobody():
    upd = pb.Update()
    upd.message.senderUid = 5
    upd.message.message.textMessage.text = "x"
    assert events.NewMessage(from_users=[]).build(None, upd, b"") is None
    assert events.NewMessage(from_users=[5]).build(None, upd, b"") is not None
    # accepts str / object ids too
    assert events.NewMessage(from_users=["5"]).build(None, upd, b"") is not None


def test_longtext_message_is_decoded_as_text():
    from bale import normalize
    m = pb.Message()
    m.longTextMessage.text = "a very long body"
    c = normalize.parse_message(m)
    assert c.kind == "text" and c.text == "a very long body"


def test_forward_iter_rid_date_forms():
    from bale.client import _iter_rid_date
    from bale import Message
    from bale.normalize import Content
    # single (rid, date) pair
    assert list(_iter_rid_date((5, 99))) == [(5, 99)]
    # list of pairs
    assert list(_iter_rid_date([(1, 2), (3, 4)])) == [(1, 2), (3, 4)]
    # dict
    assert list(_iter_rid_date({"rid": 7, "date": 8})) == [(7, 8)]
    # a single Message/HistoryEntry (carries rid + date)
    msg = Message(rid=11, date=22, sender_id=0, content=Content(kind="text", text="x"))
    assert list(_iter_rid_date(msg)) == [(11, 22)]
    assert list(_iter_rid_date([msg, msg])) == [(11, 22), (11, 22)]


def test_forward_requires_both_rid_and_date():
    from bale.client import _iter_rid_date
    with pytest.raises(TypeError):
        list(_iter_rid_date([(5,)]))  # rid only -> missing date


def test_forward_request_sets_date_value():
    # the ForwardMessages request must carry date (Int64Value) per source msg
    req = pb.ForwardMessagesRequest()
    for rid, date in _iter_rid_date([(123, 456)]):
        fwd = req.forwardedMessages.add()
        fwd.rid = rid
        fwd.date.value = date
    assert req.forwardedMessages[0].rid == 123
    assert req.forwardedMessages[0].date.value == 456


def test_coerce_builder_rejects_garbage():
    with pytest.raises(TypeError):
        events.coerce_builder(object())


# --------------------------------------------------------------------------- #
# peer + token
# --------------------------------------------------------------------------- #

def test_peer_roundtrip():
    p = Peer.channel(123, access_hash=9)
    proto = p.to_proto()
    assert proto.type == 2 and proto.id == 123 and proto.accessHash == 9
    assert Peer.from_proto(proto) == p


def test_uid_from_token_handles_garbage():
    assert _uid_from_token("not-a-jwt") is None
    assert _uid_from_token("") is None
