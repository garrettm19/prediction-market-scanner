"""
Read-only odds endpoints.

GET /api/odds/tennis
  Normalised H2H tennis odds from The Odds API.

GET /api/odds/matches
  Cross-matches those odds against live Polymarket tennis markets and
  returns only high-confidence pairs (default threshold: 0.70).
"""
import dataclasses
import logging
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from services.redaction import redact_secrets
from services.odds_provider import fetch_tennis_odds
from services.events import fetch_tennis_markets
from services.matcher import match_markets, MIN_CONFIDENCE

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared response models
# ---------------------------------------------------------------------------

class BookmakerLineOut(BaseModel):
    bookmaker_key: str
    bookmaker_title: str
    home_odds: int
    away_odds: int
    last_update: str


class TennisOddsEventOut(BaseModel):
    event_id: str
    sport_key: str
    tournament: str
    home_player: str
    away_player: str
    home_player_norm: str
    away_player_norm: str
    commence_time: str
    bookmakers: list[BookmakerLineOut]
    consensus_home_odds: int | None
    consensus_away_odds: int | None
    home_implied: float | None
    away_implied: float | None


class OddsResponse(BaseModel):
    events: list[TennisOddsEventOut]
    total: int
    sports_fetched: list[str]
    quota_remaining: str | None
    quota_used: str | None


class MatchedEventOut(BaseModel):
    """Odds event summary embedded in a match result (no bookmaker list)."""
    event_id: str
    sport_key: str
    tournament: str
    home_player: str
    away_player: str
    commence_time: str
    consensus_home_odds: int | None
    consensus_away_odds: int | None
    home_implied: float | None
    away_implied: float | None
    bookmakers_count: int


class MatchResultOut(BaseModel):
    market_id: str
    market_question: str
    market_end_date: str | None
    confidence: float
    matched_players: list[str]
    date_delta_hours: float | None
    odds: MatchedEventOut


class MatchesResponse(BaseModel):
    matches: list[MatchResultOut]
    total: int
    markets_searched: int
    events_searched: int
    min_confidence: float
    quota_remaining: str | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/odds/tennis", response_model=OddsResponse)
async def get_tennis_odds(
    response: Response,
    max_sports: int = Query(
        default=10, ge=1, le=30,
        description="Max tennis sport keys to query (each costs 1 API request)",
    ),
) -> OddsResponse:
    """
    Fetch upcoming tennis H2H odds from The Odds API.

    Returns normalised player names, per-bookmaker lines, and a consensus
    line averaged across all returned books.  Requires ODDS_API_KEY.
    """
    try:
        events, meta = await fetch_tennis_odds(max_sports=max_sports)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error(f"Odds fetch failed: {redact_secrets(exc)}")
        raise HTTPException(status_code=502, detail="Failed to fetch odds from The Odds API")

    if meta.get("quota_remaining"):
        response.headers["X-Odds-Quota-Remaining"] = str(meta["quota_remaining"])
    if meta.get("quota_used"):
        response.headers["X-Odds-Quota-Used"] = str(meta["quota_used"])

    events_out = [TennisOddsEventOut(**dataclasses.asdict(e)) for e in events]

    return OddsResponse(
        events=events_out,
        total=len(events_out),
        sports_fetched=meta.get("sports_fetched", []),
        quota_remaining=meta.get("quota_remaining"),
        quota_used=meta.get("quota_used"),
    )


@router.get("/odds/matches", response_model=MatchesResponse)
async def get_matched_markets(
    response: Response,
    max_sports: int = Query(
        default=10, ge=1, le=30,
        description="Max tennis sport keys to query",
    ),
    min_confidence: float = Query(
        default=MIN_CONFIDENCE, ge=0.0, le=1.0,
        description="Minimum confidence to include a match",
    ),
) -> MatchesResponse:
    """
    Cross-match live Polymarket tennis markets against sportsbook H2H odds.

    Fetches both data sources in parallel, scores every (market, event) pair,
    and returns only high-confidence matches sorted by confidence descending.

    Confidence = name_score × 0.65 + date_score × 0.35
    Requires both player last names to appear in the Polymarket question to
    reach the default 0.70 threshold.
    """
    import asyncio

    try:
        (raw_markets, (odds_events, meta)) = await asyncio.gather(
            fetch_tennis_markets(limit=200),
            fetch_tennis_odds(max_sports=max_sports),
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error(f"Match fetch failed: {redact_secrets(exc)}")
        raise HTTPException(status_code=502, detail="Failed to fetch data for matching")

    if meta.get("quota_remaining"):
        response.headers["X-Odds-Quota-Remaining"] = str(meta["quota_remaining"])

    # Build NormalizedMarket objects for the matcher
    from models.market import Market
    from services.adapters.base import NormalizedMarket
    from services.matcher import classify_pm_market_type
    normalized = []
    for raw in raw_markets:
        try:
            mkt = Market(**raw)
            pm_type = classify_pm_market_type(mkt.question)
            if pm_type != "h2h":
                continue
            price = float(mkt.outcomePrices[0]) if mkt.outcomePrices and len(mkt.outcomePrices) >= 2 else 0.5
            normalized.append(NormalizedMarket(
                platform="polymarket",
                market_id=mkt.id,
                event=mkt.event_name or mkt.question,
                market_type="h2h",
                side="",
                line=None,
                price=price,
                liquidity=mkt.liquidity,
                url=f"https://polymarket.com/event/{mkt.event_slug}" if mkt.event_slug else None,
                timestamp=mkt.endDate,
                question=mkt.question,
                end_date=mkt.endDate,
                outcome_prices=mkt.outcomePrices,
                event_slug=mkt.event_slug,
            ))
        except Exception:
            pass

    results = match_markets(normalized, odds_events, min_confidence=min_confidence)

    matches_out: list[MatchResultOut] = []
    for r in results:
        ev = r.odds_event
        matches_out.append(MatchResultOut(
            market_id=r.market_id,
            market_question=r.market_question,
            market_end_date=r.market_end_date,
            confidence=r.confidence,
            matched_players=list(r.matched_players),
            date_delta_hours=r.date_delta_hours,
            odds=MatchedEventOut(
                event_id=ev.event_id,
                sport_key=ev.sport_key,
                tournament=ev.tournament,
                home_player=ev.home_player,
                away_player=ev.away_player,
                commence_time=ev.commence_time,
                consensus_home_odds=ev.consensus_home_odds,
                consensus_away_odds=ev.consensus_away_odds,
                home_implied=ev.home_implied,
                away_implied=ev.away_implied,
                bookmakers_count=len(ev.bookmakers),
            ),
        ))

    return MatchesResponse(
        matches=matches_out,
        total=len(matches_out),
        markets_searched=len(normalized),
        events_searched=len(odds_events),
        min_confidence=min_confidence,
        quota_remaining=meta.get("quota_remaining"),
    )
