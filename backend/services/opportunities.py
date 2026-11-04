"""
Opportunities pipeline v2: Two-pass rule engine.

Platform-agnostic engine.  Accepts NormalizedMarket objects from any adapter
(Polymarket, Kalshi, etc.) and compares them against FanDuel truth odds.

Pipeline
--------
  Pass A — Feature Extraction
    1. Fetch markets from all adapters + FanDuel odds in parallel.
    2. Cross-match every (market, event) pair by player names.
    3. Compute ALL features for each candidate without rejecting.

  Pass B — Rule Engine
    4. Evaluate every rule in the POLICY_TABLE against each candidate.
    5. Classify: BUY / WATCH / SKIP with full reasoning trace.
    6. Compute Kelly sizing for positive-edge candidates.
    7. Deduplicate by (platform, event, market_type, side, line).

Read-only.  No trade execution.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Awaitable, Callable

from services.adapters import MarketAdapter, NormalizedMarket, PolymarketAdapter, KalshiAdapter
from services.engine_config import EngineConfig
from services.feature_extractor import extract_features
from services.maker.config import MakerConfig, get_maker_config
from services.matcher import SUPPORTED_SPORTSBOOK_MARKET_TYPES
from services.normalizer import last_name
from services.odds_provider import fetch_odds, fetch_props
from services.prop_extractor import extract_prop_features
from services.rule_engine import (
    MarketFeatures,
    RuleResult,
    evaluate_rules,
    compute_kelly,
    POLICY_TABLE,
)

logger = logging.getLogger(__name__)

_BOOKMAKER = "fanduel"

# Re-export for backward compatibility with the router
DEFAULT_CONFIG = EngineConfig()

# Default adapter set
DEFAULT_ADAPTERS: list[MarketAdapter] = [PolymarketAdapter(), KalshiAdapter()]

# Signature of an injectable FanDuel odds source: same keyword arguments and
# return shape as ``services.odds_provider.fetch_odds``.  Only the offline demo
# mode (services/demo) passes one; ``None`` means the real fetch_odds call.
OddsFetcher = Callable[..., Awaitable[tuple[list, dict]]]


# ---------------------------------------------------------------------------
# Series dedup — keep only closest-date match per market
# ---------------------------------------------------------------------------

def _dedup_to_best_date_match(features: list[MarketFeatures]) -> list[MarketFeatures]:
    """
    When multiple sportsbook events match the same prediction market
    (e.g., Game 1 and Game 2 of a series), keep only the features from
    the closest-date event. This prevents cross-game price mismatches.

    Groups by (platform, market_id) and for each group, finds the event_id
    with the best date_score. Features from other events are dropped.
    """
    # Group by (platform, market_id) → {event_id: best_date_score}
    market_events: dict[tuple[str, str], dict[str, float]] = {}
    for f in features:
        key = (f.platform, f.market_id)
        market_events.setdefault(key, {})
        eid = f.matched_event_id
        if eid not in market_events[key] or f.date_score > market_events[key][eid]:
            market_events[key][eid] = f.date_score

    # For each market, find the best event_id (highest date_score)
    best_event: dict[tuple[str, str], str] = {}
    for key, events in market_events.items():
        if len(events) <= 1:
            # Only one event matched — no series dedup needed
            best_event[key] = next(iter(events))
            continue
        # Multiple events: keep the one with the best date_score
        best_eid = max(events, key=events.get)
        best_event[key] = best_eid
        logger.info(
            "Series dedup: market %s matched %d events, keeping %s (date_score=%.2f)",
            key, len(events), best_eid, events[best_eid],
        )

    # Filter features to only keep the best event per market
    result: list[MarketFeatures] = []
    for f in features:
        key = (f.platform, f.market_id)
        if key not in best_event or f.matched_event_id == best_event[key]:
            result.append(f)

    if len(result) < len(features):
        logger.info(
            "Series dedup: %d features → %d (dropped %d cross-game matches)",
            len(features), len(result), len(features) - len(result),
        )

    return result


# ---------------------------------------------------------------------------
# Ambiguity enrichment
# ---------------------------------------------------------------------------

def _enrich_ambiguity_metrics(features: list[MarketFeatures]) -> None:
    """
    For each market_id, compute how many events matched and the confidence gap.

    Groups features by (platform, market_id) and finds all distinct matched
    event_ids.  Then for each feature, sets:
      - competing_matches: how many distinct events matched this market
      - second_best_confidence: confidence of the runner-up event
      - confidence_gap: best - second_best
      - second_best_event_id: the runner-up event id
      - has_shared_last_name: whether another matched event shares a last name
    """
    # Group by (platform, market_id) → list of (event_id, confidence, features)
    market_events: dict[tuple[str, str], list[tuple[str, float, MarketFeatures]]] = {}
    for f in features:
        key = (f.platform, f.market_id)
        market_events.setdefault(key, []).append(
            (f.matched_event_id, f.event_match_confidence, f)
        )

    for key, entries in market_events.items():
        # Deduplicate by event_id, keep highest confidence per event
        best_per_event: dict[str, float] = {}
        last_names_per_event: dict[str, set[str]] = {}
        for event_id, conf, f in entries:
            if event_id not in best_per_event or conf > best_per_event[event_id]:
                best_per_event[event_id] = conf
            # Collect last names for this event
            if event_id not in last_names_per_event:
                last_names_per_event[event_id] = set()
            last_names_per_event[event_id].add(last_name(f.home_player_norm))
            last_names_per_event[event_id].add(last_name(f.away_player_norm))

        sorted_events = sorted(best_per_event.items(), key=lambda x: x[1], reverse=True)
        num_competing = len(sorted_events)

        top_event_id = sorted_events[0][0] if sorted_events else ""
        top_conf = sorted_events[0][1] if sorted_events else 0.0
        second_event_id = sorted_events[1][0] if len(sorted_events) > 1 else ""
        second_conf = sorted_events[1][1] if len(sorted_events) > 1 else 0.0
        gap = round(top_conf - second_conf, 4) if len(sorted_events) > 1 else 1.0

        # Shared last name detection: does any other event share a player last name?
        shared_names: dict[str, bool] = {}
        for event_id, names in last_names_per_event.items():
            has_shared = False
            for other_id, other_names in last_names_per_event.items():
                if other_id != event_id and names & other_names:
                    has_shared = True
                    break
            shared_names[event_id] = has_shared

        # Apply to all features for this market
        for event_id, conf, f in entries:
            f.competing_matches = num_competing
            f.best_match_confidence = top_conf
            if event_id == top_event_id:
                f.second_best_confidence = second_conf
                f.second_best_event_id = second_event_id
                f.confidence_gap = gap
            else:
                # This feature is for a non-top event
                f.second_best_confidence = top_conf
                f.second_best_event_id = top_event_id
                f.confidence_gap = round(top_conf - conf, 4)
            f.has_shared_last_name = shared_names.get(event_id, False)


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

@dataclass
class EvaluatedOpportunity:
    """Full evaluation result for one market candidate."""

    # Identity
    platform: str
    sport: str
    event: str
    event_url: str | None
    tournament: str
    start_time: str
    market_id: str
    market_type: str
    side: str
    line: float | None

    # Pricing (single normalised side)
    pm_price: float
    fd_odds: int
    p_true: float
    edge: float

    # Kelly
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
    status: str                        # BUY | WATCH | SKIP
    reject_reasons: list[str]          # CRITICAL failures
    downgrade_reasons: list[str]       # DOWNGRADE failures

    # Observability / audit trail
    home_tokens: tuple[str, ...]
    away_tokens: tuple[str, ...]
    name_match_score: float
    date_score: float
    date_delta_hours: float | None
    rule_evaluations: list[dict]       # full rule-by-rule trace

    # Data quality
    bid_ask_spread: float | None = None  # yes_ask - yes_bid; None if unavailable

    # Staleness tracking
    price_fetched_at: float = 0.0      # when platform price was obtained


def _features_to_opportunity(
    f: MarketFeatures,
    rule_results: list[RuleResult],
) -> EvaluatedOpportunity:
    return EvaluatedOpportunity(
        platform=f.platform,
        sport=f.sport,
        event=f.event_label,
        event_url=f.event_url,
        tournament=f.tournament,
        start_time=f.start_time,
        market_id=f.market_id,
        market_type=f.market_type,
        side=f.side,
        line=f.line,
        pm_price=f.pm_price,
        fd_odds=f.fd_odds,
        p_true=f.p_true,
        edge=f.edge,
        recommended_kelly=f.kelly_fraction,
        kelly_full=f.kelly_full,
        fanduel_overround=f.fanduel_overround,
        fanduel_line_width=f.fanduel_line_width,
        fanduel_line_width_label=f.fanduel_line_width_label,
        fanduel_confidence_label=f.fanduel_confidence_label,
        event_match_confidence=f.event_match_confidence,
        match_quality=f.match_quality,
        matched_event_id=f.matched_event_id,
        second_best_event_id=f.second_best_event_id,
        confidence_gap=f.confidence_gap,
        competing_matches=f.competing_matches,
        has_shared_last_name=f.has_shared_last_name,
        status=f.status,
        reject_reasons=f.reject_reasons,
        downgrade_reasons=f.downgrade_reasons,
        bid_ask_spread=f.bid_ask_spread,
        price_fetched_at=f.price_fetched_at,
        home_tokens=f.home_tokens,
        away_tokens=f.away_tokens,
        name_match_score=f.name_match_score,
        date_score=f.date_score,
        date_delta_hours=f.date_delta_hours,
        rule_evaluations=[
            {
                "rule": r.rule_name,
                "passed": r.passed,
                "severity": r.severity,
                "reason": r.reason_code,
            }
            for r in rule_results
        ],
    )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_key(opp: EvaluatedOpportunity) -> tuple:
    return (opp.platform, opp.event, opp.market_type, opp.side, opp.line)


def _deduplicate(opps: list[EvaluatedOpportunity]) -> list[EvaluatedOpportunity]:
    best: dict[tuple, EvaluatedOpportunity] = {}
    for opp in opps:
        key = _dedup_key(opp)
        if key not in best or opp.edge > best[key].edge:
            best[key] = opp
    return list(best.values())


# ---------------------------------------------------------------------------
# Maker pass (paper-only, gated by MakerConfig.enabled)
# ---------------------------------------------------------------------------

async def _run_maker_pass(
    features: list[MarketFeatures],
    engine_cfg: EngineConfig,
    maker_cfg: MakerConfig,
    *,
    store=None,
) -> dict:
    """
    Generate paper maker proposals from Pass A features.

    Architectural invariant: this consumes ``MarketFeatures`` directly, NOT
    ``EvaluatedOpportunity``.  It runs after Pass B (so taker status/reasons
    are populated for audit) but BEFORE the active SKIP filter (so taker
    SKIPs for ``NO_EDGE`` — exactly the candidates maker is designed to
    rescue — remain visible).

    Book source: top-of-book from the Kalshi market-list adapter, carried
    on ``MarketFeatures.best_bid`` / ``best_ask`` / ``price_fetched_at``.
    No per-feature HTTP call to ``/markets/{ticker}/orderbook`` is made —
    that endpoint frequently returned empty bodies in live testing, while
    market-list TOB is already in hand from the adapter.

    Disabled-path fast-exit: when ``maker_cfg.enabled`` is False this is
    a constant-time no-op returning a small meta dict.  No maker imports,
    no JSONL writes.

    The ``store`` kwarg exists for test injection only; production calls
    pass nothing and the default file-backed store is used.
    """
    if not maker_cfg.enabled:
        return {
            "maker_enabled": False,
            "maker_proposals_total": 0,
            "maker_proposals_eligible": 0,
            "maker_proposals_rejected": 0,
        }

    # Lazy imports — keep maker out of the import path when disabled.
    import uuid
    from services.maker.planner import MakerBookInput
    from services.maker.policy import inherited_taker_rules
    from services.maker.service import MakerService
    from services.maker.state import STATUS_PAPER_ACTIVE

    service = MakerService(engine_cfg=engine_cfg, maker_cfg=maker_cfg, store=store)
    inherited_rules = inherited_taker_rules()
    eligible_count = 0
    rejected_count = 0
    empty_book = MakerBookInput(
        best_bid=None, best_ask=None, fetched_at=0.0,
        source="market_list_top_of_book",
    )
    # One UUID per refresh — every proposal persisted in this pass shares it
    # so the /api/maker/proposals?latest_run=true filter can narrow to a
    # single scan's audit data.
    run_id = str(uuid.uuid4())

    for f in features:
        # Scope filter — out-of-scope features are silently ignored (no record,
        # no I/O).  Polymarket and non-H2H fall here.
        if f.platform not in maker_cfg.platforms:
            continue
        if f.market_type not in maker_cfg.market_types:
            continue

        # Safety pre-filter — features failing any inherited taker safety
        # rule are persisted as paper_rejected with an empty book.  The two
        # excluded rules (positive_edge, edge_threshold) are NOT in this
        # list, so a taker SKIP for NO_EDGE still passes here and proceeds
        # to TOB-based maker planning.
        safety_ok = all(rule.condition_fn(f, engine_cfg) for rule in inherited_rules)
        if not safety_ok:
            service.propose(features=f, book=empty_book, run_id=run_id)
            rejected_count += 1
            continue

        # Build the maker book input directly from MarketFeatures.  No
        # network call — best_bid / best_ask were populated by the Kalshi
        # adapter from the market-list top-of-book fields.
        book = MakerBookInput(
            best_bid=f.best_bid,
            best_ask=f.best_ask,
            fetched_at=f.price_fetched_at,
            source="market_list_top_of_book",
        )

        outcome = service.propose(features=f, book=book, run_id=run_id)
        if outcome.status == STATUS_PAPER_ACTIVE:
            eligible_count += 1
        else:
            rejected_count += 1

    return {
        "maker_enabled": True,
        "maker_proposals_total": eligible_count + rejected_count,
        "maker_proposals_eligible": eligible_count,
        "maker_proposals_rejected": rejected_count,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_opportunities(
    cfg: EngineConfig = DEFAULT_CONFIG,
    adapters: list[MarketAdapter] | None = None,
    maker_cfg: MakerConfig | None = None,
    *,
    odds_fetcher: OddsFetcher | None = None,
) -> tuple[list[EvaluatedOpportunity], dict]:
    """
    Full two-pass pipeline:
      Pass A: fetch → match → extract features
      Pass B: evaluate rules → classify → Kelly → deduplicate → sort

    ``odds_fetcher`` replaces the FanDuel source (default: the real
    ``fetch_odds``).  It is called with the same keyword arguments and must
    return the same ``(events, meta)`` shape.  Used by the offline demo mode
    only; everything downstream of the fetch is the unchanged pipeline.
    """
    if adapters is None:
        # Filter DEFAULT_ADAPTERS by ScanConfig.platforms toggles.  Default
        # ScanConfig has Kalshi enabled, Polymarket disabled — so a default
        # scan never invokes PolymarketAdapter.fetch_markets unless the
        # platform is explicitly enabled via /api/scan/config.
        from services.scan_config import get_scan_config
        scan_cfg = get_scan_config()
        adapters = [
            a for a in DEFAULT_ADAPTERS
            if scan_cfg.platforms.get(a.platform_name) is None
            or scan_cfg.platforms[a.platform_name].enabled
        ]

    # --- Fetch all sources in parallel ---
    fetch_tasks = [a.fetch_markets() for a in adapters]
    _odds = odds_fetcher if odds_fetcher is not None else fetch_odds
    fetch_tasks.append(_odds(bookmaker=_BOOKMAKER, max_sports=25))
    if cfg.enable_props:
        fetch_tasks.append(fetch_props(bookmaker=_BOOKMAKER))

    adapter_results = await asyncio.gather(*fetch_tasks)

    # Unpack: adapters first, then h2h odds, then optionally props
    n_adapters = len(adapters)
    odds_events, meta = adapter_results[n_adapters]
    all_markets: list[NormalizedMarket] = []
    for adapter_markets in adapter_results[:n_adapters]:
        all_markets.extend(adapter_markets)

    prop_events = []
    if cfg.enable_props and len(adapter_results) > n_adapters + 1:
        prop_events, prop_meta = adapter_results[n_adapters + 1]
        meta["props_fetched"] = prop_meta.get("props_fetched", 0)
        meta["props_events"] = prop_meta.get("props_events", 0)

    # --- Pass A: H2H Feature Extraction ---
    all_features: list[MarketFeatures] = []
    for market in all_markets:
        for event in odds_events:
            features = extract_features(market, event, cfg)
            all_features.extend(features)

    logger.info(
        "Pass A (h2h): %d markets × %d events → %d feature candidates",
        len(all_markets), len(odds_events), len(all_features),
    )

    # --- Pass A (props): extract prop features separately ---
    prop_feature_count = 0
    if cfg.enable_props and prop_events:
        for prop_event in prop_events:
            event_prop_count = 0
            for market in all_markets:
                if event_prop_count >= cfg.max_props_per_event:
                    break
                prop_feats = extract_prop_features(market, prop_event, cfg)
                all_features.extend(prop_feats)
                event_prop_count += len(prop_feats)
                prop_feature_count += len(prop_feats)
        logger.info(
            "Pass A (props): %d events × %d markets → %d prop features (cap=%d/event)",
            len(prop_events), len(all_markets), prop_feature_count, cfg.max_props_per_event,
        )

    # --- Alt-demand measurement: count totals candidates with no matching FD line ---
    _alt_raw: dict[str, int] = {}
    _alt_actionable: dict[str, int] = {}
    for f in all_features:
        if f.market_type == "totals" and not f.line_match_exact:
            _alt_raw[f.sport] = _alt_raw.get(f.sport, 0) + 1
            if f.has_bookmaker_data and f.line is not None:
                _alt_actionable[f.sport] = _alt_actionable.get(f.sport, 0) + 1
    if _alt_raw:
        parts = " ".join(f"{k}={v}" for k, v in sorted(_alt_raw.items()))
        logger.info("[alt-demand] raw: %s total=%d", parts, sum(_alt_raw.values()))
    if _alt_actionable:
        parts = " ".join(f"{k}={v}" for k, v in sorted(_alt_actionable.items()))
        logger.info("[alt-demand] actionable: %s total=%d", parts, sum(_alt_actionable.values()))

    # --- Series dedup FIRST: when multiple FD events match the same platform
    #     market, keep only the closest-date match to prevent cross-game
    #     mismatches in multi-game series (MLB 3-game, NBA back-to-back, ...).
    #     Must run BEFORE ambiguity enrichment so competing_matches reflects
    #     the actual surviving candidate set, not the pre-dedup raw count. ---
    all_features = _dedup_to_best_date_match(all_features)

    # --- Ambiguity enrichment (between Pass A and B) ---
    _enrich_ambiguity_metrics(all_features)

    # --- Pass B: Rule Engine ---
    all_evaluated: list[EvaluatedOpportunity] = []
    status_counts = {"BUY": 0, "WATCH": 0, "SKIP": 0}

    for f in all_features:
        status, rule_results = evaluate_rules(f, cfg)
        compute_kelly(f, cfg)

        opp = _features_to_opportunity(f, rule_results)
        all_evaluated.append(opp)
        status_counts[status] = status_counts.get(status, 0) + 1

    # --- Maker pass (paper-only, disabled by default) ---
    # Runs AFTER Pass B (so taker status/reasons are populated for audit) but
    # BEFORE the active SKIP filter (so taker SKIPs for NO_EDGE — the candidates
    # maker is designed to rescue — remain visible).  When maker_cfg.enabled
    # is False this is a constant-time no-op.
    #
    # When the caller passes ``maker_cfg=None`` (the default for the normal
    # refresh path), the runtime config is consulted at call time.  Tests
    # and the controlled in-process test pass typically pass an explicit
    # config to bypass the runtime singleton.
    if maker_cfg is None:
        maker_cfg = get_maker_config()
    maker_meta = await _run_maker_pass(all_features, cfg, maker_cfg)

    # --- Deduplicate and sort ---
    # Only deduplicate non-SKIP opportunities (SKIP are kept in full log but not returned)
    active = [o for o in all_evaluated if o.status != "SKIP"]
    active = _deduplicate(active)
    active.sort(key=lambda o: o.edge, reverse=True)

    platforms_fetched = [a.platform_name for a in adapters]

    dropped_by_type: dict[str, int] = {}
    kalshi_series_counts: dict[str, dict[str, int]] = {}
    for adapter in adapters:
        if hasattr(adapter, "dropped_by_type"):
            for k, v in adapter.dropped_by_type.items():
                dropped_by_type[k] = dropped_by_type.get(k, 0) + v
        if hasattr(adapter, "series_counts"):
            kalshi_series_counts.update(adapter.series_counts)

    logger.info(
        "Pass B: %d candidates → BUY=%d WATCH=%d SKIP=%d → %d after dedup",
        len(all_features),
        status_counts["BUY"],
        status_counts["WATCH"],
        status_counts["SKIP"],
        len(active),
    )

    meta["opportunities_count"] = len(active)
    meta["sportsbook_markets_fetched"] = list(SUPPORTED_SPORTSBOOK_MARKET_TYPES)
    meta["markets_dropped_by_type"] = dropped_by_type
    meta["platforms_fetched"] = platforms_fetched
    meta["status_counts"] = status_counts
    meta["kalshi_series_counts"] = kalshi_series_counts
    if cfg.enable_props:
        meta["prop_features"] = prop_feature_count
    meta.update(maker_meta)

    return active, meta


# ---------------------------------------------------------------------------
# Diagnostic — traces every stage with full reasoning
# ---------------------------------------------------------------------------

async def diagnose_pipeline(
    cfg: EngineConfig = DEFAULT_CONFIG,
    adapters: list[MarketAdapter] | None = None,
    *,
    odds_fetcher: OddsFetcher | None = None,
) -> dict:
    """Run the full pipeline and collect diagnostic info at every stage.

    ``odds_fetcher`` has the same meaning as in ``fetch_opportunities``.
    """
    if adapters is None:
        adapters = DEFAULT_ADAPTERS

    _odds = odds_fetcher if odds_fetcher is not None else fetch_odds
    adapter_results = await asyncio.gather(
        *[a.fetch_markets() for a in adapters],
        _odds(bookmaker=_BOOKMAKER, max_sports=25),
    )

    odds_events, meta = adapter_results[-1]
    all_markets: list[NormalizedMarket] = []
    for adapter_markets in adapter_results[:-1]:
        all_markets.extend(adapter_markets)

    dropped_by_type: dict[str, int] = {}
    for adapter in adapters:
        if hasattr(adapter, "dropped_by_type"):
            for k, v in adapter.dropped_by_type.items():
                dropped_by_type[k] = dropped_by_type.get(k, 0) + v

    platform_counts: dict[str, int] = {}
    for m in all_markets:
        platform_counts[m.platform] = platform_counts.get(m.platform, 0) + 1

    # Pass A: extract all features
    all_features: list[MarketFeatures] = []
    for market in all_markets:
        for event in odds_events:
            features = extract_features(market, event, cfg)
            all_features.extend(features)

    # Ambiguity enrichment
    _enrich_ambiguity_metrics(all_features)

    # Pass B: evaluate rules
    evaluated: list[dict] = []
    status_counts = {"BUY": 0, "WATCH": 0, "SKIP": 0}

    for f in all_features:
        status, rule_results = evaluate_rules(f, cfg)
        compute_kelly(f, cfg)
        status_counts[status] = status_counts.get(status, 0) + 1

        evaluated.append({
            "platform": f.platform,
            "market_id": f.market_id,
            "event": f.event_label,
            "side": f.side,
            "market_type": f.market_type,
            "pm_price": f.pm_price,
            "fd_odds": f.fd_odds,
            "p_true": f.p_true,
            "edge": f.edge,
            "event_match_confidence": f.event_match_confidence,
            "fanduel_line_width": f.fanduel_line_width,
            "fanduel_confidence_label": f.fanduel_confidence_label,
            "matched_event_id": f.matched_event_id,
            "second_best_event_id": f.second_best_event_id,
            "confidence_gap": f.confidence_gap,
            "competing_matches": f.competing_matches,
            "has_shared_last_name": f.has_shared_last_name,
            "home_tokens": list(f.home_tokens),
            "away_tokens": list(f.away_tokens),
            "name_match_score": f.name_match_score,
            "date_score": f.date_score,
            "date_delta_hours": f.date_delta_hours,
            "home_last_name_collision": f.home_last_name_collision,
            "away_last_name_collision": f.away_last_name_collision,
            "has_end_date": f.has_end_date,
            "prices_consistent": f.prices_internally_consistent,
            "status": status,
            "reject_reasons": f.reject_reasons,
            "downgrade_reasons": f.downgrade_reasons,
            "kelly_fraction": f.kelly_fraction,
            "kelly_full": f.kelly_full,
            "rules": [
                {
                    "rule": r.rule_name,
                    "passed": r.passed,
                    "severity": r.severity,
                    "reason": r.reason_code,
                }
                for r in rule_results
            ],
        })

    fd_sample = [
        {
            "event_id": ev.event_id,
            "event": f"{ev.home_player} vs {ev.away_player}",
            "tournament": ev.tournament,
            "commence_time": ev.commence_time,
            "bookmaker_count": len(ev.bookmakers),
        }
        for ev in odds_events[:20]
    ]

    return {
        "config": {
            "min_edge": cfg.min_edge,
            "wide_market_min_edge": cfg.wide_market_min_edge,
            "min_true_probability": cfg.min_true_probability,
            "survival_threshold": cfg.survival_threshold,
            "buy_threshold": cfg.buy_threshold,
            "max_line_width_for_normal_threshold": cfg.max_line_width_for_normal_threshold,
            "cost_buffer": cfg.cost_buffer,
            "kelly_cap": cfg.kelly_cap,
            "kelly_fraction": cfg.kelly_fraction,
        },
        "policy_table": [
            {
                "rule_name": r.rule_name,
                "stage": r.stage,
                "severity": r.severity,
                "reason_code": r.reason_code,
                "description": r.description,
            }
            for r in POLICY_TABLE
        ],
        "stage_1_fetch": {
            "platforms": platform_counts,
            "total_markets": len(all_markets),
            "fd_events": len(odds_events),
            "fd_sports_fetched": meta.get("sports_fetched", []),
            "dropped_by_type": dropped_by_type,
        },
        "pass_a_features": {
            "total_candidates": len(all_features),
        },
        "pass_b_classification": {
            "status_counts": status_counts,
            "evaluated": evaluated[:100],  # cap for response size
        },
        "fd_events_sample": fd_sample,
    }
