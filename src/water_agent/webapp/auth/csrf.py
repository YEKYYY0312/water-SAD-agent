"""P5 Step 37: CSRF double-submit cookie protection.

Pattern: server sets a non-HttpOnly `wa_csrf` cookie with a random token on
every authenticated response; state-mutating routes require the same token in
the `X-CSRF-Token` header.  The comparison uses `hmac.compare_digest` to
prevent timing attacks.

Step 38 can store the expected token server-side (in the session row) for
tighter validation; this implementation trusts the session cookie as the
binding and treats header == cookie as the double-submit contract.
"""
from __future__ import annotations

import hmac

from fastapi import Cookie, Header, HTTPException

CSRF_COOKIE_NAME = "wa_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def validate_csrf(
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER_NAME),
    wa_csrf: str | None = Cookie(default=None),
) -> None:
    """FastAPI dependency: raise 403 if CSRF double-submit check fails.

    Attach to any state-mutating route (POST/PUT/PATCH/DELETE).
    """
    if not wa_csrf or not x_csrf_token:
        raise HTTPException(status_code=403, detail={"code": "AUTH_CSRF_INVALID", "message": "CSRF token missing"})
    if not hmac.compare_digest(wa_csrf, x_csrf_token):
        raise HTTPException(status_code=403, detail={"code": "AUTH_CSRF_INVALID", "message": "CSRF token mismatch"})
