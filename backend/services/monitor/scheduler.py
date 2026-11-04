"""
Monitor scheduler — orchestrates all sources and the alert manager.

Architecture:
  1. OddsApiPoller runs on a scheduled cadence (default 15 min).
     When it completes, the full pipeline re-evaluates all opportunities.
  2. PolymarketWsConsumer streams price changes in real-time.
     When a watched market's price moves, a targeted re-evaluation runs.
  3. KalshiWsConsumer streams orderbook changes in real-time.
     Same trigger logic as Polymarket.

The scheduler coordinates these three sources and feeds results
into the AlertManager for notification decisions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from services.adapters import PolymarketAdapter, KalshiAdapter, MarketAdapter
from services.engine_config import EngineConfig
from services.monitor.config import MonitorConfig
from services.monitor.manager import AlertManager, CycleResult
from services.monitor.odds_poller import OddsApiPoller
from services.monitor.ws_polymarket import PolymarketWsConsumer
from services.monitor.ws_kalshi import KalshiWsConsumer
from services.monitor.state import AlertStateStore
from services.monitor.notifier import PushoverNotifier, DryRunNotifier
from services.opportunities import EvaluatedOpportunity
from services.snapshot import refresh_snapshot

logger = logging.getLogger(__name__)


@dataclass
class MonitorStatus:
    """Current monitoring system status."""
    running: bool = False
    enabled: bool = False
    last_cycle_ts: float = 0.0
    total_cycles: int = 0
    total_alerts_sent: int = 0
    odds_poller: dict = field(default_factory=dict)
    polymarket_ws: dict = field(default_factory=dict)
    kalshi_ws: dict = field(default_factory=dict)
    last_cycle_result: dict | None = None


class MonitorScheduler:
    """
    Top-level orchestrator for the monitoring system.

    Manages the lifecycle of all source consumers and coordinates
    pipeline evaluation → alert decisions → notification delivery.
    """

    def __init__(
        self,
        monitor_config: MonitorConfig,
        engine_config: EngineConfig | None = None,
    ):
        self.monitor_config = monitor_config
        self.engine_config = engine_config or EngineConfig(
            min_edge=monitor_config.min_ev,
        )

        # State
        self.state = AlertStateStore()

        # Notifier
        if monitor_config.dry_run:
            self.notifier = DryRunNotifier()
        else:
            self.notifier = PushoverNotifier(
                user_key=monitor_config.pushover_user_key,
                api_token=monitor_config.pushover_api_token,
            )

        # Alert manager
        self.alert_manager = AlertManager(
            config=monitor_config,
            state=self.state,
            notifier=self.notifier,
        )

        # Source consumers
        self.odds_poller = OddsApiPoller(
            poll_interval_minutes=monitor_config.refresh_interval_minutes,
            max_polls_per_day=monitor_config.odds_api_max_polls_per_day,
        )
        self.pm_ws = PolymarketWsConsumer()
        self.kalshi_ws = KalshiWsConsumer()

        # Internal state
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._total_cycles = 0
        self._total_alerts_sent = 0
        self._last_cycle_ts: float = 0.0
        self._last_cycle_result: CycleResult | None = None
        self._debounce_lock = asyncio.Lock()
        # Serializes start()/stop() so overlapping /monitor/start or
        # /monitor/stop calls cannot create duplicate or orphaned
        # poller / WS tasks.
        self._lifecycle_lock = asyncio.Lock()
        self._last_ws_eval_ts: float = 0.0
        self._ws_eval_cooldown: float = 10.0  # min seconds between WS-triggered evals

    # --- Pipeline execution ---

    async def _run_pipeline_and_alert(self, trigger: str = "scheduled") -> CycleResult | None:
        """Run the full pipeline and feed results to the alert manager.

        Routes through services.snapshot.refresh_snapshot so the scheduler and
        manual POST /refresh share a single _refresh_lock — preventing
        concurrent expensive pipeline runs and unifying refresh-status
        observability.  If the refresh fails, NO alerts are dispatched
        (avoids alerting from a stale previous snapshot).
        """
        if not self.monitor_config.enabled and not self.monitor_config.dry_run:
            return None

        async with self._debounce_lock:
            cfg = EngineConfig(
                min_edge=self.monitor_config.min_ev * 0.5,  # fetch wider, filter in alert manager
            )
            try:
                # Shared refresh path: acquires the snapshot _refresh_lock,
                # records last_trigger / refresh_started_at / duration / error,
                # stores the snapshot for dashboard reads.
                snap = await refresh_snapshot(trigger=trigger, cfg=cfg)
            except Exception as exc:
                logger.error("Monitor pipeline failed [%s]: %s", trigger, exc)
                return None

            try:
                opportunities = snap.opportunities

                result = await self.alert_manager.run_cycle(opportunities)

                # Update WS watch lists from pipeline results
                self._update_watch_lists(opportunities)

                self._total_cycles += 1
                self._total_alerts_sent += result.alerts_sent
                self._last_cycle_ts = time.time()
                self._last_cycle_result = result

                logger.info(
                    "Monitor cycle [%s]: %d opps → %d alerts sent",
                    trigger, len(opportunities), result.alerts_sent,
                )
                return result

            except Exception as exc:
                logger.error("Monitor alert phase failed [%s]: %s", trigger, exc)
                return None

    def _update_watch_lists(self, opportunities: list[EvaluatedOpportunity]) -> None:
        """Feed pipeline results into WS consumers so they know which markets to watch."""
        pm_ids: set[str] = set()
        kalshi_tickers: set[str] = set()
        for opp in opportunities:
            if opp.status == "SKIP":
                continue
            if opp.platform == "polymarket":
                pm_ids.add(opp.market_id)
            elif opp.platform == "kalshi":
                kalshi_tickers.add(opp.market_id)
        if pm_ids:
            self.pm_ws.watch(pm_ids)
        if kalshi_tickers:
            self.kalshi_ws.watch(kalshi_tickers)

    # --- Source callbacks ---

    async def _on_odds_update(self, events: list, meta: dict) -> None:
        """Callback when Odds API poll completes."""
        await self._run_pipeline_and_alert(trigger="odds_api")

    async def _on_pm_price_change(self, asset_id: str, price: float) -> None:
        """Callback when a Polymarket price changes."""
        now = time.time()
        if now - self._last_ws_eval_ts < self._ws_eval_cooldown:
            return  # debounce rapid WS updates
        self._last_ws_eval_ts = now
        await self._run_pipeline_and_alert(trigger=f"pm_ws:{asset_id[:12]}")

    async def _on_kalshi_price_change(self, ticker: str, price: float) -> None:
        """Callback when a Kalshi price changes."""
        now = time.time()
        if now - self._last_ws_eval_ts < self._ws_eval_cooldown:
            return
        self._last_ws_eval_ts = now
        await self._run_pipeline_and_alert(trigger=f"kalshi_ws:{ticker[:12]}")

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start all monitoring sources."""
        async with self._lifecycle_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self._running:
            logger.warning("MonitorScheduler: already running")
            return

        self._running = True
        logger.info(
            "MonitorScheduler: starting (enabled=%s, dry_run=%s, preset=%s)",
            self.monitor_config.enabled,
            self.monitor_config.dry_run,
            self.monitor_config.preset.value,
        )

        # Run initial evaluation immediately
        await self._run_pipeline_and_alert(trigger="startup")

        # stop() may have been called while the startup pipeline was
        # awaited.  Do not spawn background sources in that case — they
        # would be orphaned (never cancelled) and keep spending quota.
        if not self._running:
            logger.info("MonitorScheduler: stopped during startup; not starting sources")
            return

        # Start Odds API poller
        self._tasks.append(
            asyncio.create_task(
                self.odds_poller.run_loop(self._on_odds_update),
                name="odds_poller",
            )
        )

        # Start WebSocket consumers (non-blocking — they'll reconnect on their own)
        if "polymarket" in self.monitor_config.platforms:
            self._tasks.append(
                asyncio.create_task(
                    self.pm_ws.run_loop(self._on_pm_price_change),
                    name="pm_ws",
                )
            )

        if "kalshi" in self.monitor_config.platforms:
            self._tasks.append(
                asyncio.create_task(
                    self.kalshi_ws.run_loop(self._on_kalshi_price_change),
                    name="kalshi_ws",
                )
            )

    async def stop(self) -> None:
        """Stop all monitoring sources."""
        # Flag first (before waiting on the lifecycle lock) so an in-flight
        # start() bails out after its startup pipeline instead of spawning
        # background tasks.
        self._running = False
        self.odds_poller.stop()
        self.pm_ws.stop()
        self.kalshi_ws.stop()

        async with self._lifecycle_lock:
            self._running = False
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

            self.state.save()
        logger.info("MonitorScheduler: stopped")

    def update_config(self, config: MonitorConfig) -> None:
        """Update config without restarting — takes effect next cycle."""
        self.monitor_config = config
        self.alert_manager.config = config

        # Update notifier if credentials changed
        if config.dry_run:
            self.notifier = DryRunNotifier()
        else:
            self.notifier = PushoverNotifier(
                user_key=config.pushover_user_key,
                api_token=config.pushover_api_token,
            )
        self.alert_manager.notifier = self.notifier

        # Update poller cadence
        self.odds_poller.poll_interval = config.refresh_interval_minutes * 60
        self.odds_poller.max_polls_per_day = config.odds_api_max_polls_per_day

    def status(self) -> MonitorStatus:
        return MonitorStatus(
            running=self._running,
            enabled=self.monitor_config.enabled,
            last_cycle_ts=self._last_cycle_ts,
            total_cycles=self._total_cycles,
            total_alerts_sent=self._total_alerts_sent,
            odds_poller=self.odds_poller.status(),
            polymarket_ws=self.pm_ws.status(),
            kalshi_ws=self.kalshi_ws.status(),
            last_cycle_result={
                "timestamp": self._last_cycle_result.timestamp,
                "candidates": self._last_cycle_result.candidates_evaluated,
                "filter_passed": self._last_cycle_result.filter_passed,
                "alerts_sent": self._last_cycle_result.alerts_sent,
                "suppressed_cooldown": self._last_cycle_result.suppressed_cooldown,
                "suppressed_filter": self._last_cycle_result.suppressed_filter,
                "dry_run": self._last_cycle_result.dry_run,
            } if self._last_cycle_result else None,
        )
