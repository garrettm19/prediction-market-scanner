"""
Tests for services.price_history.fetch_kalshi_history pairing logic.

The 2-way cross-market identity (No M2 == Yes M1) is only valid for a
strict two-market event.  In a 3-way (soccer) event, 1 - M2 prices
(M1 OR draw), so team history would be inflated by ~draw/2 and a settled
draw would read 0.5 for both teams.  These tests pin the correct behaviour.

All network access is mocked (httpx.AsyncClient + _fetch_kalshi_candles).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import price_history
from services.price_history import fetch_kalshi_history

ET = "KXEPLGAME-26NOV10ARSCHE"
TS = 1_800_000_000


def _candle(bid: float, ask: float, ts: int = TS) -> dict:
    return {
        "end_period_ts": ts,
        "yes_bid": {"close_dollars": f"{bid:.4f}"},
        "yes_ask": {"close_dollars": f"{ask:.4f}"},
    }


def _sibling(ticker: str, sub: str) -> dict:
    return {"ticker": ticker, "yes_sub_title": sub}


def _run(primary: str, siblings: list[dict], books: dict[str, tuple[float, float]]):
    """Run fetch_kalshi_history with mocked siblings and per-ticker candles.

    Returns (points, list of tickers whose candles were fetched).
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"markets": siblings})

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    fetched: list[str] = []

    async def fake_candles(series, ticker, api_key, _client, start, end, interval):
        fetched.append(ticker)
        bid, ask = books[ticker]
        return [_candle(bid, ask)]

    with patch.dict("os.environ", {"KALSHI_API_KEY": "test-key"}), \
         patch.object(price_history.httpx, "AsyncClient", return_value=client), \
         patch.object(price_history, "_fetch_kalshi_candles", side_effect=fake_candles):
        points = asyncio.run(fetch_kalshi_history(primary))
    return points, fetched


ARS, CHE, TIE = f"{ET}-ARS", f"{ET}-CHE", f"{ET}-TIE"
THREE_WAY = [_sibling(ARS, "Arsenal"), _sibling(CHE, "Chelsea"), _sibling(TIE, "Tie")]
PREGAME = {ARS: (0.44, 0.46), CHE: (0.29, 0.31), TIE: (0.24, 0.26)}
SETTLED_DRAW = {ARS: (0.00, 0.01), CHE: (0.00, 0.01), TIE: (0.99, 1.00)}


class TestKalshi3WayHistory:
    @pytest.mark.parametrize("primary,expected", [
        (ARS, 0.45), (CHE, 0.30), (TIE, 0.25),
    ])
    def test_pregame_3way_uses_own_mid(self, primary, expected):
        points, fetched = _run(primary, THREE_WAY, PREGAME)
        assert len(points) == 1
        assert points[0].price == pytest.approx(expected)
        # No paired market fetched for a 3-way event
        assert fetched == [primary]

    @pytest.mark.parametrize("primary", [ARS, CHE])
    def test_settled_draw_team_mids_near_zero(self, primary):
        points, _ = _run(primary, THREE_WAY, SETTLED_DRAW)
        assert points[-1].price <= 0.05

    def test_settled_draw_primary_is_draw(self):
        points, fetched = _run(TIE, THREE_WAY, SETTLED_DRAW)
        assert points[-1].price == pytest.approx(0.995)
        assert fetched == [TIE]

    @pytest.mark.parametrize("sibs", [
        # Draw labelled "Draw" instead of "Tie"
        [_sibling(ARS, "Arsenal"), _sibling(CHE, "Chelsea"), _sibling(f"{ET}-DRW", "Draw")],
        # Draw detected by -TIE suffix only
        [_sibling(ARS, "Arsenal"), _sibling(CHE, "Chelsea"), _sibling(TIE, "")],
        # Sibling order with the draw first
        [_sibling(TIE, "Tie"), _sibling(CHE, "Chelsea"), _sibling(ARS, "Arsenal")],
    ])
    def test_draw_detection_variants(self, sibs):
        points, fetched = _run(ARS, sibs, PREGAME)
        assert points[0].price == pytest.approx(0.45)
        assert fetched == [ARS]

    def test_draw_market_missing_from_sibling_list_but_primary_is_tie(self):
        """Primary itself is the draw (ticker suffix) — never paired."""
        sibs = [_sibling(ARS, "Arsenal"), _sibling(TIE, "Tie")]
        points, fetched = _run(TIE, sibs, PREGAME)
        assert points[0].price == pytest.approx(0.25)
        assert fetched == [TIE]

    def test_more_than_two_non_draw_markets_uses_own_mid(self):
        x = f"{ET}-XXX"
        sibs = [_sibling(ARS, "Arsenal"), _sibling(CHE, "Chelsea"), _sibling(x, "Other")]
        books = dict(PREGAME, **{x: (0.20, 0.22)})
        points, fetched = _run(ARS, sibs, books)
        assert points[0].price == pytest.approx(0.45)
        assert fetched == [ARS]


class TestKalshi2WayHistoryUnchanged:
    A = "KXATPMATCH-26NOV10SINALC-SIN"
    B = "KXATPMATCH-26NOV10SINALC-ALC"
    SIBS = [_sibling(A, "Jannik Sinner"), _sibling(B, "Carlos Alcaraz")]

    def test_2way_pair_uses_cross_market_min_max(self):
        # A: 0.40/0.48 ; B: 0.55/0.58
        # best_buy  = min(0.48, 1 - 0.55) = 0.45
        # best_sell = max(0.40, 1 - 0.58) = 0.42
        books = {self.A: (0.40, 0.48), self.B: (0.55, 0.58)}
        points, fetched = _run(self.A, self.SIBS, books)
        assert fetched == [self.A, self.B]
        assert points[0].price == pytest.approx((0.45 + 0.42) / 2)

    def test_2way_pair_from_other_side(self):
        books = {self.A: (0.40, 0.48), self.B: (0.55, 0.58)}
        points, fetched = _run(self.B, self.SIBS, books)
        assert fetched == [self.B, self.A]
        # best_buy = min(0.58, 1 - 0.40) = 0.58 ; best_sell = max(0.55, 1 - 0.48) = 0.55
        assert points[0].price == pytest.approx((0.58 + 0.55) / 2)

    def test_no_siblings_uses_own_mid(self):
        books = {self.A: (0.40, 0.48)}
        points, fetched = _run(self.A, [], books)
        assert fetched == [self.A]
        assert points[0].price == pytest.approx(0.44)
