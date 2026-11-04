"""
The Odds API client for tennis H2H markets.

Fetches upcoming tennis events, normalises player names, start times,
bookmaker keys, and American-format moneyline odds so the data is ready
for matching against Polymarket tennis events.

Requires env var: ODDS_API_KEY

Read-only.  No orders, no automation.
"""
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from services.normalizer import normalize_name
from services.redaction import redact_secrets

logger = logging.getLogger(__name__)

_BASE = "https://api.the-odds-api.com/v4"
_ODDS_FORMAT = "american"   # keeps consistent with the pricing engine


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookmakerLine:
    """Moneyline H2H odds from one bookmaker for one match."""
    bookmaker_key: str       # e.g. "draftkings"
    bookmaker_title: str     # e.g. "DraftKings"
    home_odds: int           # American odds for home player winning
    away_odds: int           # American odds for away player winning
    last_update: str         # ISO-8601 UTC timestamp from API
    draw_odds: int | None = None  # American odds for draw (3-way markets only)


@dataclass(frozen=True)
class TotalsLine:
    """Game totals (over/under) from one bookmaker for one match."""
    point: float             # e.g. 220.5
    over_odds: int           # American odds for Over
    under_odds: int          # American odds for Under


@dataclass
class TennisOddsEvent:
    """
    Normalised tennis match ready for Polymarket matching.

    *_norm fields are lowercase ASCII — use these for fuzzy name matching.
    consensus_* fields are the average across all bookmakers (implied prob
    space), converted back to American.  None when no bookmakers returned.
    """
    event_id: str
    sport_key: str           # e.g. "tennis_atp_french_open"
    tournament: str          # e.g. "ATP French Open"
    home_player: str         # original capitalisation from API
    away_player: str
    home_player_norm: str    # lowercase ASCII for matching
    away_player_norm: str
    commence_time: str       # ISO-8601 UTC
    bookmakers: list[BookmakerLine] = field(default_factory=list)
    totals: list[TotalsLine] = field(default_factory=list)
    consensus_home_odds: int | None = None
    consensus_away_odds: int | None = None
    # Implied probabilities derived from consensus line (include vig)
    home_implied: float | None = None
    away_implied: float | None = None


@dataclass(frozen=True)
class PropLine:
    """One player prop line from a bookmaker (over/under pair)."""
    bookmaker_key: str
    player_name: str           # e.g. "Patrick Mahomes"
    player_name_norm: str      # normalized for matching
    prop_type: str             # Odds API market key: "player_pass_tds", "batter_hits", etc.
    line: float                # e.g. 2.5
    over_odds: int             # American odds for Over
    under_odds: int            # American odds for Under
    last_update: str


@dataclass
class PropEvent:
    """
    All prop lines for one game, ready for matching against prediction markets.

    Each PropEvent maps to one Odds API event (game). It contains multiple
    PropLines — one per player/prop_type/line combination.
    """
    event_id: str
    sport_key: str
    tournament: str
    home_team: str
    away_team: str
    home_team_norm: str
    away_team_norm: str
    commence_time: str
    props: list[PropLine] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_player_name(name: str) -> str:
    """
    Normalize a player name for matching.

    Delegates to normalizer.normalize_name so that player norms and
    question text use the same pipeline (diacritics, hyphens, punctuation).

    e.g. "Carlos Alcaraz"          → "carlos alcaraz"
         "Iga Świątek"             → "iga swiatek"
         "Félix Auger-Aliassime"   → "felix auger aliassime"
    """
    return normalize_name(name)


def _american_to_implied(odds: int) -> float:
    """American moneyline → implied probability (includes vig)."""
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _implied_to_american(p: float) -> int:
    """Implied probability → nearest American moneyline integer."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return -round(p / (1.0 - p) * 100)
    return round((1.0 - p) / p * 100)


def _consensus_line(
    lines: list[BookmakerLine],
) -> tuple[int | None, int | None, float | None, float | None]:
    """
    Average implied probabilities across all bookmaker lines and convert back
    to American odds.  Returns (home_odds, away_odds, home_impl, away_impl).
    """
    if not lines:
        return None, None, None, None
    home_impls = [_american_to_implied(ln.home_odds) for ln in lines]
    away_impls = [_american_to_implied(ln.away_odds) for ln in lines]
    n = len(lines)
    avg_home = sum(home_impls) / n
    avg_away = sum(away_impls) / n
    return (
        _implied_to_american(avg_home),
        _implied_to_american(avg_away),
        round(avg_home, 4),
        round(avg_away, 4),
    )


def _extract_bookmaker_line(
    bookmaker: dict,
    home_player: str,
    away_player: str,
) -> BookmakerLine | None:
    """
    Pull the H2H market from one bookmaker block and map to BookmakerLine.
    Returns None if H2H odds aren't present or player names don't match.
    """
    for market in bookmaker.get("markets") or []:
        if market.get("key") != "h2h":
            continue
        outcomes: dict[str, int] = {
            o["name"]: int(o["price"])
            for o in market.get("outcomes") or []
            if "name" in o and "price" in o
        }
        if home_player not in outcomes or away_player not in outcomes:
            continue
        draw_odds = outcomes.get("Draw")
        return BookmakerLine(
            bookmaker_key=bookmaker.get("key", "unknown"),
            bookmaker_title=bookmaker.get("title", bookmaker.get("key", "unknown")),
            home_odds=outcomes[home_player],
            away_odds=outcomes[away_player],
            last_update=market.get("last_update") or bookmaker.get("last_update") or "",
            draw_odds=draw_odds,
        )
    return None


def _normalize_event(raw: dict) -> TennisOddsEvent | None:
    """
    Parse one raw API event dict into a TennisOddsEvent.
    Returns None if required fields are missing.
    """
    event_id = raw.get("id")
    home = raw.get("home_team")
    away = raw.get("away_team")
    sport_key = raw.get("sport_key", "")
    tournament = raw.get("sport_title", sport_key)
    commence_time = raw.get("commence_time", "")

    if not (event_id and home and away):
        return None

    lines: list[BookmakerLine] = []
    totals_lines: list[TotalsLine] = []
    for bm in raw.get("bookmakers") or []:
        line = _extract_bookmaker_line(bm, home, away)
        if line:
            lines.append(line)
        # Extract totals markets
        for market in bm.get("markets") or []:
            if market.get("key") != "totals":
                continue
            outcomes = market.get("outcomes") or []
            # Group by point value to pair Over/Under
            by_point: dict[float, dict[str, int]] = {}
            for o in outcomes:
                name = o.get("name", "")
                point = o.get("point")
                price = o.get("price")
                if name in ("Over", "Under") and point is not None and price is not None:
                    by_point.setdefault(float(point), {})[name] = int(price)
            for pt, sides in by_point.items():
                if "Over" in sides and "Under" in sides:
                    totals_lines.append(TotalsLine(
                        point=pt,
                        over_odds=sides["Over"],
                        under_odds=sides["Under"],
                    ))

    c_home, c_away, h_impl, a_impl = _consensus_line(lines)

    return TennisOddsEvent(
        event_id=event_id,
        sport_key=sport_key,
        tournament=tournament,
        home_player=home,
        away_player=away,
        home_player_norm=normalize_player_name(home),
        away_player_norm=normalize_player_name(away),
        commence_time=commence_time,
        bookmakers=lines,
        totals=totals_lines,
        consensus_home_odds=c_home,
        consensus_away_odds=c_away,
        home_implied=h_impl,
        away_implied=a_impl,
    )


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

async def _fetch_active_tennis_sports(api_key: str) -> list[str]:
    """
    Return sport keys for all active, non-outright tennis sports.
    Calls GET /v4/sports/?apiKey=...
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_BASE}/sports/", params={"apiKey": api_key})
        resp.raise_for_status()
        sports: list[dict] = resp.json()

    keys = [
        s["key"]
        for s in sports
        if isinstance(s, dict)
        and s.get("active")
        and not s.get("has_outrights", False)
        and "tennis" in s.get("group", "").lower()
    ]
    logger.info(f"Active tennis sport keys: {keys}")
    return keys


# ---------------------------------------------------------------------------
# Player props
# ---------------------------------------------------------------------------

async def fetch_sport_events(
    sport_key: str,
    api_key: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    """
    Fetch event list for a sport (free — no quota cost).

    Returns raw event dicts with id, home_team, away_team, commence_time.
    """
    resp = await client.get(
        f"{_BASE}/sports/{sport_key}/events/",
        params={"apiKey": api_key},
    )
    resp.raise_for_status()
    return resp.json()


# Market types to probe during discovery (cost: 1 credit per type)
_DISCOVERY_MARKET_TYPES = "h2h,totals,spreads,alternate_totals,alternate_spreads"


async def discover_event_markets(
    sport_key: str,
    event_id: str,
    api_key: str,
    client: httpx.AsyncClient,
    bookmaker: str = "fanduel",
) -> list[str]:
    """
    Discover which markets a bookmaker offers for a specific event.

    Calls GET /v4/sports/{sport}/events/{eventId}/odds with a broad set of
    market types and extracts the keys that the bookmaker actually returned.
    Costs 1 credit per market type probed (~5 credits per call).

    Returns a list of market key strings (e.g., ["h2h", "totals"]).
    Returns empty list on any error or if the bookmaker has no data.
    """
    try:
        resp = await client.get(
            f"{_BASE}/sports/{sport_key}/events/{event_id}/odds",
            params={
                "apiKey": api_key,
                "bookmakers": bookmaker,
                "markets": _DISCOVERY_MARKET_TYPES,
                "oddsFormat": _ODDS_FORMAT,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Market discovery error %s/%s: HTTP %s", sport_key, event_id, exc.response.status_code)
        return []
    except Exception as exc:
        logger.error("Market discovery error %s/%s: %s", sport_key, event_id, redact_secrets(exc))
        return []

    # Extract market keys from the target bookmaker's response.
    # The API returns markets as a list of objects: [{"key": "h2h", ...}, ...]
    for bm in body.get("bookmakers") or []:
        if bm.get("key") != bookmaker:
            continue
        raw_markets = bm.get("markets") or []
        if not isinstance(raw_markets, list):
            continue
        market_keys = []
        for m in raw_markets:
            if isinstance(m, dict):
                k = m.get("key")
                if k and isinstance(k, str):
                    market_keys.append(k)
            elif isinstance(m, str):
                market_keys.append(m)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_keys: list[str] = []
        for k in market_keys:
            if k not in seen:
                seen.add(k)
                unique_keys.append(k)
        logger.info(
            "[%s] discovered %d FanDuel markets for event %s: %s",
            sport_key, len(unique_keys), event_id,
            unique_keys[:10] if len(unique_keys) > 10 else unique_keys,
        )
        return unique_keys

    logger.info("[%s] no %s data for event %s", sport_key, bookmaker, event_id)
    return []


async def discover_sport_markets(
    sport_key: str,
    api_key: str,
    client: httpx.AsyncClient,
    bookmaker: str = "fanduel",
) -> list[str] | None:
    """
    Discover which markets FanDuel offers for a sport.

    Picks the first upcoming event, calls discover_event_markets for it
    (1 credit), and stores the result in discovery_cache.

    Returns the discovered market keys, or None if no event is available.
    """
    from services import discovery_cache

    try:
        raw_events = await fetch_sport_events(sport_key, api_key, client)
    except Exception as exc:
        logger.error("[%s] discovery: event fetch failed: %s", sport_key, redact_secrets(exc))
        return None

    if not raw_events:
        logger.info("[%s] discovery: no events available", sport_key)
        return None

    # Pick first event with an ID
    event_id: str | None = None
    for ev in raw_events:
        eid = ev.get("id")
        if eid:
            event_id = eid
            break

    if not event_id:
        logger.info("[%s] discovery: no event with valid ID", sport_key)
        return None

    market_keys = await discover_event_markets(
        sport_key, event_id, api_key, client, bookmaker,
    )

    discovery_cache.store_discovered(sport_key, event_id, market_keys)
    logger.info(
        "[%s] discovery: stored %d markets from event %s",
        sport_key, len(market_keys), event_id,
    )
    return market_keys


# Alternate market types probed during sampling (event-level only; sport-level returns 422)
_ALT_SAMPLE_MARKETS = "alternate_spreads,alternate_totals"


async def _sample_alternate_lines(
    sport_key: str,
    event_id: str,
    api_key: str,
    client: httpx.AsyncClient,
    bookmaker: str = "fanduel",
) -> None:
    """
    Logging-only: fetch alternate market data for one event at the event level
    and log the number of outcomes per market type.

    The sport-level /odds/ endpoint returns 422 for alternate markets, so this
    uses the event-level /events/{id}/odds endpoint instead.

    Does NOT store or return data — purely for volume measurement.
    """
    try:
        resp = await client.get(
            f"{_BASE}/sports/{sport_key}/events/{event_id}/odds",
            params={
                "apiKey": api_key,
                "bookmakers": bookmaker,
                "markets": _ALT_SAMPLE_MARKETS,
                "oddsFormat": _ODDS_FORMAT,
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        logger.error("[%s] alt-sample error event %s: %s", sport_key, event_id, redact_secrets(exc))
        return

    for bm in body.get("bookmakers") or []:
        if bm.get("key") != bookmaker:
            continue
        counts: dict[str, int] = {}
        for market in bm.get("markets") or []:
            mkey = market.get("key", "")
            n = len(market.get("outcomes") or [])
            counts[mkey] = counts.get(mkey, 0) + n
        if counts:
            parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            logger.info("[%s] event %s %s", sport_key, event_id, parts)
        else:
            logger.info("[%s] event %s no alternate outcomes", sport_key, event_id)
        return

    logger.info("[%s] event %s no %s data for alternates", sport_key, event_id, bookmaker)


async def fetch_event_props(
    sport_key: str,
    event_id: str,
    prop_market_keys: list[str],
    api_key: str,
    client: httpx.AsyncClient,
    bookmaker: str = "fanduel",
) -> tuple[list[PropLine], dict]:
    """
    Fetch player prop odds for one event from the Odds API.

    Uses GET /v4/sports/{sport}/events/{eventId}/odds with prop market keys.
    Returns (prop_lines, quota_info).
    """
    resp = await client.get(
        f"{_BASE}/sports/{sport_key}/events/{event_id}/odds",
        params={
            "apiKey": api_key,
            "bookmakers": bookmaker,
            "markets": ",".join(prop_market_keys),
            "oddsFormat": _ODDS_FORMAT,
        },
    )
    resp.raise_for_status()
    quota = {
        "remaining": resp.headers.get("x-requests-remaining"),
        "used": resp.headers.get("x-requests-used"),
    }

    body = resp.json()
    lines: list[PropLine] = []

    for bm in body.get("bookmakers") or []:
        bm_key = bm.get("key", "unknown")
        for market in bm.get("markets") or []:
            prop_type = market.get("key", "")
            last_update = market.get("last_update") or bm.get("last_update") or ""

            # Group outcomes by player (description field) to pair Over/Under
            player_outcomes: dict[str, dict[str, tuple[int, float]]] = {}
            for o in market.get("outcomes") or []:
                name = o.get("name", "")        # "Over" or "Under"
                player = o.get("description", "")
                price = o.get("price")
                point = o.get("point")
                if not player or price is None or point is None:
                    continue
                player_outcomes.setdefault(player, {})[name] = (int(price), float(point))

            for player, sides in player_outcomes.items():
                over = sides.get("Over")
                under = sides.get("Under")
                if over and under and over[1] == under[1]:  # lines must match
                    lines.append(PropLine(
                        bookmaker_key=bm_key,
                        player_name=player,
                        player_name_norm=normalize_player_name(player),
                        prop_type=prop_type,
                        line=over[1],
                        over_odds=over[0],
                        under_odds=under[0],
                        last_update=last_update,
                    ))

    return lines, quota


async def fetch_props(
    bookmaker: str = "fanduel",
    ttl_seconds: float = 900,
) -> tuple[list[PropEvent], dict]:
    """
    Fetch player prop odds for all configured sports, with per-sport caching.

    Two-step process per sport:
      1. GET /events (free) to discover event IDs
      2. GET /events/{id}/odds per event (costs quota per market type)

    Only fetches props for events starting within 24 hours.
    Uses prop_cache to avoid redundant API calls within the TTL.
    Returns (prop_events, meta).
    """
    from services.sports_config import sports_with_props
    from services import prop_cache
    from datetime import datetime, timezone

    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        raise ValueError("ODDS_API_KEY environment variable is not set")

    prop_config = sports_with_props()
    if not prop_config:
        return [], {"props_fetched": 0}

    now = datetime.now(timezone.utc)
    total_fetched = 0
    last_quota: dict = {}
    fetched_sports: list[str] = []
    cached_sports: list[str] = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        for sport_key, market_keys in prop_config.items():
            # Check cache first
            cached = prop_cache.get_cached(sport_key, ttl_seconds)
            if cached is not None:
                cached_sports.append(sport_key)
                total_fetched += sum(len(pe.props) for pe in cached.events)
                logger.info("[%s] props: cache hit (%d events)", sport_key, len(cached.events))
                continue

            # Cache miss — fetch fresh
            sport_events: list[PropEvent] = []
            try:
                raw_events = await fetch_sport_events(sport_key, api_key, client)

                upcoming = []
                for ev in raw_events:
                    ct = ev.get("commence_time", "")
                    try:
                        start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        hours_until = (start - now).total_seconds() / 3600
                        if 0 < hours_until <= 24:
                            upcoming.append(ev)
                    except (ValueError, AttributeError):
                        continue

                for ev in upcoming:
                    eid = ev.get("id", "")
                    if not eid:
                        continue
                    try:
                        lines, quota = await fetch_event_props(
                            sport_key, eid, market_keys, api_key, client, bookmaker,
                        )
                        last_quota = quota
                        if lines:
                            home = ev.get("home_team", "")
                            away = ev.get("away_team", "")
                            sport_events.append(PropEvent(
                                event_id=eid,
                                sport_key=sport_key,
                                tournament=ev.get("sport_title", sport_key),
                                home_team=home,
                                away_team=away,
                                home_team_norm=normalize_player_name(home),
                                away_team_norm=normalize_player_name(away),
                                commence_time=ev.get("commence_time", ""),
                                props=lines,
                            ))
                            total_fetched += len(lines)
                    except httpx.HTTPStatusError as exc:
                        logger.error("Props fetch error %s/%s: %s", sport_key, eid, exc.response.status_code)
                    except Exception as exc:
                        logger.error("Props fetch error %s/%s: %s", sport_key, eid, redact_secrets(exc))

                prop_cache.store_cached(sport_key, sport_events)
                fetched_sports.append(sport_key)
                logger.info(
                    "[%s] props: fetched %d events, %d lines",
                    sport_key, len(sport_events),
                    sum(len(pe.props) for pe in sport_events),
                )
            except Exception as exc:
                logger.error("Props event discovery failed for %s: %s", sport_key, redact_secrets(exc))

    # Merge all from cache (includes freshly stored)
    all_prop_events = prop_cache.get_all_cached_props()

    meta = {
        "props_fetched": total_fetched,
        "props_events": len(all_prop_events),
        "props_sports_fetched": fetched_sports,
        "props_sports_cached": cached_sports,
        "quota_remaining": last_quota.get("remaining"),
    }
    logger.info(
        "fetch_props: %d fetched, %d cached → %d events, %d lines",
        len(fetched_sports), len(cached_sports), len(all_prop_events), total_fetched,
    )
    return all_prop_events, meta


# ---------------------------------------------------------------------------
# H2H odds
# ---------------------------------------------------------------------------

async def _fetch_sport_odds(
    sport_key: str,
    api_key: str,
    client: httpx.AsyncClient,
    bookmaker: str = "fanduel",
    market_types: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """
    Fetch odds for one sport from a single bookmaker (FanDuel).

    market_types controls which markets are requested (e.g., ["h2h", "totals"]).
    Defaults to ["h2h"] if not specified.
    """
    types = market_types or ["h2h"]
    markets = ",".join(types)
    resp = await client.get(
        f"{_BASE}/sports/{sport_key}/odds/",
        params={
            "apiKey": api_key,
            "bookmakers": bookmaker,
            "markets": markets,
            "oddsFormat": _ODDS_FORMAT,
        },
    )
    resp.raise_for_status()
    quota = {
        "remaining": resp.headers.get("x-requests-remaining"),
        "used": resp.headers.get("x-requests-used"),
    }
    # Estimated cost: 1 credit per market type per bookmaker region
    est_credits = len(types)
    logger.info(
        "[%s] fetched markets=%s credits~%d remaining=%s",
        sport_key, markets, est_credits, quota.get("remaining", "?"),
    )
    return resp.json(), quota


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_tennis_odds(
    max_sports: int = 10,
    bookmaker: str = "fanduel",
) -> tuple[list[TennisOddsEvent], dict]:
    """
    Fetch and normalise upcoming tennis H2H odds from The Odds API.

    Steps:
      1. Discover active tennis sport keys.
      2. Fetch H2H odds for up to max_sports of them.
      3. Normalise each event: player names, start time, per-book lines,
         consensus odds, implied probabilities.

    Returns:
      (events, meta) where meta carries quota info and which sports were
      fetched — useful for the API response and for debugging.

    Raises:
      ValueError  – if ODDS_API_KEY env var is not set.
      httpx.HTTPStatusError – on API errors.
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        raise ValueError("ODDS_API_KEY environment variable is not set")

    sport_keys = await _fetch_active_tennis_sports(api_key)
    sport_keys = sport_keys[:max_sports]

    if not sport_keys:
        logger.warning("No active tennis sport keys returned from The Odds API")
        return [], {"sports_fetched": [], "quota_remaining": None, "quota_used": None}

    events: list[TennisOddsEvent] = []
    last_quota: dict = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        for key in sport_keys:
            try:
                raw_events, quota = await _fetch_sport_odds(key, api_key, client, bookmaker=bookmaker, market_types=["h2h"])
                last_quota = quota
                for raw in raw_events:
                    event = _normalize_event(raw)
                    if event:
                        events.append(event)
                logger.info(
                    f"[{key}] fetched {len(raw_events)} events · "
                    f"quota remaining: {quota.get('remaining')}"
                )
            except httpx.HTTPStatusError as exc:
                logger.error(f"Odds API error for {key}: {exc.response.status_code}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Unexpected error fetching odds for {key}: {redact_secrets(exc)}")

    meta = {
        "sports_fetched": sport_keys,
        "quota_remaining": last_quota.get("remaining"),
        "quota_used": last_quota.get("used"),
        "total_events": len(events),
    }
    return events, meta


# ---------------------------------------------------------------------------
# Multi-sport entry point
# ---------------------------------------------------------------------------

# Sport-key discovery cache (avoids 1 API call per pipeline run)
_discovery_cache: dict[str, str] | None = None
_discovery_cache_ts: float = 0.0
_DISCOVERY_TTL: float = 3600.0  # 1 hour


async def _resolve_all_sport_keys(api_key: str) -> dict[str, str]:
    """
    Resolve all Odds API sport keys from sports config.

    Returns {odds_api_key: sport_config_key} mapping.

    - Sports with explicit `odds_api_keys` map directly.
    - Sports with `odds_api_group` (e.g., Tennis) are discovered
      dynamically from the /v4/sports/ endpoint, cached for 1 hour.
    """
    global _discovery_cache, _discovery_cache_ts

    from services.sports_config import SPORTS

    key_to_sport: dict[str, str] = {}
    groups_to_sport: dict[str, str] = {}  # group_lower → sport_config_key

    for sport_key, sc in SPORTS.items():
        if not sc.enabled:
            continue
        for api_key_val in sc.odds_api_keys:
            key_to_sport[api_key_val] = sport_key
        if sc.odds_api_group:
            groups_to_sport[sc.odds_api_group.lower()] = sport_key

    # Discover group-based keys if needed
    if groups_to_sport:
        now = time.time()
        if _discovery_cache is not None and (now - _discovery_cache_ts) < _DISCOVERY_TTL:
            # Serve from cache
            key_to_sport.update(_discovery_cache)
            logger.info("Sport-key discovery: served from cache (age=%.0fs)", now - _discovery_cache_ts)
        else:
            # Fetch fresh
            discovered: dict[str, str] = {}
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{_BASE}/sports/", params={"apiKey": api_key})
                    resp.raise_for_status()
                    for s in resp.json():
                        if (
                            isinstance(s, dict)
                            and s.get("active")
                            and not s.get("has_outrights", False)
                        ):
                            group = s.get("group", "").lower()
                            if group in groups_to_sport:
                                discovered[s["key"]] = groups_to_sport[group]
                _discovery_cache = discovered
                _discovery_cache_ts = now
                logger.info("Sport-key discovery: fetched %d keys, cached", len(discovered))
            except Exception as exc:
                logger.error("Sport-key discovery failed: %s", redact_secrets(exc))
                if _discovery_cache is not None:
                    # Serve stale cache on error
                    discovered = _discovery_cache
                    logger.info("Sport-key discovery: serving stale cache after error")
            key_to_sport.update(discovered)

    logger.info("Resolved %d sport keys: %s", len(key_to_sport), list(key_to_sport.keys()))
    return key_to_sport


def _record_sport_fetch_failure(odds_cache, key: str, ttl: float, error: str) -> dict:
    """
    Fail-closed handling for a sport key whose refetch failed.

    If the key's cached entry is past TTL it is evicted, so expired FanDuel
    odds are never merged back in as pricing truth.  A still-fresh entry is
    left untouched.  Returns a meta record for ``sports_failed``; ``error``
    must be a short, secret-free string (e.g. "HTTP 429" or an exception
    type name).
    """
    fetched_at = odds_cache.get_fetched_at(key)
    stale_age: float | None = None
    if fetched_at:
        stale_age = round(time.time() - fetched_at, 1)
        if odds_cache.get_cached(key, ttl) is None:
            odds_cache.invalidate(key)
            logger.warning(
                "[%s] refetch failed (%s); evicted expired odds (age %.0fs > ttl %ss)",
                key, error, stale_age, ttl,
            )
    return {"sport_key": key, "error": error, "stale_age_seconds": stale_age}


async def fetch_odds(
    bookmaker: str = "fanduel",
    max_sports: int = 25,
) -> tuple[list[TennisOddsEvent], dict]:
    """
    Fetch odds for configured sports, respecting per-sport scan config and TTL cache.

    For each sport key:
      1. Check if the parent sport is enabled in ScanConfig
      2. Check odds_cache — skip fetch if within TTL
      3. Fetch from API and store in cache if stale

    Market types are driven by SportConfig.market_types per sport.
    Returns merged events from cache + fresh fetches.
    """
    from services.scan_config import get_scan_config
    from services import odds_cache
    from services.sports_config import SPORTS

    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        raise ValueError("ODDS_API_KEY environment variable is not set")

    key_to_sport = await _resolve_all_sport_keys(api_key)
    sport_keys = list(key_to_sport.keys())[:max_sports]
    scan_cfg = get_scan_config()

    if not sport_keys:
        logger.warning("No active sport keys resolved")
        return [], {"sports_fetched": [], "quota_remaining": None, "quota_used": None}

    fetched_keys: list[str] = []
    cached_keys: list[str] = []
    skipped_keys: list[str] = []
    failed: list[dict] = []
    last_quota: dict = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        for key in sport_keys:
            sport_config_key = key_to_sport.get(key, "")
            sport_scan = scan_cfg.sports.get(sport_config_key)

            # Skip disabled sports
            if sport_scan and not sport_scan.enabled:
                skipped_keys.append(key)
                continue

            # Check cache
            ttl = sport_scan.odds_ttl_seconds if sport_scan else 900
            cached = odds_cache.get_cached(key, ttl)
            if cached is not None:
                cached_keys.append(key)
                continue

            # Fetch fresh — market types driven by sport config
            sc = SPORTS.get(sport_config_key)
            sport_market_types = list(sc.market_types) if sc else ["h2h"]

            # Optional discovery: validate configured markets against FanDuel availability
            if sc and sc.enable_discovery:
                from services import discovery_cache
                disc = discovery_cache.get_discovered(key)
                if disc is None:
                    disc_keys = await discover_sport_markets(key, api_key, client, bookmaker)
                    if disc_keys is not None:
                        disc = discovery_cache.get_discovered(key)
                if disc and disc.market_keys:
                    available = set(disc.market_keys)
                    missing = [m for m in sport_market_types if m not in available]
                    if missing:
                        logger.warning(
                            "[%s] discovery: configured markets not available on FanDuel: %s",
                            key, missing,
                        )
                    logger.info(
                        "[%s] discovery: configured=%s available=%d validated",
                        key, sport_market_types, len(available),
                    )

            try:
                raw_events, quota = await _fetch_sport_odds(
                    key, api_key, client, bookmaker=bookmaker,
                    market_types=sport_market_types,
                )
                last_quota = quota
                events: list[TennisOddsEvent] = []
                for raw in raw_events:
                    event = _normalize_event(raw)
                    if event:
                        events.append(event)
                odds_cache.store_cached(key, events, quota)
                fetched_keys.append(key)
                logger.info(
                    "[%s] %d events, markets=%s, credits~%d, remaining=%s",
                    key, len(raw_events), ",".join(sport_market_types),
                    len(sport_market_types), quota.get("remaining", "?"),
                )

                # Logging-only: sample alternate line volume for the first event
                if sc and sc.enable_discovery and raw_events:
                    first_eid = raw_events[0].get("id")
                    if first_eid:
                        await _sample_alternate_lines(key, first_eid, api_key, client, bookmaker)

            except httpx.HTTPStatusError as exc:
                logger.error("Odds API error for %s: %s", key, exc.response.status_code)
                if key not in fetched_keys:
                    failed.append(_record_sport_fetch_failure(
                        odds_cache, key, ttl, f"HTTP {exc.response.status_code}",
                    ))
            except Exception as exc:
                logger.error(
                    "Unexpected error fetching odds for %s: %s",
                    key, redact_secrets(exc),
                )
                if key not in fetched_keys:
                    failed.append(_record_sport_fetch_failure(
                        odds_cache, key, ttl, type(exc).__name__,
                    ))

    # Merge all events from cache (includes freshly stored ones).
    # Keys whose refetch failed past TTL were evicted above, so expired
    # FanDuel odds never reach Pass A as pricing truth.
    all_events = odds_cache.get_all_cached_events()

    logger.info(
        "fetch_odds: %d fetched, %d cached, %d skipped → %d total events",
        len(fetched_keys), len(cached_keys), len(skipped_keys), len(all_events),
    )

    meta = {
        "sports_fetched": fetched_keys,
        "sports_cached": cached_keys,
        "sports_skipped": skipped_keys,
        "quota_remaining": last_quota.get("remaining"),
        "quota_used": last_quota.get("used"),
        "total_events": len(all_events),
        "sports_failed": failed,
    }
    return all_events, meta


# ---------------------------------------------------------------------------
# Historical odds for CLV
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoricalOddsSnapshot:
    """FanDuel odds snapshot at a point in time for one event."""
    event_id: str
    home_team: str
    away_team: str
    home_odds: int              # American
    away_odds: int
    draw_odds: int | None       # 3-way markets only
    snapshot_time: str          # ISO-8601 — actual snapshot timestamp from API
    home_implied: float         # raw implied (includes vig)
    away_implied: float


async def fetch_historical_odds(
    sport_key: str,
    event_id: str,
    date_iso: str,
    bookmaker: str = "fanduel",
) -> HistoricalOddsSnapshot | None:
    """
    Fetch the closest FanDuel odds snapshot at or before date_iso.

    Uses GET /v4/historical/sports/{sport}/odds/?date={date}&eventIds={id}
    The Odds API returns the nearest snapshot <= the requested timestamp
    (5-minute intervals on paid plans).

    Returns None if no snapshot is available.
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return None

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                f"{_BASE}/historical/sports/{sport_key}/odds/",
                params={
                    "apiKey": api_key,
                    "bookmakers": bookmaker,
                    "markets": "h2h",
                    "oddsFormat": _ODDS_FORMAT,
                    "eventIds": event_id,
                    "date": date_iso,
                },
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            logger.error("Historical odds fetch failed (%s/%s): %s", sport_key, event_id, redact_secrets(exc))
            return None

    snapshot_time = body.get("timestamp", date_iso)
    events = body.get("data", [])
    if not events:
        return None

    # Find our event
    ev = None
    for e in events:
        if e.get("id") == event_id:
            ev = e
            break
    if ev is None and events:
        ev = events[0]  # single event requested — should be the one

    home = ev.get("home_team", "")
    away = ev.get("away_team", "")

    # Extract FanDuel line
    for bm in ev.get("bookmakers") or []:
        if bm.get("key") != bookmaker:
            continue
        line = _extract_bookmaker_line(bm, home, away)
        if line:
            return HistoricalOddsSnapshot(
                event_id=ev.get("id", event_id),
                home_team=home,
                away_team=away,
                home_odds=line.home_odds,
                away_odds=line.away_odds,
                draw_odds=line.draw_odds,
                snapshot_time=snapshot_time,
                home_implied=round(_american_to_implied(line.home_odds), 4),
                away_implied=round(_american_to_implied(line.away_odds), 4),
            )

    return None
