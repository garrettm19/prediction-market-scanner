"""
Alert manager — state-change-driven alert decisions.

Evaluates pipeline output against user filters and alert state.
Decides which opportunities trigger notifications and which are
suppressed by cooldowns, rate limits, or filter rules.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from services.monitor.config import MonitorConfig
from services.monitor.state import AlertStateStore, AlertRecord, make_key
from services.monitor.notifier import (
    Notifier,
    AlertPayload,
    format_instant_alert,
    format_digest_alert,
)
from services.opportunities import EvaluatedOpportunity

logger = logging.getLogger(__name__)

# Ambiguity reason codes — used to filter ambiguity-downgraded opportunities
_AMBIGUITY_REASONS = {
    "AMBIGUOUS_MATCH_GAP",
    "SHARED_LAST_NAME",
    "EXCESS_COMPETING_MATCHES",
    "LAST_NAME_COLLISION",
}


@dataclass
class CycleResult:
    """Result of one monitoring cycle — for observability."""
    timestamp: float = 0.0
    candidates_evaluated: int = 0
    filter_passed: int = 0
    new_alerts: int = 0
    re_alerts: int = 0
    suppressed_cooldown: int = 0
    suppressed_rate_limit: int = 0
    suppressed_filter: int = 0
    alerts_sent: int = 0
    alerts_failed: int = 0
    dry_run: bool = False
    details: list[dict] = field(default_factory=list)


class AlertManager:
    """Evaluates opportunities and dispatches notifications."""

    def __init__(
        self,
        config: MonitorConfig,
        state: AlertStateStore,
        notifier: Notifier,
    ):
        self.config = config
        self.state = state
        self.notifier = notifier

    async def run_cycle(
        self,
        opportunities: list[EvaluatedOpportunity],
    ) -> CycleResult:
        """
        Evaluate a batch of pipeline results and send qualifying alerts.

        State-change driven:
          - NEW: opportunity first seen or newly re-qualifies → alert
          - IMPROVED: edge improved by >= resend_edge_improvement → re-alert
          - COOLDOWN: still active but within cooldown window → suppress
          - FILTERED: doesn't match user filters → suppress
        """
        cfg = self.config
        result = CycleResult(
            timestamp=time.time(),
            candidates_evaluated=len(opportunities),
            dry_run=cfg.dry_run,
        )

        if not cfg.enabled and not cfg.dry_run:
            return result

        # Mark all existing state inactive, then re-activate as we see them
        self.state.mark_all_inactive()

        to_alert: list[tuple[EvaluatedOpportunity, AlertRecord, str]] = []  # (opp, rec, reason)

        for opp in opportunities:
            key = make_key(opp.platform, opp.event, opp.market_type, opp.side, opp.line)
            now = time.time()

            # --- Filter check ---
            if not self._passes_filters(opp):
                result.suppressed_filter += 1
                result.details.append({
                    "key": key, "action": "FILTERED", "reason": "user_filters",
                    "edge": opp.edge, "status": opp.status,
                })
                continue

            result.filter_passed += 1

            # --- Update state ---
            rec = self.state.upsert(
                key,
                platform=opp.platform,
                event=opp.event,
                market_type=opp.market_type,
                side=opp.side,
                line=opp.line,
                current_edge=opp.edge,
                current_status=opp.status,
                is_active=True,
                last_seen_ts=now,
            )
            if rec.first_seen_ts == 0:
                rec.first_seen_ts = now

            # --- Alert decision ---
            decision = self._decide(rec, opp, now)

            if decision == "NEW" or decision == "IMPROVED":
                to_alert.append((opp, rec, decision))
            elif decision == "COOLDOWN":
                result.suppressed_cooldown += 1
                rec.suppressed_count += 1
                result.details.append({
                    "key": key, "action": "SUPPRESSED", "reason": "cooldown",
                    "edge": opp.edge,
                })
            # "ACTIVE" = still qualifying but no state change → silent

        # --- Rate limit ---
        hourly_remaining = cfg.max_alerts_per_hour - self.state.alerts_sent_this_hour()
        # Clamp at 0: if the cap was lowered below alerts already sent this
        # hour, hourly_remaining is negative and a negative slice would keep
        # items instead of suppressing them all.
        send_cap = max(0, min(hourly_remaining, len(to_alert)))

        if send_cap < len(to_alert):
            result.suppressed_rate_limit += len(to_alert) - send_cap
            to_alert.sort(key=lambda x: x[0].edge, reverse=True)
            to_alert = to_alert[:send_cap]

        # --- Send instant pushes ---
        for opp, rec, reason in to_alert:
            payload = format_instant_alert(
                event=opp.event,
                platform=opp.platform,
                market_type=opp.market_type,
                side=opp.side,
                edge=opp.edge,
                p_true=opp.p_true,
                pm_price=opp.pm_price,
                fd_confidence=opp.fanduel_confidence_label,
                url=opp.event_url,
                event_slug=getattr(opp, "market_id", None),
                fd_odds=opp.fd_odds,
                kelly_pct=opp.recommended_kelly * 100 if opp.recommended_kelly > 0 else 0.0,
                high_priority_min_edge=cfg.priority_high_min_edge,
            )
            success = await self.notifier.send(payload)
            if success:
                result.alerts_sent += 1
                self._mark_alerted(rec, opp.edge)
                if reason == "NEW":
                    result.new_alerts += 1
                else:
                    result.re_alerts += 1
                result.details.append({
                    "key": rec.key, "action": "ALERTED", "reason": reason,
                    "edge": opp.edge, "status": opp.status,
                })
            else:
                result.alerts_failed += 1

        # --- Persist ---
        self.state.save()

        logger.info(
            "AlertManager cycle: %d evaluated → %d passed filters → "
            "%d sent (%d new, %d re) · %d cooldown · %d rate-limited · %d filtered%s",
            result.candidates_evaluated,
            result.filter_passed,
            result.alerts_sent,
            result.new_alerts,
            result.re_alerts,
            result.suppressed_cooldown,
            result.suppressed_rate_limit,
            result.suppressed_filter,
            " [DRY RUN]" if cfg.dry_run else "",
        )
        return result

    # --- Private helpers ---

    def _passes_filters(self, opp: EvaluatedOpportunity) -> bool:
        cfg = self.config

        if opp.status != "BUY":
            return False
        if opp.edge < cfg.min_ev:
            return False
        if opp.platform not in cfg.platforms:
            return False
        if opp.market_type not in cfg.market_types:
            return False
        if cfg.exclude_ambiguity_downgraded:
            if any(r in _AMBIGUITY_REASONS for r in opp.downgrade_reasons):
                return False
        return True

    def _decide(self, rec: AlertRecord, opp: EvaluatedOpportunity, now: float) -> str:
        """
        Decide alert action for one opportunity.

        Returns: "NEW" | "IMPROVED" | "COOLDOWN" | "ACTIVE"
        """
        cfg = self.config

        # Never alerted before → NEW
        if rec.alert_count == 0:
            return "NEW"

        # Within cooldown window → check if edge improved enough to override
        cooldown_expires = rec.last_alert_ts + cfg.cooldown_minutes * 60
        if now < cooldown_expires:
            edge_improvement = opp.edge - rec.last_alert_edge
            if edge_improvement >= cfg.resend_edge_improvement:
                return "IMPROVED"
            return "COOLDOWN"

        # Cooldown expired → treat as new qualifying event
        return "NEW"

    def _mark_alerted(self, rec: AlertRecord, edge: float) -> None:
        rec.last_alert_ts = time.time()
        rec.last_alert_edge = edge
        rec.alert_count += 1
        self.state.record_alert_sent()
