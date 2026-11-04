"""
Tests for 3-way devigging (soccer markets with draw) and the
corrected edge calculation pipeline.

Covers:
  - devig_3way produces correct probabilities
  - 3-way devigged p_true is lower than 2-way (draw probability absorbed)
  - extract_h2h_features uses 3-way devig when draw_odds present
  - extract_h2h_features uses 2-way devig when draw_odds absent (tennis)
  - Edge is correct for 3-way markets
  - Phantom edge eliminated
"""
import pytest

from services.devig import devig_multiplicative, devig_3way
from services.feature_extractor import extract_features, _american_to_implied
from services.engine_config import EngineConfig
from services.normalizer import normalize_name
from services.odds_provider import TennisOddsEvent, BookmakerLine
from services.adapters.base import NormalizedMarket


def _soccer_event(
    home: str = "Manchester City",
    away: str = "Arsenal",
    home_odds: int = 100,
    away_odds: int = 200,
    draw_odds: int = 250,
) -> TennisOddsEvent:
    return TennisOddsEvent(
        event_id="ev_soccer",
        sport_key="soccer_epl",
        tournament="EPL",
        home_player=home,
        away_player=away,
        home_player_norm=normalize_name(home),
        away_player_norm=normalize_name(away),
        commence_time="2099-06-01T20:00:00Z",
        bookmakers=[BookmakerLine(
            bookmaker_key="fanduel",
            bookmaker_title="FanDuel",
            home_odds=home_odds,
            away_odds=away_odds,
            last_update="2099-06-01T20:00:00Z",
            draw_odds=draw_odds,
        )],
    )


def _tennis_event(
    home: str = "Carlos Alcaraz",
    away: str = "Jannik Sinner",
    home_odds: int = -180,
    away_odds: int = 150,
) -> TennisOddsEvent:
    return TennisOddsEvent(
        event_id="ev_tennis",
        sport_key="tennis_atp",
        tournament="ATP",
        home_player=home,
        away_player=away,
        home_player_norm=normalize_name(home),
        away_player_norm=normalize_name(away),
        commence_time="2099-06-01T12:00:00Z",
        bookmakers=[BookmakerLine(
            bookmaker_key="fanduel",
            bookmaker_title="FanDuel",
            home_odds=home_odds,
            away_odds=away_odds,
            last_update="2099-06-01T12:00:00Z",
            draw_odds=None,
        )],
    )


def _market(question: str, prices=None) -> NormalizedMarket:
    return NormalizedMarket(
        platform="polymarket", market_id="m1", event=question,
        market_type="h2h", side="", line=None, price=0.55,
        liquidity=5000.0, url=None, timestamp=None,
        question=question, end_date="2099-06-02T06:00:00Z",
        outcome_prices=prices or ["0.42", "0.58"], event_slug="test",
    )


# ---------------------------------------------------------------------------
# devig_3way unit tests
# ---------------------------------------------------------------------------

class TestDevig3Way:
    def test_sums_to_one(self):
        h, a, d = devig_3way(0.50, 0.33, 0.29)
        assert h + a + d == pytest.approx(1.0, abs=0.001)

    def test_proportions_preserved(self):
        h, a, d = devig_3way(0.50, 0.33, 0.29)
        # Home had the highest implied → should have highest true prob
        assert h > a > d

    def test_lower_than_2way(self):
        """3-way devig should produce LOWER team probs than 2-way (draw eats probability)."""
        h_impl, a_impl, d_impl = 0.50, 0.33, 0.29
        h_3, a_3, _ = devig_3way(h_impl, a_impl, d_impl)
        h_2, a_2 = devig_multiplicative(h_impl, a_impl)
        assert h_3 < h_2
        assert a_3 < a_2

    def test_zero_overround(self):
        h, a, d = devig_3way(0.0, 0.0, 0.0)
        assert h == pytest.approx(0.3333, abs=0.001)

    def test_typical_soccer_odds(self):
        """Typical EPL match: home +100, away +200, draw +250."""
        h_impl = _american_to_implied(100)   # 0.5000
        a_impl = _american_to_implied(200)   # 0.3333
        d_impl = _american_to_implied(250)   # 0.2857
        h, a, d = devig_3way(h_impl, a_impl, d_impl)
        # True home prob should be ~42-47%, not ~58-60%
        assert h < 0.50
        assert h > 0.35
        # Draw should be ~25%
        assert d > 0.20
        assert d < 0.30


# ---------------------------------------------------------------------------
# Pipeline uses correct devig method
# ---------------------------------------------------------------------------

class TestPipelineDevigSelection:
    def test_soccer_uses_3way_devig(self):
        """Soccer event with draw_odds → 3-way devig → lower p_true."""
        market = _market("Will Manchester City beat Arsenal?", ["0.42", "0.58"])
        event = _soccer_event(home_odds=100, away_odds=200, draw_odds=250)
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

        city = next(f for f in features if f.side == "Manchester City")
        # With 3-way devig, p_true for +100 should be ~44%, not ~58%
        assert city.p_true < 0.50
        # p_true_home + p_true_away should be < 1.0 (draw takes the rest)
        arsenal = next(f for f in features if f.side == "Arsenal")
        assert city.p_true + arsenal.p_true < 1.0

    def test_tennis_uses_2way_devig(self):
        """Tennis event without draw_odds → 2-way devig → probs sum to 1.0."""
        market = _market("Will Alcaraz beat Sinner?", ["0.55", "0.45"])
        event = _tennis_event(home_odds=-180, away_odds=150)
        features = extract_features(market, event, EngineConfig())
        assert len(features) == 2

        alcaraz = next(f for f in features if f.side == "Carlos Alcaraz")
        sinner = next(f for f in features if f.side == "Jannik Sinner")
        assert alcaraz.p_true + sinner.p_true == pytest.approx(1.0, abs=0.001)

    def test_soccer_phantom_edge_eliminated(self):
        """
        Before fix: Man City +100, pm_price=0.42 → edge ~+15% (phantom).
        After fix: 3-way devig gives p_true ~0.44, edge ~0.01 (real).
        """
        market = _market("Will Manchester City beat Arsenal?", ["0.42", "0.58"])
        event = _soccer_event(home_odds=100, away_odds=200, draw_odds=250)
        features = extract_features(market, event, EngineConfig())

        city = next(f for f in features if f.side == "Manchester City")
        # Edge should be small (near 0 or slightly positive/negative), not +15%
        assert city.edge < 0.10  # was ~0.15 before fix
        # The exact value depends on the specific odds, but it should not be
        # implausibly large

    def test_soccer_overround_includes_draw(self):
        """Overround should be computed from all 3 outcomes."""
        market = _market("Will Manchester City beat Arsenal?", ["0.42", "0.58"])
        event = _soccer_event(home_odds=100, away_odds=200, draw_odds=250)
        features = extract_features(market, event, EngineConfig())

        city = next(f for f in features if f.side == "Manchester City")
        # 3-way overround: 0.50 + 0.33 + 0.29 - 1.0 ≈ 0.12
        # 2-way would have been: 0.50 + 0.33 - 1.0 = -0.17 (nonsensical)
        assert city.fanduel_overround > 0
