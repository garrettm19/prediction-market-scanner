"""
GET /api/opportunities

Returns matched + priced prediction market x FanDuel opportunities.
Two-pass rule engine: features first, then classify BUY/WATCH/SKIP.
Each row includes full reasoning trace.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from services.demo import is_demo_mode
from services.demo.guards import block_in_demo
from services.engine_config import EngineConfig
from services.opportunities import (
    diagnose_pipeline,
    DEFAULT_CONFIG,
)
from services.price_history import fetch_price_history
from services.odds_provider import fetch_historical_odds
from services.rule_engine import POLICY_TABLE
from services.redaction import redact_secrets
from services.snapshot import (
    get_snapshot,
    refresh_snapshot,
    is_refreshing,
    start_background_refresh,
    get_refresh_state,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class RuleEvaluation(BaseModel):
    rule: str
    passed: bool
    severity: str
    reason: str


class OpportunityOut(BaseModel):
    # Platform & sport
    platform: str
    sport: str

    # Event
    event: str
    event_url: str | None
    tournament: str
    start_time: str

    # Market
    market_id: str
    market_type: str
    side: str
    line: float | None

    # Pricing (single normalised side)
    pm_price: float
    fd_odds: int
    p_true: float
    edge: float
    recommended_kelly: float
    kelly_full: float

    # FanDuel metrics
    fanduel_overround: float
    fanduel_line_width: float
    fanduel_line_width_label: str
    fanduel_confidence_label: str

    # Match quality
    event_match_confidence: float
    match_quality: str

    # Ambiguity
    matched_event_id: str
    second_best_event_id: str
    confidence_gap: float
    competing_matches: int
    has_shared_last_name: bool

    # Classification
    status: str
    reject_reasons: list[str]
    downgrade_reasons: list[str]

    # Data quality
    bid_ask_spread: float | None

    # Staleness
    price_fetched_at: float

    # Observability
    home_tokens: list[str]
    away_tokens: list[str]
    name_match_score: float
    date_score: float
    date_delta_hours: float | None
    rule_evaluations: list[RuleEvaluation]


class PolicyRuleOut(BaseModel):
    rule_name: str
    stage: str
    severity: str
    reason_code: str
    description: str


class OpportunitiesResponse(BaseModel):
    opportunities: list[OpportunityOut]
    total: int
    status_counts: dict[str, int]
    quota_remaining: str | None
    sportsbook_markets_fetched: list[str]
    markets_dropped_by_type: dict[str, int]
    platforms_fetched: list[str]
    updated_at: float | None = None
    is_refreshing: bool = False
    # Additive: "demo" (offline synthetic data) | "live" | None (unknown)
    data_source: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/opportunities", response_model=OpportunitiesResponse)
async def get_opportunities(
    response: Response,
    platform: str | None = Query(
        default=None,
        description="Filter to a single platform (e.g. 'polymarket', 'kalshi')",
    ),
    include_watch: bool = Query(
        default=True,
        description="Include WATCH opportunities in response",
    ),
) -> OpportunitiesResponse:
    """
    Returns opportunities from the cached snapshot.
    On cold start (no snapshot), runs a single pipeline to populate the cache.
    Use POST /refresh to trigger a fresh scan.
    """
    snapshot = get_snapshot()

    if snapshot is None:
        # Cold start — no snapshot yet, run pipeline once with defaults
        try:
            # only_if_missing: concurrent cold-start requests queue on the
            # refresh lock; the first one runs the pipeline and the rest
            # reuse its snapshot instead of re-running it back to back.
            snap = await refresh_snapshot(trigger="cold_start", only_if_missing=True)
            opportunities, meta, updated_at = snap.opportunities, snap.meta, snap.updated_at
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=redact_secrets(exc))
        except Exception as exc:
            logger.error("Opportunities pipeline failed: %s", redact_secrets(exc))
            raise HTTPException(status_code=502, detail="Failed to build opportunities")
    else:
        opportunities = snapshot.opportunities
        meta = snapshot.meta
        updated_at = snapshot.updated_at

    if meta.get("quota_remaining"):
        response.headers["X-Odds-Quota-Remaining"] = str(meta["quota_remaining"])

    opps = opportunities
    if platform:
        opps = [o for o in opps if o.platform == platform]
    if not include_watch:
        opps = [o for o in opps if o.status == "BUY"]

    return OpportunitiesResponse(
        opportunities=[
            OpportunityOut(
                platform=o.platform,
                sport=o.sport,
                event=o.event,
                event_url=o.event_url,
                tournament=o.tournament,
                start_time=o.start_time,
                market_id=o.market_id,
                market_type=o.market_type,
                side=o.side,
                line=o.line,
                pm_price=o.pm_price,
                fd_odds=o.fd_odds,
                p_true=o.p_true,
                edge=o.edge,
                recommended_kelly=o.recommended_kelly,
                kelly_full=o.kelly_full,
                fanduel_overround=o.fanduel_overround,
                fanduel_line_width=o.fanduel_line_width,
                fanduel_line_width_label=o.fanduel_line_width_label,
                fanduel_confidence_label=o.fanduel_confidence_label,
                event_match_confidence=o.event_match_confidence,
                match_quality=o.match_quality,
                matched_event_id=o.matched_event_id,
                second_best_event_id=o.second_best_event_id,
                confidence_gap=o.confidence_gap,
                competing_matches=o.competing_matches,
                has_shared_last_name=o.has_shared_last_name,
                status=o.status,
                reject_reasons=o.reject_reasons,
                downgrade_reasons=o.downgrade_reasons,
                bid_ask_spread=o.bid_ask_spread,
                price_fetched_at=o.price_fetched_at,
                home_tokens=list(o.home_tokens),
                away_tokens=list(o.away_tokens),
                name_match_score=o.name_match_score,
                date_score=o.date_score,
                date_delta_hours=o.date_delta_hours,
                rule_evaluations=[
                    RuleEvaluation(**r) for r in o.rule_evaluations
                ],
            )
            for o in opps
        ],
        total=len(opps),
        status_counts=meta.get("status_counts", {}),
        quota_remaining=meta.get("quota_remaining"),
        sportsbook_markets_fetched=meta.get("sportsbook_markets_fetched", ["h2h"]),
        markets_dropped_by_type=meta.get("markets_dropped_by_type", {}),
        platforms_fetched=meta.get("platforms_fetched", []),
        updated_at=updated_at,
        is_refreshing=is_refreshing(),
        data_source=meta.get("data_source"),
    )


@router.get("/opportunities/snapshot", response_model=OpportunitiesResponse)
async def get_snapshot_opportunities(
    platform: str | None = Query(
        default=None,
        description="Filter to a single platform (e.g. 'polymarket', 'kalshi')",
    ),
    include_watch: bool = Query(
        default=True,
        description="Include WATCH opportunities in response",
    ),
) -> OpportunitiesResponse:
    """
    Read-only snapshot of the latest pipeline result.

    Never triggers pipeline recomputation. Returns 204 if no snapshot exists.
    The scheduler and force_refresh populate the snapshot; this endpoint only reads.
    """
    snapshot = get_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=204)

    opps = snapshot.opportunities
    if platform:
        opps = [o for o in opps if o.platform == platform]
    if not include_watch:
        opps = [o for o in opps if o.status == "BUY"]

    meta = snapshot.meta
    return OpportunitiesResponse(
        opportunities=[
            OpportunityOut(
                platform=o.platform,
                sport=o.sport,
                event=o.event,
                event_url=o.event_url,
                tournament=o.tournament,
                start_time=o.start_time,
                market_id=o.market_id,
                market_type=o.market_type,
                side=o.side,
                line=o.line,
                pm_price=o.pm_price,
                fd_odds=o.fd_odds,
                p_true=o.p_true,
                edge=o.edge,
                recommended_kelly=o.recommended_kelly,
                kelly_full=o.kelly_full,
                fanduel_overround=o.fanduel_overround,
                fanduel_line_width=o.fanduel_line_width,
                fanduel_line_width_label=o.fanduel_line_width_label,
                fanduel_confidence_label=o.fanduel_confidence_label,
                event_match_confidence=o.event_match_confidence,
                match_quality=o.match_quality,
                matched_event_id=o.matched_event_id,
                second_best_event_id=o.second_best_event_id,
                confidence_gap=o.confidence_gap,
                competing_matches=o.competing_matches,
                has_shared_last_name=o.has_shared_last_name,
                status=o.status,
                reject_reasons=o.reject_reasons,
                downgrade_reasons=o.downgrade_reasons,
                bid_ask_spread=o.bid_ask_spread,
                price_fetched_at=o.price_fetched_at,
                home_tokens=list(o.home_tokens),
                away_tokens=list(o.away_tokens),
                name_match_score=o.name_match_score,
                date_score=o.date_score,
                date_delta_hours=o.date_delta_hours,
                rule_evaluations=[
                    RuleEvaluation(**r) for r in o.rule_evaluations
                ],
            )
            for o in opps
        ],
        total=len(opps),
        status_counts=meta.get("status_counts", {}),
        quota_remaining=meta.get("quota_remaining"),
        sportsbook_markets_fetched=meta.get("sportsbook_markets_fetched", ["h2h"]),
        markets_dropped_by_type=meta.get("markets_dropped_by_type", {}),
        platforms_fetched=meta.get("platforms_fetched", []),
        updated_at=snapshot.updated_at,
        is_refreshing=is_refreshing(),
        data_source=meta.get("data_source"),
    )


class SnapshotStatusResponse(BaseModel):
    has_snapshot: bool
    updated_at: float | None
    is_refreshing: bool
    refresh_started_at: float | None
    last_refresh_error: str | None
    last_refresh_duration_seconds: float | None
    trigger: str | None                # most recent successful snapshot trigger
    last_trigger: str | None           # most recent attempted trigger (success or failure)
    opportunity_count: int
    status_counts: dict[str, int]
    platforms_fetched: list[str]
    # Additive: per-sport Odds API failures from the last successful snapshot
    # (fetch_odds meta["sports_failed"]).  degraded=True means at least one
    # sport's FanDuel odds could not be refreshed, so its events are absent
    # (expired odds are evicted, never served as truth).
    odds_failures: list[dict] = []
    degraded: bool = False
    # Additive: True when the backend runs in offline demo mode
    # (MASG_DEMO_MODE=1) -- synthetic data, no external requests.
    demo_mode: bool = False
    # Additive: meta["data_source"] of the stored snapshot ("demo" | "live").
    data_source: str | None = None


def _build_status_response() -> SnapshotStatusResponse:
    """Compose the lightweight status payload from current module state."""
    snapshot = get_snapshot()
    rstate = get_refresh_state()
    if snapshot is None:
        return SnapshotStatusResponse(
            has_snapshot=False,
            updated_at=None,
            is_refreshing=rstate["is_refreshing"],
            refresh_started_at=rstate["refresh_started_at"],
            last_refresh_error=rstate["last_refresh_error"],
            last_refresh_duration_seconds=rstate["last_refresh_duration_seconds"],
            trigger=None,
            last_trigger=rstate["last_trigger"],
            opportunity_count=0,
            status_counts={},
            platforms_fetched=[],
            demo_mode=is_demo_mode(),
        )
    return SnapshotStatusResponse(
        has_snapshot=True,
        updated_at=snapshot.updated_at,
        is_refreshing=rstate["is_refreshing"],
        refresh_started_at=rstate["refresh_started_at"],
        last_refresh_error=rstate["last_refresh_error"],
        last_refresh_duration_seconds=rstate["last_refresh_duration_seconds"],
        trigger=snapshot.trigger,
        last_trigger=rstate["last_trigger"],
        opportunity_count=len(snapshot.opportunities),
        status_counts=snapshot.meta.get("status_counts", {}),
        platforms_fetched=snapshot.meta.get("platforms_fetched", []),
        odds_failures=list(snapshot.meta.get("sports_failed") or []),
        degraded=bool(snapshot.meta.get("sports_failed")),
        demo_mode=is_demo_mode(),
        data_source=snapshot.meta.get("data_source"),
    )


@router.get("/opportunities/status", response_model=SnapshotStatusResponse)
async def get_snapshot_status() -> SnapshotStatusResponse:
    """
    Lightweight status of the cached snapshot.

    Never triggers pipeline recomputation. Designed for frontend polling
    to check whether data is fresh or a refresh is in progress.
    """
    return _build_status_response()


class RefreshRequest(BaseModel):
    scope: str = "stale"   # "all" | "stale" | sport config key (e.g. "tennis")


@router.post(
    "/opportunities/refresh",
    response_model=SnapshotStatusResponse,
    status_code=202,
)
async def refresh_opportunities(
    req: RefreshRequest | None = None,
) -> SnapshotStatusResponse:
    """
    Spawn a background pipeline refresh and return current status immediately.

    Body (optional):
      {"scope": "stale"}   — only re-fetch sports with expired TTL (default, cheapest)
      {"scope": "all"}     — invalidate all cached odds, fetch everything fresh
      {"scope": "tennis"}  — invalidate one sport, fetch it fresh

    Always returns 202 with the current SnapshotStatusResponse. If a refresh
    is already running, this call is a no-op (no duplicate refresh is
    spawned) and the response reflects the in-flight refresh.

    The frontend should poll GET /opportunities/status until updated_at
    changes, then GET /opportunities/snapshot to read the new data.

    Does not affect the monitor scheduler — the scheduler continues
    on its own cadence independently.
    """
    scope = req.scope if req else "stale"
    await start_background_refresh(trigger="manual", scope=scope)
    # Always 202: whether we spawned a new refresh or one was already running,
    # the request was accepted; clients poll /status for completion.
    return _build_status_response()


@router.get("/opportunities/policy", response_model=list[PolicyRuleOut])
async def get_policy_table() -> list[PolicyRuleOut]:
    """Return the current rule policy table."""
    return [
        PolicyRuleOut(
            rule_name=r.rule_name,
            stage=r.stage,
            severity=r.severity,
            reason_code=r.reason_code,
            description=r.description,
        )
        for r in POLICY_TABLE
    ]


@router.get("/opportunities/history")
async def get_price_history(
    platform: str = Query(..., description="Platform name (polymarket, kalshi)"),
    market_id: str = Query(..., description="Platform-specific market ID or ticker"),
) -> dict:
    """Fetch price history for a specific market.

    Demo mode: returns no points (demo markets are synthetic and have no
    Kalshi / Polymarket history; no external call is made).
    """
    if is_demo_mode():
        return {"platform": platform, "market_id": market_id, "points": [], "count": 0}
    points = await fetch_price_history(platform, market_id)
    return {
        "platform": platform,
        "market_id": market_id,
        "points": [{"t": p.timestamp, "p": round(p.price, 4)} for p in points],
        "count": len(points),
    }


@router.get(
    "/opportunities/historical-odds",
    dependencies=[Depends(block_in_demo("historical odds require The Odds API (paid request)"))],
)
async def get_historical_odds(
    sport_key: str = Query(..., description="Odds API sport key (e.g. 'icehockey_nhl')"),
    event_id: str = Query(..., description="Odds API event ID"),
    date: str = Query(..., description="ISO-8601 timestamp — returns closest snapshot at or before this time"),
) -> dict:
    """Fetch historical FanDuel odds snapshot for CLV calculation."""
    snapshot = await fetch_historical_odds(sport_key, event_id, date)
    if snapshot is None:
        return {"snapshot": None}
    return {
        "snapshot": {
            "event_id": snapshot.event_id,
            "home_team": snapshot.home_team,
            "away_team": snapshot.away_team,
            "home_odds": snapshot.home_odds,
            "away_odds": snapshot.away_odds,
            "draw_odds": snapshot.draw_odds,
            "snapshot_time": snapshot.snapshot_time,
            "home_implied": snapshot.home_implied,
            "away_implied": snapshot.away_implied,
        },
    }


@router.get("/opportunities/debug")
async def debug_opportunities(
    min_edge: float = Query(default=DEFAULT_CONFIG.min_edge, ge=0.0, le=1.0),
    wide_market_min_edge: float = Query(default=DEFAULT_CONFIG.wide_market_min_edge, ge=0.0, le=1.0),
    survival_threshold: float = Query(default=DEFAULT_CONFIG.survival_threshold, ge=0.0, le=1.0),
    buy_threshold: float = Query(default=DEFAULT_CONFIG.buy_threshold, ge=0.0, le=1.0),
) -> dict:
    """Diagnostic endpoint — traces each pipeline stage with full rule evaluations."""
    cfg = EngineConfig(
        min_edge=min_edge,
        wide_market_min_edge=wide_market_min_edge,
        survival_threshold=survival_threshold,
        buy_threshold=buy_threshold,
    )
    try:
        if is_demo_mode():
            # Same diagnostic pipeline on the synthetic demo inputs (all demo
            # platforms, mirroring diagnose_pipeline's unfiltered default).
            from services.demo.fixtures import demo_adapters, make_demo_odds_fetcher
            result = await diagnose_pipeline(
                cfg=cfg,
                adapters=demo_adapters(),
                odds_fetcher=make_demo_odds_fetcher(),
            )
            result["data_source"] = "demo"
            return result
        return await diagnose_pipeline(cfg=cfg)
    except Exception as exc:
        logger.error("Debug pipeline failed: %s", redact_secrets(exc))
        raise HTTPException(status_code=500, detail=redact_secrets(exc))
