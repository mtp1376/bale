"""
Bale phone-OTP login.

Bale's auth uses gRPC-Web over plain HTTPS (not the WebSocket transport):
  POST https://next-ws.bale.ai/bale.auth.v1.Auth/StartPhoneAuth
  POST https://next-ws.bale.ai/bale.auth.v1.Auth/ValidateCode

gRPC-Web framing on the wire:
  request body  = b"\\x00" + len(proto).to_bytes(4,'big') + proto_bytes
  response body = same data frame, then a trailers frame:
                  b"\\x80" + len(trailers).to_bytes(4,'big') + b"grpc-status: 0\\r\\n..."

The JWT we use for the WebSocket auth is delivered as a Set-Cookie
(`access_token=...`) on the ValidateCode response.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Optional

from . import bale_pb2 as pb

# Static api_key baked into Bale's official web client (web@5.0.0+155103). Observed
# identical across 3 captured StartPhoneAuth requests. This is the per-app key,
# analogous to Telegram's api_id/api_hash. Server rejects requests with empty key.
WEB_API_KEY = "C28D46DC4C3A7A26564BFCC48B929086A95C93C98E789A19847BEE8627DE4E7D"

logger = logging.getLogger(__name__)

BASE = "https://next-ws.bale.ai"
ORIGIN = "https://web.bale.ai"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


def _default_headers() -> dict:
    sid = str(int(time.time() * 1000))
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "Origin": ORIGIN,
        "User-Agent": USER_AGENT,
        "app_version": "155103",
        "browser_type": "1",
        "browser_version": "148.0.0.0",
        "os_type": "5",
        "session_id": sid,
        "mt_app_version": "155103",
        "mt_browser_type": "1",
        "mt_browser_version": "148.0.0.0",
        "mt_os_type": "5",
        "mt_session_id": sid,
    }


def _grpc_web_encode(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def _grpc_web_decode(body: bytes) -> tuple[bytes, Optional[int], dict[str, str]]:
    """Return (data_payload, status_code_or_None, trailers_dict)."""
    o = 0
    data = b""
    status: Optional[int] = None
    trailers: dict[str, str] = {}
    while o + 5 <= len(body):
        flag = body[o]
        ln = int.from_bytes(body[o + 1 : o + 5], "big")
        o += 5
        chunk = body[o : o + ln]
        o += ln
        if flag == 0:
            data = chunk
        elif flag & 0x80:
            for line in chunk.decode("utf-8", "replace").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    trailers[k.strip().lower()] = v.strip()
            if "grpc-status" in trailers:
                status = int(trailers["grpc-status"])
    return data, status, trailers


def _post_grpc_web(
    path: str, payload: bytes, *, extra_headers: dict | None = None, timeout: float = 30.0
) -> tuple[bytes, list[str]]:
    headers = _default_headers()
    if extra_headers:
        headers.update(extra_headers)
    body = _grpc_web_encode(payload)
    req = urllib.request.Request(f"{BASE}{path}", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
            response_body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        data, status, trailers = _grpc_web_decode(body)
        msg = trailers.get("grpc-message", e.reason)
        raise RuntimeError(f"HTTP {e.code}: {msg}") from None

    data, status, trailers = _grpc_web_decode(response_body)
    if status not in (None, 0):
        raise RuntimeError(f"gRPC error {status}: {trailers.get('grpc-message','')}")
    return data, cookies


# ---- public API ----

@dataclass
class AuthSession:
    transaction_hash: str
    is_registered: bool
    activation_type: int
    sent_code_type: int
    raw: pb.StartPhoneAuthResponse


@dataclass
class LoginResult:
    access_token: str
    user_id: int
    user_name: str
    raw_response_bytes: bytes


def start_phone_auth(
    phone: int,
    *,
    app_id: int = 4,
    device_uuid: Optional[str] = None,
    device_title: str = "Chrome_148.0.0.0, macOS",
    api_key: str = WEB_API_KEY,
) -> AuthSession:
    """Trigger an SMS to the given phone (full international, e.g. 989123456789)."""
    if not isinstance(phone, int) or isinstance(phone, bool) or phone <= 0:
        raise ValueError(
            "phone must be a positive int in full international form, e.g. 989123456789"
        )
    if device_uuid is None:
        device_uuid = str(uuid.uuid4())

    req = pb.StartPhoneAuthRequest()
    req.phoneNumber = phone
    req.appId = app_id
    req.apiKey = api_key                          # the static web-client key (REQUIRED)
    req.deviceHash = device_uuid.encode("ascii")  # per-install UUID, sent as ASCII bytes
    req.deviceTitle = device_title
    req.sendCodeType = 1                          # 1 = SMS

    data, _ = _post_grpc_web("/bale.auth.v1.Auth/StartPhoneAuth", req.SerializeToString())
    resp = pb.StartPhoneAuthResponse()
    resp.ParseFromString(data)
    logger.info("StartPhoneAuth: tx=%s registered=%s sentCodeType=%s",
                (resp.transactionHash[:12] + "…") if resp.transactionHash else "<empty>",
                resp.isRegistered, resp.sentCodeType)
    return AuthSession(
        transaction_hash=resp.transactionHash,
        is_registered=resp.isRegistered,
        activation_type=resp.activationType,
        sent_code_type=resp.sentCodeType,
        raw=resp,
    )


def validate_code(transaction_hash: str, code: str) -> LoginResult:
    """Submit OTP. Returns the access_token (JWT) and basic user info."""
    if not transaction_hash:
        raise ValueError("transaction_hash is required (from start_phone_auth)")
    if not code:
        raise ValueError("code is required")
    req = pb.ValidateCodeRequest()
    req.transactionHash = transaction_hash
    req.code = code
    req.isJwt.value = True   # tells server to return a JWT (and set the cookie)

    data, cookies = _post_grpc_web("/bale.auth.v1.Auth/ValidateCode", req.SerializeToString())

    # Primary: scan response body for JWT pattern. Bale embeds the access_token
    # as a string field deep in the ValidateCode response (our extracted decoder
    # for this type was incomplete, so we extract by signature instead).
    access_token = None
    m = re.search(rb"(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", data)
    if m:
        access_token = m.group(1).decode("ascii")

    # Fallback: Set-Cookie (works when running through a proxy that exposes it).
    if not access_token:
        for c in cookies:
            cm = re.match(r"\s*access_token=([^;]+)", c)
            if cm:
                access_token = cm.group(1)
                break

    if not access_token:
        raise RuntimeError(
            "ValidateCode succeeded but no access_token found "
            f"(body {len(data)}B, cookies={cookies})"
        )

    # Best-effort user info parse from response body. The token is already
    # extracted above, so a malformed/truncated trailer must never discard a
    # successful login — the whole walk is wrapped defensively.
    user_id = 0
    user_name = ""
    try:
        # Walk top-level fields generically; field 2 holds the User message.
        o = 0
        while o + 1 < len(data):
            tag = data[o]; o += 1
            wire = tag & 7; field = tag >> 3
            if wire == 2:
                ln = 0; shift = 0
                while True:
                    b = data[o]; o += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                sub = data[o : o + ln]; o += ln
                if field == 2:
                    # User message: f1=int32 id, f3=string name (heuristic)
                    user = pb.User()
                    try:
                        user.ParseFromString(sub)
                        user_id = getattr(user, "id", 0)
                        user_name = getattr(user, "name", "")
                    except Exception:
                        pass
            elif wire == 0:
                while True:
                    b = data[o]; o += 1
                    if not (b & 0x80): break
            else:
                break
    except (IndexError, Exception):  # pragma: no cover - best-effort
        logger.debug("validate_code: user-info parse failed (token still returned)")

    return LoginResult(
        access_token=access_token,
        user_id=user_id,
        user_name=user_name,
        raw_response_bytes=data,
    )


def login(phone: int, code_provider) -> LoginResult:
    """End-to-end blocking login. ``code_provider`` is a callable returning the OTP string.

    Example:
        result = login(989123456789, lambda: input("OTP code: "))
        print(result.access_token)
    """
    session = start_phone_auth(phone)
    code = code_provider()
    return validate_code(session.transaction_hash, code.strip())
