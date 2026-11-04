"""
Kalshi adapter — fetches active tennis match markets from the Kalshi API
and normalizes them into NormalizedMarket objects.

Kalshi H2H events have TWO markets per match — one per player.  Buying
"Yes Player A" is equivalent to buying "No Player B", but the prices can
differ because different traders participate in each market.  This adapter
groups the pair, finds the best (cheapest) price to bet on each player
across both markets, and emits ONE NormalizedMarket per event.

Only H2H match-winner markets (KXATPMATCH, KXWTAMATCH) are fetched.

API: https://api.elections.kalshi.com/trade-api/v2
Auth: API key via Authorization header.
"""
import asyncio
import logging
import os
import time
from collections import defaultdict

import httpx

from services.adapters.base import NormalizedMarket

logger = logging.getLogger(__name__)

from services.sports_config import all_kalshi_series

_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAdapter:
    platform_name: str = "kalshi"

    def __init__(self, api_key: str | None = None):
        self._explicit_key = api_key
        self._series_counts: dict[str, dict[str, int]] = {}

    @property
    def _api_key(self) -> str:
        return self._explicit_key or os.getenv("KALSHI_API_KEY", "")

    @property
    def series_counts(self) -> dict[str, dict[str, int]]:
        """Per-series {ticker: {"raw": N, "normalized": M}} from last fetch."""
        return self._series_counts

    async def fetch_markets(self) -> list[NormalizedMarket]:
        """
        Fetch active H2H tennis match markets from Kalshi.

        Groups paired markets by event, picks the best price for each
        player, and emits one NormalizedMarket per event.
        """
        if not self._api_key:
            logger.debug("KalshiAdapter: no KALSHI_API_KEY, skipping")
            return []

        headers = {"Authorization": self._api_key}
        all_raw: list[tuple[dict, str, str]] = []  # (market_dict, series, slug)
        match_series = all_kalshi_series()
        fetch_ts = time.time()
        raw_per_series: dict[str, int] = {t: 0 for t in match_series}

        async with httpx.AsyncClient(timeout=12.0) as client:
            for series_ticker, series_slug in match_series.items():
                try:
                    cursor: str | None = None
                    while True:
                        params: dict = {
                            "series_ticker": series_ticker,
                            "status": "open",
                            "limit": 1000,
                            "min_close_ts": int(time.time()),
                        }
                        if cursor:
                            params["cursor"] = cursor
                        resp = await client.get(
                            f"{_BASE}/markets",
                            params=params,
                            headers=headers,
                        )
                        resp.raise_for_status()
                        body = resp.json()
                        for m in body.get("markets", []):
                            all_raw.append((m, series_ticker, series_slug))
                            raw_per_series[series_ticker] += 1
                        cursor = body.get("cursor") or None
                        if not cursor:
                            break
                except httpx.HTTPStatusError as exc:
                    logger.error("Kalshi API error %s: %s", series_ticker, exc.response.status_code)
                    raw_per_series[series_ticker] = -1  # signal error
                except Exception as exc:
                    logger.error("Kalshi fetch error %s: %s", series_ticker, exc)
                    raw_per_series[series_ticker] = -1
                await asyncio.sleep(0.1)  # 10 req/sec pacing; Kalshi Basic tier = 20/sec

        # Group by event_ticker (each event has 2 markets — one per player)
        events: dict[str, list[tuple[dict, str, str]]] = defaultdict(list)
        for m, series, slug in all_raw:
            et = m.get("event_ticker", "")
            if et:
                events[et].append((m, series, slug))

        normalized: list[NormalizedMarket] = []
        norm_per_series: dict[str, int] = {t: 0 for t in match_series}
        for event_ticker, market_group in events.items():
            nms = _build_event_markets(event_ticker, market_group, fetch_ts)
            normalized.extend(nms)
            # Attribute normalized count to the series of the first market in the group
            if nms and market_group:
                series_key = market_group[0][1]  # series_ticker
                norm_per_series[series_key] += len(nms)

        self._series_counts = {
            t: {"raw": raw_per_series[t], "normalized": norm_per_series[t]}
            for t in match_series
        }

        for t in match_series:
            r, n = raw_per_series[t], norm_per_series[t]
            if r == -1:
                logger.warning("[KALSHI] %s → ERROR (API call failed)", t)
            elif r == 0:
                logger.info("[KALSHI] %s → raw: 0", t)
            else:
                logger.info("[KALSHI] %s → raw: %d, normalized: %d", t, r, n)

        logger.info(
            "KalshiAdapter: %d raw markets, %d events, %d normalized",
            len(all_raw), len(events), len(normalized),
        )
        return normalized


def _safe_float(val: object) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _safe_max_positive(*values: float) -> float | None:
    """Return ``max(...)`` of strictly-positive values, rounded to 4dp.

    Returns None when no input is positive.  Used to synthesize the
    cross-market best YES bid in 2-way Kalshi events: a NO bid on M2 is
    economically identical to a YES bid on M1, so the best YES_M1 bid is
    ``max(M1.yes_bid, M2.no_bid)``.
    """
    positives = [v for v in values if v > 0]
    return round(max(positives), 4) if positives else None


def _route_best_yes_bid(
    direct_bid: float,
    opp_no_bid: float,
    direct_ticker: str,
    opp_ticker: str,
) -> tuple[float | None, str | None, str | None]:
    """Pick the best YES bid route across the two equivalent paths in a
    2-way Kalshi event.

    Returns ``(value, market_id, contract_side)`` where contract_side is
    ``"yes"`` for the direct-YES route or ``"no"`` for the equivalent-NO
    route on the opposing ticker.  Ties prefer the direct YES route.
    Returns ``(None, None, None)`` when neither route has a positive bid.
    """
    candidates: list[tuple[float, str, str]] = []
    if direct_bid > 0:
        candidates.append((direct_bid, direct_ticker, "yes"))
    if opp_no_bid > 0:
        candidates.append((opp_no_bid, opp_ticker, "no"))
    if not candidates:
        return None, None, None
    # Sort: highest bid first; on tie, prefer direct YES.
    candidates.sort(key=lambda c: (-c[0], 0 if c[2] == "yes" else 1))
    best = candidates[0]
    return round(best[0], 4), best[1], best[2]


def _identify_m1_team(
    m1_ticker: str,
    m1_title: str,
    m2_ticker: str,
    m2_title: str,
) -> str:
    """
    Identify which team/player M1 represents.

    Strategy order:
      1. "Will X win" title format → extract subject directly (tennis, EPL)
      2. Ticker suffix matching against title team names (UFL, MLB, NBA)

    Kalshi title formats:
      "Will Casper Ruud win the Moutet vs Ruud : Round Of 32 match?"
      "Birmingham Stallions vs St. Louis Battlehawks winner?"
      "Indiana at Brooklyn Winner?"
      "Texas vs Los Angeles D Winner?"
    """
    import re

    # Strategy 1: "Will X win" — most reliable when available
    subject = _extract_title_subject(m1_title)
    if subject:
        return subject

    # Strategy 2: ticker suffix matching against title teams
    m1_suffix = m1_ticker.rsplit("-", 1)[-1].lower() if "-" in m1_ticker else ""
    m2_suffix = m2_ticker.rsplit("-", 1)[-1].lower() if "-" in m2_ticker else ""

    if not m1_suffix:
        return ""

    # Parse team names — split on "vs", "vs.", or "at"
    title_clean = re.sub(r'\s*(?:winner\??|match\??)$', '', m1_title, flags=re.IGNORECASE).strip()
    parts = re.split(r'\s+(?:vs\.?|at)\s+', title_clean, maxsplit=1)
    if len(parts) != 2:
        return ""

    team_a = parts[0].strip()
    team_b = parts[1].strip()

    # Match M1's suffix against team names
    # Suffix can be a prefix of a word ("tex" → "texas") or appear as a
    # substring of a word ("bhm" in "birmingham", "lad" in "los angeles dodgers")
    team_a_lower = team_a.lower()
    team_b_lower = team_b.lower()

    def _suffix_matches(suffix: str, team: str) -> bool:
        # Check if suffix starts any word (common: "tex"→"texas", "stl"→"st.")
        if any(word.startswith(suffix) for word in team.split()):
            return True
        # Check if suffix appears as substring in team name (abbreviation)
        if suffix in team.replace(" ", "").replace(".", ""):
            return True
        return False

    a_match = _suffix_matches(m1_suffix, team_a_lower)
    b_match = _suffix_matches(m1_suffix, team_b_lower)

    if a_match and not b_match:
        return team_a
    if b_match and not a_match:
        return team_b

    # Fallback: check M2's suffix
    a_match2 = _suffix_matches(m2_suffix, team_a_lower)
    b_match2 = _suffix_matches(m2_suffix, team_b_lower)

    if a_match2 and not b_match2:
        return team_b
    if b_match2 and not a_match2:
        return team_a

    return ""


def _extract_match_date_from_ticker(event_ticker: str) -> str | None:
    """
    Extract match date from Kalshi event ticker.

    Tickers like 'KXATPMATCH-26APR08MOURUU' embed the date as YYMONDD.
    Returns ISO format 'YYYY-MM-DDT23:59:00Z' or None.
    """
    import re
    # Match YYMMMDD pattern (e.g., 26APR08, 26APR12)
    m = re.search(r'-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})', event_ticker, re.IGNORECASE)
    if not m:
        return None
    year = 2000 + int(m.group(1))
    month_map = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                 "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    month = month_map.get(m.group(2).upper(), 0)
    day = int(m.group(3))
    if month == 0:
        return None
    return f"{year}-{month:02d}-{day:02d}T23:59:00Z"


def _extract_title_subject(title: str) -> str:
    """
    Extract the subject player/team from a Kalshi market title.

    Only handles the "Will {Subject} win/beat" format where the subject
    is explicitly named. Does NOT handle "X vs Y winner?" format
    because both paired markets share the same title and subject
    extraction can't distinguish M1 from M2 — that requires ticker-suffix
    matching in _identify_m1_team.

    Returns the subject name, or empty string if parsing fails.
    """
    import re
    m = re.match(r"^Will\s+(.+?)\s+(?:win|beat)\b", title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# Kalshi labels the draw outcome of a 3-way (soccer) event as "Tie" (or
# "Draw") in yes_sub_title, with a "-TIE" ticker suffix.
_DRAW_LABELS = {"tie", "draw"}


def _is_draw_market(yes_sub_title: str | None, ticker: str | None) -> bool:
    """True when a raw Kalshi market is the draw/Tie leg of a 3-way event."""
    sub = (yes_sub_title or "").lower().strip()
    ticker = ticker or ""
    suffix = ticker.rsplit("-", 1)[-1].lower() if "-" in ticker else ""
    return sub in _DRAW_LABELS or suffix == "tie"


def _build_event_markets(
    event_ticker: str,
    market_group: list[tuple[dict, str, str]],
    fetch_ts: float = 0.0,
) -> list[NormalizedMarket]:
    """
    Build NormalizedMarket(s) from a Kalshi event.

    2-way events (tennis, NBA, etc.): emits ONE NormalizedMarket with
    cross-market best prices.

    3-way events (soccer): emits TWO NormalizedMarkets — one per team —
    each with its own direct yes_ask price.  The draw market is excluded
    from pricing because "No Home" ≠ "Buy Away" when a draw exists.
    """
    # A raw group containing a draw/Tie market is a 3-way event.  If two
    # valid team markets cannot be formed below, emit NOTHING for it: the
    # draw contract must never be emitted as an h2h team market, and a lone
    # team market's NO price (1 - team) includes the draw mass, so it cannot
    # price the opponent either.
    group_has_draw = any(
        _is_draw_market(m.get("yes_sub_title"), m.get("ticker"))
        for m, _series, _slug in market_group
    )

    if len(market_group) < 2:
        # Single market — can't pair. Use it directly.
        # (_single_market_fallback itself rejects a draw/Tie market.)
        m, series, slug = market_group[0]
        nm = _single_market_fallback(m, series, slug, fetch_ts)
        return [nm] if nm else []

    # Parse both markets
    parsed = []
    for m, series, slug in market_group:
        yes_ask = _safe_float(m.get("yes_ask_dollars"))
        no_ask = _safe_float(m.get("no_ask_dollars"))
        yes_bid = _safe_float(m.get("yes_bid_dollars"))
        no_bid = _safe_float(m.get("no_bid_dollars"))
        if yes_ask <= 0 or yes_ask >= 1:
            continue
        # Kalshi provides last_updated_ts (Unix seconds) per market;
        # prefer it over our fetch timestamp for more accurate staleness.
        api_ts = m.get("last_updated_ts")
        market_fetched_at = float(api_ts) if api_ts else fetch_ts

        parsed.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", ""),
            "yes_sub_title": m.get("yes_sub_title", ""),
            "yes_ask": yes_ask,
            "no_ask": no_ask if no_ask > 0 else 1.0 - yes_ask,
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "volume": _safe_float(m.get("volume_fp")),
            "liquidity": _safe_float(m.get("liquidity_dollars")),
            "close_time": m.get("close_time") or m.get("expiration_time"),
            "series": series,
            "slug": slug,
            "fetched_at": market_fetched_at,
        })

    if len(parsed) < 2:
        if parsed and not group_has_draw:
            m, series, slug = market_group[0]
            nm = _single_market_fallback(m, series, slug, fetch_ts)
            return [nm] if nm else []
        return []

    # --- 3-way detection: separate draw market from team markets ---
    # Soccer events have 3 markets (home/away/draw). The draw market
    # must be excluded from the 2-market pairing logic because buying
    # "No Home" ≠ "Buy Away" when a draw is possible.
    draw_market = None
    team_markets = []
    for p in parsed:
        if _is_draw_market(p["yes_sub_title"], p["ticker"]):
            draw_market = p
        else:
            team_markets.append(p)

    if len(team_markets) < 2:
        # Can't form a team pair — fall back (never for a 3-way event; see
        # group_has_draw above).
        if parsed and not group_has_draw:
            m, series, slug = market_group[0]
            nm = _single_market_fallback(m, series, slug, fetch_ts)
            return [nm] if nm else []
        return []

    # Sort by ticker so M1/M2 assignment is deterministic regardless of API order
    team_markets.sort(key=lambda p: p["ticker"])
    m1, m2 = team_markets[0], team_markets[1]

    if draw_market:
        # 3-way market: use direct yes_ask prices — cross-market best-price
        # is invalid because "No Home" includes draw probability.
        best_m1_player = m1["yes_ask"]
        best_m2_player = m2["yes_ask"]
        # Same constraint applies to bids: M2's NO bid covers (M1 wins ∪ draw)
        # in 3-way, so it is NOT an equivalent YES_M1 bid.  Direct only.
        best_m1_bid = round(m1["yes_bid"], 4) if m1["yes_bid"] > 0 else None
        best_m2_bid = round(m2["yes_bid"], 4) if m2["yes_bid"] > 0 else None
        m1_bid_ticker = m1["ticker"] if best_m1_bid is not None else None
        m1_bid_side = "yes" if best_m1_bid is not None else None
        m2_bid_ticker = m2["ticker"] if best_m2_bid is not None else None
        m2_bid_side = "yes" if best_m2_bid is not None else None
    else:
        # 2-way market: pick cheapest route across the pair
        best_m1_player = min(m1["yes_ask"], m2["no_ask"])
        best_m2_player = min(m2["yes_ask"], m1["no_ask"])
        # Symmetric for bids: NO bid on M2 ≡ YES bid on M1 in 2-way, so
        # best YES_M1 bid = max(M1.yes_bid, M2.no_bid).  Capture which
        # ticker / contract side the best bid came from so paper-maker
        # proposals can name the actual execution route.
        best_m1_bid, m1_bid_ticker, m1_bid_side = _route_best_yes_bid(
            m1["yes_bid"], m2["no_bid"], m1["ticker"], m2["ticker"]
        )
        best_m2_bid, m2_bid_ticker, m2_bid_side = _route_best_yes_bid(
            m2["yes_bid"], m1["no_bid"], m2["ticker"], m1["ticker"]
        )

    # Validate prices
    if best_m1_player <= 0.02 or best_m1_player >= 0.98:
        return []
    if best_m2_player <= 0.02 or best_m2_player >= 0.98:
        return []

    # Determine team names for the YES hint.
    m1_subject = m1["yes_sub_title"] or _identify_m1_team(m1["ticker"], m1["title"], m2["ticker"], m2["title"])
    m2_subject = m2["yes_sub_title"] or _identify_m1_team(m2["ticker"], m2["title"], m1["ticker"], m1["title"])
    if m1_subject and m2_subject:
        question = f"Will {m1_subject} beat {m2_subject}?"
    else:
        question = m1["title"]

    # Use the ticker of the market with more volume for URL / history
    primary = m1 if m1["volume"] >= m2["volume"] else m2
    series = primary["series"]
    slug_name = primary["slug"]
    url = f"https://kalshi.com/markets/{series.lower()}/{slug_name}/{event_ticker.lower()}"

    all_event_markets = [m1, m2] + ([draw_market] if draw_market else [])
    total_liq = sum(m["liquidity"] for m in all_event_markets)
    total_vol = sum(m["volume"] for m in all_event_markets)
    best_fetched = max(m["fetched_at"] for m in all_event_markets)

    if draw_market:
        # 3-way market (soccer): emit one NormalizedMarket per team.
        # Each uses the team's direct yes_ask price with (1 - yes_ask) as
        # the complement.  This keeps YES + NO = 1.0 for price consistency
        # and gives each team its own accurate Kalshi price for edge calc.
        results: list[NormalizedMarket] = []
        for team, price, team_bid in [
            (m1, best_m1_player, best_m1_bid),
            (m2, best_m2_player, best_m2_bid),
        ]:
            subject = team["yes_sub_title"] or _identify_m1_team(
                team["ticker"], team["title"], m2["ticker"] if team is m1 else m1["ticker"],
                m2["title"] if team is m1 else m1["title"],
            )
            complement = round(1.0 - price, 4)
            spread = round(team["yes_ask"] - team["yes_bid"], 4) if team["yes_bid"] > 0 else None
            results.append(NormalizedMarket(
                platform="kalshi",
                market_id=team["ticker"],
                event=question,
                market_type="h2h",
                side=subject,
                line=None,
                price=price,
                liquidity=total_liq if total_liq > 0 else total_vol,
                url=url,
                timestamp=team["close_time"],
                question=question,
                # Prefer the ticker-derived game date.  Kalshi `close_time`
                # is the market settlement deadline (often ~2 weeks after the
                # game), NOT the game start, so it cannot be used as the
                # primary timing signal.  close_time only serves as a fallback
                # for tickers without a parseable date.
                end_date=_extract_match_date_from_ticker(event_ticker) or team["close_time"],
                outcome_prices=[str(round(price, 4)), str(complement)],
                event_slug=event_ticker,
                bid_ask_spread=spread,
                best_bid=team_bid,
                best_ask=round(price, 4),
                # 3-way: best YES bid is direct on the team's own ticker;
                # never routed through the opposing NO side because that
                # would include draw mass.  no_player_* stays None — each
                # team has its own NormalizedMarket so there is no
                # complement view to carry.
                best_bid_market_id=team["ticker"] if team_bid is not None else None,
                best_bid_contract_side="yes" if team_bid is not None else None,
                fetched_at=best_fetched,
            ))
        return results

    # 2-way market: single NormalizedMarket with cross-market best prices.
    # Both M1 and M2 perspectives are carried — the canonical (M1 / yes_player)
    # pair as best_bid/best_ask + route, and the M2 / no_player pair as the
    # no_player_* fields.  feature_extractor selects the right pair per side.
    outcome_prices = [
        str(round(best_m1_player, 4)),
        str(round(best_m2_player, 4)),
    ]
    # Spread from M1's direct market (the side used for market_id / price history)
    m1_spread = round(m1["yes_ask"] - m1["yes_bid"], 4) if m1["yes_bid"] > 0 else None
    return [NormalizedMarket(
        platform="kalshi",
        market_id=m1["ticker"],
        event=question,
        market_type="h2h",
        side=m1_subject,
        line=None,
        price=best_m1_player,
        liquidity=total_liq if total_liq > 0 else total_vol,
        url=url,
        timestamp=m1["close_time"],
        question=question,
        # Prefer ticker-derived game date over close_time (settlement). See
        # comment on the 3-way emit path above.
        end_date=_extract_match_date_from_ticker(event_ticker) or m1["close_time"],
        outcome_prices=outcome_prices,
        event_slug=event_ticker,
        bid_ask_spread=m1_spread,
        # Canonical (M1 / yes_player) top-of-book and execution route
        best_bid=best_m1_bid,
        best_ask=round(best_m1_player, 4),
        best_bid_market_id=m1_bid_ticker,
        best_bid_contract_side=m1_bid_side,
        # Complement (M2 / no_player) top-of-book and execution route
        no_player_best_bid=best_m2_bid,
        no_player_best_ask=round(best_m2_player, 4),
        no_player_best_bid_market_id=m2_bid_ticker,
        no_player_best_bid_contract_side=m2_bid_side,
        fetched_at=best_fetched,
    )]


def _single_market_fallback(
    m: dict,
    series_ticker: str,
    series_slug: str,
    fetch_ts: float = 0.0,
) -> NormalizedMarket | None:
    """Fallback for unpaired markets — use yes_ask directly.

    Never emits the draw/Tie leg of a 3-way event: it is not a team
    market and its side hint ("Tie") must not reach h2h matching.
    """
    if _is_draw_market(m.get("yes_sub_title"), m.get("ticker")):
        return None
    yes_ask = _safe_float(m.get("yes_ask_dollars"))
    if yes_ask <= 0.02 or yes_ask >= 0.98:
        return None

    ticker = m.get("ticker", "")
    event_ticker = m.get("event_ticker", "")
    title = m.get("title", "")
    # Use actual no_ask if available; only synthesize as last resort
    no_ask = _safe_float(m.get("no_ask_dollars"))
    no_price = round(no_ask, 4) if no_ask > 0 else round(1.0 - yes_ask, 4)

    # Extract subject (the YES player) — prefer yes_sub_title from API
    subject = m.get("yes_sub_title", "") or _extract_title_subject(title)

    api_ts = m.get("last_updated_ts")
    market_fetched_at = float(api_ts) if api_ts else fetch_ts

    yes_bid = _safe_float(m.get("yes_bid_dollars"))
    spread = round(yes_ask - yes_bid, 4) if yes_bid > 0 else None
    best_bid = round(yes_bid, 4) if yes_bid > 0 else None

    return NormalizedMarket(
        platform="kalshi",
        market_id=ticker,
        event=title,
        market_type="h2h",
        side=subject,  # YES hint: outcome_prices[0] is this player's price
        line=None,
        price=round(yes_ask, 4),
        liquidity=_safe_float(m.get("liquidity_dollars")) or _safe_float(m.get("volume_fp")),
        url=f"https://kalshi.com/markets/{series_ticker.lower()}/{series_slug}/{event_ticker.lower()}",
        timestamp=m.get("close_time") or m.get("expiration_time"),
        question=m.get("title", ""),
        # Prefer ticker-derived game date over close_time (settlement) — see
        # _build_event_markets above for rationale.
        end_date=(_extract_match_date_from_ticker(event_ticker)
                  or m.get("close_time") or m.get("expiration_time")),
        outcome_prices=[str(round(yes_ask, 4)), str(no_price)],
        event_slug=event_ticker,
        bid_ask_spread=spread,
        best_bid=best_bid,
        best_ask=round(yes_ask, 4),
        # Single-market fallback: only the direct YES route is available;
        # there is no paired complement market.
        best_bid_market_id=ticker if best_bid is not None else None,
        best_bid_contract_side="yes" if best_bid is not None else None,
        fetched_at=market_fetched_at,
    )
