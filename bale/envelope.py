"""
Bale wire framing.

Each WebSocket binary frame is a protobuf-encoded Envelope. This file is the
hand-rolled codec for that envelope (the inner `request` payload is encoded
with the protoc-generated bale_pb2 messages).

Wire format (reverse-engineered, confirmed against ~1200 captured frames):
    Envelope (client → server) {
        RpcCall rpc  = 1;       // length-delimited
    }
    RpcCall {
        string  service  = 1;       // e.g. "bale.messaging.v2.Messaging"
        string  method   = 2;       // e.g. "SendMessage"
        bytes   request  = 3;       // serialized request proto
        Headers headers  = 4;
        uint32  seq      = 5;       // varint, per-connection counter
    }
    Server frame { Inner inner = 1; }
    Inner        { bytes response = 2;  uint32 seq = 3; }
    Headers {
        repeated Header items = 1;
    }
    Header {
        string      key   = 1;
        StringWrap  value = 2;
    }
    StringWrap { string s = 1; }

Initial 6-byte ClientHello: bytes 1a 04 08 01 10 01 (= envelope.f3=ClientHello
{f1=1, f2=1}). Server replies with bytes 2a 04 08 01 10 01 (envelope.f5).
"""
from __future__ import annotations

import time
from typing import Iterable, Tuple

CLIENT_HELLO = bytes([0x1A, 0x04, 0x08, 0x01, 0x10, 0x01])
SERVER_HELLO = bytes([0x2A, 0x04, 0x08, 0x01, 0x10, 0x01])


# ---- protobuf wire primitives ----

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        if n < 0x80:
            out.append(n)
            return bytes(out)
        out.append((n & 0x7F) | 0x80)
        n >>= 7


def _tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def f_varint(field: int, n: int) -> bytes:
    return _tag(field, 0) + varint(n)


def f_bytes(field: int, b: bytes) -> bytes:
    return _tag(field, 2) + varint(len(b)) + b


def f_string(field: int, s: str) -> bytes:
    return f_bytes(field, s.encode("utf-8"))


def read_varint(buf: bytes, o: int) -> Tuple[int, int]:
    v = 0
    s = 0
    while o < len(buf):
        b = buf[o]
        o += 1
        v |= (b & 0x7F) << s
        if (b & 0x80) == 0:
            return v, o
        s += 7
        if s > 63:  # a 64-bit varint is at most 10 bytes
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def parse_top_fields(buf: bytes):
    """Parse a single-level protobuf message into [(field_num, wire_type, value)] tuples."""
    out = []
    o = 0
    n = len(buf)
    while o < n:
        tag, o = read_varint(buf, o)
        wire = tag & 7
        f = tag >> 3
        if wire == 0:
            v, o = read_varint(buf, o)
            out.append((f, 0, v))
        elif wire == 1:
            if o + 8 > n:
                raise ValueError("truncated 64-bit field")
            out.append((f, 1, buf[o : o + 8])); o += 8
        elif wire == 2:
            ln, o = read_varint(buf, o)
            if ln < 0 or o + ln > n:
                raise ValueError("truncated length-delimited field")
            out.append((f, 2, buf[o : o + ln])); o += ln
        elif wire == 5:
            if o + 4 > n:
                raise ValueError("truncated 32-bit field")
            out.append((f, 5, buf[o : o + 4])); o += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


# ---- header metadata sent on every call ----

def default_headers() -> list[tuple[str, str]]:
    sid = str(int(time.time() * 1000))
    return [
        ("app_version", "155103"),
        ("browser_type", "1"),
        ("browser_version", "148.0.0.0"),
        ("os_type", "5"),
        ("session_id", sid),
        ("mt_app_version", "155103"),
        ("mt_browser_type", "1"),
        ("mt_browser_version", "148.0.0.0"),
        ("mt_os_type", "5"),
        ("mt_session_id", sid),
    ]


def encode_header(key: str, value: str) -> bytes:
    # Header { f1 string key, f2 StringWrap{ f1 string s } }
    inner = f_string(1, value)
    wrap = f_bytes(2, inner)
    return f_bytes(1, f_string(1, key) + wrap)


def encode_headers(items: Iterable[tuple[str, str]]) -> bytes:
    inner = b"".join(encode_header(k, v) for k, v in items)
    return f_bytes(4, inner)


# ---- envelope build ----

def encode_rpc_call(service: str, method: str, request_bytes: bytes,
                    seq: int,
                    headers: Iterable[tuple[str, str]] | None = None) -> bytes:
    """Build the RpcCall body. Seq lives INSIDE RpcCall.f5 (not in the outer
    envelope) — confirmed against captured web.bale.ai frames."""
    if headers is None:
        headers = default_headers()
    body = (
        f_string(1, service)
        + f_string(2, method)
        + f_bytes(3, request_bytes)
        + encode_headers(headers)
        + f_varint(5, seq)
    )
    return body


def encode_envelope(rpc_call_bytes: bytes) -> bytes:
    """Wrap an RpcCall body in the outer envelope: { f1 = RpcCall }."""
    return f_bytes(1, rpc_call_bytes)


# ---- server frame parsing ----

def parse_response_frame(raw: bytes):
    """Extract ``(seq, response_bytes)`` from a server RPC-response frame.

    Layout: ``outer.f1 = { f3 = seq varint, f2 = response_bytes }``.
    Returns ``None`` if the frame doesn't match (e.g. the 6-byte hello or a
    server-pushed update frame, which carry a different shape).
    """
    try:
        outer = parse_top_fields(raw)
    except ValueError:
        return None
    body = next((v for f, _w, v in outer if f == 1), None)
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        inner = parse_top_fields(body)
    except ValueError:
        return None
    seq = None
    resp = None
    for f, _w, v in inner:
        if f == 3 and isinstance(v, int):
            seq = v
        elif f == 2 and isinstance(v, (bytes, bytearray)):
            resp = bytes(v)
    if seq is None:
        return None
    return seq, resp or b""
