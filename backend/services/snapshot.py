"""
In-memory opportunity snapshot.

Stores the latest pipeline result so the frontend can read cached
opportunities without re-running the pipeline on every request.

Writers:
  - MonitorScheduler (after each pipeline cycle)
  - POST /api/opportunities/refresh (manual refresh, runs in background)

Readers:
  - GET /api/opportunities (returns snapshot if fresh)
  - GET /api/opportunities/snapshot (read-only)
  - GET /api/opportunities/status (lightweight metadata)
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass

from services.demo import is_demo_mode
from services.opportunities import EvaluatedOpportunity
from services.redaction import redact_secrets

logger = logging.getLogger(__name__)


@dataclass
class OpportunitySnapshot:
    """Immutable snapshot of the last pipeline result."""
    opportunities: list[EvaluatedOpportunity]
    meta: dict
    updated_at: float                  # time.time() of pipeline completion
    trigger: str                       # "startup" | "scheduled" | "manual" | "ws:..."


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_snapshot: OpportunitySnapshot | None = None
_is_refreshing: bool = False
_refresh_lock: asyncio.Lock = asyncio.Lock()      # serializes pipeline runs

# Background refresh metadata (manual refresh runs detached so POST returns fast)
_start_lock: asyncio.Lock = asyncio.Lock()        # serializes spawn decision (brief)
_refresh_started_at: float | None = None
_last_refresh_error: str | None = None
_last_refresh_duration_seconds: float | None = None
_last_trigger: str | None = None
_background_task: asyncio.Task | None = None     # held to prevent GC


def get_snapshot() -> OpportunitySnapshot | None:
    """Read the current snapshot. No I/O, no pipeline."""
    return _snapshot


def is_refreshing() -> bool:
    """True if a pipeline run is in progress."""
    return _is_refreshing


def get_refresh_state() -> dict:
    """Lightweight read-only snapshot of background-refresh metadata."""
    return {
        "is_refreshing": _is_refreshing,
        "refresh_started_at": _refresh_started_at,
        "last_refresh_error": _last_refresh_error,
        "last_refresh_duration_seconds": _last_refresh_duration_seconds,
        "last_trigger": _last_trigger,
    }


def store_snapshot(
    opportunities: list[EvaluatedOpportunity],
    meta: dict,
    trigger: str = "unknown",
) -> OpportunitySnapshot:
    """Store a new snapshot from an already-completed pipeline run."""
    global _snapshot
    _snapshot = OpportunitySnapshot(
        opportunities=opportunities,
        meta=dict(meta),
        updated_at=time.time(),
        trigger=trigger,
    )
    logger.info(
        "Snapshot stored: %d opportunities, trigger=%s",
        len(opportunities), trigger,
    )
    return _snapshot


async def refresh_snapshot(
    trigger: str = "manual",
    scope: str = "stale",
    cfg=None,
    only_if_missing: bool = False,
) -> OpportunitySnapshot:
    """
    Run the pipeline and store the result as the new snapshot.

    Single shared path for both manual (POST /refresh) and scheduler-driven
    refreshes.  All callers acquire _refresh_lock so the expensive pipeline
    cannot run concurrently from independent code paths.

    scope controls odds cache invalidation before the pipeline runs:
      "stale"       — only fetch sports whose TTL expired (default, cheapest)
      "all"         — invalidate entire odds cache, fetch everything fresh
      "{sport_key}" — invalidate one sport's cache, fetch it fresh

    cfg lets the scheduler pass a custom EngineConfig (e.g. wider min_edge
    for monitoring); falls back to DEFAULT_CONFIG when None.

    Acquires _refresh_lock, manages is_refreshing/last_trigger/started_at/
    duration/error metadata centrally so /status reflects every refresh
    regardless of which caller initiated it.

    only_if_missing (cold-start path): after acquiring _refresh_lock, if a
    snapshot was stored while this caller was waiting (e.g. by a concurrent
    cold-start request or a background refresh), return it instead of
    running the full pipeline again.

    Per-sport Odds API failures reported by fetch_odds (meta["sports_failed"])
    are kept in the stored snapshot meta so /status can surface a degraded
    (partial-failure) refresh instead of presenting it as fully fresh.

    Demo mode (SCANNER_DEMO_MODE=1, see services/demo): the same pipeline runs
    on the synthetic demo adapters + odds fetcher, ``scope`` is ignored (no
    odds cache), the paper maker is forced off, and meta["data_source"] is
    "demo".  Otherwise meta["data_source"] is "live".
    """
    global _is_refreshing, _refresh_started_at, _last_trigger
    global _last_refresh_error, _last_refresh_duration_seconds

    # Import here to avoid circular import (snapshot ← opportunities → snapshot)
    from services.opportunities import fetch_opportunities, DEFAULT_CONFIG
    from services import odds_cache

    if cfg is None:
        cfg = DEFAULT_CONFIG

    started = time.time()

    async with _refresh_lock:
        if only_if_missing and _snapshot is not None:
            return _snapshot
        _is_refreshing = True
        _refresh_started_at = started
        _last_trigger = trigger
        try:
            if is_demo_mode():
                # Offline demo: same pipeline, synthetic inputs.  No odds
                # cache to invalidate, no network, maker forced off (the
                # default MakerConfig is disabled + paper-only), props off.
                from services.demo.fixtures import (
                    demo_adapters_for_scan_config,
                    make_demo_odds_fetcher,
                )
                from services.maker.config import MakerConfig

                if cfg.enable_props:
                    cfg = dataclasses.replace(cfg, enable_props=False)
                opportunities, meta = await fetch_opportunities(
                    cfg=cfg,
                    adapters=demo_adapters_for_scan_config(),
                    odds_fetcher=make_demo_odds_fetcher(),
                    maker_cfg=MakerConfig(),
                )
                meta = dict(meta)
                meta["data_source"] = "demo"
            else:
                # Invalidate cache per scope
                if scope == "all":
                    odds_cache.invalidate_all()
                elif scope != "stale":
                    odds_cache.invalidate(scope)
                # "stale" → no invalidation; TTL checks in fetch_odds handle it

                opportunities, meta = await fetch_opportunities(cfg=cfg)
                meta = dict(meta)
                meta["data_source"] = "live"
            meta["sports_failed"] = list(meta.get("sports_failed") or [])
            snap = store_snapshot(opportunities, meta, trigger=trigger)
            _last_refresh_error = None
            return snap
        except Exception as exc:
            # Record so /status surfaces the failure for any caller path.
            # Redact before truncation so a credential is never half-kept.
            _last_refresh_error = (
                f"{type(exc).__name__}: {redact_secrets(exc)[:300]}"
            )
            raise
        finally:
            _last_refresh_duration_seconds = time.time() - started
            _refresh_started_at = None
            _is_refreshing = False


async def _run_and_record(trigger: str, scope: str, started_at: float) -> None:
    """
    Detached coroutine that runs refresh_snapshot inside a `try` so the
    spawned asyncio task never raises into the void.  All status metadata
    (is_refreshing, started_at, duration, error, trigger) is owned by
    refresh_snapshot itself, so this wrapper has no bookkeeping to do.
    """
    try:
        await refresh_snapshot(trigger=trigger, scope=scope)
    except Exception:
        logger.exception("Background refresh failed")
        # refresh_snapshot already wrote the error to _last_refresh_error
        # and reset the lock/flags in its finally block.
        pass


async def start_background_refresh(
    trigger: str = "manual",
    scope: str = "stale",
) -> dict:
    """
    Spawn a detached background refresh if one is not already running.

    Returns immediately with status:
      {
        "started": bool,            # True if we spawned a new refresh
        "already_running": bool,    # True if a refresh was already in flight
        "started_at": float | None, # unix seconds when current refresh began
      }

    Race-safe: uses _start_lock to serialize the spawn decision. _is_refreshing,
    _refresh_started_at and _last_trigger are set synchronously under the lock
    so the very next status read (or near-simultaneous double POST) sees a
    consistent in-flight state without waiting for the spawned task to start.
    """
    global _background_task, _is_refreshing, _refresh_started_at, _last_trigger
    async with _start_lock:
        if _is_refreshing or _refresh_lock.locked():
            return {
                "started": False,
                "already_running": True,
                "started_at": _refresh_started_at,
            }
        started_at = time.time()
        _is_refreshing = True
        _refresh_started_at = started_at
        _last_trigger = trigger
        _background_task = asyncio.create_task(
            _run_and_record(trigger, scope, started_at)
        )
        return {
            "started": True,
            "already_running": False,
            "started_at": started_at,
        }


def clear_snapshot() -> None:
    """Clear the snapshot AND background-refresh state. For testing only."""
    global _snapshot, _is_refreshing
    global _refresh_started_at, _last_refresh_error
    global _last_refresh_duration_seconds, _last_trigger, _background_task
    _snapshot = None
    _is_refreshing = False
    _refresh_started_at = None
    _last_refresh_error = None
    _last_refresh_duration_seconds = None
    _last_trigger = None
    _background_task = None
