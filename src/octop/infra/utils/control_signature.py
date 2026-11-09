"""HMAC signatures for unified-entry calls to an instance's ``/api/control`` routes.

The signature covers method, path, query, body digest, the target instance (audience) and
a timestamp. Replay inside the window only returns the stored receipt of the same operation;
stale versions are refused by the instance, so no nonce table is kept.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Octop-Control-Timestamp"
SIGNATURE_HEADER = "X-Octop-Control-Signature"
MAX_SKEW_SECONDS = 300


def control_signature(
    key: str, *, method: str, path: str, query: str, body: bytes, audience: str, timestamp: int
) -> str:
    message = "\n".join(
        (
            "octop-control-v1",
            method.upper(),
            path,
            query,
            audience,
            str(timestamp),
            hashlib.sha256(body).hexdigest(),
        )
    )
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def control_headers(
    key: str,
    *,
    method: str,
    path: str,
    body: bytes,
    audience: str,
    query: str = "",
    now: float | None = None,
) -> dict[str, str]:
    timestamp = int(time.time() if now is None else now)
    signature = control_signature(
        key,
        method=method,
        path=path,
        query=query,
        body=body,
        audience=audience,
        timestamp=timestamp,
    )
    return {TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: signature}


def verify_control_signature(
    key: str,
    *,
    method: str,
    path: str,
    query: str,
    body: bytes,
    audience: str,
    timestamp: str | None,
    signature: str | None,
    now: float | None = None,
) -> bool:
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > MAX_SKEW_SECONDS:
        return False
    expected = control_signature(
        key,
        method=method,
        path=path,
        query=query,
        body=body,
        audience=audience,
        timestamp=int(timestamp),
    )
    return hmac.compare_digest(expected, signature)
