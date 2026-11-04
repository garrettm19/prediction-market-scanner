"""
Tests for services.feature_extractor — Pass A feature extraction.

Covers:
  - name scoring edge cases
  - date scoring tiers
  - outcome alignment
  - totals/handicap parsers
  - full feature extraction pipeline
  - metadata quality detection
  - last-name collision detection
  - observability token population
"""
import pytest
from services.adapters.base import NormalizedMarket
from services.engine_config import EngineConfig
from services.feature_extractor import (
    extract_features,
    _name_score,
    _date_score,
    _identify_yes_player,
    _parse_totals_question,
    _parse_handicap_question,
    _detect_last_name_collision,
    _check_price_consistency,
)
from services.odds_provider import TennisOddsEvent, BookmakerLine, normalize_player_name
from services.normalizer import normalize_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    home: str = "Carlos Alcaraz",
    away: str = "Jannik Sinner",
    event_id: str = "ev1",
    sport_key: str = "tennis_atp_french_open",
    commence_time: str = "2026-06-01T12:00:00Z",
    home_odds: int = -180,
    away_odds: int = 150,
) -> TennisOddsEvent:
    return TennisOddsEvent(
        event_id=event_id,
        sport_key=sport_key,
        tournament="ATP French Open",
        home_player=home,
        away_player=away,
        home_player_norm=normalize_name(home),
        away_player_norm=normalize_name(away),
        commence_time=commence_time,
        bookmakers=[
            BookmakerLine(
                bookmaker_key="fanduel",
                bookmaker_title="FanDuel",
                home_odds=home_odds,
                away_odds=away_odds,
                last_update=commence_time,
            )
        ],
    )


def _make_market(
    question: str = "Will Alcaraz beat Sinner?",
    market_id: str = "m1",
    market_type: str = "h2h",
    prices: list[str] | None = None,
    end_date: str | None = "2026-06-01T18:00:00Z",
) -> NormalizedMarket:
    return NormalizedMarket(
        platform="polymarket",
        market_id=market_id,
        event="Alcaraz vs Sinner",
        market_type=market_type,
        side="",
        line=None,
        price=0.55,
        liquidity=1000.0,
        url="https://polymarket.com/event/test",
        timestamp=None,
        question=question,
        end_date=end_date,
        outcome_prices=prices or ["0.55", "0.45"],
        event_slug="test",
    )


# ---------------------------------------------------------------------------
# Name scoring
# ---------------------------------------------------------------------------

class TestNameScore:
    def test_both_last_names_found(self):
        q = normalize_name("Will Alcaraz beat Sinner?")
        score, matched = _name_score(q, "carlos alcaraz", "jannik sinner", 3)
        assert score == 1.0
        assert len(matched) == 2

    def test_one_name_found(self):
        q = normalize_name("Will Alcaraz win the tournament?")
        score, matched = _name_score(q, "carlos alcaraz", "jannik sinner", 3)
        assert score == 0.0
        assert len(matched) == 1

    def test_no_names_found(self):
        q = normalize_name("Who will win the ATP Finals?")
        score, matched = _name_score(q, "carlos alcaraz", "jannik sinner", 3)
        assert score == 0.0

    def test_team_name_substring(self):
        q = normalize_name("Will Kolkata Knight Riders beat Chennai Super Kings?")
        score, matched = _name_score(
            q, "kolkata knight riders", "chennai super kings", 3
        )
        assert score == 1.0

    def test_short_last_name_rejected(self):
        q = normalize_name("Li vs Na")
        score, matched = _name_score(q, "li na", "zhang yi", 3)
        assert score == 0.0

    def test_accented_names(self):
        q = normalize_name("Świątek vs Sabalenka")
        score, matched = _name_score(q, "iga swiatek", "aryna sabalenka", 3)
        assert score == 1.0

    def test_hyphenated_name(self):
        q = normalize_name("Auger-Aliassime vs Sinner")
        score, matched = _name_score(
            q, "felix auger aliassime", "jannik sinner", 3
        )
        assert score == 1.0

    def test_multi_word_surname(self):
        q = normalize_name("de Minaur vs Djokovic")
        score, matched = _name_score(
            q, "alex de minaur", "novak djokovic", 3
        )
        assert score == 1.0


# ---------------------------------------------------------------------------
# Date scoring
# ---------------------------------------------------------------------------

class TestDateScore:
    def test_same_time(self):
        score, delta = _date_score("2026-06-01T12:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 1.0
        assert delta == pytest.approx(0.0, abs=0.01)

    def test_within_6h(self):
        score, _ = _date_score("2026-06-01T17:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 1.0

    def test_within_24h(self):
        score, _ = _date_score("2026-06-02T10:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 0.95

    def test_within_7days(self):
        score, _ = _date_score("2026-06-06T12:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 0.85

    def test_within_21days(self):
        score, _ = _date_score("2026-06-20T12:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 0.70

    def test_21day_confidence_below_buy_threshold(self):
        """21-day delta should produce confidence below buy threshold (0.90)."""
        # name_score=1.0, date_score=0.70 → confidence = 0.65 + 0.245 = 0.895
        confidence = 1.0 * 0.65 + 0.70 * 0.35
        assert confidence < 0.90

    def test_date_score_matches_matcher(self):
        """feature_extractor._date_score and matcher._date_score must agree on all tiers."""
        from services.matcher import _date_score as matcher_date_score

        cases = [
            ("2026-06-01T12:00:00Z", "2026-06-01T12:00:00Z"),   # 0h
            ("2026-06-01T17:00:00Z", "2026-06-01T12:00:00Z"),   # 5h
            ("2026-06-02T10:00:00Z", "2026-06-01T12:00:00Z"),   # 22h
            ("2026-06-06T12:00:00Z", "2026-06-01T12:00:00Z"),   # 5d
            ("2026-06-20T12:00:00Z", "2026-06-01T12:00:00Z"),   # 19d
            ("2026-08-01T12:00:00Z", "2026-06-01T12:00:00Z"),   # >21d
            (None, "2026-06-01T12:00:00Z"),                      # missing
        ]
        for end_date, commence_time in cases:
            fe_score, _ = _date_score(end_date, commence_time)
            m_score, _ = matcher_date_score(end_date, commence_time)
            assert fe_score == m_score, (
                f"Divergence at end_date={end_date}: "
                f"feature_extractor={fe_score}, matcher={m_score}"
            )

    def test_beyond_21days(self):
        score, _ = _date_score("2026-08-01T12:00:00Z", "2026-06-01T12:00:00Z")
        assert score == 0.0

    def test_missing_end_date(self):
        score, delta = _date_score(None, "2026-06-01T12:00:00Z")
        assert score == 0.5
        assert delta is None

    def test_invalid_date(self):
        score, delta = _date_score("not-a-date", "2026-06-01T12:00:00Z")
        assert score == 0.5


# ---------------------------------------------------------------------------
# Outcome alignment
# ---------------------------------------------------------------------------

class TestIdentifyYesPlayer:
    def test_home_first(self):
        q = normalize_name("Alcaraz vs Sinner")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert yes == "Carlos Alcaraz"
        assert no == "Jannik Sinner"
        assert matched is True

    def test_away_first(self):
        q = normalize_name("Sinner vs Alcaraz")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert yes == "Jannik Sinner"
        assert no == "Carlos Alcaraz"
        assert matched is True

    def test_only_one_found(self):
        q = normalize_name("Will Alcaraz win?")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert matched is False

    def test_neither_found(self):
        q = normalize_name("Who will win the tournament?")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert matched is False

    def test_hyphenated_player(self):
        """Hyphenated player name must align correctly after normalization."""
        q = normalize_name("Auger-Aliassime vs Sinner")
        yes, no, matched = _identify_yes_player(
            q,
            "felix auger aliassime",  # normalized (hyphen → space)
            "jannik sinner",
            "Félix Auger-Aliassime",
            "Jannik Sinner",
        )
        assert matched is True
        assert yes == "Félix Auger-Aliassime"
        assert no == "Jannik Sinner"

    def test_will_X_beat_Y_phrasing(self):
        """Standard Polymarket phrasing: YES player appears first after 'Will'."""
        q = normalize_name("Will Sinner beat Alcaraz in the ATP Finals?")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert matched is True
        assert yes == "Jannik Sinner"

    def test_colon_separated_event(self):
        """Kalshi-style question: 'Event: Player A vs Player B'."""
        q = normalize_name("ATP Finals: Alcaraz vs Sinner - Match Winner")
        yes, no, matched = _identify_yes_player(
            q, "carlos alcaraz", "jannik sinner", "Carlos Alcaraz", "Jannik Sinner"
        )
        assert matched is True
        assert yes == "Carlos Alcaraz"

    def test_same_last_name_unresolvable(self):
        """Two players with same last name — regex matches at same position, outcome_aligned=False."""
        q = normalize_name("Smith vs Smith in the final")
        yes, no, matched = _identify_yes_player(
            q, "carlos smith", "john smith", "Carlos Smith", "John Smith"
        )
        # Both last names are "smith" — regex finds the same match position
        # so we can't determine YES/NO order. Must return outcome_aligned=False.
        assert matched is False

    def test_same_last_name_with_full_names_in_question(self):
        """Same last name but full names in question — full-name position resolves order."""
        q = normalize_name("Carlos Smith vs John Smith")
        yes, no, matched = _identify_yes_player(
            q, "carlos smith", "john smith", "Carlos Smith", "John Smith"
        )
        # Full-name substring position: "carlos smith" at 0, "john smith" at 16
        # Different positions → aligned=True, YES=Carlos Smith (first)
        assert matched is True
        assert yes == "Carlos Smith"

    def test_same_last_name_only_last_names_in_question(self):
        """Same last name, only last names in question — falls to last-name strategy, ambiguous."""
        q = normalize_name("Smith vs Smith in the final")
        yes, no, matched = _identify_yes_player(
            q, "carlos smith", "john smith", "Carlos Smith", "John Smith"
        )
        # Full names not in question → Strategy A fails
        # Last-name strategy: both "smith" → same position → ambiguous
        assert matched is False


# ---------------------------------------------------------------------------
# Totals parser
# ---------------------------------------------------------------------------

class TestParseTotalsQuestion:
    def test_match_ou(self):
        side, line, unit = _parse_totals_question("Match O/U 22.5")
        assert side == "over"
        assert line == 22.5
        assert unit == "game"

    def test_total_sets(self):
        side, line, unit = _parse_totals_question("Total Sets O/U 2.5")
        assert unit == "set"

    def test_set_level_skipped(self):
        side, line, unit = _parse_totals_question("Set 1 Games O/U 9.5")
        assert side is None

    def test_no_line(self):
        side, line, unit = _parse_totals_question("Over/Under something")
        assert side is None

    def test_games_explicit(self):
        side, line, unit = _parse_totals_question("Total Games O/U 22.5")
        assert unit == "game"
        assert line == 22.5


# ---------------------------------------------------------------------------
# Handicap parser
# ---------------------------------------------------------------------------

class TestParseHandicapQuestion:
    def test_set_handicap(self):
        name, line, unit = _parse_handicap_question(
            "Set Handicap: Dimitrov (-1.5) vs Etcheverry (+1.5)"
        )
        assert name == "dimitrov"
        assert line == 1.5
        assert unit == "set"

    def test_no_handicap(self):
        name, line, unit = _parse_handicap_question("Alcaraz vs Sinner")
        assert name is None

    def test_game_handicap(self):
        name, line, unit = _parse_handicap_question(
            "Game Handicap: Sinner (-4.5) vs Alcaraz (+4.5)"
        )
        assert name == "sinner"
        assert line == 4.5
        assert unit == "game"


# ---------------------------------------------------------------------------
# Last-name collision detection
# ---------------------------------------------------------------------------

class TestLastNameCollision:
    def test_no_collision(self):
        h, a = _detect_last_name_collision(
            "carlos alcaraz", "jannik sinner", "alcaraz vs sinner"
        )
        assert h is False
        assert a is False

    def test_collision_shared_token(self):
        """If away_norm contains a token matching home's last name."""
        # Contrived: home="John Lee", away="Lee Duck Hee" — "lee" appears in both
        h, a = _detect_last_name_collision(
            "john lee", "lee duck hee", "john lee vs lee duck hee"
        )
        assert h is True  # "lee" (home's last) is in away's tokens

    def test_same_last_name_not_collision(self):
        """Same last name on both players is NOT a collision (it's identity, handled elsewhere)."""
        h, a = _detect_last_name_collision(
            "carlos smith", "john smith", "smith vs smith"
        )
        assert h is False
        assert a is False

    def test_short_last_name_skipped(self):
        """Last names < 3 chars should not trigger collision."""
        h, a = _detect_last_name_collision("li na", "zhang li", "li vs zhang")
        assert h is False
        assert a is False


# ---------------------------------------------------------------------------
# Price consistency
# ---------------------------------------------------------------------------

class TestPriceConsistency:
    def test_normal_prices(self):
        assert _check_price_consistency(0.55, 0.45) is True

    def test_slight_spread(self):
        assert _check_price_consistency(0.55, 0.43) is True  # 0.98

    def test_wildly_inconsistent(self):
        assert _check_price_consistency(0.90, 0.90) is False  # 1.80

    def test_very_low_total(self):
        assert _check_price_consistency(0.30, 0.30) is False  # 0.60


# ---------------------------------------------------------------------------
# Full feature extraction
# ---------------------------------------------------------------------------

class TestExtractFeatures:
    def test_h2h_produces_two_sides(self):
        market = _make_market()
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2
        sides = {f.side for f in features}
        assert "Carlos Alcaraz" in sides
        assert "Jannik Sinner" in sides

    def test_h2h_features_populated(self):
        market = _make_market()
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        f = features[0]

        assert f.platform == "polymarket"
        assert f.market_type == "h2h"
        assert f.name_match_score == 1.0
        assert f.outcome_aligned is True
        assert f.has_bookmaker_data is True
        assert f.price_in_range is True
        assert f.p_true > 0
        assert f.fanduel_overround >= 0
        assert f.matched_event_id == "ev1"

    def test_metadata_fields_populated(self):
        market = _make_market()
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        f = features[0]
        assert f.has_end_date is True
        assert f.has_outcome_prices is True
        assert f.prices_internally_consistent is True

    def test_missing_end_date_flagged(self):
        market = _make_market(end_date=None)
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert features[0].has_end_date is False

    def test_observability_tokens_populated(self):
        market = _make_market()
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        f = features[0]
        assert "alcaraz" in f.home_tokens
        assert "sinner" in f.away_tokens
        assert len(f.question_tokens) > 0

    def test_no_name_match_returns_empty(self):
        market = _make_market(question="Will Djokovic beat Nadal?")
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert features == []

    def test_no_bookmaker_still_produces_features(self):
        market = _make_market()
        event = TennisOddsEvent(
            event_id="ev1",
            sport_key="tennis_atp",
            tournament="ATP Test",
            home_player="Carlos Alcaraz",
            away_player="Jannik Sinner",
            home_player_norm="carlos alcaraz",
            away_player_norm="jannik sinner",
            commence_time="2026-06-01T12:00:00Z",
            bookmakers=[],
        )
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 1
        assert features[0].has_bookmaker_data is False

    def test_accented_names_match(self):
        market = _make_market(question="Will Świątek beat Sabalenka?")
        event = _make_event(home="Iga Świątek", away="Aryna Sabalenka")
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2
        assert features[0].name_match_score == 1.0

    def test_hyphenated_name_match(self):
        market = _make_market(question="Auger-Aliassime vs Sinner")
        event = _make_event(home="Félix Auger-Aliassime", away="Jannik Sinner")
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

    def test_unknown_market_type_returns_empty(self):
        market = _make_market(market_type="outright")
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert features == []

    def test_inconsistent_prices_flagged(self):
        market = _make_market(prices=["0.90", "0.90"])
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2
        assert features[0].prices_internally_consistent is False

    def test_pm_price_no_uses_actual_counterpart(self):
        """pm_price_no should use the actual NO price, not 1.0 - price."""
        market = _make_market(prices=["0.55", "0.43"])  # sum = 0.98, not 1.0
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

        yes_side = next(f for f in features if f.pm_price == 0.55)
        no_side = next(f for f in features if f.pm_price == 0.43)
        # YES side's pm_price_no should be the actual NO price (0.43), not 1.0 - 0.55 = 0.45
        assert yes_side.pm_price_no == pytest.approx(0.43, abs=0.001)
        # NO side's pm_price_no should be the actual YES price (0.55), not 1.0 - 0.43 = 0.57
        assert no_side.pm_price_no == pytest.approx(0.55, abs=0.001)

    def test_non_fanduel_bookmaker_treated_as_no_data(self):
        """If bookmaker is not FanDuel, features are produced without edge."""
        market = _make_market()
        event = TennisOddsEvent(
            event_id="ev1",
            sport_key="tennis_atp",
            tournament="ATP Test",
            home_player="Carlos Alcaraz",
            away_player="Jannik Sinner",
            home_player_norm="carlos alcaraz",
            away_player_norm="jannik sinner",
            commence_time="2026-06-01T12:00:00Z",
            bookmakers=[
                BookmakerLine(
                    bookmaker_key="draftkings",
                    bookmaker_title="DraftKings",
                    home_odds=-180,
                    away_odds=150,
                    last_update="2026-06-01T12:00:00Z",
                )
            ],
        )
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 1
        assert features[0].has_bookmaker_data is False
        assert features[0].edge == 0.0

    def test_hyphenated_name_with_real_player_norms(self):
        """
        Full pipeline with normalize_player_name (production path) for a
        hyphenated player. Before the normalizer fix, this would fail because
        normalize_player_name preserved hyphens while normalize_name removed them.
        """
        market = _make_market(
            question="Auger-Aliassime vs Sinner",
            prices=["0.40", "0.60"],
        )
        event = TennisOddsEvent(
            event_id="ev_hyph",
            sport_key="tennis_atp",
            tournament="ATP Test",
            home_player="Félix Auger-Aliassime",
            away_player="Jannik Sinner",
            home_player_norm=normalize_player_name("Félix Auger-Aliassime"),
            away_player_norm=normalize_player_name("Jannik Sinner"),
            commence_time="2026-06-01T12:00:00Z",
            bookmakers=[
                BookmakerLine(
                    bookmaker_key="fanduel",
                    bookmaker_title="FanDuel",
                    home_odds=-150,
                    away_odds=130,
                    last_update="2026-06-01T12:00:00Z",
                )
            ],
        )
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2
        assert features[0].name_match_score == 1.0
        assert features[0].outcome_aligned is True

    def test_wrong_event_with_shared_player_rejected(self):
        """
        A sportsbook event sharing only ONE player with the market question
        must not produce features — the name_score gate (both names required)
        prevents cross-event false positives.
        """
        # Market is about Alcaraz vs Sinner
        market = _make_market(question="Will Alcaraz beat Sinner?")
        # Wrong event: Alcaraz vs Djokovic (shares Alcaraz but not Sinner)
        wrong_event = _make_event(
            home="Carlos Alcaraz",
            away="Novak Djokovic",
            event_id="ev_wrong",
        )
        features = extract_features(market, wrong_event, EngineConfig())
        assert features == []  # rejected by name_score == 0.0

    def test_apostrophe_name_with_real_player_norms(self):
        """Player with apostrophe — verify pipeline handles it end-to-end."""
        market = _make_market(
            question="O'Sullivan vs Sinner",
            prices=["0.45", "0.55"],
        )
        event = TennisOddsEvent(
            event_id="ev_apos",
            sport_key="tennis_atp",
            tournament="ATP Test",
            home_player="Jannik O'Sullivan",
            away_player="Jannik Sinner",
            home_player_norm=normalize_player_name("Jannik O'Sullivan"),
            away_player_norm=normalize_player_name("Jannik Sinner"),
            commence_time="2026-06-01T12:00:00Z",
            bookmakers=[
                BookmakerLine(
                    bookmaker_key="fanduel",
                    bookmaker_title="FanDuel",
                    home_odds=110,
                    away_odds=-130,
                    last_update="2026-06-01T12:00:00Z",
                )
            ],
        )
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2
        assert features[0].name_match_score == 1.0


# ---------------------------------------------------------------------------
# Platform-aware cost buffer
# ---------------------------------------------------------------------------

class TestPlatformCostBuffer:
    """Polymarket uses zero cost buffer; Kalshi uses global cost_buffer."""

    def test_polymarket_zero_cost_buffer(self):
        market = _make_market(prices=["0.55", "0.45"])
        assert market.platform == "polymarket"
        event = _make_event()
        features = extract_features(market, event, EngineConfig())
        f = next(f for f in features if f.pm_price == 0.55)
        # pm_price_effective = pm_price + 0.0 (no buffer for polymarket)
        assert f.pm_price_effective == pytest.approx(0.55, abs=0.0001)

    def test_kalshi_uses_cost_buffer(self):
        market = NormalizedMarket(
            platform="kalshi",
            market_id="k1",
            event="Alcaraz vs Sinner",
            market_type="h2h",
            side="Carlos Alcaraz",
            line=None,
            price=0.55,
            liquidity=None,
            url=None,
            timestamp=None,
            question="Will Alcaraz beat Sinner?",
            end_date="2026-06-01T18:00:00Z",
            outcome_prices=["0.55", "0.45"],
            event_slug="KXTEST-26JUN01ALCSNN",
        )
        event = _make_event()
        cfg = EngineConfig()  # cost_buffer=0.01
        features = extract_features(market, event, cfg)
        f = next(f for f in features if f.pm_price == 0.55)
        # pm_price_effective = 0.55 + 0.01 = 0.56
        assert f.pm_price_effective == pytest.approx(0.56, abs=0.0001)

    def test_polymarket_edge_higher_than_kalshi(self):
        """Same prices, same event — Polymarket edge is 1 cent higher."""
        event = _make_event()
        cfg = EngineConfig()

        poly_market = _make_market(prices=["0.55", "0.45"])
        poly_features = extract_features(poly_market, event, cfg)
        poly_f = next(f for f in poly_features if f.pm_price == 0.55)

        kalshi_market = NormalizedMarket(
            platform="kalshi",
            market_id="k1",
            event="Alcaraz vs Sinner",
            market_type="h2h",
            side="Carlos Alcaraz",
            line=None,
            price=0.55,
            liquidity=None,
            url=None,
            timestamp=None,
            question="Will Alcaraz beat Sinner?",
            end_date="2026-06-01T18:00:00Z",
            outcome_prices=["0.55", "0.45"],
            event_slug="KXTEST-26JUN01ALCSNN",
        )
        kalshi_features = extract_features(kalshi_market, event, cfg)
        kalshi_f = next(f for f in kalshi_features if f.pm_price == 0.55)

        assert poly_f.edge == pytest.approx(kalshi_f.edge + cfg.cost_buffer, abs=0.0001)


# ---------------------------------------------------------------------------
# Top-of-book pass-through — best_bid / best_ask must flow from
# NormalizedMarket through `_base_features` to MarketFeatures.  Maker
# planning will read these fields directly in a follow-up commit.
# ---------------------------------------------------------------------------

class TestBestBidAskPassThrough:
    """Per-side TOB pass-through.

    The yes_player feature inherits the canonical best_bid/best_ask; the
    no_player feature inherits the complement values (no_player_best_bid /
    no_player_best_ask) so each team carries its own correct view.  This
    matters for 2-way Kalshi where the cheapest YES exposure for the
    no_player team can route through the canonical ticker's NO side.
    """

    def _market_with_book(
        self,
        best_bid: float | None,
        best_ask: float | None,
        no_player_best_bid: float | None = None,
        no_player_best_ask: float | None = None,
        no_player_best_bid_market_id: str | None = None,
        no_player_best_bid_contract_side: str | None = None,
    ) -> NormalizedMarket:
        return NormalizedMarket(
            platform="kalshi",
            market_id="K1",
            event="Alcaraz vs Sinner",
            market_type="h2h",
            side="Carlos Alcaraz",
            line=None,
            price=0.60,
            liquidity=500.0,
            url="https://kalshi.com/markets/test",
            timestamp="2026-06-01T18:00:00Z",
            question="Will Alcaraz beat Sinner?",
            end_date="2026-06-01T18:00:00Z",
            outcome_prices=["0.60", "0.40"],
            event_slug="test",
            best_bid=best_bid,
            best_ask=best_ask,
            no_player_best_bid=no_player_best_bid,
            no_player_best_ask=no_player_best_ask,
            no_player_best_bid_market_id=no_player_best_bid_market_id,
            no_player_best_bid_contract_side=no_player_best_bid_contract_side,
        )

    def test_yes_player_inherits_canonical_best_bid_ask(self):
        market = self._market_with_book(best_bid=0.40, best_ask=0.60)
        features = extract_features(market, _make_event(), EngineConfig())
        yes_feat = next(f for f in features if f.side == "Carlos Alcaraz")
        assert yes_feat.best_bid == pytest.approx(0.40)
        assert yes_feat.best_ask == pytest.approx(0.60)

    def test_no_player_uses_complement_when_provided(self):
        """When the adapter populates no_player_* (the 2-way Kalshi case),
        the no_player feature's TOB and route fields come from those values,
        not from the canonical pair."""
        market = self._market_with_book(
            best_bid=0.40, best_ask=0.60,
            no_player_best_bid=0.38, no_player_best_ask=0.42,
            no_player_best_bid_market_id="K2",
            no_player_best_bid_contract_side="yes",
        )
        features = extract_features(market, _make_event(), EngineConfig())
        no_feat = next(f for f in features if f.side == "Jannik Sinner")
        assert no_feat.best_bid == pytest.approx(0.38)
        assert no_feat.best_ask == pytest.approx(0.42)
        assert no_feat.best_bid_market_id == "K2"
        assert no_feat.best_bid_contract_side == "yes"

    def test_no_player_falls_back_to_none_when_complement_unset(self):
        """If the adapter doesn't populate no_player_* (3-way emit, single-
        fallback, Polymarket), the no_player feature gets None on TOB.  The
        maker pass will reject it with BOOK_CROSSED_OR_EMPTY — correct,
        because there is no per-side data to route a maker order with."""
        market = self._market_with_book(best_bid=0.40, best_ask=0.60)
        features = extract_features(market, _make_event(), EngineConfig())
        no_feat = next(f for f in features if f.side == "Jannik Sinner")
        assert no_feat.best_bid is None
        assert no_feat.best_ask is None
        assert no_feat.best_bid_market_id is None
        assert no_feat.best_bid_contract_side is None

    def test_defaults_to_none_when_unset(self):
        """A NormalizedMarket with neither canonical nor complement TOB —
        e.g., the Polymarket adapter today — leaves both features with
        all None on these fields."""
        market = NormalizedMarket(
            platform="polymarket",
            market_id="PM1",
            event="Alcaraz vs Sinner",
            market_type="h2h",
            side="",
            line=None,
            price=0.55,
            liquidity=1000.0,
            url=None,
            timestamp=None,
            question="Will Alcaraz beat Sinner?",
            end_date="2026-06-01T18:00:00Z",
            outcome_prices=["0.55", "0.45"],
            event_slug="test",
        )
        features = extract_features(market, _make_event(), EngineConfig())
        assert len(features) == 2
        for f in features:
            assert f.best_bid is None
            assert f.best_ask is None
            assert f.best_bid_market_id is None
            assert f.best_bid_contract_side is None

    def test_canonical_route_propagated(self):
        """The canonical best_bid_market_id / best_bid_contract_side flow
        through to the yes_player feature."""
        market = self._market_with_book(best_bid=0.40, best_ask=0.60)
        # Override the route fields by reconstructing with explicit values
        market = NormalizedMarket(
            platform=market.platform, market_id=market.market_id,
            event=market.event, market_type=market.market_type,
            side=market.side, line=market.line, price=market.price,
            liquidity=market.liquidity, url=market.url,
            timestamp=market.timestamp, question=market.question,
            end_date=market.end_date, outcome_prices=market.outcome_prices,
            event_slug=market.event_slug,
            best_bid=0.40, best_ask=0.60,
            best_bid_market_id="K1",
            best_bid_contract_side="yes",
        )
        features = extract_features(market, _make_event(), EngineConfig())
        yes_feat = next(f for f in features if f.side == "Carlos Alcaraz")
        assert yes_feat.best_bid_market_id == "K1"
        assert yes_feat.best_bid_contract_side == "yes"

    def test_passthrough_does_not_alter_existing_pricing_fields(self):
        """Adding TOB/route fields must not perturb pm_price, pm_price_no,
        pm_price_effective, edge, bid_ask_spread, price_fetched_at."""
        market = NormalizedMarket(
            platform="kalshi",
            market_id="K1",
            event="Alcaraz vs Sinner",
            market_type="h2h",
            side="Carlos Alcaraz",
            line=None,
            price=0.60,
            liquidity=500.0,
            url=None,
            timestamp=None,
            question="Will Alcaraz beat Sinner?",
            end_date="2026-06-01T18:00:00Z",
            outcome_prices=["0.60", "0.40"],
            event_slug="test",
            bid_ask_spread=0.02,
            best_bid=0.58,
            best_ask=0.60,
            best_bid_market_id="K1",
            best_bid_contract_side="yes",
            fetched_at=1234567890.0,
        )
        features = extract_features(market, _make_event(), EngineConfig())
        assert len(features) == 2
        # Sanity: existing pricing/staleness fields preserved exactly
        for f in features:
            assert f.bid_ask_spread == pytest.approx(0.02)
            assert f.price_fetched_at == 1234567890.0
        # Yes-player gets canonical TOB; no-player falls back to None
        yes_feat = next(f for f in features if f.side == "Carlos Alcaraz")
        assert yes_feat.best_bid == pytest.approx(0.58)
        assert yes_feat.best_ask == pytest.approx(0.60)
        assert yes_feat.best_bid_market_id == "K1"
        assert yes_feat.best_bid_contract_side == "yes"


# ---------------------------------------------------------------------------
# Adversarial: an unbuyable NO side (no_ask >= 0.99) must not be re-priced
# at 1 - yes and surfaced as BUY.
# ---------------------------------------------------------------------------

class TestUnbuyableNoSideNotSynthesized:
    """Previously reproduced false positive: single-market Kalshi fallback with
    Sinner yes_ask 0.40, no YES bids → no_ask 1.00.  _parse_prices rewrote NO
    to 0.60 and the Alcaraz (complement) side was classified BUY at edge
    ~0.056 although the only executable NO price was 1.00."""

    @staticmethod
    def _setup(no_ask: str | None):
        from datetime import datetime, timedelta, timezone
        from services.adapters.kalshi import _single_market_fallback

        d = datetime.now(timezone.utc) + timedelta(days=6)
        et = f"KXATPMATCH-{d:%y}{d:%b}{d:%d}SINALC".upper()
        commence = d.strftime("%Y-%m-%dT20:00:00Z")
        raw = {
            "ticker": et + "-SIN",
            "event_ticker": et,
            "title": "Will Jannik Sinner win the Sinner vs Alcaraz : Final match?",
            "yes_sub_title": "Jannik Sinner",
            "yes_ask_dollars": "0.40",
            "yes_bid_dollars": "0",
            "no_bid_dollars": "0.60",
            "liquidity_dollars": "500",
            "close_time": "2026-12-24T00:00:00Z",
        }
        if no_ask is not None:
            raw["no_ask_dollars"] = no_ask
        market = _single_market_fallback(raw, "KXATPMATCH", "atp")
        assert market is not None
        event = _make_event(
            home="Jannik Sinner", away="Carlos Alcaraz",
            sport_key="tennis_atp_finals", commence_time=commence,
            home_odds=190, away_odds=-220,
        )
        return market, event

    def test_parse_prices_passes_through_real_no_at_one(self):
        from services.feature_extractor import _parse_prices
        m = _make_market(prices=["0.40", "1.0"])
        assert _parse_prices(m) == (0.40, 1.0)
        m = _make_market(prices=["0.40", "0.995"])
        assert _parse_prices(m) == (0.40, 0.995)

    def test_parse_prices_still_synthesizes_missing_no(self):
        from services.feature_extractor import _parse_prices
        for no in ("0", "0.0", "-1", "0.005"):
            m = _make_market(prices=["0.40", no])
            assert _parse_prices(m) == (0.40, 0.60)

    def test_parse_prices_unparsable_still_rejected(self):
        from services.feature_extractor import _parse_prices
        assert _parse_prices(_make_market(prices=["0.40", "n/a"])) is None

    def test_complement_side_not_buy_or_watch_when_no_ask_is_one(self):
        from services.rule_engine import evaluate_rules
        cfg = EngineConfig()
        market, event = self._setup(no_ask="1.0000")
        assert market.outcome_prices == ["0.4", "1.0"]
        features = extract_features(market, event, cfg)
        alc = next(f for f in features if f.side == "Carlos Alcaraz")
        assert alc.pm_price == pytest.approx(1.0)
        assert alc.price_in_range is False
        assert alc.prices_internally_consistent is False
        status, _ = evaluate_rules(alc, cfg)
        assert status == "SKIP"

    def test_yes_side_pricing_unaffected(self):
        cfg = EngineConfig()
        market, event = self._setup(no_ask="1.0000")
        features = extract_features(market, event, cfg)
        sin = next(f for f in features if f.side == "Jannik Sinner")
        assert sin.outcome_aligned is True
        assert sin.pm_price == pytest.approx(0.40)
        assert sin.price_in_range is True
        # Edge/p_true computed exactly as before (from the YES ask).
        assert sin.edge == pytest.approx(round(sin.p_true - sin.pm_price_effective, 4))

    def test_missing_no_ask_still_synthesized_downstream(self):
        """no_ask absent → adapter synthesizes 1 - yes; behaviour unchanged."""
        cfg = EngineConfig()
        market, event = self._setup(no_ask=None)
        assert market.outcome_prices == ["0.4", "0.6"]
        features = extract_features(market, event, cfg)
        alc = next(f for f in features if f.side == "Carlos Alcaraz")
        assert alc.pm_price == pytest.approx(0.60)
        assert alc.price_in_range is True
        assert alc.prices_internally_consistent is True
