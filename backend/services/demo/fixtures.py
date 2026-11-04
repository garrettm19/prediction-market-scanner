"""
Deterministic synthetic dataset for the offline demo mode.

Everything here is FICTIONAL: team / player names are invented, every
market id starts with ``DEMO`` (Kalshi tickers keep a real series prefix
such as ``KXNBAGAME-`` only because Pass A uses that prefix for sport
detection), ``url`` is always ``None`` and every FanDuel "tournament" label
says DEMO.  Prices and odds are fixed; only the event times move, computed
relative to a reference ``now`` so every event is a few hours to ~2 days in
the future (never ``EVENT_LIVE``).

The objects produced are the real pipeline types:
  * ``NormalizedMarket`` -- exactly what the Kalshi / Polymarket adapters
    emit (2-way Kalshi: one market with cross-market prices and the M1 team
    as the ``side`` hint; 3-way Kalshi soccer: one market PER TEAM with
    ``[price, 1 - price]``; Polymarket totals: ``"... : O/U 221.5"``).
  * ``TennisOddsEvent`` / ``BookmakerLine`` / ``TotalsLine`` -- exactly what
    ``odds_provider._normalize_event`` builds, including the ``*_norm`` name
    fields and ``draw_odds`` for soccer.

No status is assigned here.  Each fixture carries a comment naming the rule
it is designed to exercise; the BUY / WATCH / SKIP result comes out of
``rule_engine.evaluate_rules`` (see ``tests/test_demo_mode.py``).

Note on ``end_date``: the real Kalshi adapter derives it from the ticker
date (``YYYY-MM-DDT23:59:00Z``).  Demo markets instead carry the exact game
start so the date-proximity features do not depend on the wall-clock time
of day the demo is run at.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from services.adapters.base import NormalizedMarket
from services.odds_provider import (
    BookmakerLine,
    TennisOddsEvent,
    TotalsLine,
    _consensus_line,
    normalize_player_name,
)

_FD_KEY = "fanduel"
_FD_TITLE = "FanDuel"


# ---------------------------------------------------------------------------
# Builders (mirror the real normalizers)
# ---------------------------------------------------------------------------

def _iso(now: float, hours: float) -> str:
    dt = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(hours=hours)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fd_event(
    *,
    event_id: str,
    sport_key: str,
    tournament: str,
    home: str,
    away: str,
    commence_time: str,
    home_odds: int,
    away_odds: int,
    draw_odds: int | None = None,
    totals: list[TotalsLine] | None = None,
) -> TennisOddsEvent:
    """Build a FanDuel event the same way odds_provider._normalize_event does."""
    line = BookmakerLine(
        bookmaker_key=_FD_KEY,
        bookmaker_title=_FD_TITLE,
        home_odds=home_odds,
        away_odds=away_odds,
        last_update=commence_time,
        draw_odds=draw_odds,
    )
    c_home, c_away, h_impl, a_impl = _consensus_line([line])
    return TennisOddsEvent(
        event_id=event_id,
        sport_key=sport_key,
        tournament=tournament,
        home_player=home,
        away_player=away,
        home_player_norm=normalize_player_name(home),
        away_player_norm=normalize_player_name(away),
        commence_time=commence_time,
        bookmakers=[line],
        totals=list(totals or []),
        consensus_home_odds=c_home,
        consensus_away_odds=c_away,
        home_implied=h_impl,
        away_implied=a_impl,
    )


def _kalshi_2way(
    *,
    ticker: str,
    m1_team: str,
    m2_team: str,
    m1_price: float,
    m2_price: float,
    game_time: str | None,
    now: float,
) -> NormalizedMarket:
    """Kalshi paired 2-way event, shaped like KalshiAdapter's 2-way emit."""
    question = f"Will {m1_team} beat {m2_team}?"
    return NormalizedMarket(
        platform="kalshi",
        market_id=ticker,
        event=question,
        market_type="h2h",
        side=m1_team,
        line=None,
        price=m1_price,
        liquidity=25_000.0,
        url=None,
        timestamp=game_time,
        question=question,
        end_date=game_time,
        outcome_prices=[str(m1_price), str(m2_price)],
        event_slug=ticker,
        bid_ask_spread=0.01,
        best_bid=round(m1_price - 0.01, 4),
        best_ask=m1_price,
        best_bid_market_id=ticker,
        best_bid_contract_side="yes",
        no_player_best_bid=round(m2_price - 0.01, 4),
        no_player_best_ask=m2_price,
        no_player_best_bid_market_id=ticker,
        no_player_best_bid_contract_side="no",
        fetched_at=now,
    )


def _kalshi_3way(
    *,
    event_ticker: str,
    home_team: str,
    away_team: str,
    home_price: float,
    away_price: float,
    game_time: str,
    now: float,
) -> list[NormalizedMarket]:
    """Kalshi soccer 3-way event (home / away / draw).

    Like KalshiAdapter's 3-way emit: the draw contract is NOT emitted, each
    team gets its own market priced at its own YES ask with ``1 - price``
    as the complement, and both share the "Will M1 beat M2?" question.
    """
    question = f"Will {home_team} beat {away_team}?"
    out: list[NormalizedMarket] = []
    for suffix, team, price in (("H", home_team, home_price), ("A", away_team, away_price)):
        ticker = f"{event_ticker}-{suffix}"
        out.append(NormalizedMarket(
            platform="kalshi",
            market_id=ticker,
            event=question,
            market_type="h2h",
            side=team,
            line=None,
            price=price,
            liquidity=12_000.0,
            url=None,
            timestamp=game_time,
            question=question,
            end_date=game_time,
            outcome_prices=[str(price), str(round(1.0 - price, 4))],
            event_slug=event_ticker,
            bid_ask_spread=0.01,
            best_bid=round(price - 0.01, 4),
            best_ask=price,
            best_bid_market_id=ticker,
            best_bid_contract_side="yes",
            fetched_at=now,
        ))
    return out


def _polymarket(
    *,
    market_id: str,
    question: str,
    market_type: str,
    side: str,
    yes_price: float,
    no_price: float,
    game_time: str | None,
    now: float,
) -> NormalizedMarket:
    """Polymarket market shaped like PolymarketAdapter's emit."""
    return NormalizedMarket(
        platform="polymarket",
        market_id=market_id,
        event=question,
        market_type=market_type,
        side=side,
        line=None,
        price=yes_price,
        liquidity=8_000.0,
        url=None,
        timestamp=game_time,
        question=question,
        end_date=game_time,
        outcome_prices=[f"{yes_price:.4f}", f"{no_price:.4f}"],
        event_slug=None,
        bid_ask_spread=0.01,
        fetched_at=now,
    )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemoDataset:
    markets: list[NormalizedMarket]
    events: list[TennisOddsEvent]


def build_demo_dataset(now: float | None = None) -> DemoDataset:
    """Build the full synthetic dataset relative to ``now`` (default: time.time())."""
    if now is None:
        now = time.time()

    markets: list[NormalizedMarket] = []
    events: list[TennisOddsEvent] = []

    # -- NBA: Harbor City Herons vs Glenmoor Lanterns (+6h) -----------------
    t_nba = _iso(now, 6)
    events.append(_fd_event(
        event_id="demo-evt-nba-herons-lanterns",
        sport_key="basketball_nba",
        tournament="NBA (DEMO - synthetic)",
        home="Harbor City Herons", away="Glenmoor Lanterns",
        commence_time=t_nba,
        home_odds=-150, away_odds=+130,          # devig ~0.580 / 0.420
        totals=[TotalsLine(point=221.5, over_odds=-110, under_odds=-110)],
    ))
    # BUY: clean 2-way Kalshi h2h.  Herons 0.53 (+1c cost) vs p_true ~0.580
    # -> edge ~0.040 >= NBA min_edge 0.03, high match confidence, single
    # event match.  The Lanterns side (0.48 vs ~0.420) is a NO_EDGE SKIP.
    markets.append(_kalshi_2way(
        ticker="KXNBAGAME-DEMO01HCHGLL",
        m1_team="Harbor City Herons", m2_team="Glenmoor Lanterns",
        m1_price=0.53, m2_price=0.48, game_time=t_nba, now=now,
    ))
    # BUY (Polymarket, totals): "O/U 221.5" matches FanDuel's 221.5 line
    # exactly; Over 0.46 (no cost buffer on Polymarket) vs p_true 0.50.
    markets.append(_polymarket(
        market_id="DEMO-PM-NBA-TOTAL-2215",
        question="Harbor City Herons vs. Glenmoor Lanterns: O/U 221.5",
        market_type="totals", side="Over",
        yes_price=0.46, no_price=0.55, game_time=t_nba, now=now,
    ))
    # SKIP LINE_MISMATCH: "O/U 226.5" has no FanDuel line at 226.5, so it
    # must never be priced off the 221.5 line.
    markets.append(_polymarket(
        market_id="DEMO-PM-NBA-TOTAL-2265",
        question="Harbor City Herons vs. Glenmoor Lanterns: O/U 226.5",
        market_type="totals", side="Over",
        yes_price=0.38, no_price=0.63, game_time=t_nba, now=now,
    ))

    # -- MLB series: Cedar Falls Foxes vs Port Avalon Pilots ----------------
    # FanDuel has posted Game 1 (+5h) and Game 2 (+29h) of a 3-game series.
    t_g1 = _iso(now, 5)
    t_g2 = _iso(now, 29)
    t_g3 = _iso(now, 53)
    events.append(_fd_event(
        event_id="demo-evt-mlb-foxes-pilots-g1",
        sport_key="baseball_mlb",
        tournament="MLB (DEMO - synthetic)",
        home="Cedar Falls Foxes", away="Port Avalon Pilots",
        commence_time=t_g1,
        home_odds=-120, away_odds=+105,
    ))
    events.append(_fd_event(
        event_id="demo-evt-mlb-foxes-pilots-g2",
        sport_key="baseball_mlb",
        tournament="MLB (DEMO - synthetic)",
        home="Cedar Falls Foxes", away="Port Avalon Pilots",
        commence_time=t_g2,
        home_odds=-135, away_odds=+115,          # devig ~0.553 / 0.447
    ))
    # BUY + series dedup: the Game 2 market matches BOTH FanDuel games by
    # name; _dedup_to_best_date_match keeps Game 2 (date delta 0h) and drops
    # the Game 1 pairing.  Foxes 0.50 (+1c) vs ~0.553 -> edge ~0.043.
    markets.append(_kalshi_2way(
        ticker="KXMLBGAME-DEMO02G2CFFPAP",
        m1_team="Cedar Falls Foxes", m2_team="Port Avalon Pilots",
        m1_price=0.50, m2_price=0.52, game_time=t_g2, now=now,
    ))
    # SKIP DATE_TOO_FAR: Game 3 has no FanDuel line yet.  The closest FD
    # event is Game 2, 24h away (> MLB max_date_delta_hours 12h), so the
    # apparent ~9% edge is a wrong-game comparison and is rejected.
    markets.append(_kalshi_2way(
        ticker="KXMLBGAME-DEMO02G3CFFPAP",
        m1_team="Cedar Falls Foxes", m2_team="Port Avalon Pilots",
        m1_price=0.45, m2_price=0.57, game_time=t_g3, now=now,
    ))

    # -- NHL: Northgate Narwhals vs Silverpine Sentinels (+8h) --------------
    t_nhl = _iso(now, 8)
    events.append(_fd_event(
        event_id="demo-evt-nhl-narwhals-sentinels",
        sport_key="icehockey_nhl",
        tournament="NHL (DEMO - synthetic)",
        home="Northgate Narwhals", away="Silverpine Sentinels",
        commence_time=t_nhl,
        home_odds=-160, away_odds=+140,          # devig ~0.596 / 0.404
    ))
    # WATCH EDGE_IMPLAUSIBLE: Narwhals 0.46 (+1c) vs ~0.596 -> edge ~0.126,
    # above the NHL max_plausible_edge 0.12.  Large edges in liquid markets
    # are treated as probable data errors, not BUYs.
    markets.append(_kalshi_2way(
        ticker="KXNHLGAME-DEMO03NNASSE",
        m1_team="Northgate Narwhals", m2_team="Silverpine Sentinels",
        m1_price=0.46, m2_price=0.56, game_time=t_nhl, now=now,
    ))
    # SKIP CONFIDENCE_BELOW_SURVIVAL: an undated market (no end_date) with
    # team-name outcomes.  Names match, but without a date the match
    # confidence is 0.65 + 0.5*0.35 = 0.825 < survival 0.85 -- we cannot
    # confirm which game it refers to.
    markets.append(_polymarket(
        market_id="DEMO-PM-NHL-UNDATED",
        question="Northgate Narwhals vs. Silverpine Sentinels",
        market_type="h2h", side="Northgate Narwhals",
        yes_price=0.50, no_price=0.51, game_time=None, now=now,
    ))

    # -- NFL: Bayside Bramblers vs Ironwood Owls (+30h) ---------------------
    t_nfl = _iso(now, 30)
    events.append(_fd_event(
        event_id="demo-evt-nfl-bramblers-owls",
        sport_key="americanfootball_nfl",
        tournament="NFL (DEMO - synthetic)",
        home="Bayside Bramblers", away="Ironwood Owls",
        commence_time=t_nfl,
        home_odds=-400, away_odds=+320,          # devig ~0.771 / 0.229
    ))
    # SKIP PRICE_PROB_DIVERGENCE: the market quotes the heavy favourite at
    # 0.30 (looks like a 47% edge).  |p_true - price| > 0.40 means the
    # YES/NO side is probably inverted, so it is rejected, not bought.
    markets.append(_kalshi_2way(
        ticker="KXNFLGAME-DEMO04BBRIOW",
        m1_team="Bayside Bramblers", m2_team="Ironwood Owls",
        m1_price=0.30, m2_price=0.72, game_time=t_nfl, now=now,
    ))

    # -- ATP tennis: Rowan Quillfeather vs Tobias Brightwater (+4h) ---------
    t_atp = _iso(now, 4)
    events.append(_fd_event(
        event_id="demo-evt-atp-quillfeather-brightwater",
        sport_key="tennis_atp_demo_open",
        tournament="ATP Demo Open (DEMO - synthetic)",
        home="Rowan Quillfeather", away="Tobias Brightwater",
        commence_time=t_atp,
        home_odds=-125, away_odds=-105,          # overround ~6.8% -> "Low"
    ))
    # WATCH FD_CONFIDENCE_LOW: edge ~0.060 clears the 5% bar, but FanDuel's
    # own line carries ~6.8% vig (> overround_medium_max 6%), so the truth
    # source itself is low-confidence.
    markets.append(_kalshi_2way(
        ticker="KXATPMATCH-DEMO05QUIBRI",
        m1_team="Rowan Quillfeather", m2_team="Tobias Brightwater",
        m1_price=0.45, m2_price=0.57, game_time=t_atp, now=now,
    ))

    # -- WTA tennis: Marin Holloway vs Holloway Strand (+7h) ----------------
    t_wta = _iso(now, 7)
    events.append(_fd_event(
        event_id="demo-evt-wta-holloway-strand",
        sport_key="tennis_wta_demo_classic",
        tournament="WTA Demo Classic (DEMO - synthetic)",
        home="Marin Holloway", away="Holloway Strand",
        commence_time=t_wta,
        home_odds=-110, away_odds=-110,          # devig 0.50 / 0.50
    ))
    # WATCH LAST_NAME_COLLISION: "holloway" is one player's last name AND a
    # token of the opponent's name, so a last-name-only mapping could pick
    # the wrong player.  Edge 0.06 would otherwise be a BUY.
    markets.append(_kalshi_2way(
        ticker="KXWTAMATCH-DEMO06HOLSTR",
        m1_team="Marin Holloway", m2_team="Holloway Strand",
        m1_price=0.43, m2_price=0.58, game_time=t_wta, now=now,
    ))

    # -- EPL soccer 3-way WITH draw odds (+27h) -----------------------------
    t_epl = _iso(now, 27)
    events.append(_fd_event(
        event_id="demo-evt-epl-harriers-larks",
        sport_key="soccer_epl",
        tournament="EPL (DEMO - synthetic)",
        home="Kingsbridge Harriers", away="Lowmoor Larks",
        commence_time=t_epl,
        home_odds=+125, away_odds=+230, draw_odds=+250,   # 3-way devig ~0.430 / 0.293 / 0.277
    ))
    # Kalshi soccer 3-way: one market per team (draw contract not emitted).
    # WATCH EDGE_BELOW_THRESHOLD: Harriers 0.385 (+1c) vs 3-way devigged
    # ~0.430 -> edge ~0.035, positive but below the soccer min_edge 0.05.
    # (A 2-way devig that ignored the draw would claim p ~0.595 -- a phantom
    # 20% edge.)  The "No Harriers" leg (0.615 vs Larks ~0.293) and the
    # whole Larks market are NO_EDGE / TRUE_PROB_TOO_LOW SKIPs.
    markets.extend(_kalshi_3way(
        event_ticker="KXEPLGAME-DEMO07KHALOL",
        home_team="Kingsbridge Harriers", away_team="Lowmoor Larks",
        home_price=0.385, away_price=0.30, game_time=t_epl, now=now,
    ))

    # -- MLS soccer 3-way WITHOUT draw odds (+31h) --------------------------
    t_mls = _iso(now, 31)
    events.append(_fd_event(
        event_id="demo-evt-mls-comets-tides",
        sport_key="soccer_usa_mls",
        tournament="MLS (DEMO - synthetic)",
        home="Redfield Comets", away="Saltmarsh Tides",
        commence_time=t_mls,
        home_odds=+105, away_odds=+190, draw_odds=None,   # FanDuel posted no Draw
    ))
    # SKIP NO_BOOKMAKER_DATA: soccer is 3-way; without draw odds a 2-way
    # devig would inflate Comets to ~0.586 vs price 0.40 (phantom ~17%
    # edge).  Pass A routes it to NO_BOOKMAKER_DATA instead.
    markets.extend(_kalshi_3way(
        event_ticker="KXMLSGAME-DEMO08RFCSMT",
        home_team="Redfield Comets", away_team="Saltmarsh Tides",
        home_price=0.40, away_price=0.28, game_time=t_mls, now=now,
    ))

    return DemoDataset(markets=markets, events=events)


# ---------------------------------------------------------------------------
# Adapters / odds fetcher served to the real pipeline
# ---------------------------------------------------------------------------

class DemoMarketAdapter:
    """MarketAdapter that serves the demo fixtures for one platform.

    ``reference_now`` pins the dataset's clock (tests); ``None`` uses
    time.time() at each fetch so events stay in the future.
    """

    def __init__(self, platform_name: str, reference_now: float | None = None):
        self.platform_name = platform_name
        self._reference_now = reference_now

    async def fetch_markets(self) -> list[NormalizedMarket]:
        ds = build_demo_dataset(self._reference_now)
        return [m for m in ds.markets if m.platform == self.platform_name]


DEMO_PLATFORMS = ("kalshi", "polymarket")


def demo_adapters(
    reference_now: float | None = None,
    platforms: tuple[str, ...] | list[str] | None = None,
) -> list[DemoMarketAdapter]:
    """Demo adapters for ``platforms`` (default: all demo platforms)."""
    names = DEMO_PLATFORMS if platforms is None else tuple(platforms)
    return [DemoMarketAdapter(p, reference_now) for p in DEMO_PLATFORMS if p in names]


def demo_adapters_for_scan_config(reference_now: float | None = None) -> list[DemoMarketAdapter]:
    """Demo adapters filtered by ScanConfig.platforms, like the live default.

    Keeps the dashboard "Books" toggle meaningful in demo mode: with the
    default (Kalshi on, Polymarket off) only the Kalshi fixtures are scanned.
    """
    from services.scan_config import get_scan_config
    scan_cfg = get_scan_config()
    enabled = [
        p for p in DEMO_PLATFORMS
        if scan_cfg.platforms.get(p) is None or scan_cfg.platforms[p].enabled
    ]
    return demo_adapters(reference_now, enabled)


def make_demo_odds_fetcher(reference_now: float | None = None):
    """Return an async drop-in for ``odds_provider.fetch_odds`` (no network)."""

    async def demo_fetch_odds(bookmaker: str = "fanduel", max_sports: int = 25, **_ignored):
        ds = build_demo_dataset(reference_now)
        sport_keys = sorted({e.sport_key for e in ds.events})
        meta = {
            "sports_fetched": sport_keys,
            "sports_cached": [],
            "sports_skipped": [],
            "quota_remaining": None,
            "quota_used": None,
            "total_events": len(ds.events),
            "sports_failed": [],
            "data_source": "demo",
        }
        return list(ds.events), meta

    return demo_fetch_odds
