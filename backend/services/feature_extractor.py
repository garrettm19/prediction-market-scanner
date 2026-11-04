"""
Pass A: Feature extraction.

Computes all attributes for each (market, event) candidate without
rejecting anything.  Every candidate gets a fully populated MarketFeatures
object that the rule engine can classify.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from services.adapters.base import NormalizedMarket
from services.devig import devig_multiplicative, devig_3way
from services.engine_config import EngineConfig
from services.normalizer import normalize_name, last_name, tokenize
from services import odds_cache
from services.odds_provider import TennisOddsEvent
from services.rule_engine import MarketFeatures

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cost_buffer(platform: str, cfg: EngineConfig) -> float:
    """Platform-aware trading cost. Polymarket has no fees; others use global buffer."""
    if platform == "polymarket":
        return 0.0
    return cfg.cost_buffer


def _is_three_way_sport(sport_key: str) -> bool:
    """
    True for sports whose native h2h market is 3-way (home / away / draw).

    Currently this is all soccer leagues (Odds API keys all begin with
    "soccer_": soccer_usa_mls, soccer_epl, soccer_germany_bundesliga, etc.).
    The 2-way devig path inflates both teams' probabilities by the missing
    draw mass when FanDuel doesn't publish draw odds — used here to gate
    that fallback for soccer.
    """
    return sport_key.startswith("soccer_")


def _american_to_implied(odds: int) -> float:
    if odds >= 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _is_live(commence_time: str) -> bool:
    try:
        start_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return start_dt <= datetime.now(timezone.utc)
    except (ValueError, AttributeError):
        return False


def _parse_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _line_width_label(line_width: float) -> str:
    if line_width <= 0.10:
        return "Tight"
    if line_width <= 0.35:
        return "Moderate"
    return "Wide"


def _fd_confidence_label(
    overround: float,
    line_width_label: str,
    cfg: EngineConfig,
) -> str:
    if overround <= cfg.overround_high_max:
        tier = "High"
    elif overround <= cfg.overround_medium_max:
        tier = "Medium"
    else:
        tier = "Low"
    # Wide lines downgrade one tier
    if line_width_label == "Wide":
        if tier == "High":
            tier = "Medium"
        elif tier == "Medium":
            tier = "Low"
    return tier


# ---------------------------------------------------------------------------
# Name scoring (same logic as old matcher, but returns features not decisions)
# ---------------------------------------------------------------------------

def _name_score(
    question_norm: str,
    home_norm: str,
    away_norm: str,
    min_len: int,
) -> tuple[float, list[str]]:
    """
    Returns (score, matched_names).  1.0 if both found, 0.0 otherwise.

    Strategy order matters for safety:
      1. Full-name substring (definitive — no false positives from generic tokens)
      2. Last-name word matching (fallback for questions with only last names)

    Strategy 2 (last-name) is skipped when both names share the same last
    token (e.g., "city", "united", "fc") to prevent cross-team false matches.
    """
    # Strategy 1: full-name substring (team sports, long-form questions)
    matched_full: list[str] = []
    for name_norm in (home_norm, away_norm):
        if len(name_norm) >= min_len and name_norm in question_norm:
            matched_full.append(name_norm)
    if len(matched_full) == 2:
        return 1.0, matched_full

    # Strategy 2: last-name word matching (individual sports, short questions)
    home_ln = last_name(home_norm)
    away_ln = last_name(away_norm)

    words = set(re.findall(r"[a-z]+", question_norm))

    # Guard: if both teams share the same last token (e.g., "city" vs "city"),
    # last-name matching cannot distinguish them — skip to Strategy 3
    matched_ln: list[str] = []
    if home_ln != away_ln:
        for name_norm in (home_norm, away_norm):
            ln = last_name(name_norm)
            if len(ln) >= min_len and ln in words:
                matched_ln.append(name_norm)
    if len(matched_ln) == 2:
        # Confirm: last-name tokens that are shared across many teams
        # (e.g., "city", "united", "fc") can match the wrong team.
        # When a name's last token is in this set AND the full name is NOT
        # a substring of the question, the match is unconfirmed → reject.
        _AMBIGUOUS_SUFFIXES = {
            # Soccer
            "city", "united", "town", "villa", "rovers", "wanderers",
            "albion", "athletic", "hotspur", "palace", "forest", "county",
            # Shared across baseball/basketball/hockey/KBO leagues
            "tigers", "twins", "giants", "eagles", "lions", "bears",
            "bulls", "hawks", "kings", "nets", "rays", "reds", "cubs",
            "sox", "mets", "heat", "jazz", "suns", "wild",
        }
        confirmed = True
        for name_norm in matched_ln:
            ln = last_name(name_norm)
            if ln in _AMBIGUOUS_SUFFIXES and name_norm not in question_norm:
                confirmed = False
                break
        if confirmed:
            return 1.0, matched_ln

    return 0.0, matched_full or matched_ln


def _date_score(end_date: str | None, commence_time: str) -> tuple[float, float | None]:
    if not end_date:
        return 0.5, None
    market_dt = _parse_dt(end_date)
    event_dt = _parse_dt(commence_time)
    if market_dt is None or event_dt is None:
        return 0.5, None
    delta_h = abs((market_dt - event_dt).total_seconds()) / 3600.0
    if delta_h <= 6.0:
        return 1.0, delta_h
    if delta_h <= 24.0:
        return 0.95, delta_h
    if delta_h <= 168.0:
        return 0.85, delta_h
    if delta_h <= 504.0:
        return 0.70, delta_h  # 21-day window: confidence 0.895 → WATCH, not BUY
    return 0.0, delta_h


# ---------------------------------------------------------------------------
# Outcome alignment
# ---------------------------------------------------------------------------

def _identify_yes_player(
    question_norm: str,
    home_player_norm: str,
    away_player_norm: str,
    home_player: str,
    away_player: str,
) -> tuple[str, str, bool]:
    """
    Return (yes_player, no_player, outcome_match).

    Determines YES/NO by positional order in the question text.
    Tries full-name position first (safe for team sports with generic
    suffixes like City/United/FC), falls back to last-name position
    (needed for individual sports where only last names appear).
    """
    # Strategy A: full-name substring position (safe for team sports)
    home_full_pos = question_norm.find(home_player_norm)
    away_full_pos = question_norm.find(away_player_norm)

    if home_full_pos >= 0 and away_full_pos >= 0:
        if home_full_pos == away_full_pos:
            # One name is a prefix of the other — ambiguous
            return home_player, away_player, False
        if home_full_pos < away_full_pos:
            return home_player, away_player, True
        return away_player, home_player, True

    # Strategy B: last-name word-boundary position (individual sports)
    home_last = last_name(home_player_norm)
    away_last = last_name(away_player_norm)

    home_m = re.search(r"\b" + re.escape(home_last) + r"\b", question_norm)
    away_m = re.search(r"\b" + re.escape(away_last) + r"\b", question_norm)

    home_pos = home_m.start() if home_m else -1
    away_pos = away_m.start() if away_m else -1

    outcome_match = home_pos >= 0 and away_pos >= 0

    if home_pos >= 0 and away_pos >= 0:
        if home_pos == away_pos:
            # Same position (e.g., same last name) — can't determine order
            return home_player, away_player, False
        if home_pos < away_pos:
            return home_player, away_player, True
        return away_player, home_player, True
    if home_pos >= 0:
        return home_player, away_player, False
    if away_pos >= 0:
        return away_player, home_player, False
    return home_player, away_player, False


# ---------------------------------------------------------------------------
# Totals / handicap parsers
# ---------------------------------------------------------------------------

def _parse_totals_question(question: str) -> tuple[str | None, float | None, str | None]:
    q = question.lower()
    if re.search(r'set\s+\d', q):
        return None, None, None
    unit = None
    if "total sets" in q or "sets o/u" in q:
        unit = "set"
    elif "match o/u" in q or "games" in q:
        unit = "game"
    elif "o/u" in q:
        unit = "game"
    m = re.search(r'(\d+\.5)', q)
    if not m:
        return None, None, None
    return "over", float(m.group(1)), unit


def _parse_handicap_question(question: str) -> tuple[str | None, float | None, str | None]:
    q = question.lower()
    unit = None
    if "set" in q:
        unit = "set"
    elif "game" in q:
        unit = "game"
    m = re.search(r'(\w[\w\s]*?)\s*\(\s*-(\d+\.?\d*)\s*\)', q)
    if not m:
        return None, None, None
    return m.group(1).strip(), float(m.group(2)), unit


# ---------------------------------------------------------------------------
# Pass A: extract features for a single (market, event) pair
# ---------------------------------------------------------------------------

def extract_h2h_features(
    market: NormalizedMarket,
    event: TennisOddsEvent,
    name_sc: float,
    matched: list[str],
    date_sc: float,
    delta_h: float | None,
    confidence: float,
    cfg: EngineConfig,
) -> list[MarketFeatures]:
    """
    Build MarketFeatures for an H2H market — one per side (two candidates).
    """
    prices = _parse_prices(market)
    if not prices:
        return []

    pm_yes, pm_no = prices
    has_bm = bool(event.bookmakers)

    if not has_bm:
        # Still produce features so rule engine can log the rejection
        base = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        base.pm_price = pm_yes
        base.pm_price_no = pm_no
        base.price_in_range = True
        base.has_bookmaker_data = False
        return [base]

    fd = next((bm for bm in event.bookmakers if bm.bookmaker_key == "fanduel"), None)
    if fd is None:
        # No FanDuel line — treat as no bookmaker data
        base = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        base.pm_price = pm_yes
        base.pm_price_no = pm_no
        base.price_in_range = True
        base.has_bookmaker_data = False
        return [base]
    # Soccer / 3-way sports: if FanDuel didn't publish a Draw outcome we
    # cannot devig safely.  The 2-way fallback would force home+away to
    # sum to 1.0 and inflate both teams' p_true by the missing draw mass,
    # producing phantom positive EV on BOTH sides of the same match.
    # Treat as no bookmaker data → routed to NO_BOOKMAKER_DATA SKIP.
    if _is_three_way_sport(event.sport_key) and fd.draw_odds is None:
        base = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        base.pm_price = pm_yes
        base.pm_price_no = pm_no
        base.price_in_range = True
        base.has_bookmaker_data = False
        return [base]
    fd_home_impl = _american_to_implied(fd.home_odds)
    fd_away_impl = _american_to_implied(fd.away_odds)

    # 3-way markets (soccer): include draw in devig to avoid inflating team probabilities
    if fd.draw_odds is not None:
        fd_draw_impl = _american_to_implied(fd.draw_odds)
        overround = round(fd_home_impl + fd_away_impl + fd_draw_impl - 1.0, 4)
        p_true_home, p_true_away, _ = devig_3way(fd_home_impl, fd_away_impl, fd_draw_impl)
    else:
        overround = round(fd_home_impl + fd_away_impl - 1.0, 4)
        p_true_home, p_true_away = devig_multiplicative(fd_home_impl, fd_away_impl)
    line_width = round(abs(p_true_home - p_true_away), 4)
    lw_label = _line_width_label(line_width)

    question_norm = normalize_name(market.question)

    # Determine YES/NO player mapping.
    # If the adapter provides a YES hint (market.side), use it to match
    # against the sportsbook event's home/away. This is more reliable than
    # positional parsing for platforms like Kalshi where the question text
    # can have the opponent's name first.
    if market.side:
        side_norm = normalize_name(market.side)
        home_norm_full = event.home_player_norm
        away_norm_full = event.away_player_norm
        if side_norm in home_norm_full or home_norm_full in side_norm:
            yes_player, no_player, outcome_match = event.home_player, event.away_player, True
        elif side_norm in away_norm_full or away_norm_full in side_norm:
            yes_player, no_player, outcome_match = event.away_player, event.home_player, True
        else:
            # Adapter hint doesn't match either player — fall back to positional
            yes_player, no_player, outcome_match = _identify_yes_player(
                question_norm, event.home_player_norm, event.away_player_norm,
                event.home_player, event.away_player,
            )
    else:
        yes_player, no_player, outcome_match = _identify_yes_player(
            question_norm, event.home_player_norm, event.away_player_norm,
            event.home_player, event.away_player,
        )
    yes_is_home = (yes_player == event.home_player)

    sides = [
        (yes_player, pm_yes, pm_no,
         fd.home_odds if yes_is_home else fd.away_odds,
         fd.away_odds if yes_is_home else fd.home_odds,
         p_true_home if yes_is_home else p_true_away,
         p_true_away if yes_is_home else p_true_home),
        (no_player, pm_no, pm_yes,
         fd.away_odds if yes_is_home else fd.home_odds,
         fd.home_odds if yes_is_home else fd.away_odds,
         p_true_away if yes_is_home else p_true_home,
         p_true_home if yes_is_home else p_true_away),
    ]

    price_consistent = _check_price_consistency(pm_yes, pm_no)

    results: list[MarketFeatures] = []
    for is_yes_player, (side_name, price, other_price, fd_odds, fd_odds_other, p_true, p_true_other) in zip(
        (True, False), sides
    ):
        f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        f.market_type = "h2h"
        f.side = side_name
        f.pm_price = price
        f.pm_price_no = other_price
        f.pm_price_effective = round(price + _cost_buffer(market.platform, cfg), 4)
        f.price_in_range = cfg.min_price < price < cfg.max_price
        f.prices_internally_consistent = price_consistent

        f.outcome_aligned = outcome_match
        f.yes_player = yes_player
        f.no_player = no_player

        f.fd_odds = fd_odds
        f.fd_odds_other = fd_odds_other
        f.fd_home_implied = fd_home_impl
        f.fd_away_implied = fd_away_impl
        f.p_true = p_true
        f.p_true_other = p_true_other

        f.fanduel_overround = overround
        f.fanduel_line_width = line_width
        f.fanduel_line_width_label = lw_label
        f.fanduel_confidence_label = _fd_confidence_label(overround, lw_label, cfg)

        f.edge = round(p_true - f.pm_price_effective, 4)
        f.has_bookmaker_data = True

        # h2h has no line/unit concerns
        f.line_match_exact = True
        f.unit_match = True
        f.side_match = True

        # The no_player feature uses the complement perspective from the
        # NormalizedMarket so its best_bid/best_ask and execution route
        # reflect THIS team's viewpoint, not the canonical M1 view that
        # _base_features copies in.  When complement data is unavailable
        # (3-way emit gives each team its own NM, single-fallback emits
        # only one direction, Polymarket leaves all None) the no_player
        # feature falls back to None on these fields and the maker pass
        # rejects it with BOOK_CROSSED_OR_EMPTY — correct, because we
        # cannot route a maker order without per-side data.
        if not is_yes_player:
            f.best_bid = market.no_player_best_bid
            f.best_ask = market.no_player_best_ask
            f.best_bid_market_id = market.no_player_best_bid_market_id
            f.best_bid_contract_side = market.no_player_best_bid_contract_side

        results.append(f)

    return results


def extract_totals_features(
    market: NormalizedMarket,
    event: TennisOddsEvent,
    name_sc: float,
    matched: list[str],
    date_sc: float,
    delta_h: float | None,
    confidence: float,
    cfg: EngineConfig,
) -> list[MarketFeatures]:
    """Build MarketFeatures for a totals market."""
    prices = _parse_prices(market)
    if not prices:
        return []
    pm_yes, _ = prices

    event_totals = getattr(event, "totals", None) or []
    if not event_totals:
        f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        f.market_type = "totals"
        f.pm_price = pm_yes
        f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
        f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
        f.has_bookmaker_data = False
        return [f]

    pm_side, pm_line, pm_unit = _parse_totals_question(market.question)
    if pm_side is None or pm_line is None:
        f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        f.market_type = "totals"
        f.pm_price = pm_yes
        f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
        f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
        f.has_bookmaker_data = True
        f.outcome_aligned = False  # can't parse question
        return [f]

    # Find matching sportsbook line
    matching_total = None
    for tl in event_totals:
        if abs(tl.point - pm_line) < 0.01:
            matching_total = tl
            break

    f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
    f.market_type = "totals"
    f.side = "Over"
    f.line = pm_line
    f.pm_price = pm_yes
    f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
    f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
    f.outcome_aligned = True  # O/U YES = Over is a known convention
    f.has_bookmaker_data = True

    f.line_match_exact = matching_total is not None
    f.unit_match = pm_unit != "set"  # sportsbook totals are games
    f.side_match = True  # not applicable to totals

    if matching_total:
        over_impl = _american_to_implied(matching_total.over_odds)
        under_impl = _american_to_implied(matching_total.under_odds)
        overround = round(over_impl + under_impl - 1.0, 4)
        p_true_over, p_true_under = devig_multiplicative(over_impl, under_impl)
        line_width = round(abs(p_true_over - p_true_under), 4)
        lw_label = _line_width_label(line_width)

        f.fd_odds = matching_total.over_odds
        f.fd_odds_other = matching_total.under_odds
        f.p_true = p_true_over
        f.p_true_other = p_true_under
        f.fanduel_overround = overround
        f.fanduel_line_width = line_width
        f.fanduel_line_width_label = lw_label
        f.fanduel_confidence_label = _fd_confidence_label(overround, lw_label, cfg)
        f.edge = round(p_true_over - f.pm_price_effective, 4)

    return [f]


def extract_handicap_features(
    market: NormalizedMarket,
    event: TennisOddsEvent,
    name_sc: float,
    matched: list[str],
    date_sc: float,
    delta_h: float | None,
    confidence: float,
    cfg: EngineConfig,
) -> list[MarketFeatures]:
    """Build MarketFeatures for a handicap market."""
    prices = _parse_prices(market)
    if not prices:
        return []
    pm_yes, _ = prices

    event_spreads = getattr(event, "spreads", None) or []
    if not event_spreads:
        f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        f.market_type = "handicap"
        f.pm_price = pm_yes
        f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
        f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
        f.has_bookmaker_data = False
        return [f]

    pm_favored_norm, pm_line, pm_unit = _parse_handicap_question(market.question)
    if pm_favored_norm is None or pm_line is None:
        f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
        f.market_type = "handicap"
        f.pm_price = pm_yes
        f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
        f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
        f.has_bookmaker_data = True
        f.outcome_aligned = False
        return [f]

    # Find matching sportsbook spread
    matching_spread = None
    for sl in event_spreads:
        if abs(abs(sl.home_point) - pm_line) < 0.01:
            matching_spread = sl
            break

    f = _base_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
    f.market_type = "handicap"
    f.line = -pm_line
    f.pm_price = pm_yes
    f.pm_price_effective = round(pm_yes + _cost_buffer(market.platform, cfg), 4)
    f.price_in_range = cfg.min_price < pm_yes < cfg.max_price
    f.outcome_aligned = True
    f.has_bookmaker_data = True

    f.line_match_exact = matching_spread is not None
    f.unit_match = not pm_unit or pm_unit == "set"

    if matching_spread:
        # Determine sportsbook favored player
        if matching_spread.home_point < 0:
            sb_favored_norm = event.home_player_norm
            sb_favored = event.home_player
            sb_fav_odds = matching_spread.home_odds
            sb_dog_odds = matching_spread.away_odds
        else:
            sb_favored_norm = event.away_player_norm
            sb_favored = event.away_player
            sb_fav_odds = matching_spread.away_odds
            sb_dog_odds = matching_spread.home_odds

        f.side = sb_favored

        # Side match check
        sb_fav_last = last_name(sb_favored_norm)
        f.side_match = (
            sb_fav_last in pm_favored_norm or pm_favored_norm in sb_favored_norm
        )

        fav_impl = _american_to_implied(sb_fav_odds)
        dog_impl = _american_to_implied(sb_dog_odds)
        overround = round(fav_impl + dog_impl - 1.0, 4)
        p_true_fav, p_true_dog = devig_multiplicative(fav_impl, dog_impl)
        line_width = round(abs(p_true_fav - p_true_dog), 4)
        lw_label = _line_width_label(line_width)

        f.fd_odds = sb_fav_odds
        f.fd_odds_other = sb_dog_odds
        f.p_true = p_true_fav
        f.p_true_other = p_true_dog
        f.fanduel_overround = overround
        f.fanduel_line_width = line_width
        f.fanduel_line_width_label = lw_label
        f.fanduel_confidence_label = _fd_confidence_label(overround, lw_label, cfg)
        f.edge = round(p_true_fav - f.pm_price_effective, 4)
    else:
        f.side_match = False

    return [f]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_prices(market: NormalizedMarket) -> tuple[float, float] | None:
    if not market.outcome_prices or len(market.outcome_prices) < 2:
        return None
    try:
        pm_yes = float(market.outcome_prices[0])
        pm_no = float(market.outcome_prices[1])
    except (ValueError, TypeError):
        return None
    if not (0.01 <= pm_yes <= 0.99):
        return None
    # Synthesize NO only when it is missing / near zero.  A real NO price
    # above 0.99 (e.g. Kalshi no_ask=1.00 when nobody bids YES) means the
    # complement is unbuyable at any sane price — pass it through so the
    # price_in_range / prices_consistent rules reject that side instead of
    # pricing it at an optimistic, non-executable 1 - yes.
    if pm_no < 0.01:
        pm_no = round(1.0 - pm_yes, 4)
    return pm_yes, pm_no


def _detect_last_name_collision(
    home_norm: str,
    away_norm: str,
    question_norm: str,
) -> tuple[bool, bool]:
    """
    Detect if a player's last name collides with the other player's name tokens.

    Example: home="Alex de Minaur", away="Daniil Medvedev"
    If "de" appears in both normalized names, that's a token collision risk.

    Returns (home_collision, away_collision).
    """
    home_ln = last_name(home_norm)
    away_ln = last_name(away_norm)

    # Collision = one player's last name is a substring/token of the other
    # player's full normalized name (e.g., "lee" in both "lee duck hee" and "felix lee")
    home_collision = (
        len(home_ln) >= 3
        and home_ln != away_ln
        and home_ln in away_norm.split()
    )
    away_collision = (
        len(away_ln) >= 3
        and away_ln != home_ln
        and away_ln in home_norm.split()
    )
    return home_collision, away_collision


def _check_price_consistency(pm_yes: float, pm_no: float) -> bool:
    """YES + NO should sum to approximately 1.0 (within 15% tolerance for PM spreads)."""
    total = pm_yes + pm_no
    return 0.85 <= total <= 1.15


def _base_features(
    market: NormalizedMarket,
    event: TennisOddsEvent,
    name_sc: float,
    matched: list[str],
    date_sc: float,
    delta_h: float | None,
    confidence: float,
    cfg: EngineConfig,
) -> MarketFeatures:
    """Create a MarketFeatures with all common fields populated."""
    event_label = f"{event.home_player} vs {event.away_player}"
    question_norm = normalize_name(market.question)

    home_coll, away_coll = _detect_last_name_collision(
        event.home_player_norm, event.away_player_norm, question_norm,
    )

    return MarketFeatures(
        platform=market.platform,
        market_id=market.market_id,
        sport=event.sport_key,
        event_label=event_label,
        event_url=market.url,
        tournament=event.tournament,
        start_time=event.commence_time,
        market_type=market.market_type,
        question=market.question,
        home_player=event.home_player,
        away_player=event.away_player,
        home_player_norm=event.home_player_norm,
        away_player_norm=event.away_player_norm,
        name_match_score=name_sc,
        matched_players=tuple(matched),
        date_score=date_sc,
        date_delta_hours=delta_h,
        event_match_confidence=confidence,
        match_quality="verified" if confidence >= cfg.buy_threshold else "unverified",
        is_live=_is_live(event.commence_time),
        matched_event_id=event.event_id,
        best_match_confidence=confidence,
        # Ambiguity: last-name collision
        home_last_name_collision=home_coll,
        away_last_name_collision=away_coll,
        # Data quality
        bid_ask_spread=market.bid_ask_spread,
        # Top-of-book — maker-planning inputs; pure pass-through from adapter.
        # _base_features uses the canonical (yes_player) values; the no_player
        # side is overridden in extract_h2h_features below.
        best_bid=market.best_bid,
        best_ask=market.best_ask,
        best_bid_market_id=market.best_bid_market_id,
        best_bid_contract_side=market.best_bid_contract_side,
        # Staleness tracking
        price_fetched_at=market.fetched_at,
        fd_fetched_at=odds_cache.get_fetched_at(event.sport_key),
        # Metadata quality
        has_end_date=bool(market.end_date),
        has_outcome_prices=bool(market.outcome_prices and len(market.outcome_prices) >= 2),
        # Observability tokens
        home_tokens=tuple(tokenize(event.home_player_norm)),
        away_tokens=tuple(tokenize(event.away_player_norm)),
        question_tokens=tuple(sorted(set(re.findall(r"[a-z]+", question_norm)))),
    )


# ---------------------------------------------------------------------------
# Dispatcher: extract features for any market type
def _sport_compatible(market: NormalizedMarket, event: TennisOddsEvent) -> bool:
    """
    Check if the Kalshi market's sport matches the FanDuel event's sport.

    Extracts the series ticker from the Kalshi event_slug (e.g.,
    "KXNBAGAME-26APR09TORNYK" → "KXNBAGAME") and looks up the sport
    config key.  Then checks if the FD event's sport_key belongs to
    the same config entry.

    Returns True if sports match, or if the sport can't be determined
    (non-Kalshi platforms, missing slug).
    """
    if market.platform != "kalshi" or not market.event_slug:
        return True  # non-Kalshi or no slug — allow expansion

    from services.sports_config import SPORTS, kalshi_series_to_sport
    series_map = kalshi_series_to_sport()

    # Extract series ticker: first segment of event_slug before the date
    series_ticker = market.event_slug.split("-")[0] if "-" in market.event_slug else market.event_slug
    kalshi_sport = series_map.get(series_ticker, "")
    if not kalshi_sport:
        return True  # unknown series — allow expansion

    # Check if the FD event's sport_key belongs to this config entry
    sc = SPORTS.get(kalshi_sport)
    if not sc:
        return True

    if event.sport_key in sc.odds_api_keys:
        return True
    if sc.odds_api_group and event.sport_key.startswith(kalshi_sport):
        return True

    return False


def _find_best_span(team_norm: str, question_norm: str) -> str | None:
    """
    Find the longest contiguous word span from team_norm that appears in
    question_norm.  Returns the span string, or None if no span >= 4 chars.

    Tries every contiguous sub-sequence of the team name's words, longest
    first, and returns the first match.  This handles:
      - Leading prefix: "toronto" in "toronto maple leafs"
      - Trailing suffix: "kansas city" in "sporting kansas city"
      - Middle span: "islanders" in "new york islanders"
      - Abbreviated: "maple leafs" in "toronto maple leafs"
    """
    words = team_norm.split()
    # Try every span length from longest to shortest
    for span_len in range(len(words), 0, -1):
        for start in range(len(words) - span_len + 1):
            span = " ".join(words[start:start + span_len])
            if len(span) >= 4 and span in question_norm and span != team_norm:
                return span
    return None


# ---------------------------------------------------------------------------

def extract_features(
    market: NormalizedMarket,
    event: TennisOddsEvent,
    cfg: EngineConfig,
) -> list[MarketFeatures]:
    """
    Extract features for a (market, event) pair.

    Returns 0-2 MarketFeatures objects (H2H produces two sides).
    All features are computed; nothing is filtered.
    """
    question_norm = normalize_name(market.question)

    # --- City-name expansion for abbreviated platform questions ---
    # Kalshi uses abbreviated names ("NYI Islanders", "Toronto") while
    # FanDuel uses full names ("New York Islanders", "Toronto Maple Leafs").
    # When the platform provides a side hint, find the longest contiguous
    # word span from each FanDuel name that appears in the question, then
    # replace it with the full name so Strategy 1 can match.
    #
    # Sport guard: only expand when the Kalshi series sport matches the
    # FanDuel event sport.  Prevents cross-sport matches (e.g., NBA
    # "Toronto at New York" matching NHL "New York Islanders vs Toronto
    # Maple Leafs" because city names overlap).
    expanded_q = question_norm
    if market.side and _sport_compatible(market, event):
        expansions: list[tuple[str, str]] = []  # (span_in_q, full_name)
        for team_norm in (event.home_player_norm, event.away_player_norm):
            best_span = _find_best_span(team_norm, question_norm)
            if best_span:
                expansions.append((best_span, team_norm))
        # Only expand if both teams mapped to different spans
        if (
            len(expansions) == 2
            and expansions[0][1] != expansions[1][1]
            and expansions[0][0] != expansions[1][0]
        ):
            expanded_q = question_norm
            # Replace longer span first to avoid substring overlap
            for span, full in sorted(expansions, key=lambda x: -len(x[0])):
                expanded_q = expanded_q.replace(span, full, 1)

    name_sc, matched = _name_score(
        expanded_q,
        event.home_player_norm,
        event.away_player_norm,
        cfg.min_name_length,
    )

    # Fast reject: no name match at all → don't produce features
    # (this is the only hard filter in Pass A — it's O(M×E) so we skip
    # candidates that have zero overlap to avoid noise)
    if name_sc == 0.0:
        return []

    date_sc, delta_h = _date_score(market.end_date, event.commence_time)
    confidence = round(name_sc * 0.65 + date_sc * 0.35, 3)

    mt = market.market_type
    if mt == "h2h":
        return extract_h2h_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
    elif mt == "totals":
        return extract_totals_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)
    elif mt == "handicap":
        return extract_handicap_features(market, event, name_sc, matched, date_sc, delta_h, confidence, cfg)

    return []
