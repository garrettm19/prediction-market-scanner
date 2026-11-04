"""
Cross-site request guard for the local API.

The backend has no authentication: it is a single-user tool bound to
127.0.0.1.  CORS (configured in ``main.py``) only stops a browser from
*reading* cross-origin responses — it does not stop a page on another site
from *sending* a "simple" request (a bodyless POST, or a form/text POST)
to ``http://localhost:8000``.  Without this guard any website the user
visits could stop the monitor, kick off quota-consuming refreshes, or call
``POST /api/monitor/test`` (which sends a real Pushover notification).

Policy: a state-changing request (any method other than GET/HEAD/OPTIONS)
that carries an ``Origin`` header must come from an allowed origin.
Browsers always attach ``Origin`` to cross-origin POSTs, so this blocks
cross-site requests.  Requests without ``Origin`` (curl, scripts, the
Next.js server-side renderer, tests) are allowed — they are not
browser-driven CSRF.  A literal ``Origin: null`` (sandboxed iframes,
``file://`` pages) is rejected.
"""
from __future__ import annotations

import json
from typing import Iterable

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginGuardMiddleware:
    """Pure ASGI middleware: reject unsafe-method requests from foreign origins."""

    def __init__(self, app, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(o.rstrip("/") for o in allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        origin = None
        for name, value in scope.get("headers", []):
            if name == b"origin":
                origin = value.decode("latin-1").rstrip("/")
                break

        if origin is None or origin in self.allowed_origins:
            await self.app(scope, receive, send)
            return

        body = json.dumps(
            {"detail": f"Cross-origin request from {origin!r} rejected"}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
