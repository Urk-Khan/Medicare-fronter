"""Telnyx webhook signature verification (Ed25519).

Telnyx signs every webhook: signature = Ed25519(f"{timestamp}|{raw_body}") using the
account's key pair. We verify with the public key from Portal -> Account -> Public Key.
"""

import base64
import time

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

MAX_AGE_SECONDS = 300


def verify_telnyx_signature(public_key_b64: str, signature_b64: str | None, timestamp: str | None, body: bytes,
                            now: float | None = None) -> bool:
    if not public_key_b64 or not signature_b64 or not timestamp:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > MAX_AGE_SECONDS:
            return False
        key = VerifyKey(base64.b64decode(public_key_b64))
        key.verify(f"{timestamp}|".encode() + body, base64.b64decode(signature_b64))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False
    except Exception:
        return False
