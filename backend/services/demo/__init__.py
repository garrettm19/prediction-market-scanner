"""
Offline demo mode.

Activated by ``MASG_DEMO_MODE=1`` (``true`` / ``yes`` also accepted).  In
demo mode the snapshot refresh feeds the REAL pipeline (Pass A feature
extraction -> series dedup / ambiguity enrichment -> Pass B rule engine ->
Kelly) from a deterministic synthetic dataset instead of Kalshi /
Polymarket / The Odds API, and endpoints that would reach an external
service, send a notification or enable the paper maker are refused.

Every BUY / WATCH / SKIP shown in demo mode is produced by the rule engine;
nothing in this package assigns a status.

Contents:
  fixtures.py -- synthetic markets + FanDuel events and the demo adapters /
                 odds fetcher that serve them.
  guards.py   -- FastAPI helpers that return 403 for unsafe endpoints.
  __main__.py -- ``python -m services.demo``: run the pipeline once and
                 print a table (no Node / dashboard needed).
"""
from __future__ import annotations

import os

DEMO_ENV_VAR = "MASG_DEMO_MODE"
_TRUTHY = {"1", "true", "yes"}


def is_demo_mode() -> bool:
    """True when MASG_DEMO_MODE is set to 1/true/yes (case-insensitive).

    Read at call time (not import time) so tests can monkeypatch the env.
    """
    return os.getenv(DEMO_ENV_VAR, "").strip().lower() in _TRUTHY
