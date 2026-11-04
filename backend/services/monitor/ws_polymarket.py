"""
Polymarket WebSocket consumer.

Connects to the Polymarket CLOB WebSocket and subscribes to price
updates for watched condition IDs (market asset ids).

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Protocol: JSON messages — subscribe with asset_ids, receive price ticks.

This consumer watches for price changes on markets that the pipeline
has identified as interesting, and triggers re-evaluation when prices
move meaningfully.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_RECONNECT_DELAY = 5
_MAX_RECONNECT_DELAY = 60
_PING_INTERVAL = 30
_MEANINGFUL_PRICE_CHANGE = 0.01  # 1 cent minimum change to trigger re-eval


def _backoff_delay(attempt: int) -> int:
    """Exponential reconnect backoff: 5, 10, 20, 40, then capped at 60s."""
    attempt = max(1, attempt)
    return min(_RECONNECT_DELAY * (2 ** min(attempt - 1, 10)), _MAX_RECONNECT_DELAY)


class PolymarketWsConsumer:
    """
    WebSocket consumer for Polymarket real-time price updates.

    Subscribes to specific asset_ids and calls on_price_change when
    a watched market's price moves by >= threshold.
    """

    def __init__(self, min_price_change: float = _MEANINGFUL_PRICE_CHANGE):
        self._watched_ids: set[str] = set()
        self._last_prices: dict[str, float] = {}
        self._min_change = min_price_change
        self._running = False
        self._connected = False
        self._last_message_ts: float = 0.0
        self._reconnect_count: int = 0

    def watch(self, asset_ids: set[str]) -> None:
        """Set the asset IDs to watch. Call before or during run_loop."""
        self._watched_ids = asset_ids
        logger.info("PolymarketWs: watching %d assets", len(asset_ids))

    def unwatch_all(self) -> None:
        self._watched_ids.clear()
        self._last_prices.clear()

    async def run_loop(
        self,
        on_price_change: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """
        Connect to WebSocket and stream price updates.

        Reconnects automatically on disconnection.
        Calls on_price_change(market_id, new_price) when a watched
        market's price changes by >= min_price_change.
        """
        try:
            import websockets
        except ImportError:
            logger.error("PolymarketWs: websockets package not installed, skipping")
            return

        self._running = True
        logger.info("PolymarketWs: starting consumer loop")

        while self._running:
            try:
                async with websockets.connect(
                    _WS_URL,
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    # _reconnect_count is reset only once the connection has
                    # proven healthy (first message received), so a server
                    # that accepts then immediately closes still backs off.
                    healthy = False
                    logger.info("PolymarketWs: connected")

                    # Subscribe to watched assets
                    if self._watched_ids:
                        sub_msg = {
                            "type": "market",
                            "assets_ids": list(self._watched_ids),
                        }
                        await ws.send(json.dumps(sub_msg))
                        logger.info("PolymarketWs: subscribed to %d assets", len(self._watched_ids))

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
                if not self._running:
                    break
                self._reconnect_count += 1
                delay = _backoff_delay(self._reconnect_count)
                logger.warning(
                    "PolymarketWs: connection closed by server, reconnecting in %ds (attempt %d)",
                    delay, self._reconnect_count,
                )
                await asyncio.sleep(delay)

            except Exception as exc:
                self._connected = False
                self._reconnect_count += 1
                delay = _backoff_delay(self._reconnect_count)
                logger.warning(
                    "PolymarketWs: disconnected (%s), reconnecting in %ds (attempt %d)",
                    exc, delay, self._reconnect_count,
                )
                await asyncio.sleep(delay)

    async def _handle_message(
        self,
        msg: dict | list,
        on_price_change: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Process a WebSocket message and fire callback if price changed."""
        # Polymarket sends price updates as a list of market snapshots
        updates = msg if isinstance(msg, list) else [msg]

        for update in updates:
            asset_id = update.get("asset_id") or update.get("market") or ""
            if asset_id not in self._watched_ids:
                continue

            price = None
            if "price" in update:
                try:
                    price = float(update["price"])
                except (ValueError, TypeError):
                    continue
            elif "best_ask" in update:
                try:
                    price = float(update["best_ask"])
                except (ValueError, TypeError):
                    continue

            if price is None:
                continue

            prev = self._last_prices.get(asset_id)
            if prev is None or abs(price - prev) >= self._min_change:
                self._last_prices[asset_id] = price
                await on_price_change(asset_id, price)

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "watched_count": len(self._watched_ids),
            "last_message_ts": self._last_message_ts,
            "reconnect_count": self._reconnect_count,
        }
