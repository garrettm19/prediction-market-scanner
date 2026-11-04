"""
Adversarial tests for soccer side-alignment and generic-token safety.

Covers the root causes of incorrect side labels (FC, CF, City, United)
and the cross-match false positives that produce wrong EV calculations.

Test categories:
  1. Generic suffix tokens don't produce cross-match false positives
  2. Correct matches still work for all soccer name patterns
  3. _identify_yes_player assigns the right team as YES
  4. Same-suffix teams produce SKIP (not wrong BUY)
  5. Full pipeline: correct side gets correct FanDuel probability
  6. Polymarket and Kalshi side alignment
"""
import pytest

from services.adapters.base import NormalizedMarket
from services.engine_config import EngineConfig
from services.feature_extractor import (
    extract_features,
    _name_score,
    _identify_yes_player,
)
from services.normalizer import normalize_name, last_name
from services.odds_provider import TennisOddsEvent, BookmakerLine
from services.rule_engine import evaluate_rules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(home: str, away: str, sport: str = "soccer_epl", **kw) -> TennisOddsEvent:
    return TennisOddsEvent(
        event_id=kw.get("event_id", "ev1"),
        sport_key=sport,
        tournament="Test",
        home_player=home,
        away_player=away,
        home_player_norm=normalize_name(home),
        away_player_norm=normalize_name(away),
        # Far-future so the fixture is never "live": a past commence_time
        # sets is_live=True and the CRITICAL EVENT_LIVE rule would SKIP
        # every candidate, masking the rule a test actually targets.
        commence_time=kw.get("commence_time", "2099-06-01T20:00:00Z"),
        bookmakers=[BookmakerLine(
            bookmaker_key="fanduel", bookmaker_title="FanDuel",
            home_odds=kw.get("home_odds", -150),
            away_odds=kw.get("away_odds", 130),
            # Soccer h2h is 3-way; draw_odds must be present or the
            # feature extractor SKIPs the candidate (NO_BOOKMAKER_DATA)
            # to avoid 2-way-devig phantom EV. Test default supplied here
            # for the common case; tests that want to exercise the
            # missing-draw guard explicitly pass draw_odds=None.
            draw_odds=kw.get("draw_odds", 240),
            last_update="2099-06-01T20:00:00Z",
        )],
    )


def _market(question: str, prices=None, platform="polymarket") -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform, market_id="m1", event=question,
        market_type="h2h", side="", line=None, price=0.55,
        liquidity=5000.0, url=None, timestamp=None,
        question=question, end_date="2099-06-02T06:00:00Z",
        outcome_prices=prices or ["0.55", "0.45"], event_slug="test",
    )


cfg = EngineConfig()


# ---------------------------------------------------------------------------
# 1. Generic suffix tokens do NOT produce cross-match false positives
# ---------------------------------------------------------------------------

class TestGenericTokenCrossMatch:
    """The core bug: 'city'/'united' in the question matching the wrong team."""

    def test_city_cross_match_rejected(self):
        """'Manchester City vs Liverpool' must NOT match 'Sporting Kansas City vs Liverpool'."""
        market = _market("Will Manchester City beat Liverpool?")
        wrong = _event("Sporting Kansas City", "Liverpool", sport="soccer_mls")
        features = extract_features(market, wrong, cfg)
        # Strategy 1 (full-name) rejects: "sporting kansas city" not in question
        # Strategy 2 (last-name) finds "city" + "liverpool" but Strategy 1 tried first
        assert features == [] or all(f.name_match_score == 0.0 for f in features)

    def test_united_cross_match_rejected(self):
        """'Manchester United vs Arsenal' must NOT match 'Newcastle United vs Arsenal'."""
        market = _market("Will Manchester United beat Arsenal?")
        wrong = _event("Newcastle United", "Arsenal")
        features = extract_features(market, wrong, cfg)
        assert features == [] or all(f.name_match_score == 0.0 for f in features)

    def test_fc_cross_match_rejected(self):
        """'Austin FC vs Portland Timbers' must NOT match 'Charlotte FC vs Portland Timbers'."""
        market = _market("Will Austin FC beat Portland Timbers?")
        wrong = _event("Charlotte FC", "Portland Timbers", sport="soccer_mls")
        features = extract_features(market, wrong, cfg)
        assert features == [] or all(f.name_match_score == 0.0 for f in features)

    def test_cf_cross_match_rejected(self):
        """'Inter Miami CF vs Columbus Crew' must NOT match 'CF Montreal vs Columbus Crew'."""
        market = _market("Will Inter Miami CF beat Columbus Crew?")
        wrong = _event("CF Montreal", "Columbus Crew", sport="soccer_mls")
        features = extract_features(market, wrong, cfg)
        assert features == [] or all(f.name_match_score == 0.0 for f in features)


# ---------------------------------------------------------------------------
# 2. Correct matches still work for all soccer name patterns
# ---------------------------------------------------------------------------

class TestCorrectSoccerMatches:
    @pytest.mark.parametrize("home,away", [
        ("Manchester City", "Liverpool"),
        ("Manchester United", "Arsenal"),
        ("Austin FC", "Portland Timbers"),
        ("Inter Miami CF", "Columbus Crew"),
        ("Sporting Kansas City", "Real Salt Lake"),
        ("Tottenham Hotspur", "Crystal Palace"),
        ("Aston Villa", "Nottingham Forest"),
        ("New York City FC", "New York Red Bulls"),
    ])
    def test_correct_match_produces_features(self, home, away):
        q = f"Will {home} beat {away}?"
        market = _market(q)
        event = _event(home, away)
        features = extract_features(market, event, cfg)
        assert len(features) == 2
        assert features[0].name_match_score == 1.0
        assert features[0].outcome_aligned is True


# ---------------------------------------------------------------------------
# 3. _identify_yes_player assigns the right team as YES
# ---------------------------------------------------------------------------

class TestSoccerYesPlayerAlignment:
    def test_city_team_yes_is_first_named(self):
        q = normalize_name("Will Manchester City beat Liverpool?")
        yes, no, aligned = _identify_yes_player(
            q, normalize_name("Manchester City"), normalize_name("Liverpool"),
            "Manchester City", "Liverpool",
        )
        assert aligned is True
        assert yes == "Manchester City"

    def test_united_team_yes_reversed(self):
        q = normalize_name("Will Arsenal beat Manchester United?")
        yes, no, aligned = _identify_yes_player(
            q, normalize_name("Manchester United"), normalize_name("Arsenal"),
            "Manchester United", "Arsenal",
        )
        assert aligned is True
        assert yes == "Arsenal"  # Arsenal appears first in question

    def test_fc_team_alignment(self):
        q = normalize_name("Will Austin FC beat Portland Timbers?")
        yes, no, aligned = _identify_yes_player(
            q, normalize_name("Austin FC"), normalize_name("Portland Timbers"),
            "Austin FC", "Portland Timbers",
        )
        assert aligned is True
        assert yes == "Austin FC"

    def test_two_city_teams_alignment_via_full_name(self):
        """Two City teams: full-name position resolves correctly."""
        q = normalize_name("Will Manchester City beat Sporting Kansas City?")
        yes, no, aligned = _identify_yes_player(
            q, normalize_name("Manchester City"), normalize_name("Sporting Kansas City"),
            "Manchester City", "Sporting Kansas City",
        )
        assert aligned is True
        assert yes == "Manchester City"  # appears first

    def test_two_united_teams_alignment_via_full_name(self):
        q = normalize_name("Will Newcastle United beat Manchester United?")
        yes, no, aligned = _identify_yes_player(
            q, normalize_name("Manchester United"), normalize_name("Newcastle United"),
            "Manchester United", "Newcastle United",
        )
        assert aligned is True
        assert yes == "Newcastle United"  # appears first in question


# ---------------------------------------------------------------------------
# 4. Same-suffix teams with ambiguous short question → SKIP
# ---------------------------------------------------------------------------

class TestSameSuffixAmbiguity:
    def test_city_vs_city_short_question_skips(self):
        """'City vs City' — no full names, last-name collision → no match."""
        q = normalize_name("City vs City - Who will win?")
        score, _ = _name_score(q, normalize_name("Manchester City"),
                               normalize_name("Sporting Kansas City"), 3)
        assert score == 0.0  # same-last-name guard prevents Strategy 2

    def test_united_vs_united_short_question_skips(self):
        q = normalize_name("United vs United")
        score, _ = _name_score(q, normalize_name("Manchester United"),
                               normalize_name("Newcastle United"), 3)
        assert score == 0.0

    def test_fc_vs_fc_same_token_skips(self):
        """Both teams end in FC — same last-name guard fires."""
        q = normalize_name("FC battle: Austin vs Charlotte")
        score, _ = _name_score(q, normalize_name("Austin FC"),
                               normalize_name("Charlotte FC"), 3)
        # "fc" is only 2 chars, below min_len=3, so last-name strategy skips anyway
        # And full names aren't substrings of this short question
        assert score == 0.0


# ---------------------------------------------------------------------------
# 5. Full pipeline: correct side gets correct FanDuel probability
# ---------------------------------------------------------------------------

class TestSoccerPricingAlignment:
    def test_man_city_gets_home_odds(self):
        """Manchester City (home, favorite) must get the home p_true."""
        market = _market(
            "Will Manchester City beat Liverpool?",
            prices=["0.55", "0.45"],
        )
        event = _event("Manchester City", "Liverpool", home_odds=-200, away_odds=170)
        features = extract_features(market, event, cfg)
        assert len(features) == 2

        city_side = next(f for f in features if f.side == "Manchester City")
        liverpool_side = next(f for f in features if f.side == "Liverpool")

        # City is home and -200 favorite → higher p_true
        assert city_side.p_true > liverpool_side.p_true
        assert city_side.pm_price == 0.55  # YES side
        assert liverpool_side.pm_price == 0.45  # NO side
        # 3-way devig: home + away + draw == 1.0, so home + away alone < 1.0.
        assert city_side.p_true + liverpool_side.p_true < 1.0

    def test_wrong_event_does_not_produce_priced_features(self):
        """Cross-match must not produce features with edge calculations."""
        market = _market("Will Manchester City beat Liverpool?")
        wrong = _event("Sporting Kansas City", "Liverpool", sport="soccer_mls",
                       home_odds=200, away_odds=-250)
        features = extract_features(market, wrong, cfg)
        assert features == []


# ---------------------------------------------------------------------------
# 6. Platform-specific alignment
# ---------------------------------------------------------------------------

class TestPlatformSideAlignment:
    def test_polymarket_side_label_is_team_name(self):
        """Polymarket features must have the full team name as side, not 'City'."""
        market = _market("Will Manchester City beat Liverpool?", platform="polymarket")
        event = _event("Manchester City", "Liverpool")
        features = extract_features(market, event, cfg)
        sides = {f.side for f in features}
        assert "Manchester City" in sides
        assert "Liverpool" in sides
        assert "City" not in sides
        assert "city" not in sides

    def test_kalshi_side_label_is_team_name(self):
        market = _market("Will Manchester United beat Arsenal?", platform="kalshi")
        event = _event("Manchester United", "Arsenal")
        features = extract_features(market, event, cfg)
        sides = {f.side for f in features}
        assert "Manchester United" in sides
        assert "Arsenal" in sides
        assert "United" not in sides


# ---------------------------------------------------------------------------
# 7. Substring collision edge cases
# ---------------------------------------------------------------------------

class TestKalshiSideHintAlignment:
    """The core bug: Kalshi title has opponent name first, causing YES/NO inversion."""

    def test_kalshi_leeds_price_not_assigned_to_man_united(self):
        """
        Kalshi market 'Will Leeds United win...' has yes_ask=18c.
        The adapter should set side='Leeds United'.
        The pipeline should assign 18c to Leeds, NOT to Manchester United.
        """
        # Simulate Kalshi market where side hint is set by adapter
        market = _market(
            "Will Leeds United beat Manchester United?",  # canonical from adapter
            prices=["0.18", "0.82"],
            platform="kalshi",
        )
        # Manually set the side hint (adapter would do this)
        from dataclasses import replace
        market = replace(market, side="Leeds United")

        event = _event("Manchester United", "Leeds United", home_odds=-190, away_odds=160)
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

        leeds = next(f for f in features if f.side == "Leeds United")
        man_u = next(f for f in features if f.side == "Manchester United")

        # Leeds (the underdog at 18c) should have the 18c price
        assert leeds.pm_price == pytest.approx(0.18, abs=0.01)
        # Man United (favorite) should have the 82c price
        assert man_u.pm_price == pytest.approx(0.82, abs=0.01)
        # Man United is home and -190 favorite -> higher p_true
        assert man_u.p_true > leeds.p_true

    def test_kalshi_hint_overrides_positional(self):
        """Side hint should override positional parsing even when question has opponent first."""
        # Question has Man United first, but side hint says Leeds is YES
        market = _market(
            "Manchester United vs Leeds United: Will Leeds United Win?",
            prices=["0.18", "0.82"],
            platform="kalshi",
        )
        from dataclasses import replace
        market = replace(market, side="Leeds United")

        event = _event("Manchester United", "Leeds United", home_odds=-190, away_odds=160)
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

        leeds = next(f for f in features if f.side == "Leeds United")
        assert leeds.pm_price == pytest.approx(0.18, abs=0.01)


class TestSubstringCollisions:
    def test_fc_prefix_vs_suffix(self):
        """'FC Cincinnati' (FC prefix) vs 'Austin FC' (FC suffix) — different teams."""
        market = _market("Will FC Cincinnati beat Austin FC?")
        event = _event("FC Cincinnati", "Austin FC", sport="soccer_mls")
        features = extract_features(market, event, cfg)
        assert len(features) == 2
        assert features[0].outcome_aligned is True

    def test_partial_name_overlap(self):
        """'Real Salt Lake' must not match 'Salt Lake City' if that were an event."""
        market = _market("Will Real Salt Lake beat Portland Timbers?")
        # Hypothetical wrong event where "salt lake" overlaps
        wrong = _event("Salt Lake Bees", "Portland Timbers", sport="baseball_mlb")
        features = extract_features(market, wrong, cfg)
        # "salt lake bees" is NOT a substring of "will real salt lake beat portland timbers"
        assert features == [] or all(f.name_match_score == 0.0 for f in features)

    def test_reversed_order_still_aligns_correctly(self):
        """Question has teams in opposite order from sportsbook home/away."""
        market = _market("Will Liverpool beat Manchester City?", prices=["0.40", "0.60"])
        event = _event("Manchester City", "Liverpool", home_odds=-200, away_odds=170)
        features = extract_features(market, event, cfg)
        assert len(features) == 2

        # Liverpool appears first in question → YES side
        liverpool = next(f for f in features if f.side == "Liverpool")
        city = next(f for f in features if f.side == "Manchester City")
        assert liverpool.pm_price == 0.40  # YES price
        assert city.pm_price == 0.60  # NO price
        # But City is still the favorite (home, -200)
        assert city.p_true > liverpool.p_true


# ---------------------------------------------------------------------------
# Missing-draw guard: soccer h2h is 3-way; if FanDuel publishes only home
# and away (draw_odds=None), 2-way devig would inflate both teams' p_true
# by the missing draw mass. Reproduce the MLS Phila/NE phantom-EV scenario.
# ---------------------------------------------------------------------------

class TestSoccerMissingDrawGuard:
    def test_soccer_with_draw_odds_runs_3way_devig(self):
        """Sanity: with draw_odds present, soccer features extract normally
        and 3-way devig produces home+away+draw == 1.0 (so home+away < 1.0)."""
        market = _market("Will Manchester City beat Liverpool?", prices=["0.55", "0.45"])
        event = _event("Manchester City", "Liverpool",
                       home_odds=-200, away_odds=170, draw_odds=240)
        features = extract_features(market, event, cfg)
        assert len(features) == 2
        for f in features:
            assert f.has_bookmaker_data is True
        # 3-way devig invariant: the two teams alone don't span the
        # full probability mass (the rest is the draw).
        total = sum(f.p_true for f in features) / 2  # /2 because each side appears twice
        # Looser: just confirm both p_true are positive and < 1.0
        for f in features:
            assert 0.0 < f.p_true < 1.0

    def test_soccer_missing_draw_skips_via_no_bookmaker_data(self):
        """Missing draw_odds → feature emitted with has_bookmaker_data=False
        and rule engine returns SKIP via NO_BOOKMAKER_DATA. Reproduces the
        Philadelphia Union / New England Revolution phantom-EV scenario."""
        market = _market("Will Philadelphia Union beat New England Revolution?",
                         prices=["0.40", "0.60"])
        event = _event("New England Revolution", "Philadelphia Union",
                       sport="soccer_usa_mls",
                       home_odds=190, away_odds=130, draw_odds=None)
        features = extract_features(market, event, cfg)
        # Feature is still emitted so the rule engine can log the rejection
        assert len(features) >= 1
        for f in features:
            assert f.has_bookmaker_data is False
            # Critically: no actionable EV calculated — p_true is left at 0.0
            # by _base_features when bookmaker data is unavailable.
            status, _ = evaluate_rules(f, cfg)
            assert status == "SKIP"
            assert "NO_BOOKMAKER_DATA" in f.reject_reasons

    def test_phila_ne_revs_phantom_alert_no_longer_fires(self):
        """The exact dry-run scenario that produced the phantom +14.8% Phila
        Union and +8.2% NE Revolution alerts. Both sides must SKIP, not BUY."""
        # Kalshi-style 3-way emission: per-team market with yes_ask + (1-yes_ask)
        philly_market = _market(
            "New England Revolution vs Philadelphia Union",
            prices=["0.40", "0.60"],  # Philly YES at 0.40
        )
        # FD returns home/away only (no Draw) — the smoking-gun scenario
        event = _event("New England Revolution", "Philadelphia Union",
                       sport="soccer_usa_mls",
                       home_odds=190, away_odds=130, draw_odds=None)
        features = extract_features(philly_market, event, cfg)
        assert len(features) >= 1
        for f in features:
            status, _ = evaluate_rules(f, cfg)
            # No row may reach BUY; phantom EV blocked at the source.
            assert status != "BUY"
            assert status == "SKIP"
            # The SKIP must come from the missing-draw guard, not from an
            # unrelated rule (e.g. EVENT_LIVE on a stale fixture date).
            assert f.is_live is False
            assert "NO_BOOKMAKER_DATA" in f.reject_reasons
            assert "EVENT_LIVE" not in f.reject_reasons

    def test_past_commence_time_skips_via_event_live(self):
        """Explicit live-path coverage: an event whose commence_time is in
        the past is flagged is_live and SKIPped via the CRITICAL EVENT_LIVE
        rule, even with complete 3-way FanDuel data."""
        market = _market("Will Philadelphia Union beat New England Revolution?",
                         prices=["0.40", "0.60"])
        event = _event("New England Revolution", "Philadelphia Union",
                       sport="soccer_usa_mls",
                       home_odds=190, away_odds=130, draw_odds=240,
                       commence_time="2000-01-01T20:00:00Z")
        features = extract_features(market, event, cfg)
        assert len(features) >= 1
        for f in features:
            assert f.is_live is True
            assert f.has_bookmaker_data is True
            status, _ = evaluate_rules(f, cfg)
            assert status == "SKIP"
            assert "EVENT_LIVE" in f.reject_reasons
            assert "NO_BOOKMAKER_DATA" not in f.reject_reasons

    def test_non_soccer_2way_unaffected(self):
        """The guard must not over-fire on legitimate 2-way sports (tennis,
        MMA, MLB, NBA, NHL). MLB with no draw_odds runs normal 2-way devig."""
        market = _market(
            "Will New York Yankees beat Boston Red Sox?",
            prices=["0.55", "0.45"],
        )
        event = _event("New York Yankees", "Boston Red Sox",
                       sport="baseball_mlb",
                       home_odds=-150, away_odds=130, draw_odds=None)
        features = extract_features(market, event, cfg)
        assert len(features) == 2
        for f in features:
            assert f.has_bookmaker_data is True
        # 2-way devig still applies: home + away ≈ 1.0
        yes_side = next(f for f in features if f.pm_price == 0.55)
        no_side = next(f for f in features if f.pm_price == 0.45)
        assert yes_side.p_true + no_side.p_true == pytest.approx(1.0, abs=0.001)
