"""
Read-only maker proposal endpoints.

Exposes the paper maker proposal store under ``/api/maker``.  Read-only
by design: no POST/PUT/DELETE, no order placement, no live trading.

Two endpoints:

  GET /api/maker/proposals
      Filterable list of recent paper proposal records.

  GET /api/maker/summary
      Aggregate counts and top rejection reasons.

Both endpoints return empty results gracefully when the store directory
is missing or empty.  Corrupt JSONL lines are skipped at the store
layer with a logged warning.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from services.demo.guards import ensure_not_demo
from services.maker.config import (
    MakerConfig,
    get_maker_config,
    set_maker_config,
)
from services.maker.state import (
    PaperMakerStore,
    STATUS_PAPER_ACTIVE,
    STATUS_PAPER_REJECTED,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Lazy module-level singleton.  Production callers share one store
# instance; tests use FastAPI's ``app.dependency_overrides`` to swap
# in a tmp-path-backed store without touching this module.
_default_store: PaperMakerStore | None = None


def get_paper_store() -> PaperMakerStore:
    global _default_store
    if _default_store is None:
        _default_store = PaperMakerStore()
    return _default_store


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class MakerProposalsResponse(BaseModel):
    days: int
    count: int
    proposals: list[dict[str, Any]]


class TopRejectionReason(BaseModel):
    reason: str
    count: int


class ClosestRejectedRecord(BaseModel):
    """Compact view of the rejected record with the highest
    ``estimated_maker_edge`` among records that had real book data.
    Used by the dashboard to answer "how close did we get this scan?"."""
    side: str
    event_label: str
    best_bid: float | None
    best_ask: float | None
    proposed_price: float | None
    maker_max_bid: float | None
    estimated_maker_edge: float | None
    rejection_reasons: list[str]


class MakerSummaryResponse(BaseModel):
    days: int
    total: int
    eligible: int
    rejected: int
    by_status: dict[str, int]
    by_platform: dict[str, int]
    top_rejection_reasons: list[TopRejectionReason]
    average_estimated_maker_edge: float | None
    # Near-miss diagnostics — answer "did we have real near-misses or just
    # noisy unusable records?".  All counts apply to whatever records the
    # endpoint is summarizing (full audit or latest run).
    records_with_real_book: int
    records_with_valid_inside_spread_possible: int
    rejected_for_edge_only: int
    closest_rejected_edge: float | None
    closest_rejected_record: ClosestRejectedRecord | None
    # First failing reason per record only — collapses the noise of records
    # that accumulate 4-5 reasons.  ``top_rejection_reasons`` (every reason
    # counted) is preserved for debug continuity.
    primary_rejection_reasons: list[TopRejectionReason]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# Tick size used to evaluate whether an inside-spread bid is mathematically
# possible.  Mirrors services.maker.tick.TICK_DOLLARS but kept local to the
# router so summary aggregation is independent of maker-pipeline imports.
_TICK_DOLLARS = 0.01

# Edge-only failures: rejection codes whose presence (with no structural
# blocker) means "the only thing standing between this candidate and
# eligibility was the edge math."  Used for ``rejected_for_edge_only``.
_EDGE_ONLY_REASONS: frozenset[str] = frozenset({
    "MAKER_EDGE_TOO_LOW",
    "BID_ABOVE_MAX",
})

# Structural blockers — if any of these fired, the rejection isn't an edge
# near-miss because something more fundamental was wrong (no book, stale
# data, scope mismatch, etc.).  The presence of any structural reason
# disqualifies a record from ``rejected_for_edge_only``.
_STRUCTURAL_BLOCKERS: frozenset[str] = frozenset({
    "BOOK_CROSSED_OR_EMPTY",
    "BOOK_STALE",
    "SPREAD_TOO_NARROW",
    "BID_NOT_INSIDE_SPREAD",
    "FD_STALE",
    "PLATFORM_OUT_OF_SCOPE",
    "MARKET_TYPE_OUT_OF_SCOPE",
    "MAKER_DISABLED",
})


def _has_real_book(rec: dict) -> bool:
    """True when both top-of-book sides are present numbers (not None,
    not the JSON-null placeholder for missing data)."""
    bb = rec.get("best_bid")
    ba = rec.get("best_ask")
    return isinstance(bb, (int, float)) and isinstance(ba, (int, float))


def _filter_to_latest_run(records: list[dict]) -> list[dict]:
    """Narrow to records sharing the run_id of the chronologically newest
    record that has a run_id at all.

    Records without a string ``run_id`` are treated as legacy (pre-run_id
    schema).  When *some* records have run_ids, legacy records are
    excluded from the latest-run slice.  When *every* record is legacy,
    we fall back to returning all records so the endpoint never goes
    silent against an old data file.
    """
    with_run = [
        r for r in records
        if isinstance(r.get("run_id"), str) and r["run_id"]
    ]
    if not with_run:
        # All-legacy fallback — graceful: return everything we have so the
        # client still sees data instead of an empty list.
        return list(records)
    latest_record = max(
        with_run,
        key=lambda r: float(r.get("created_at") or 0.0),
    )
    target_run_id = latest_record["run_id"]
    return [r for r in records if r.get("run_id") == target_run_id]


@router.get("/maker/proposals", response_model=MakerProposalsResponse)
def list_maker_proposals(
    days: int = Query(1, ge=1, le=90),
    status: str | None = Query(None),
    eligible: bool | None = Query(None),
    platform: str | None = Query(None),
    market_id: str | None = Query(None),
    latest_run: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
    store: PaperMakerStore = Depends(get_paper_store),
) -> MakerProposalsResponse:
    """Return recent paper maker proposals.

    Filterable by ``status``, ``eligible``, ``platform``, ``market_id``.
    ``latest_run=true`` narrows to the newest scan's records only;
    ``limit`` caps the response size (default 100, max 1000).  Records
    are returned newest-first by ``created_at``.  Empty store ⇒ empty
    list (200 OK, never 404).
    """
    records = store.read_recent(days=days)

    if latest_run:
        records = _filter_to_latest_run(records)

    def _matches(rec: dict) -> bool:
        if status is not None and rec.get("status") != status:
            return False
        if eligible is not None and bool(rec.get("eligible")) != eligible:
            return False
        if platform is not None and rec.get("platform") != platform:
            return False
        if market_id is not None and rec.get("market_id") != market_id:
            return False
        return True

    filtered = [r for r in records if _matches(r)]
    # Newest first: handy for dashboards, also makes the limit cut off the
    # least-recent records rather than the most-relevant ones.
    filtered.sort(
        key=lambda r: float(r.get("created_at") or 0.0),
        reverse=True,
    )
    capped = filtered[:limit]
    return MakerProposalsResponse(
        days=days,
        count=len(capped),
        proposals=capped,
    )


# ---------------------------------------------------------------------------
# Runtime maker config — GET to inspect, POST to update safe fields only.
# Unsafe fields (paper_only, platforms, market_types) are NOT in the update
# request model and are clamped to their safe values on every write.
# Live trading is impossible: there is no order-placement code anywhere.
# ---------------------------------------------------------------------------

# Allowed mutable fields.  Anything not in this set is filtered out before
# applying.  Keep in sync with MakerConfigUpdateRequest.
_SAFE_UPDATE_FIELDS: frozenset[str] = frozenset({
    "enabled",
    "min_estimated_maker_edge",
    "max_book_age_seconds",
    "max_fd_age_seconds",
    "require_min_spread",
    "max_contracts_per_order",
    "max_notional_per_order_usd",
    "max_notional_per_market_usd",
    "max_open_orders_total",
    "max_open_orders_per_market",
    "max_orders_per_hour",
    "max_daily_notional_usd",
    "default_ttl_seconds",
    "cancel_before_event_offset_s",
})


class MakerConfigResponse(BaseModel):
    """Full runtime config view.  ``paper_only`` / ``platforms`` /
    ``market_types`` are surfaced so the caller can see they are locked,
    but they cannot be changed via POST."""
    # Non-finite floats cannot be JSON-serialized; fail at model build time
    # so the POST endpoint can validate before storing.
    model_config = ConfigDict(allow_inf_nan=False)

    enabled: bool
    paper_only: bool
    platforms: list[str]
    market_types: list[str]
    min_estimated_maker_edge: float
    require_min_spread: float
    max_book_age_seconds: float
    max_fd_age_seconds: float
    max_contracts_per_order: int
    max_notional_per_order_usd: float
    max_notional_per_market_usd: float
    max_open_orders_total: int
    max_open_orders_per_market: int
    max_orders_per_hour: int
    max_daily_notional_usd: float
    default_ttl_seconds: int
    cancel_before_event_offset_s: int


class MakerConfigUpdateRequest(BaseModel):
    """Subset of MakerConfig that is safe to mutate at runtime.

    Unsafe fields (``paper_only``, ``platforms``, ``market_types``) are
    intentionally absent.  Pydantic ignores unknown JSON keys by default,
    so a client sending ``paper_only=false`` is silently dropped — and
    the endpoint additionally clamps the resulting config to enforce the
    safety invariants regardless of input.
    """
    # Reject Infinity / NaN / 1e309 at the request layer (422).  A
    # non-finite value would otherwise be stored in the runtime singleton
    # and then fail JSON serialization on every later GET/POST, and an
    # infinite max_book_age_seconds would silently remove the book
    # freshness gate.
    model_config = ConfigDict(allow_inf_nan=False)

    enabled: bool | None = None
    min_estimated_maker_edge: float | None = Field(default=None, ge=0)
    max_book_age_seconds: float | None = Field(default=None, ge=0)
    max_fd_age_seconds: float | None = Field(default=None, ge=0)
    require_min_spread: float | None = Field(default=None, ge=0)
    max_contracts_per_order: int | None = Field(default=None, ge=1)
    max_notional_per_order_usd: float | None = Field(default=None, ge=0)
    max_notional_per_market_usd: float | None = Field(default=None, ge=0)
    max_open_orders_total: int | None = Field(default=None, ge=1)
    max_open_orders_per_market: int | None = Field(default=None, ge=1)
    max_orders_per_hour: int | None = Field(default=None, ge=1)
    max_daily_notional_usd: float | None = Field(default=None, ge=0)
    default_ttl_seconds: int | None = Field(default=None, ge=1)
    cancel_before_event_offset_s: int | None = Field(default=None, ge=0)


def _to_response(cfg: MakerConfig) -> MakerConfigResponse:
    return MakerConfigResponse(
        enabled=cfg.enabled,
        paper_only=cfg.paper_only,
        platforms=list(cfg.platforms),
        market_types=list(cfg.market_types),
        min_estimated_maker_edge=cfg.min_estimated_maker_edge,
        require_min_spread=cfg.require_min_spread,
        max_book_age_seconds=cfg.max_book_age_seconds,
        max_fd_age_seconds=cfg.max_fd_age_seconds,
        max_contracts_per_order=cfg.max_contracts_per_order,
        max_notional_per_order_usd=cfg.max_notional_per_order_usd,
        max_notional_per_market_usd=cfg.max_notional_per_market_usd,
        max_open_orders_total=cfg.max_open_orders_total,
        max_open_orders_per_market=cfg.max_open_orders_per_market,
        max_orders_per_hour=cfg.max_orders_per_hour,
        max_daily_notional_usd=cfg.max_daily_notional_usd,
        default_ttl_seconds=cfg.default_ttl_seconds,
        cancel_before_event_offset_s=cfg.cancel_before_event_offset_s,
    )


def _enforce_safety(cfg: MakerConfig) -> MakerConfig:
    """Force the paper-mode safety invariants regardless of caller input.
    Defense-in-depth: ``MakerConfigUpdateRequest`` already excludes these
    fields, but this clamp guarantees the runtime singleton can never be
    set to an unsafe value via this endpoint."""
    return dataclasses.replace(
        cfg,
        paper_only=True,
        platforms=("kalshi",),
        market_types=("h2h",),
    )


@router.get("/maker/config", response_model=MakerConfigResponse)
def get_maker_config_endpoint() -> MakerConfigResponse:
    """Return the current runtime maker config.  Empty store / no override
    ⇒ disabled paper-only defaults."""
    return _to_response(get_maker_config())


@router.post("/maker/config", response_model=MakerConfigResponse)
def update_maker_config_endpoint(req: MakerConfigUpdateRequest) -> MakerConfigResponse:
    """Update the runtime maker config with safe paper-mode fields only.

    Returns the updated config so the caller can confirm the safety
    invariants (``paper_only=True``, ``platforms=["kalshi"]``,
    ``market_types=["h2h"]``) are still in place.
    """
    if req.enabled:
        ensure_not_demo("the paper maker cannot be enabled on synthetic demo data")
    current = get_maker_config()
    raw = req.model_dump(exclude_unset=True)
    safe_updates = {
        k: v for k, v in raw.items()
        if k in _SAFE_UPDATE_FIELDS and v is not None
    }
    updated = dataclasses.replace(current, **safe_updates)
    updated = _enforce_safety(updated)
    # Build (and thereby validate) the response BEFORE storing, so the
    # endpoint never stores a config it cannot serialize.
    response = _to_response(updated)
    set_maker_config(updated)
    return response


@router.get("/maker/summary", response_model=MakerSummaryResponse)
def maker_summary(
    days: int = Query(1, ge=1, le=90),
    top_n: int = Query(10, ge=1, le=50),
    latest_run: bool = Query(False),
    store: PaperMakerStore = Depends(get_paper_store),
) -> MakerSummaryResponse:
    """Aggregated counts for recent paper proposals.

    ``latest_run=true`` aggregates only the newest scan's records — useful
    for a dashboard showing what the most recent refresh produced.  The
    default (``latest_run=false``) preserves the full audit summary
    semantics across the requested ``days`` window.
    """
    records = store.read_recent(days=days)
    if latest_run:
        records = _filter_to_latest_run(records)

    by_status: Counter[str] = Counter()
    by_platform: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    primary_reasons: Counter[str] = Counter()
    edges: list[float] = []

    # Near-miss diagnostics
    records_with_real_book = 0
    records_with_valid_inside_spread_possible = 0
    rejected_for_edge_only = 0
    closest_edge: float | None = None
    closest_record: ClosestRejectedRecord | None = None

    for rec in records:
        st = rec.get("status", "")
        if st:
            by_status[st] += 1
        plat = rec.get("platform", "")
        if plat:
            by_platform[plat] += 1

        # Real-book / inside-spread-possible counts apply to every record
        # regardless of eligibility — they describe the input the maker
        # pass had to work with, not the verdict.
        has_real_book = _has_real_book(rec)
        if has_real_book:
            records_with_real_book += 1
            bb = float(rec["best_bid"])  # type: ignore[arg-type]
            ba = float(rec["best_ask"])  # type: ignore[arg-type]
            # An inside-spread bid is mathematically possible iff there
            # is at least one tick of room above best_bid below best_ask.
            if bb + _TICK_DOLLARS < ba:
                records_with_valid_inside_spread_possible += 1

        if rec.get("eligible"):
            edge = rec.get("estimated_maker_edge")
            if isinstance(edge, (int, float)):
                edges.append(float(edge))
            continue

        # --- Rejected branch ---
        reasons = [
            r for r in (rec.get("rejection_reasons") or [])
            if isinstance(r, str)
        ]
        for code in reasons:
            rejection_reasons[code] += 1
        # Primary rejection = first failing rule (policy-iteration order),
        # which collapses the multi-reason noise that polluted
        # top_rejection_reasons.
        if reasons:
            primary_reasons[reasons[0]] += 1

        # Edge-only near-miss: real book + at least one edge-only reason +
        # zero structural blockers.  This is the rejected pile that's
        # closest to becoming eligible.
        if has_real_book and reasons:
            reason_set = set(reasons)
            has_edge_reason = bool(reason_set & _EDGE_ONLY_REASONS)
            has_structural = bool(reason_set & _STRUCTURAL_BLOCKERS)
            if has_edge_reason and not has_structural:
                rejected_for_edge_only += 1

        # Closest edge among rejected records that had a real book.
        # Excludes None edges by isinstance guard.
        edge = rec.get("estimated_maker_edge")
        if has_real_book and isinstance(edge, (int, float)):
            edge_f = float(edge)
            if closest_edge is None or edge_f > closest_edge:
                closest_edge = edge_f
                closest_record = ClosestRejectedRecord(
                    side=str(rec.get("side") or rec.get("display_side") or ""),
                    event_label=str(rec.get("event_label") or ""),
                    best_bid=rec.get("best_bid"),
                    best_ask=rec.get("best_ask"),
                    proposed_price=rec.get("proposed_price"),
                    maker_max_bid=rec.get("maker_max_bid"),
                    estimated_maker_edge=edge_f,
                    rejection_reasons=reasons,
                )

    top = [
        TopRejectionReason(reason=code, count=cnt)
        for code, cnt in rejection_reasons.most_common(top_n)
    ]
    primary_top = [
        TopRejectionReason(reason=code, count=cnt)
        for code, cnt in primary_reasons.most_common(top_n)
    ]

    return MakerSummaryResponse(
        days=days,
        total=len(records),
        eligible=by_status.get(STATUS_PAPER_ACTIVE, 0),
        rejected=by_status.get(STATUS_PAPER_REJECTED, 0),
        by_status=dict(by_status),
        by_platform=dict(by_platform),
        top_rejection_reasons=top,
        average_estimated_maker_edge=(
            round(sum(edges) / len(edges), 6) if edges else None
        ),
        records_with_real_book=records_with_real_book,
        records_with_valid_inside_spread_possible=records_with_valid_inside_spread_possible,
        rejected_for_edge_only=rejected_for_edge_only,
        closest_rejected_edge=(
            round(closest_edge, 6) if closest_edge is not None else None
        ),
        closest_rejected_record=closest_record,
        primary_rejection_reasons=primary_top,
    )
