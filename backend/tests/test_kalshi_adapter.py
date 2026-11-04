"""
Tests for services.adapters.kalshi — market pairing and price synthesis.

Covers:
  - M1/M2 ordering stability (deterministic by ticker)
  - Best-price selection across paired markets
  - Single-market fallback
  - Cursor-based pagination
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.adapters.kalshi import _build_event_markets, _extract_title_subject, KalshiAdapter
from services.adapters.base import NormalizedMarket


def _build_one(event_ticker, market_group, fetch_ts=0.0) -> NormalizedMarket | None:
    """Unwrap _build_event_markets for 2-way tests that expect a single result."""
    results = _build_event_markets(event_ticker, market_group, fetch_ts)
    return results[0] if results else None


def _make_market_entry(
    ticker: str,
    title: str,
    yes_ask: float,
    no_ask: float,
    volume: float = 100.0,
    liquidity: float = 500.0,
    close_time: str = "2026-06-01T18:00:00Z",
) -> tuple[dict, str, str]:
    return (
        {
            "ticker": ticker,
            "title": title,
            "yes_ask_dollars": str(yes_ask),
            "no_ask_dollars": str(no_ask),
            "yes_bid_dollars": str(yes_ask - 0.02),
            "no_bid_dollars": str(no_ask - 0.02),
            "volume_fp": str(volume),
            "liquidity_dollars": str(liquidity),
            "close_time": close_time,
            "event_ticker": "EVT-123",
        },
        "KXATPMATCH",
        "atp-tennis-match",
    )


class TestBuildEventMarketOrdering:
    def test_m1_m2_stable_regardless_of_input_order(self):
        """Same result whether API returns market A or B first."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win the Alcaraz vs Sinner match?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        mb = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win the Alcaraz vs Sinner match?",
            yes_ask=0.40,
            no_ask=0.62,
        )

        result_ab = _build_one("EVT-123", [ma, mb])
        result_ba = _build_one("EVT-123", [mb, ma])

        assert result_ab is not None
        assert result_ba is not None
        assert result_ab.outcome_prices == result_ba.outcome_prices
        assert result_ab.market_id == result_ba.market_id
        assert result_ab.question == result_ba.question

    def test_best_price_selection(self):
        """Picks cheapest route for each player across the pair."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.62,  # buy Alcaraz via A yes
            no_ask=0.40,   # buy Sinner via A no
        )
        mb = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.42,  # buy Sinner via B yes
            no_ask=0.60,   # buy Alcaraz via B no
        )

        result = _build_one("EVT-123", [ma, mb])
        assert result is not None

        prices = [float(p) for p in result.outcome_prices]
        # M1 = A (sorted by ticker). best_A = min(A.yes_ask=0.62, B.no_ask=0.60) = 0.60
        assert prices[0] == pytest.approx(0.60, abs=0.01)
        # best_B = min(B.yes_ask=0.42, A.no_ask=0.40) = 0.40
        assert prices[1] == pytest.approx(0.40, abs=0.01)


class TestSingleMarketFallback:
    def test_single_market_produces_result(self):
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win the match?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        result = _build_one("EVT-123", [ma])
        assert result is not None
        assert result.platform == "kalshi"
        prices = [float(p) for p in result.outcome_prices]
        assert prices[0] == pytest.approx(0.65, abs=0.01)

    def test_single_market_uses_actual_no_ask(self):
        """Fallback should use actual no_ask_dollars, not synthetic 1.0 - yes_ask."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win the match?",
            yes_ask=0.65,
            no_ask=0.38,  # real no_ask differs from 1.0 - 0.65 = 0.35
        )
        result = _build_one("EVT-123", [(ma_dict, series, slug)])
        assert result is not None
        prices = [float(p) for p in result.outcome_prices]
        assert prices[1] == pytest.approx(0.38, abs=0.001)  # actual, not 0.35

    def test_single_market_missing_no_ask_falls_back(self):
        """If no_ask is 0 or missing, synthesize from 1.0 - yes_ask."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.65,
            no_ask=0.0,  # missing
        )
        ma_dict["no_ask_dollars"] = "0"
        result = _build_one("EVT-123", [(ma_dict, series, slug)])
        assert result is not None
        prices = [float(p) for p in result.outcome_prices]
        assert prices[1] == pytest.approx(0.35, abs=0.001)  # synthesized

    def test_single_market_out_of_range_returns_none(self):
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.99,
            no_ask=0.01,
        )
        result = _build_one("EVT-123", [ma])
        assert result is None

    def test_single_market_sets_side_hint(self):
        """Single market should set side to the title subject (YES player)."""
        ma = _make_market_entry(
            ticker="KXEPLGAME-A",
            title="Will Leeds United win the Manchester United vs Leeds United EPL match?",
            yes_ask=0.18,
            no_ask=0.83,
        )
        result = _build_one("EVT-456", [ma])
        assert result is not None
        assert result.side == "Leeds United"
        # outcome_prices[0] should be Leeds' price (18c)
        assert float(result.outcome_prices[0]) == pytest.approx(0.18, abs=0.01)


class TestExtractTitleSubject:
    def test_will_x_win(self):
        assert _extract_title_subject("Will Manchester City win the EPL match?") == "Manchester City"

    def test_will_x_beat(self):
        assert _extract_title_subject("Will Leeds United beat Arsenal?") == "Leeds United"

    def test_complex_title(self):
        assert _extract_title_subject(
            "Will Sporting Kansas City win the Sporting Kansas City vs Real Salt Lake MLS match?"
        ) == "Sporting Kansas City"

    def test_no_match(self):
        assert _extract_title_subject("Some random title") == ""

    def test_empty(self):
        assert _extract_title_subject("") == ""


class TestPairedMarketSideHint:
    def test_paired_market_sets_side_to_m1_team(self):
        """Paired markets should set side to M1's team (identified by ticker suffix)."""
        ma = _make_market_entry(
            ticker="KXEPLGAME-ARS",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.55,
            no_ask=0.47,
        )
        mb = _make_market_entry(
            ticker="KXEPLGAME-LIV",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.45,
            no_ask=0.57,
        )
        result = _build_one("EVT-789", [ma, mb])
        assert result is not None
        # M1 = ARS (sorted), side should be Arsenal
        assert result.side == "Arsenal"

    def test_paired_market_question_has_m1_first(self):
        """Canonical question should have M1's team first."""
        ma = _make_market_entry(
            ticker="KXEPLGAME-ARS",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.55,
            no_ask=0.47,
        )
        mb = _make_market_entry(
            ticker="KXEPLGAME-LIV",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.45,
            no_ask=0.57,
        )
        result = _build_one("EVT-789", [ma, mb])
        assert result is not None
        assert "Arsenal" in result.question
        assert result.question.index("Arsenal") < result.question.index("Liverpool")


class TestYesSubTitle:
    """Tests for yes_sub_title extraction — preferred over ticker/title heuristics."""

    def test_paired_market_uses_yes_sub_title(self):
        """When yes_sub_title is present, use it instead of ticker-suffix heuristics."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXNBAGAME-26APR10DETCHA-DET",
            title="Detroit vs Charlotte Winner?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        ma_dict["yes_sub_title"] = "Detroit Pistons"

        mb_dict, _, _ = _make_market_entry(
            ticker="KXNBAGAME-26APR10DETCHA-CHA",
            title="Detroit vs Charlotte Winner?",
            yes_ask=0.37,
            no_ask=0.65,
        )
        mb_dict["yes_sub_title"] = "Charlotte Hornets"

        result = _build_one(
            "KXNBAGAME-26APR10DETCHA",
            [(ma_dict, series, slug), (mb_dict, series, slug)],
        )
        assert result is not None
        # M1 = CHA (sorted by ticker), so side = Charlotte Hornets
        assert result.side == "Charlotte Hornets"
        assert "Charlotte Hornets" in result.question
        assert "Detroit Pistons" in result.question

    def test_paired_market_falls_back_without_yes_sub_title(self):
        """Without yes_sub_title, existing ticker-suffix heuristics are used."""
        ma = _make_market_entry(
            ticker="KXEPLGAME-ARS",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.55,
            no_ask=0.47,
        )
        mb = _make_market_entry(
            ticker="KXEPLGAME-LIV",
            title="Arsenal vs Liverpool winner?",
            yes_ask=0.45,
            no_ask=0.57,
        )
        # No yes_sub_title in _make_market_entry → falls back to _identify_m1_team
        result = _build_one("EVT-789", [ma, mb])
        assert result is not None
        assert result.side == "Arsenal"

    def test_single_market_uses_yes_sub_title(self):
        """Single-market fallback should prefer yes_sub_title over title parsing."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXNBAGAME-26APR10DETCHA-DET",
            title="Detroit vs Charlotte Winner?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        ma_dict["yes_sub_title"] = "Detroit Pistons"

        result = _build_one(
            "KXNBAGAME-26APR10DETCHA",
            [(ma_dict, series, slug)],
        )
        assert result is not None
        assert result.side == "Detroit Pistons"

    def test_single_market_falls_back_without_yes_sub_title(self):
        """Single market without yes_sub_title uses title parsing."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win the match?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        result = _build_one("EVT-123", [ma])
        assert result is not None
        assert result.side == "Alcaraz"

    def test_yes_sub_title_empty_string_triggers_fallback(self):
        """An empty yes_sub_title should trigger fallback, not set side to ''."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win the match?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        ma_dict["yes_sub_title"] = ""

        result = _build_one("EVT-123", [(ma_dict, series, slug)])
        assert result is not None
        assert result.side == "Alcaraz"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _make_api_market(ticker: str, event_ticker: str, title: str, yes_ask: float) -> dict:
    """Build a raw Kalshi API market dict for pagination tests."""
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "title": title,
        "yes_ask_dollars": str(yes_ask),
        "no_ask_dollars": str(round(1.0 - yes_ask, 4)),
        "yes_bid_dollars": str(yes_ask - 0.02),
        "no_bid_dollars": str(round(1.0 - yes_ask - 0.02, 4)),
        "volume_fp": "100",
        "liquidity_dollars": "500",
        "close_time": "2026-06-01T18:00:00Z",
    }


def _mock_response(markets: list[dict], cursor: str | None = None) -> MagicMock:
    """Build a mock httpx response with .json() and .raise_for_status()."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"markets": markets, "cursor": cursor}
    return resp


class TestPagination:
    """Tests for cursor-based pagination in KalshiAdapter.fetch_markets."""

    def test_multi_page_fetches_all_markets(self):
        """Adapter follows cursor across multiple pages."""
        page1_markets = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]
        page2_markets = [
            _make_api_market("KXATPMATCH-EVT2-C", "EVT2", "Will C win?", 0.55),
            _make_api_market("KXATPMATCH-EVT2-D", "EVT2", "Will D win?", 0.47),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(page1_markets, cursor="page2_cursor"),
            _mock_response(page2_markets, cursor=None),
        ])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert len(results) == 2
        event_slugs = {r.event_slug for r in results}
        assert "EVT1" in event_slugs
        assert "EVT2" in event_slugs
        assert mock_client.get.call_count == 2

    def test_single_page_no_cursor(self):
        """When API returns no cursor, adapter stops after one request."""
        markets = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(markets, cursor=None))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert len(results) == 1
        assert mock_client.get.call_count == 1

    def test_no_duplicate_markets_across_pages(self):
        """Markets from different pages should not produce duplicates."""
        page1 = [
            _make_api_market("KXATPMATCH-E1-A", "E1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-E1-B", "E1", "Will B win?", 0.42),
        ]
        page2 = [
            _make_api_market("KXATPMATCH-E2-C", "E2", "Will C win?", 0.55),
            _make_api_market("KXATPMATCH-E2-D", "E2", "Will D win?", 0.47),
        ]
        page3 = [
            _make_api_market("KXATPMATCH-E3-E", "E3", "Will E win?", 0.70),
            _make_api_market("KXATPMATCH-E3-F", "E3", "Will F win?", 0.32),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(page1, cursor="c2"),
            _mock_response(page2, cursor="c3"),
            _mock_response(page3, cursor=None),
        ])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert len(results) == 3
        market_ids = [r.market_id for r in results]
        assert len(market_ids) == len(set(market_ids)), "Duplicate market_ids found"
        assert mock_client.get.call_count == 3

    def test_empty_cursor_string_stops_pagination(self):
        """An empty string cursor (not None) should also stop pagination."""
        markets = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(markets, cursor=""))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert len(results) == 1
        assert mock_client.get.call_count == 1

    def test_min_close_ts_sent_in_request(self):
        """API request includes min_close_ts set to current time."""
        import time

        markets = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(markets, cursor=None))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        before = int(time.time())
        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                asyncio.run(adapter.fetch_markets())
        after = int(time.time())

        # Inspect the params passed to the first GET call
        call_kwargs = mock_client.get.call_args_list[0]
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        assert "min_close_ts" in params
        ts = params["min_close_ts"]
        assert before <= ts <= after, f"min_close_ts {ts} not between {before} and {after}"

    def test_expired_markets_excluded_by_api_filter(self):
        """Only future markets are returned — expired ones are filtered by the API via min_close_ts."""
        future_markets = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(future_markets, cursor=None))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert len(results) == 1
        for call in mock_client.get.call_args_list:
            params = call.kwargs.get("params") or call[1].get("params")
            assert "min_close_ts" in params
            assert isinstance(params["min_close_ts"], int)


class TestStaleness:
    """Tests for fetched_at / staleness propagation."""

    def test_kalshi_fetched_at_uses_last_updated_ts(self):
        """When Kalshi API provides last_updated_ts, it becomes fetched_at."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        ma_dict["last_updated_ts"] = 1712500000

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.42,
            no_ask=0.60,
        )
        mb_dict["last_updated_ts"] = 1712500100  # more recent

        result = _build_one(
            "EVT-123",
            [(ma_dict, series, slug), (mb_dict, series, slug)],
            fetch_ts=1712499000.0,
        )
        assert result is not None
        # Should use max of the two API timestamps
        assert result.fetched_at == 1712500100.0

    def test_kalshi_fetched_at_falls_back_to_fetch_ts(self):
        """Without last_updated_ts, fetched_at uses the adapter's fetch timestamp."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.42,
            no_ask=0.60,
        )

        result = _build_one(
            "EVT-123",
            [(ma_dict, series, slug), (mb_dict, series, slug)],
            fetch_ts=1712499000.0,
        )
        assert result is not None
        assert result.fetched_at == 1712499000.0

    def test_single_market_fetched_at_with_api_ts(self):
        """Single-market fallback uses last_updated_ts when available."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        ma_dict["last_updated_ts"] = 1712500000

        result = _build_one(
            "EVT-123",
            [(ma_dict, series, slug)],
            fetch_ts=1712499000.0,
        )
        assert result is not None
        assert result.fetched_at == 1712500000.0

    def test_single_market_fetched_at_without_api_ts(self):
        """Single-market fallback uses fetch_ts when no API timestamp."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.65,
            no_ask=0.37,
        )

        result = _build_one(
            "EVT-123",
            [(ma_dict, series, slug)],
            fetch_ts=1712499000.0,
        )
        assert result is not None
        assert result.fetched_at == 1712499000.0

    def test_fetched_at_propagates_through_pipeline(self):
        """fetched_at flows from NormalizedMarket → MarketFeatures → EvaluatedOpportunity."""
        from services.adapters.base import NormalizedMarket
        from services.rule_engine import MarketFeatures
        from services.opportunities import EvaluatedOpportunity

        nm = NormalizedMarket(
            platform="kalshi", market_id="TEST", event="A vs B",
            market_type="h2h", side="A", line=None, price=0.60,
            liquidity=500.0, url=None, timestamp=None,
            question="Will A beat B?", end_date=None,
            outcome_prices=["0.60", "0.40"], event_slug="EVT",
            fetched_at=1712500000.0,
        )
        assert nm.fetched_at == 1712500000.0

        mf = MarketFeatures(price_fetched_at=nm.fetched_at)
        assert mf.price_fetched_at == 1712500000.0

        eo = EvaluatedOpportunity(
            platform="kalshi", sport="tennis", event="A vs B",
            event_url=None, tournament="ATP", start_time="2026-04-08T12:00:00Z",
            market_id="TEST", market_type="h2h", side="A", line=None,
            pm_price=0.60, fd_odds=-150, p_true=0.65, edge=0.05,
            recommended_kelly=0.02, kelly_full=0.08,
            fanduel_overround=0.05, fanduel_line_width=0.30,
            fanduel_line_width_label="Moderate", fanduel_confidence_label="High",
            event_match_confidence=0.95, match_quality="verified",
            matched_event_id="E1", second_best_event_id="",
            confidence_gap=1.0, competing_matches=1, has_shared_last_name=False,
            status="BUY", reject_reasons=[], downgrade_reasons=[],
            home_tokens=(), away_tokens=(), name_match_score=1.0,
            date_score=1.0, date_delta_hours=2.0, rule_evaluations=[],
            price_fetched_at=mf.price_fetched_at,
        )
        assert eo.price_fetched_at == 1712500000.0

    def test_default_fetched_at_is_zero(self):
        """Without explicit fetched_at, NormalizedMarket defaults to 0.0."""
        from services.adapters.base import NormalizedMarket

        nm = NormalizedMarket(
            platform="test", market_id="T", event="X",
            market_type="h2h", side="", line=None, price=0.5,
            liquidity=None, url=None, timestamp=None,
            question="Q", end_date=None, outcome_prices=None, event_slug=None,
        )
        assert nm.fetched_at == 0.0


# ---------------------------------------------------------------------------
# 3-way soccer market handling
# ---------------------------------------------------------------------------

def _make_soccer_entry(
    ticker: str,
    yes_sub_title: str,
    yes_ask: float,
    event_ticker: str = "KXEPLGAME-26APR21BRICFC",
) -> tuple[dict, str, str]:
    """Build a raw Kalshi market dict for soccer 3-way tests."""
    return (
        {
            "ticker": ticker,
            "event_ticker": event_ticker,
            "title": "Brighton vs Chelsea winner?",
            "yes_sub_title": yes_sub_title,
            "yes_ask_dollars": str(yes_ask),
            "no_ask_dollars": str(round(1.0 - yes_ask, 4)),
            "yes_bid_dollars": str(yes_ask - 0.02),
            "no_bid_dollars": str(round(1.0 - yes_ask - 0.02, 4)),
            "volume_fp": "100",
            "liquidity_dollars": "500",
            "close_time": "2026-06-01T18:00:00Z",
        },
        "KXEPLGAME",
        "epl-game",
    )


class TestSoccer3Way:
    """3-way soccer market: home, away, draw correctly separated."""

    def test_epl_3way_emits_two_markets(self):
        """EPL event with 3 markets emits two NormalizedMarkets (one per team)."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC", [home, away, draw]
        )

        assert len(results) == 2
        sides = {r.side for r in results}
        assert "Brighton" in sides
        assert "Chelsea" in sides
        # Draw should NOT appear as a side
        assert "Tie" not in sides

    def test_epl_3way_prices_consistent(self):
        """Each team's outcome_prices should sum to 1.0 (YES + NO = 1.0)."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC", [home, away, draw]
        )

        for r in results:
            prices = [float(p) for p in r.outcome_prices]
            total = prices[0] + prices[1]
            assert total == pytest.approx(1.0, abs=0.001), (
                f"{r.side}: YES={prices[0]} + NO={prices[1]} = {total}, expected ~1.0"
            )

    def test_epl_3way_each_team_gets_own_price(self):
        """Each team's pm_price is its own Kalshi yes_ask, not cross-market."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC", [home, away, draw]
        )

        price_by_side = {r.side: r.price for r in results}
        assert price_by_side["Brighton"] == pytest.approx(0.35, abs=0.001)
        assert price_by_side["Chelsea"] == pytest.approx(0.38, abs=0.001)

    def test_epl_3way_draw_not_paired_with_team(self):
        """Even when team ticker sorts after TIE (e.g., TOT, WOL), draw is excluded."""
        # -TOT sorts after -TIE alphabetically
        home = _make_soccer_entry("KXEPLGAME-26APR21BURTOT-BUR", "Burnley", 0.40)
        away = _make_soccer_entry("KXEPLGAME-26APR21BURTOT-TOT", "Tottenham", 0.35)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BURTOT-TIE", "Tie", 0.28)

        results = _build_event_markets(
            "KXEPLGAME-26APR21BURTOT", [home, away, draw]
        )

        assert len(results) == 2
        sides = {r.side for r in results}
        assert sides == {"Burnley", "Tottenham"}

    def test_epl_3way_wolves_suffix_after_tie(self):
        """WOL suffix sorts after TIE — must still pair correctly."""
        home = _make_soccer_entry("KXEPLGAME-26APR21ARSWOL-ARS", "Arsenal", 0.55)
        away = _make_soccer_entry("KXEPLGAME-26APR21ARSWOL-WOL", "Wolves", 0.15)
        draw = _make_soccer_entry("KXEPLGAME-26APR21ARSWOL-TIE", "Tie", 0.32)

        results = _build_event_markets(
            "KXEPLGAME-26APR21ARSWOL", [home, away, draw]
        )

        assert len(results) == 2
        price_by_side = {r.side: r.price for r in results}
        assert price_by_side["Arsenal"] == pytest.approx(0.55, abs=0.001)
        assert price_by_side["Wolves"] == pytest.approx(0.15, abs=0.001)

    def test_mls_3way(self):
        """MLS match with draw market handled correctly."""
        home = _make_soccer_entry(
            "KXMLSGAME-26APR21NYCRBS-NYC", "New York City FC", 0.42,
            event_ticker="KXMLSGAME-26APR21NYCRBS",
        )
        away = _make_soccer_entry(
            "KXMLSGAME-26APR21NYCRBS-RBS", "New York Red Bulls", 0.30,
            event_ticker="KXMLSGAME-26APR21NYCRBS",
        )
        draw = _make_soccer_entry(
            "KXMLSGAME-26APR21NYCRBS-TIE", "Tie", 0.31,
            event_ticker="KXMLSGAME-26APR21NYCRBS",
        )

        results = _build_event_markets(
            "KXMLSGAME-26APR21NYCRBS", [home, away, draw]
        )

        assert len(results) == 2
        sides = {r.side for r in results}
        assert "New York City FC" in sides
        assert "New York Red Bulls" in sides

    def test_draw_heavy_market(self):
        """Draw-dominant market (e.g., 0-0 grind) still produces two team markets."""
        home = _make_soccer_entry("KXEPLGAME-26APR21CRYEVE-CRY", "Crystal Palace", 0.20)
        away = _make_soccer_entry("KXEPLGAME-26APR21CRYEVE-EVE", "Everton", 0.22)
        draw = _make_soccer_entry("KXEPLGAME-26APR21CRYEVE-TIE", "Tie", 0.60)

        results = _build_event_markets(
            "KXEPLGAME-26APR21CRYEVE", [home, away, draw]
        )

        assert len(results) == 2
        for r in results:
            prices = [float(p) for p in r.outcome_prices]
            assert prices[0] + prices[1] == pytest.approx(1.0, abs=0.001)
        # Each team's complement includes draw — NO price should be high
        price_by_side = {r.side: float(r.outcome_prices[1]) for r in results}
        assert price_by_side["Crystal Palace"] == pytest.approx(0.80, abs=0.01)
        assert price_by_side["Everton"] == pytest.approx(0.78, abs=0.01)

    def test_draw_detected_by_yes_sub_title_draw(self):
        """Draw market with yes_sub_title='Draw' (not 'Tie') is detected."""
        home = _make_soccer_entry("KXEPLGAME-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-CFC", "Chelsea", 0.38)
        draw_dict, series, slug = _make_soccer_entry("KXEPLGAME-DRW", "Draw", 0.30)

        results = _build_event_markets(
            "KXEPLGAME-EVT1", [home, away, (draw_dict, series, slug)]
        )

        assert len(results) == 2
        assert "Draw" not in {r.side for r in results}

    def test_2way_unaffected_by_3way_logic(self):
        """NBA 2-way event still uses cross-market best-price logic."""
        ma = _make_market_entry(
            ticker="KXNBAGAME-26APR10DETCHA-CHA",
            title="Detroit vs Charlotte Winner?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        mb = _make_market_entry(
            ticker="KXNBAGAME-26APR10DETCHA-DET",
            title="Detroit vs Charlotte Winner?",
            yes_ask=0.42,
            no_ask=0.60,
        )

        results = _build_event_markets(
            "KXNBAGAME-26APR10DETCHA", [ma, mb]
        )

        # Should be single market (2-way), not two
        assert len(results) == 1
        prices = [float(p) for p in results[0].outcome_prices]
        # Cross-market best-price: min(0.42, 0.42) = 0.42, min(0.60, 0.60) = 0.60
        assert prices[0] + prices[1] == pytest.approx(1.02, abs=0.05)

    def test_3way_input_order_irrelevant(self):
        """Result is the same regardless of API return order for 3-way markets."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        r1 = _build_event_markets("KXEPLGAME-26APR21BRICFC", [home, away, draw])
        r2 = _build_event_markets("KXEPLGAME-26APR21BRICFC", [draw, home, away])
        r3 = _build_event_markets("KXEPLGAME-26APR21BRICFC", [away, draw, home])

        # All orderings produce same set of markets
        for ra, rb in [(r1, r2), (r2, r3)]:
            assert len(ra) == len(rb) == 2
            sides_a = {(r.side, r.price) for r in ra}
            sides_b = {(r.side, r.price) for r in rb}
            assert sides_a == sides_b

    def test_3way_market_id_uses_team_ticker(self):
        """Each emitted market uses the team's ticker as market_id, not draw's."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC", [home, away, draw]
        )

        market_ids = {r.market_id for r in results}
        assert "KXEPLGAME-26APR21BRICFC-BRI" in market_ids
        assert "KXEPLGAME-26APR21BRICFC-CFC" in market_ids
        assert "KXEPLGAME-26APR21BRICFC-TIE" not in market_ids


# ---------------------------------------------------------------------------
# Bid-ask spread
# ---------------------------------------------------------------------------

class TestBidAskSpread:
    """Tests for bid_ask_spread computation."""

    def test_2way_spread_from_m1(self):
        """2-way market computes spread from M1's yes_ask - yes_bid."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        mb = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.42,
            no_ask=0.60,
        )
        result = _build_one("EVT-123", [ma, mb])
        assert result is not None
        # _make_market_entry sets yes_bid = yes_ask - 0.02
        # M1 = A (sorted), spread = 0.60 - 0.58 = 0.02
        assert result.bid_ask_spread == pytest.approx(0.02, abs=0.001)

    def test_3way_spread_per_team(self):
        """3-way market: each team gets its own spread."""
        home = _make_soccer_entry("KXEPLGAME-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-TIE", "Tie", 0.30)

        results = _build_event_markets("KXEPLGAME-EVT", [home, away, draw])

        assert len(results) == 2
        for r in results:
            # yes_bid = yes_ask - 0.02 in helper → spread = 0.02
            assert r.bid_ask_spread == pytest.approx(0.02, abs=0.001)

    def test_single_market_spread(self):
        """Single-market fallback computes spread."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        result = _build_one("EVT-123", [ma])
        assert result is not None
        assert result.bid_ask_spread == pytest.approx(0.02, abs=0.001)

    def test_missing_bid_yields_none(self):
        """If yes_bid is 0 (missing), spread is None."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.65,
            no_ask=0.37,
        )
        ma_dict["yes_bid_dollars"] = "0"

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.37,
            no_ask=0.65,
        )
        mb_dict["yes_bid_dollars"] = "0"

        result = _build_one("EVT-123", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.bid_ask_spread is None

    def test_wide_spread(self):
        """A market with a wide spread reports the correct value."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A",
            title="Will Alcaraz win?",
            yes_ask=0.60,
            no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.50"  # 10c spread

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B",
            title="Will Sinner win?",
            yes_ask=0.42,
            no_ask=0.60,
        )

        result = _build_one("EVT-123", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        # M1 = A (sorted), spread = 0.60 - 0.50 = 0.10
        assert result.bid_ask_spread == pytest.approx(0.10, abs=0.001)

    def test_polymarket_has_no_spread(self):
        """Polymarket NormalizedMarket defaults to None for bid_ask_spread."""
        nm = NormalizedMarket(
            platform="polymarket", market_id="PM1", event="X",
            market_type="h2h", side="", line=None, price=0.5,
            liquidity=None, url=None, timestamp=None,
            question="Q", end_date=None, outcome_prices=None, event_slug=None,
        )
        assert nm.bid_ask_spread is None


# ---------------------------------------------------------------------------
# Top-of-book best_bid / best_ask synthesis
# ---------------------------------------------------------------------------

class TestBestBidAsk:
    """NormalizedMarket carries best_bid / best_ask for maker planning.

    For 2-way binary Kalshi events, buying NO of M2 is economically identical
    to buying YES of M1, so the best YES_M1 bid is `max(M1.yes_bid, M2.no_bid)`.
    For 3-way (soccer), NO_M2 includes draw mass and is NOT a YES_M1 bid;
    only direct M1.yes_bid is used.
    """

    def test_2way_best_bid_uses_cross_market_max(self):
        """M1.yes_bid=0.40, M2.no_bid=0.42 → M1-side best_bid = 0.42."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.40"     # M1.yes_bid

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.42"      # M2.no_bid → cross-market YES_M1 bid

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        # M1 = ticker A (sorted).  best_bid = max(0.40, 0.42) = 0.42
        assert result.best_bid == pytest.approx(0.42)

    def test_2way_best_bid_uses_yes_bid_when_higher(self):
        """If M1.yes_bid > M2.no_bid, direct YES bid wins."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.45"     # higher than M2.no_bid

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.30"

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid == pytest.approx(0.45)

    def test_2way_best_ask_matches_existing_price(self):
        """best_ask must equal `price` for Kalshi 2-way (cross-market min)."""
        ma = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.62, no_ask=0.40,
        )
        mb = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        result = _build_one("EVT", [ma, mb])
        assert result is not None
        # M1 = A.  cross-market best_ask = min(A.yes_ask=0.62, B.no_ask=0.60) = 0.60
        assert result.best_ask == pytest.approx(0.60)
        assert result.best_ask == pytest.approx(result.price)

    def test_3way_best_bid_direct_only(self):
        """3-way soccer: M2.no_bid covers (M1 wins ∪ draw) — NOT a YES_M1 bid.
        best_bid must use direct M1.yes_bid only."""
        home_dict, series, slug = _make_soccer_entry(
            "KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35,
        )
        away_dict, _, _ = _make_soccer_entry(
            "KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38,
        )
        draw_dict, _, _ = _make_soccer_entry(
            "KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30,
        )
        # Override defaults: home direct YES bid 0.30, away NO bid 0.50
        home_dict["yes_bid_dollars"] = "0.30"
        away_dict["no_bid_dollars"] = "0.50"   # MUST NOT leak into Brighton's best_bid

        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC",
            [(home_dict, series, slug), (away_dict, series, slug), (draw_dict, series, slug)],
        )
        bri = next(r for r in results if r.side == "Brighton")
        # Direct only: 0.30, NOT 0.50 from Chelsea's no_bid
        assert bri.best_bid == pytest.approx(0.30)

    def test_3way_best_ask_direct_only(self):
        """3-way: best_ask is the team's own yes_ask, equal to `price`."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)
        results = _build_event_markets("KXEPLGAME-26APR21BRICFC", [home, away, draw])

        for r in results:
            assert r.best_ask == pytest.approx(r.price)
            if r.side == "Brighton":
                assert r.best_ask == pytest.approx(0.35)
            elif r.side == "Chelsea":
                assert r.best_ask == pytest.approx(0.38)

    def test_single_fallback_best_bid_ask_from_direct(self):
        """Unpaired market path uses direct yes_bid_dollars / yes_ask_dollars."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will Alcaraz win?",
            yes_ask=0.65, no_ask=0.37,
        )
        ma_dict["yes_bid_dollars"] = "0.63"
        result = _build_one("EVT", [(ma_dict, series, slug)])
        assert result is not None
        assert result.best_ask == pytest.approx(0.65)
        assert result.best_bid == pytest.approx(0.63)

    def test_2way_missing_both_bids_yields_none(self):
        """If M1.yes_bid AND M2.no_bid are both 0/missing, best_bid is None."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0"

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0"

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid is None

    def test_2way_one_side_missing_uses_other(self):
        """M1.yes_bid=0 but M2.no_bid=0.42 → best_bid=0.42 (not None)."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0"

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.42"

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid == pytest.approx(0.42)

    def test_single_fallback_missing_yes_bid_yields_none(self):
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will Alcaraz win?",
            yes_ask=0.65, no_ask=0.37,
        )
        ma_dict["yes_bid_dollars"] = "0"
        result = _build_one("EVT", [(ma_dict, series, slug)])
        assert result is not None
        assert result.best_bid is None
        # best_ask still set from direct yes_ask
        assert result.best_ask == pytest.approx(0.65)

    def test_3way_missing_yes_bid_yields_none(self):
        home_dict, series, slug = _make_soccer_entry(
            "KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35,
        )
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)
        home_dict["yes_bid_dollars"] = "0"
        results = _build_event_markets(
            "KXEPLGAME-26APR21BRICFC",
            [(home_dict, series, slug), away, draw],
        )
        bri = next(r for r in results if r.side == "Brighton")
        assert bri.best_bid is None
        assert bri.best_ask == pytest.approx(0.35)

    def test_default_normalizedmarket_best_bid_ask_none(self):
        """A NormalizedMarket constructed without best_bid/best_ask defaults None.
        Pins the contract for non-Kalshi adapters (Polymarket today)."""
        nm = NormalizedMarket(
            platform="polymarket", market_id="PM1", event="X",
            market_type="h2h", side="", line=None, price=0.5,
            liquidity=None, url=None, timestamp=None,
            question="Q", end_date=None, outcome_prices=None, event_slug=None,
        )
        assert nm.best_bid is None
        assert nm.best_ask is None
        assert nm.best_bid_market_id is None
        assert nm.best_bid_contract_side is None
        assert nm.no_player_best_bid is None
        assert nm.no_player_best_ask is None
        assert nm.no_player_best_bid_market_id is None
        assert nm.no_player_best_bid_contract_side is None


# ---------------------------------------------------------------------------
# Execution-route info on best YES bid — disambiguates
# direct-YES vs equivalent-NO routes for paper maker proposals.
# ---------------------------------------------------------------------------

class TestBestBidRoute:
    """In 2-way Kalshi events, buying NO of M2 ≡ buying YES of M1.  The
    cheapest YES bid for M1's player can therefore come from either side
    of the pair.  The adapter records which ticker / contract side won,
    so the maker layer can name the actual execution route."""

    def test_2way_direct_yes_route_when_yes_bid_higher(self):
        """M1.yes_bid > M2.no_bid → canonical route is direct YES on M1."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.45"   # higher than M2.no_bid

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.30"

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid == pytest.approx(0.45)
        assert result.best_bid_market_id == "KXATPMATCH-A"
        assert result.best_bid_contract_side == "yes"

    def test_2way_equivalent_no_route_when_no_bid_higher(self):
        """M2.no_bid > M1.yes_bid → canonical route is NO on M2.
        Display side stays the M1 player; execution is on the M2 ticker."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.40"   # lower

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.42"    # higher → wins the route

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid == pytest.approx(0.42)
        assert result.best_bid_market_id == "KXATPMATCH-B"
        assert result.best_bid_contract_side == "no"
        # The displayed side stays the canonical (M1) team; execution route
        # points to the opposing ticker's NO contract.
        assert result.market_id == "KXATPMATCH-A"

    def test_2way_tie_prefers_direct_yes(self):
        """Equal bid values → direct YES route wins.  Defensible default
        (clearer execution: no implicit cross-side conversion)."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.40"

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0.40"   # tie

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid_market_id == "KXATPMATCH-A"
        assert result.best_bid_contract_side == "yes"

    def test_2way_no_player_route_independent(self):
        """The no_player (M2 / complement) route is computed separately
        and may differ from the canonical (M1) route."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0.40"   # M1 yes bid
        ma_dict["no_bid_dollars"] = "0.55"    # M1 no bid (= equivalent YES bid for M2)

        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["yes_bid_dollars"] = "0.50"   # M2 yes bid (direct for M2)
        mb_dict["no_bid_dollars"] = "0.42"    # M2 no bid (= equivalent YES bid for M1)

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None

        # Canonical (M1 / yes_player) — best YES bid for A is max(0.40, 0.42) = 0.42 via M2.no
        assert result.best_bid == pytest.approx(0.42)
        assert result.best_bid_market_id == "KXATPMATCH-B"
        assert result.best_bid_contract_side == "no"

        # Complement (M2 / no_player) — best YES bid for B is max(0.50, 0.55) = 0.55 via M1.no
        assert result.no_player_best_bid == pytest.approx(0.55)
        assert result.no_player_best_bid_market_id == "KXATPMATCH-A"
        assert result.no_player_best_bid_contract_side == "no"
        # Complement best ask = M2's cross-market YES ask = min(M2.yes_ask, M1.no_ask) = min(0.42, 0.42) = 0.42
        assert result.no_player_best_ask == pytest.approx(0.42)

    def test_3way_route_always_direct_yes(self):
        """3-way (soccer with draw): NO bid on the opposing market includes
        draw mass — NEVER an equivalent YES bid.  Route must always be
        direct YES on the team's own ticker."""
        home = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-BRI", "Brighton", 0.35)
        away = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-CFC", "Chelsea", 0.38)
        draw = _make_soccer_entry("KXEPLGAME-26APR21BRICFC-TIE", "Tie", 0.30)

        results = _build_event_markets("KXEPLGAME-26APR21BRICFC", [home, away, draw])
        bri = next(r for r in results if r.side == "Brighton")
        cfc = next(r for r in results if r.side == "Chelsea")

        assert bri.best_bid_market_id == "KXEPLGAME-26APR21BRICFC-BRI"
        assert bri.best_bid_contract_side == "yes"
        assert cfc.best_bid_market_id == "KXEPLGAME-26APR21BRICFC-CFC"
        assert cfc.best_bid_contract_side == "yes"
        # No complement view — each team has its own NormalizedMarket.
        assert bri.no_player_best_bid is None
        assert bri.no_player_best_bid_market_id is None
        assert bri.no_player_best_bid_contract_side is None

    def test_single_fallback_route_direct_yes(self):
        """Single-market fallback: only the direct YES route is available."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will Alcaraz win?",
            yes_ask=0.65, no_ask=0.37,
        )
        ma_dict["yes_bid_dollars"] = "0.63"
        result = _build_one("EVT", [(ma_dict, series, slug)])
        assert result is not None
        assert result.best_bid_market_id == "KXATPMATCH-A"
        assert result.best_bid_contract_side == "yes"
        assert result.no_player_best_bid is None

    def test_2way_missing_bids_yields_none_route(self):
        """Both M1.yes_bid and M2.no_bid are 0 → no route, all None."""
        ma_dict, series, slug = _make_market_entry(
            ticker="KXATPMATCH-A", title="Will A win?",
            yes_ask=0.60, no_ask=0.42,
        )
        ma_dict["yes_bid_dollars"] = "0"
        mb_dict, _, _ = _make_market_entry(
            ticker="KXATPMATCH-B", title="Will B win?",
            yes_ask=0.42, no_ask=0.60,
        )
        mb_dict["no_bid_dollars"] = "0"

        result = _build_one("EVT", [(ma_dict, series, slug), (mb_dict, series, slug)])
        assert result is not None
        assert result.best_bid is None
        assert result.best_bid_market_id is None
        assert result.best_bid_contract_side is None


# ---------------------------------------------------------------------------
# Per-series fetch counts
# ---------------------------------------------------------------------------

class TestSeriesCounts:
    """Tests for per-series raw/normalized visibility."""

    def test_series_counts_populated_after_fetch(self):
        """After fetch, series_counts has an entry per configured series."""
        page = [
            _make_api_market("KXATPMATCH-EVT1-A", "EVT1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-EVT1-B", "EVT1", "Will B win?", 0.42),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(page, cursor=None))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXATPMATCH": "atp-tennis-match"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                asyncio.run(adapter.fetch_markets())

        assert "KXATPMATCH" in adapter.series_counts
        assert adapter.series_counts["KXATPMATCH"]["raw"] == 2
        assert adapter.series_counts["KXATPMATCH"]["normalized"] == 1  # 2 markets → 1 event

    def test_series_counts_zero_for_empty_series(self):
        """Series with no open markets reports raw=0."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response([], cursor=None))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={
            "KXATPMATCH": "atp-tennis-match",
            "KXNBAGAME": "nba-game",
        }):
            with patch("httpx.AsyncClient", return_value=mock_client):
                asyncio.run(adapter.fetch_markets())

        assert adapter.series_counts["KXATPMATCH"]["raw"] == 0
        assert adapter.series_counts["KXNBAGAME"]["raw"] == 0

    def test_series_counts_multi_series(self):
        """Multiple series tracked independently."""
        tennis_markets = [
            _make_api_market("KXATPMATCH-E1-A", "E1", "Will A win?", 0.60),
            _make_api_market("KXATPMATCH-E1-B", "E1", "Will B win?", 0.42),
        ]
        nba_markets = [
            _make_api_market("KXNBAGAME-E2-C", "E2", "Will C win?", 0.55),
            _make_api_market("KXNBAGAME-E2-D", "E2", "Will D win?", 0.47),
            _make_api_market("KXNBAGAME-E3-E", "E3", "Will E win?", 0.40),
            _make_api_market("KXNBAGAME-E3-F", "E3", "Will F win?", 0.62),
        ]

        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            params = kwargs.get("params", {})
            series = params.get("series_ticker", "")
            if series == "KXATPMATCH":
                return _mock_response(tennis_markets, cursor=None)
            elif series == "KXNBAGAME":
                return _mock_response(nba_markets, cursor=None)
            return _mock_response([], cursor=None)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={
            "KXATPMATCH": "atp-tennis-match",
            "KXNBAGAME": "nba-game",
        }):
            with patch("httpx.AsyncClient", return_value=mock_client):
                results = asyncio.run(adapter.fetch_markets())

        assert adapter.series_counts["KXATPMATCH"]["raw"] == 2
        assert adapter.series_counts["KXATPMATCH"]["normalized"] == 1
        assert adapter.series_counts["KXNBAGAME"]["raw"] == 4
        assert adapter.series_counts["KXNBAGAME"]["normalized"] == 2
        assert len(results) == 3  # 1 tennis + 2 NBA

    def test_series_counts_error_marked_negative(self):
        """API error for a series sets raw=-1."""
        import httpx as httpx_mod

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = httpx_mod.HTTPStatusError(
            "Server Error", request=MagicMock(), response=error_resp,
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=error_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        adapter = KalshiAdapter(api_key="test-key")
        with patch("services.adapters.kalshi.all_kalshi_series", return_value={"KXBADTICKER": "bad"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                asyncio.run(adapter.fetch_markets())

        assert adapter.series_counts["KXBADTICKER"]["raw"] == -1

    def test_series_counts_default_empty_before_fetch(self):
        """Before fetch, series_counts is empty dict."""
        adapter = KalshiAdapter(api_key="test-key")
        assert adapter.series_counts == {}


# ---------------------------------------------------------------------------
# end_date precedence: ticker-date FIRST, close_time only as fallback.
#
# Rationale: Kalshi `close_time` is the market settlement deadline, typically
# set ~2 weeks after the actual game (e.g. KXNBAGAME-26MAY04… closes 2026-05-18).
# Using close_time as a primary timing signal makes every Kalshi market look
# 14 days off any FD event and over-suppresses the entire feed.
# ---------------------------------------------------------------------------

class TestEndDatePrefersTickerDate:
    def test_ticker_date_preferred_over_close_time_paired(self):
        """Paired team-sport markets emit end_date from the ticker's YYMONDD,
        NOT the much-later settlement close_time."""
        ma = _make_market_entry(
            ticker="KXMLBGAME-26MAY031415LADSTL-LAD",
            title="Los Angeles Dodgers vs St. Louis Cardinals winner?",
            yes_ask=0.58,
            no_ask=0.44,
            close_time="2026-05-17T18:15:00Z",  # settlement, ~2 weeks after game
        )
        mb = _make_market_entry(
            ticker="KXMLBGAME-26MAY031415LADSTL-STL",
            title="Los Angeles Dodgers vs St. Louis Cardinals winner?",
            yes_ask=0.44,
            no_ask=0.58,
            close_time="2026-05-17T18:15:00Z",
        )
        nm = _build_one("KXMLBGAME-26MAY031415LADSTL", [ma, mb])
        assert nm is not None
        # Ticker-derived game date beats the settlement timestamp
        assert nm.end_date == "2026-05-03T23:59:00Z"

    def test_ticker_date_preferred_over_close_time_single(self):
        """Single-market fallback path also prefers ticker-derived game date."""
        from services.adapters.kalshi import _single_market_fallback
        m = {
            "ticker": "KXMLBGAME-26MAY031415LADSTL-LAD",
            "event_ticker": "KXMLBGAME-26MAY031415LADSTL",
            "title": "Will Los Angeles Dodgers win?",
            "yes_ask_dollars": "0.58",
            "no_ask_dollars": "0.44",
            "yes_bid_dollars": "0.56",
            "yes_sub_title": "Los Angeles Dodgers",
            "close_time": "2026-05-17T18:15:00Z",  # settlement, ~2 weeks later
            "volume_fp": "100",
            "liquidity_dollars": "500",
        }
        nm = _single_market_fallback(m, "KXMLBGAME", "mlb-game")
        assert nm is not None
        assert nm.end_date == "2026-05-03T23:59:00Z"

    def test_falls_back_to_close_time_when_ticker_has_no_date(self):
        """Fallback: when the event_ticker has no parseable YYMONDD, close_time
        is used (legacy non-sports tickers and edge cases)."""
        ma = _make_market_entry(
            ticker="KXSPECIAL-NODATE-A",
            title="Will A win?",
            yes_ask=0.55,
            no_ask=0.47,
            close_time="2026-06-01T18:00:00Z",
        )
        mb = _make_market_entry(
            ticker="KXSPECIAL-NODATE-B",
            title="Will B win?",
            yes_ask=0.47,
            no_ask=0.55,
            close_time="2026-06-01T18:00:00Z",
        )
        nm = _build_one("KXSPECIAL-NODATE", [ma, mb])
        assert nm is not None
        # No YYMONDD in event_ticker → falls through to close_time
        assert nm.end_date == "2026-06-01T18:00:00Z"


# ---------------------------------------------------------------------------
# Adversarial: the draw/Tie leg must never be emitted as an h2h team market
# ---------------------------------------------------------------------------

def _future_epl_event_ticker(days_ahead: int = 6) -> tuple[str, str]:
    """Return (event_ticker, commence_time) for a match `days_ahead` days out."""
    from datetime import datetime, timedelta, timezone
    d = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    et = f"KXEPLGAME-{d:%y}{d:%b}{d:%d}ARSCHE".upper()
    return et, d.strftime("%Y-%m-%dT20:00:00Z")


def _raw_3way_leg(
    event_ticker: str,
    suffix: str,
    yes_sub_title: str,
    yes_ask: float,
    yes_bid: float,
    no_ask: float | None = None,
    no_bid: float | None = None,
) -> tuple[dict, str, str]:
    d = {
        "ticker": f"{event_ticker}-{suffix}",
        "event_ticker": event_ticker,
        "title": "Arsenal vs Chelsea Winner?",
        "yes_sub_title": yes_sub_title,
        "yes_ask_dollars": str(yes_ask),
        "yes_bid_dollars": str(yes_bid),
        "volume_fp": "100",
        "liquidity_dollars": "1000",
        "close_time": "2026-12-24T00:00:00Z",
    }
    if no_ask is not None:
        d["no_ask_dollars"] = str(no_ask)
    if no_bid is not None:
        d["no_bid_dollars"] = str(no_bid)
    return d, "KXEPLGAME", "epl"


def _assert_no_draw_emitted(results: list[NormalizedMarket]) -> None:
    for nm in results:
        assert (nm.side or "").strip().lower() not in {"tie", "draw"}, nm
        assert not nm.market_id.upper().endswith("-TIE"), nm


class TestDrawNeverEmittedAsTeamMarket:
    """A 3-way event that cannot form a valid team pair must emit nothing."""

    ET = "KXEPLGAME-26NOV10ARSCHE"

    def _tie(self, ya=0.37, sub="Tie"):
        return _raw_3way_leg(self.ET, "TIE", sub, ya, ya - 0.02, 1 - ya + 0.02, 1 - ya)

    def _ars(self, ya=0.47):
        return _raw_3way_leg(self.ET, "ARS", "Arsenal", ya, ya - 0.02, 1 - ya + 0.02, 1 - ya)

    def _che_no_ask(self):
        # No YES asks (yes_ask 0) → dropped during parsing.
        return _raw_3way_leg(self.ET, "CHE", "Chelsea", 0, 0.25, 0.75, 1.0)

    @pytest.mark.parametrize("order", [
        ("tie", "ars", "che"), ("tie", "che", "ars"),
        ("ars", "tie", "che"), ("ars", "che", "tie"),
        ("che", "tie", "ars"), ("che", "ars", "tie"),
    ])
    def test_tie_team_and_unpriced_team_emits_nothing(self, order):
        legs = {"tie": self._tie(), "ars": self._ars(), "che": self._che_no_ask()}
        results = _build_event_markets(self.ET, [legs[k] for k in order])
        _assert_no_draw_emitted(results)
        # A lone team market in a 3-way event cannot price the opponent
        # (1 - team includes the draw), so nothing may be emitted at all.
        assert results == []

    def test_tie_only_group_emits_nothing(self):
        assert _build_event_markets(self.ET, [self._tie()]) == []

    def test_draw_labelled_only_group_emits_nothing(self):
        assert _build_event_markets(self.ET, [self._tie(sub="Draw")]) == []

    @pytest.mark.parametrize("order", [("ars", "tie"), ("tie", "ars")])
    def test_team_plus_tie_pair_emits_nothing(self, order):
        legs = {"tie": self._tie(), "ars": self._ars()}
        assert _build_event_markets(self.ET, [legs[k] for k in order]) == []

    def test_unpriced_tie_with_one_team_emits_nothing(self):
        """Draw leg with no ask is dropped during parsing — the raw group is
        still 3-way, so the remaining lone team must not be emitted."""
        tie = _raw_3way_leg(self.ET, "TIE", "Tie", 0, 0.30, 0.72, 0.70)
        assert _build_event_markets(self.ET, [self._ars(), tie]) == []

    def test_tie_detected_by_ticker_suffix_only(self):
        """yes_sub_title missing — the -TIE ticker suffix alone marks the draw."""
        tie = _raw_3way_leg(self.ET, "TIE", "", 0.37, 0.35, 0.65, 0.63)
        results = _build_event_markets(self.ET, [tie, self._ars(), self._che_no_ask()])
        assert results == []

    def test_single_market_fallback_rejects_draw(self):
        from services.adapters.kalshi import _single_market_fallback
        for sub in ("Tie", "tie", " Draw ", ""):
            m, series, slug = _raw_3way_leg(self.ET, "TIE", sub, 0.37, 0.35, 0.65, 0.63)
            assert _single_market_fallback(m, series, slug) is None
        # Draw by yes_sub_title even with a non-TIE ticker suffix
        m, series, slug = _raw_3way_leg(self.ET, "DRW", "Draw", 0.37, 0.35, 0.65, 0.63)
        assert _single_market_fallback(m, series, slug) is None

    def test_normal_3way_with_both_teams_priced_unchanged(self):
        che = _raw_3way_leg(self.ET, "CHE", "Chelsea", 0.31, 0.29, 0.71, 0.69)
        results = _build_event_markets(self.ET, [self._tie(), self._ars(), che])
        assert len(results) == 2
        _assert_no_draw_emitted(results)
        by_side = {r.side: r for r in results}
        assert set(by_side) == {"Arsenal", "Chelsea"}
        assert by_side["Arsenal"].price == pytest.approx(0.47)
        assert by_side["Chelsea"].price == pytest.approx(0.31)
        assert by_side["Arsenal"].market_id == f"{self.ET}-ARS"
        assert by_side["Chelsea"].market_id == f"{self.ET}-CHE"

    def test_normal_2way_unchanged(self):
        ma = _make_market_entry("KXATPMATCH-A", "Will Alcaraz win?", 0.60, 0.42)
        mb = _make_market_entry("KXATPMATCH-B", "Will Sinner win?", 0.40, 0.62)
        results = _build_event_markets("EVT-123", [ma, mb])
        assert len(results) == 1
        prices = [float(p) for p in results[0].outcome_prices]
        assert prices == pytest.approx([0.60, 0.40])

    def test_2way_with_one_unpriced_leg_still_falls_back(self):
        """No draw in the group → existing single-market fallback preserved."""
        ma = _make_market_entry("KXATPMATCH-A", "Will Alcaraz win?", 0.60, 0.42)
        mb = _make_market_entry("KXATPMATCH-B", "Will Sinner win?", 0.0, 0.62)
        results = _build_event_markets("EVT-123", [ma, mb])
        assert len(results) == 1
        assert results[0].market_id == "KXATPMATCH-A"


class TestDrawFallbackEndToEnd:
    """Previously reproduced false positive: the TIE contract (yes_ask 0.37)
    was emitted with side 'Tie', fell back to positional alignment in Pass A,
    was labelled 'Arsenal', and classified BUY at edge ~0.065."""

    @pytest.mark.parametrize("order", [("tie", "ars", "che"), ("che", "ars", "tie")])
    def test_tie_contract_never_reaches_buy(self, order):
        from services.engine_config import EngineConfig
        from services.feature_extractor import extract_features
        from services.normalizer import normalize_name
        from services.odds_provider import TennisOddsEvent, BookmakerLine
        from services.rule_engine import evaluate_rules

        et, commence = _future_epl_event_ticker()
        legs = {
            "tie": _raw_3way_leg(et, "TIE", "Tie", 0.37, 0.35, 0.65, 0.63),
            "ars": _raw_3way_leg(et, "ARS", "Arsenal", 0.47, 0.45, 0.55, 0.53),
            "che": _raw_3way_leg(et, "CHE", "Chelsea", 0, 0.25, 0.75, 1.0),
        }
        event = TennisOddsEvent(
            event_id="e", sport_key="soccer_epl", tournament="EPL",
            home_player="Arsenal", away_player="Chelsea",
            home_player_norm=normalize_name("Arsenal"),
            away_player_norm=normalize_name("Chelsea"),
            commence_time=commence,
            bookmakers=[BookmakerLine(
                bookmaker_key="fanduel", bookmaker_title="FanDuel",
                home_odds=115, away_odds=230, draw_odds=260,
                last_update=commence,
            )],
        )
        cfg = EngineConfig()
        nms = _build_event_markets(et, [legs[k] for k in order])
        _assert_no_draw_emitted(nms)
        statuses = []
        for nm in nms:
            for f in extract_features(nm, event, cfg):
                status, _ = evaluate_rules(f, cfg)
                statuses.append(status)
        assert "BUY" not in statuses
        assert "WATCH" not in statuses
