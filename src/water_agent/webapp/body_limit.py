"""Request-body ceiling for the upload routes, enforced below FastAPI.

Why this cannot live in the route
---------------------------------
FastAPI parses a form body *before* it solves the route's dependencies: in
``fastapi.routing.get_request_handler`` the handler awaits ``request.form()``
and only then calls ``solve_dependencies`` -- and authentication and CSRF are
dependencies.  ``upload_routes._read_bounded`` therefore runs after Starlette
has already received the whole multipart body and spooled the file part into a
``SpooledTemporaryFile`` (rolled over to ``/tmp`` past 1 MB; ``/tmp`` is a tmpfs
in the container).  Measured before this module existed, with a 30 MB file: an
anonymous request without cookie or CSRF token was answered 403
``AUTH_CSRF_INVALID`` after 31 457 280 bytes had been received and written to a
temp file; a signed-in one got its 413 after the same 31 457 280 bytes.  The
only ceiling that actually bit was the reverse proxy's -- and the base compose
stack has no reverse proxy.

What happens instead (the order is the property)
-------------------------------------------------
1. A declared ``Content-Length`` above the ceiling is answered 413 before a
   single body byte is received (so the server never sends ``100 Continue``
   either).
2. Otherwise -- chunked transfer, or no length at all -- the bytes are counted
   as the application receives them and the read is aborted at the first
   message that passes the ceiling.  Whatever the application makes of the
   aborted read (FastAPI turns it into a 400 "error parsing the body") is
   discarded and the same 413 is sent instead; Starlette closes the temp files
   of a parse that failed.  If the application had already started a response
   (the upload route never does), nothing more is sent -- one request, one
   response -- and the abort propagates so the server drops the connection.

Both 413s say ``Connection: close``: the rest of the body is never read, so the
connection cannot carry another request, and the server must not keep draining
an unread body for as long as a client cares to send one.

A plain ASGI middleware on purpose, not ``BaseHTTPMiddleware``: the guard has to
sit on the ``receive`` channel itself, below every reader of the body.

The ceiling is the per-file limit plus a multipart framing allowance, not a
number of its own.  The exact per-file check stays in ``_read_bounded`` (and, as
defence in depth, in ``extract_text``); this layer only guarantees that nothing
*much* larger than an acceptable upload is ever received.  It is not the first
line of defence either: connection counts and request rates are the reverse
proxy's job, and a body under the ceiling is still parsed before authentication.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from water_agent.documents.errors import CODE_UPLOAD_TOO_LARGE, UploadError
from water_agent.documents.extractors import MAX_UPLOAD_BYTES

#: The capped path and everything below it.  ``POST /api/uploads`` is the only
#: route that parses a form body (``tests/test_upload_body_limit.py`` reads that
#: from the OpenAPI schema); ``/api/uploads/{upload_id}`` takes no body at all,
#: so capping it as well costs nothing and keeps the rule a plain prefix.
UPLOAD_PATH_PREFIX = "/api/uploads"

#: Allowance for the multipart envelope around the file: boundary lines, the part
#: headers (the filename is the only variable part) and the optional
#: ``project_id`` field.  A browser upload needs well under 2 KB of it.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

#: Largest request body ever received under :data:`UPLOAD_PATH_PREFIX`.
#: ``deploy/Caddyfile`` sets the same value for the reverse proxy.
UPLOAD_BODY_LIMIT_BYTES = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES

_API_VERSION = "v1"


class _BodyTooLarge(Exception):
    """Raised into the application from ``receive`` once the ceiling is passed."""


def _route_path(scope: Scope) -> str:
    """The path the router matches on: ``scope["path"]`` without ``root_path``."""

    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path) and path[len(root_path) :][:1] in ("", "/"):
        return path[len(root_path) :]
    return path


def _is_capped(scope: Scope) -> bool:
    path = _route_path(scope)
    return path == UPLOAD_PATH_PREFIX or path.startswith(UPLOAD_PATH_PREFIX + "/")


def _declared_length(scope: Scope) -> int | None:
    """The largest well-formed ``Content-Length`` the request declares, if any.

    A malformed value is not trusted in either direction: the body is counted.
    """

    declared: int | None = None
    for name, value in scope.get("headers", ()):
        if name.lower() == b"content-length" and value.isdigit():
            declared = max(declared or 0, int(value))
    return declared


def _too_large() -> JSONResponse:
    """The 413 ``upload_routes`` answers, byte for byte (envelope, code, message)."""

    error = UploadError(
        CODE_UPLOAD_TOO_LARGE, f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
    )
    return JSONResponse(
        status_code=error.status_code,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": error.code, "message": error.message},
        },
        headers={"Connection": "close"},
    )


class UploadBodyLimitMiddleware:
    """Refuse request bodies above ``max_body_bytes`` on the upload routes."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = UPLOAD_BODY_LIMIT_BYTES) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_capped(scope):
            await self.app(scope, receive, send)
            return

        limit = self.max_body_bytes
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _too_large()(scope, receive, send)
            return

        received = 0
        cut = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, cut
            if cut:
                # Asked again after the cut (a disconnect listener, say): as far
                # as the application is concerned, the client is gone.
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    cut = True
                    raise _BodyTooLarge(f"request body passed the {limit}-byte upload ceiling")
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if cut and not response_started:
                # The application's reaction to the aborted read is not the
                # answer; the 413 below is.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # After the cut, whatever the application raised is a consequence of
            # it -- unless a response is already on the wire, the 413 replaces it.
            if not cut or response_started:
                raise
        if cut and not response_started:
            await _too_large()(scope, receive, send)


__all__ = [
    "MULTIPART_OVERHEAD_BYTES",
    "UPLOAD_BODY_LIMIT_BYTES",
    "UPLOAD_PATH_PREFIX",
    "UploadBodyLimitMiddleware",
]
