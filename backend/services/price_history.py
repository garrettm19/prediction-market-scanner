"""
Price history fetcher for prediction market platforms.

Polymarket: CLOB API /prices-history (requires clobTokenId from Gamma API)
Kalshi: /trade-api/v2/series/{series}/markets/{ticker}/candlesticks
"""
import json as _json
import logging
import os
import time
from dataclasses import dataclass

import httpx

from services.adapters.kalshi import _is_draw_market

logger = logging.getLogger(__name__)

_GAMMA_BASE = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com")
_CLOB_BASE = "https://clob.polymarket.com"
_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class PricePoint:
    timestamp: int    # unix seconds
    price: float      # 0–1


async def fetch_polymarket_history(
    market_id: str,
    interval: str = "1w",
    fidelity: int = 60,
) -> list[PricePoint]:
    """
    Fetch price history for a Polymarket market.

    Steps:
      1. GET gamma-api/markets/{market_id} to find clobTokenIds
      2. GET clob.polymarket.com/prices-history?market={token_id}
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Step 1: get CLOB token ID
        resp = await client.get(f"{_GAMMA_BASE}/markets/{market_id}")
        resp.raise_for_status()
        market_data = resp.json()

        clob_ids = market_data.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = _json.loads(clob_ids)
            except (ValueError, TypeError):
                clob_ids = None
        if not clob_ids or not isinstance(clob_ids, list) or len(clob_ids) == 0:
            logger.warning("No clobTokenIds for PM market %s", market_id)
            return []

        token_id = clob_ids[0]  # Yes outcome token

        # Step 2: fetch price history
        resp = await client.get(
            f"{_CLOB_BASE}/prices-history",
            params={
                "market": token_id,
                "interval": interval,
                "fidelity": fidelity,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        history = data.get("history", [])
        return [
            PricePoint(timestamp=int(pt["t"]), price=float(pt["p"]))
            for pt in history
            if "t" in pt and "p" in pt
        ]


async def _fetch_kalshi_candles(
    series_ticker: str,
    market_ticker: str,
    api_key: str,
    client: httpx.AsyncClient,
    start_ts: int,
    end_ts: int,
    period_interval: int,
) -> list[dict]:
    """Fetch raw candlestick dicts for one Kalshi market."""
    resp = await client.get(
        f"{_KALSHI_BASE}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        params={
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        },
        headers={"Authorization": api_key},
    )
    resp.raise_for_status()
    return resp.json().get("candlesticks", [])


def _candle_bid_ask(candle: dict) -> tuple[float, float] | None:
    """Extract (yes_bid_close, yes_ask_close) from a candle, or None."""
    bid = candle.get("yes_bid", {}).get("close_dollars")
    ask = candle.get("yes_ask", {}).get("close_dollars")
    if bid and ask:
        try:
            return float(bid), float(ask)
        except (ValueError, TypeError):
            pass
    return None


async def fetch_kalshi_history(
    market_ticker: str,
    period_interval: int = 1,
    days_back: int = 7,
) -> list[PricePoint]:
    """
    Fetch price history for a Kalshi market as cross-market mid-prices.

    For paired 2-way markets, fetches both sides and computes the
    cross-market mid at each timestamp:
      best_buy  = min(M1 yes_ask, 1 - M2 yes_bid)
      best_sell = max(M1 yes_bid, 1 - M2 yes_ask)
      mid = (best_buy + best_sell) / 2

    Pairing is used only for a strict 2-way event (exactly two markets,
    neither a draw/Tie).  3-way events (soccer, with a draw market) and
    events with more than two markets use the single-market mid
    (bid + ask) / 2 for every market, including the draw market itself.
    Also falls back to single-market mid if no paired market is found.
    """
    api_key = os.getenv("KALSHI_API_KEY", "")
    if not api_key:
        return []

    parts = market_ticker.split("-")
    series_ticker = parts[0] if parts else market_ticker
    # Event ticker = market_ticker without the last suffix
    # KXUFLGAME-26APR07STLDAL-DAL → KXUFLGAME-26APR07STLDAL
    event_ticker = "-".join(parts[:-1]) if len(parts) >= 3 else ""

    now = int(time.time())
    start_ts = now - (days_back * 86400)

    async with httpx.AsyncClient(timeout=12.0) as client:
        # Fetch primary market candles
        primary_candles = await _fetch_kalshi_candles(
            series_ticker, market_ticker, api_key, client, start_ts, now, period_interval,
        )

        # Find and fetch paired market
        paired_candles_by_ts: dict[int, dict] = {}
        if event_ticker:
            try:
                resp = await client.get(
                    f"{_KALSHI_BASE}/markets",
                    params={"event_ticker": event_ticker, "limit": 10},
                    headers={"Authorization": api_key},
                )
                resp.raise_for_status()
                siblings = resp.json().get("markets", []) or []
                # The cross-market identity (No M2 == Yes M1) only holds
                # for a strict 2-way event: exactly two non-draw markets,
                # no draw/Tie market, and the primary is one of the two.
                # In a 3-way (soccer) event, 1 - M2 prices (M1 OR draw), so
                # every market — including the draw itself — must use its
                # own single-market mid instead.
                paired_ticker = None
                has_draw = _is_draw_market(None, market_ticker) or any(
                    _is_draw_market(m.get("yes_sub_title"), m.get("ticker"))
                    for m in siblings
                )
                tickers = [m.get("ticker", "") for m in siblings]
                if (
                    not has_draw
                    and len(tickers) == 2
                    and market_ticker in tickers
                ):
                    paired_ticker = next(
                        (t for t in tickers if t and t != market_ticker), None
                    )
                if paired_ticker:
                    paired_raw = await _fetch_kalshi_candles(
                        series_ticker, paired_ticker, api_key, client,
                        start_ts, now, period_interval,
                    )
                    for c in paired_raw:
                        ts = c.get("end_period_ts")
                        if ts:
                            paired_candles_by_ts[int(ts)] = c
            except Exception as exc:
                logger.debug("Failed to fetch paired market for %s: %s", market_ticker, exc)

        # Build price points
        points: list[PricePoint] = []
        for candle in primary_candles:
            ts = candle.get("end_period_ts")
            if not ts:
                continue
            ts_int = int(ts)
            m1 = _candle_bid_ask(candle)
            if not m1:
                continue

            m1_bid, m1_ask = m1
            paired = paired_candles_by_ts.get(ts_int)

            if paired:
                m2 = _candle_bid_ask(paired)
                if m2:
                    m2_bid, m2_ask = m2
                    # Cross-market: best route to buy/sell M1's outcome
                    best_buy = min(m1_ask, 1.0 - m2_bid)
                    best_sell = max(m1_bid, 1.0 - m2_ask)
                    mid = (best_buy + best_sell) / 2.0
                    points.append(PricePoint(timestamp=ts_int, price=round(mid, 4)))
                    continue

            # Fallback: single-market mid
            mid = (m1_bid + m1_ask) / 2.0
            points.append(PricePoint(timestamp=ts_int, price=round(mid, 4)))

        return points


async def fetch_price_history(
    platform: str,
    market_id: str,
) -> list[PricePoint]:
    """Dispatch to the correct platform fetcher."""
    try:
        if platform == "polymarket":
            return await fetch_polymarket_history(market_id)
        elif platform == "kalshi":
            return await fetch_kalshi_history(market_id)
        else:
            logger.warning("Unknown platform for price history: %s", platform)
            return []
    except Exception as exc:
        logger.error("Price history fetch failed (%s/%s): %s", platform, market_id, exc)
        return []
