"""
Demo-mode endpoint guards.

``block_in_demo(reason)`` builds a FastAPI dependency that refuses the
request with HTTP 403 ``"Disabled in demo mode: <reason>"`` when demo mode
is active, and is a no-op otherwise.  ``ensure_not_demo(reason)`` is the
same check for conditional use inside an endpoint body.
"""
from __future__ import annotations

from typing import Callable

from fastapi import HTTPException

from services.demo import is_demo_mode


def ensure_not_demo(reason: str) -> None:
    """Raise HTTP 403 if demo mode is active."""
    if is_demo_mode():
        raise HTTPException(status_code=403, detail=f"Disabled in demo mode: {reason}")


def block_in_demo(reason: str) -> Callable[[], None]:
    """Dependency factory for ``Depends(block_in_demo("..."))``."""

    def _guard() -> None:
        ensure_not_demo(reason)

    return _guard
