"""
Odds API scheduled poller.

The Odds API updates pre-match featured markets every 1–5 minutes,
but the free tier has 500 requests/month.  This poller runs on a
configurable cadence (default 15 min) with a daily budget guard.

Each poll fetches H2H odds for all configured sports from FanDuel.
A single poll costs N requests (one per sport key).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable

from services.odds_provider import TennisOddsEvent, fetch_odds
from services.redaction import redact_secrets

logger = logging.getLogger(__name__)


class OddsApiPoller:
    """Scheduled poller for The Odds API."""

    def __init__(
        self,
        poll_interval_minutes: int = 15,
        max_polls_per_day: int = 20,
        bookmaker: str = "fanduel",
        max_sports: int = 25,
    ):
        self.poll_interval = poll_interval_minutes * 60
        self.max_polls_per_day = max_polls_per_day
        self.bookmaker = bookmaker
        self.max_sports = max_sports
        self._polls_today: list[float] = []
        self._last_poll: float = 0.0
        self._running = False
        self._latest_events: list[TennisOddsEvent] = []
        self._latest_meta: dict = {}

    @property
    def latest_events(self) -> list[TennisOddsEvent]:
        return self._latest_events

    @property
    def latest_meta(self) -> dict:
        return self._latest_meta

    def polls_remaining_today(self) -> int:
        now = time.time()
        cutoff = now - 86400
        self._polls_today = [t for t in self._polls_today if t > cutoff]
        return max(0, self.max_polls_per_day - len(self._polls_today))

    async def poll_once(self) -> tuple[list[TennisOddsEvent], dict]:
        """Execute a single poll cycle."""
        if self.polls_remaining_today() <= 0:
            logger.warning("OddsApiPoller: daily budget exhausted, skipping poll")
            return self._latest_events, self._latest_meta

        try:
            events, meta = await fetch_odds(
                bookmaker=self.bookmaker,
                max_sports=self.max_sports,
            )
            self._latest_events = events
            self._latest_meta = meta
            self._last_poll = time.time()
            self._polls_today.append(self._last_poll)

            logger.info(
                "OddsApiPoller: fetched %d events, quota remaining: %s, polls today: %d/%d",
                len(events),
                meta.get("quota_remaining", "?"),
                len(self._polls_today),
                self.max_polls_per_day,
            )
            return events, meta
        except Exception as exc:
            logger.error("OddsApiPoller: poll failed: %s", redact_secrets(exc))
            return self._latest_events, self._latest_meta

    async def run_loop(
        self,
        on_update: Callable[[list[TennisOddsEvent], dict], Awaitable[None]],
    ) -> None:
        """Run the polling loop, calling on_update after each successful poll."""
        self._running = True
        logger.info(
            "OddsApiPoller: starting loop (interval=%ds, budget=%d/day)",
            self.poll_interval, self.max_polls_per_day,
        )

        while self._running:
            events, meta = await self.poll_once()
            if events:
                await on_update(events, meta)
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    def seconds_until_next_poll(self) -> int | None:
        if not self._running or self._last_poll == 0:
            return None
        elapsed = time.time() - self._last_poll
        remaining = max(0, self.poll_interval - elapsed)
        return int(remaining)

    def status(self) -> dict:
        return {
            "last_poll": self._last_poll,
            "polls_today": len(self._polls_today),
            "max_polls_per_day": self.max_polls_per_day,
            "polls_remaining": self.polls_remaining_today(),
            "interval_minutes": self.poll_interval // 60,
            "interval_seconds": self.poll_interval,
            "seconds_until_next": self.seconds_until_next_poll(),
            "latest_event_count": len(self._latest_events),
            "running": self._running,
        }
