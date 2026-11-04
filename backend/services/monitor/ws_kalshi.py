"""
Kalshi WebSocket consumer.

Connects to the Kalshi WebSocket for real-time orderbook updates
on watched market tickers.

Endpoint: wss://api.elections.kalshi.com/trade-api/ws/v2
Auth: Requires API key via Authorization header.

Subscribes to the orderbook channel and maintains a per-ticker book
of resting bids on each side.  Kalshi publishes resting bids on each
side of the book:
  - yes  → list of [price_cents, qty] for YES bids
  - no   → list of [price_cents, qty] for NO bids

The best YES bid is the highest YES bid:
    best_yes_bid_cents = max(yes_bid_prices_cents)

The best YES ask is the implied complement of the highest NO bid:
    best_yes_ask_cents = 100 - max(no_bid_prices_cents)

A NO bid at 43¢ implies a YES ask at 57¢, because filling that NO bid
is economically equivalent to selling YES at 57¢.  Triggers
re-evaluation when the synthesized best YES ask changes meaningfully.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_RECONNECT_DELAY = 5
_MAX_RECONNECT_DELAY = 60
_PING_INTERVAL = 30
_MEANINGFUL_PRICE_CHANGE = 0.01


def _backoff_delay(attempt: int) -> int:
    """Exponential reconnect backoff: 5, 10, 20, 40, then capped at 60s."""
    attempt = max(1, attempt)
    return min(_RECONNECT_DELAY * (2 ** min(attempt - 1, 10)), _MAX_RECONNECT_DELAY)


class KalshiWsConsumer:
    """
    WebSocket consumer for Kalshi real-time orderbook updates.

    Authenticates with API key, subscribes to specific market tickers,
    maintains a per-ticker resting-bid book on each side, and calls
    on_price_change when the synthesized best YES ask changes meaningfully.
    """

    def __init__(
        self,
        api_key: str | None = None,
        min_price_change: float = _MEANINGFUL_PRICE_CHANGE,
    ):
        self._api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        self._watched_tickers: set[str] = set()
        self._last_prices: dict[str, float] = {}
        self._min_change = min_price_change
        self._running = False
        self._connected = False
        self._authenticated = False
        self._last_message_ts: float = 0.0
        self._reconnect_count: int = 0
        # Per-ticker resting-bid book.  Each ticker holds two side maps,
        # {price_cents: qty} with qty > 0.  Snapshots replace the maps;
        # deltas update one level at a time.
        self._books: dict[str, dict[str, dict[int, int]]] = {}

    def watch(self, tickers: set[str]) -> None:
        """Set market tickers to watch."""
        self._watched_tickers = tickers
        logger.info("KalshiWs: watching %d tickers", len(tickers))

    def unwatch_all(self) -> None:
        self._watched_tickers.clear()
        self._last_prices.clear()
        self._books.clear()

    async def run_loop(
        self,
        on_price_change: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Connect, authenticate, subscribe, and stream price updates."""
        try:
            import websockets
        except ImportError:
            logger.error("KalshiWs: websockets package not installed, skipping")
            return

        if not self._api_key:
            logger.warning("KalshiWs: no KALSHI_API_KEY, skipping")
            return

        self._running = True
        logger.info("KalshiWs: starting consumer loop")

        while self._running:
            try:
                async with websockets.connect(
                    _WS_URL,
                    additional_headers={"Authorization": self._api_key},
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    # _reconnect_count is reset only once the connection has
                    # proven healthy (first message received), so a server
                    # that accepts then immediately closes still backs off.
                    healthy = False
                    logger.info("KalshiWs: connected")

                    # Subscribe to orderbook updates for watched tickers
                    if self._watched_tickers:
                        sub_msg = {
                            "id": 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": list(self._watched_tickers),
                            },
                        }
                        await ws.send(json.dumps(sub_msg))
                        logger.info("KalshiWs: subscribed to %d tickers", len(self._watched_tickers))

                    async for raw_msg in ws:
                        self._last_message_ts = time.time()
                        if not healthy:
                            healthy = True
                            self._reconnect_count = 0
                        try:
                            msg = json.loads(raw_msg)
                            await self._handle_message(msg, on_price_change)
                        except json.JSONDecodeError:
                            continue

                # Clean server close (1000/1001): ``async for`` ends without an
                # exception.  Treat it as a disconnect and back off too,
                # otherwise the loop reconnects in a tight loop.
                self._connected = False
                self._authenticated = False
                if not self._running:
                    break
                self._reconnect_count += 1
                delay = _backoff_delay(self._reconnect_count)
                logger.warning(
                    "KalshiWs: connection closed by server, reconnecting in %ds (attempt %d)",
                    delay, self._reconnect_count,
                )
                await asyncio.sleep(delay)

            except Exception as exc:
                self._connected = False
                self._authenticated = False
                self._reconnect_count += 1

                # Auth failures won't fix themselves — stop retrying
                exc_str = str(exc)
                if "401" in exc_str or "403" in exc_str:
                    logger.error(
                        "KalshiWs: auth rejected (HTTP %s), stopping reconnect. Check KALSHI_API_KEY.",
                        "401" if "401" in exc_str else "403",
                    )
                    self._running = False
                    return

                # Exponential backoff capped at 60s
                delay = _backoff_delay(self._reconnect_count)
                logger.warning(
                    "KalshiWs: disconnected (%s), reconnecting in %ds (attempt %d)",
                    exc, delay, self._reconnect_count,
                )
                await asyncio.sleep(delay)

    async def _handle_message(
        self,
        msg: dict,
        on_price_change: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Process a WebSocket message."""
        msg_type = msg.get("type", "")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return

        body = msg.get("msg") or {}
        ticker = body.get("market_ticker", "")
        if not ticker or ticker not in self._watched_tickers:
            return

        if msg_type == "orderbook_snapshot":
            self._apply_snapshot(ticker, body)
        else:
            self._apply_delta(ticker, body)

        best_ask_cents = self._best_yes_ask(ticker)
        if best_ask_cents is None:
            return
        price = best_ask_cents / 100.0

        prev = self._last_prices.get(ticker)
        if prev is None or abs(price - prev) >= self._min_change:
            self._last_prices[ticker] = price
            await on_price_change(ticker, price)

    def _apply_snapshot(self, ticker: str, body: dict) -> None:
        """Replace the ticker's book with the snapshot contents."""
        self._books[ticker] = {
            "yes": _levels_from_array(body.get("yes")),
            "no": _levels_from_array(body.get("no")),
        }

    def _apply_delta(self, ticker: str, body: dict) -> None:
        """Apply an incremental update to one side/price level."""
        side = body.get("side")
        if side not in ("yes", "no"):
            return
        try:
            price = int(body.get("price"))
            delta = int(body.get("delta"))
        except (TypeError, ValueError):
            return
        book = self._books.setdefault(ticker, {"yes": {}, "no": {}})
        levels = book.setdefault(side, {})
        new_qty = levels.get(price, 0) + delta
        if new_qty <= 0:
            levels.pop(price, None)
        else:
            levels[price] = new_qty

    def _best_yes_bid(self, ticker: str) -> int | None:
        """Highest YES bid in cents, or None if no resting YES bids."""
        yes_levels = self._books.get(ticker, {}).get("yes") or {}
        return max(yes_levels) if yes_levels else None

    def _best_yes_ask(self, ticker: str) -> int | None:
        """
        Synthesized best YES ask in cents.

        Kalshi publishes resting bids per side, not asks.  The best
        YES ask is the complement of the highest NO bid:
            ask_cents = 100 - max(no_bid_cents)
        Filling a NO bid at 43¢ is economically equivalent to selling
        YES at 57¢, so the cheapest YES purchase mirrors the most
        aggressive NO bid.
        """
        no_levels = self._books.get(ticker, {}).get("no") or {}
        if not no_levels:
            return None
        return 100 - max(no_levels)

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "watched_count": len(self._watched_tickers),
            "last_message_ts": self._last_message_ts,
            "reconnect_count": self._reconnect_count,
            "has_api_key": bool(self._api_key),
        }


def _levels_from_array(arr: object) -> dict[int, int]:
    """
    Convert a Kalshi orderbook snapshot array `[[price_cents, qty], ...]`
    into a `{price: qty}` map, dropping zero/negative qty entries and
    invalid shapes.
    """
    out: dict[int, int] = {}
    if not isinstance(arr, list):
        return out
    for entry in arr:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        try:
            price = int(entry[0])
            qty = int(entry[1])
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[price] = qty
    return out
