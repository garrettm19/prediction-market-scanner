"""
Tests for services.monitor.manager — AlertManager decisions.

Covers:
  - _passes_filters: status, edge, platform, market_type, ambiguity
  - _decide: NEW, IMPROVED, COOLDOWN, expired cooldown
  - run_cycle: rate limiting, deduplication, state persistence
  - make_key stability
"""
import asyncio
import time

import pytest

from services.monitor.config import MonitorConfig
from services.monitor.manager import AlertManager, CycleResult
from services.monitor.state import AlertStateStore, AlertRecord, make_key
from services.monitor.notifier import AlertPayload, DryRunNotifier
from services.opportunities import EvaluatedOpportunity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> MonitorConfig:
    defaults = dict(
        enabled=True,
        min_ev=0.05,
        platforms=["polymarket", "kalshi"],
        market_types=["h2h"],
        cooldown_minutes=30,
        resend_edge_improvement=0.03,
        max_alerts_per_hour=10,
        dry_run=False,
        exclude_ambiguity_downgraded=True,
    )
    defaults.update(overrides)
    return MonitorConfig(**defaults)


def _make_opp(
    platform: str = "polymarket",
    event: str = "Alcaraz vs Sinner",
    market_type: str = "h2h",
    side: str = "Carlos Alcaraz",
    line: float | None = None,
    edge: float = 0.08,
    status: str = "BUY",
    p_true: float = 0.65,
    pm_price: float = 0.56,
    fd_odds: int = -180,
    fanduel_confidence_label: str = "High",
    event_url: str | None = "https://polymarket.com/test",
    recommended_kelly: float = 0.02,
    kelly_full: float = 0.10,
    reject_reasons: list[str] | None = None,
    downgrade_reasons: list[str] | None = None,
) -> EvaluatedOpportunity:
    return EvaluatedOpportunity(
        platform=platform,
        sport="tennis_atp",
        event=event,
        event_url=event_url,
        tournament="ATP Test",
        start_time="2026-06-01T12:00:00Z",
        market_id="m1",
        market_type=market_type,
        side=side,
        line=line,
        pm_price=pm_price,
        fd_odds=fd_odds,
        p_true=p_true,
        edge=edge,
        recommended_kelly=recommended_kelly,
        kelly_full=kelly_full,
        fanduel_overround=0.02,
        fanduel_line_width=0.20,
        fanduel_line_width_label="Moderate",
        fanduel_confidence_label=fanduel_confidence_label,
        event_match_confidence=0.95,
        match_quality="verified",
        matched_event_id="ev1",
        second_best_event_id="",
        confidence_gap=1.0,
        competing_matches=1,
        has_shared_last_name=False,
        status=status,
        reject_reasons=reject_reasons or [],
        downgrade_reasons=downgrade_reasons or [],
        home_tokens=("carlos", "alcaraz"),
        away_tokens=("jannik", "sinner"),
        name_match_score=1.0,
        date_score=1.0,
        date_delta_hours=2.0,
        rule_evaluations=[],
    )


def _make_manager(config: MonitorConfig | None = None) -> tuple[AlertManager, AlertStateStore, DryRunNotifier]:
    cfg = config or _make_config()
    # Default path is redirected to a per-test tmp_path by the autouse
    # fixture in tests/conftest.py, so this never touches backend/data.
    state = AlertStateStore()
    notifier = DryRunNotifier()
    manager = AlertManager(config=cfg, state=state, notifier=notifier)
    return manager, state, notifier


# ---------------------------------------------------------------------------
# make_key stability
# ---------------------------------------------------------------------------

class TestMakeKey:
    def test_basic_key(self):
        key = make_key("polymarket", "Alcaraz vs Sinner", "h2h", "Carlos Alcaraz", None)
        assert key == "polymarket|Alcaraz vs Sinner|h2h|Carlos Alcaraz|none"

    def test_with_line(self):
        key = make_key("kalshi", "Event", "totals", "Over", 22.5)
        assert key == "kalshi|Event|totals|Over|22.5"

    def test_name_order_matters(self):
        """If home/away swap produces different event label, key changes — cooldown lost."""
        key1 = make_key("polymarket", "Alcaraz vs Sinner", "h2h", "Alcaraz", None)
        key2 = make_key("polymarket", "Sinner vs Alcaraz", "h2h", "Alcaraz", None)
        assert key1 != key2  # documents the fragility


# ---------------------------------------------------------------------------
# _passes_filters
# ---------------------------------------------------------------------------

class TestPassesFilters:
    def test_buy_passes(self):
        manager, _, _ = _make_manager()
        opp = _make_opp(status="BUY", edge=0.08)
        assert manager._passes_filters(opp) is True

    def test_watch_rejected(self):
        manager, _, _ = _make_manager()
        opp = _make_opp(status="WATCH", edge=0.08)
        assert manager._passes_filters(opp) is False

    def test_skip_rejected(self):
        manager, _, _ = _make_manager()
        opp = _make_opp(status="SKIP", edge=0.08)
        assert manager._passes_filters(opp) is False

    def test_edge_below_min_rejected(self):
        manager, _, _ = _make_manager(_make_config(min_ev=0.10))
        opp = _make_opp(status="BUY", edge=0.08)
        assert manager._passes_filters(opp) is False

    def test_edge_at_min_passes(self):
        manager, _, _ = _make_manager(_make_config(min_ev=0.05))
        opp = _make_opp(status="BUY", edge=0.05)
        assert manager._passes_filters(opp) is True

    def test_wrong_platform_rejected(self):
        manager, _, _ = _make_manager(_make_config(platforms=["kalshi"]))
        opp = _make_opp(platform="polymarket", status="BUY")
        assert manager._passes_filters(opp) is False

    def test_wrong_market_type_rejected(self):
        manager, _, _ = _make_manager(_make_config(market_types=["totals"]))
        opp = _make_opp(market_type="h2h", status="BUY")
        assert manager._passes_filters(opp) is False

    def test_ambiguity_downgrade_excluded(self):
        manager, _, _ = _make_manager(_make_config(exclude_ambiguity_downgraded=True))
        opp = _make_opp(status="BUY", downgrade_reasons=["SHARED_LAST_NAME"])
        assert manager._passes_filters(opp) is False

    def test_ambiguity_downgrade_allowed_when_disabled(self):
        manager, _, _ = _make_manager(_make_config(exclude_ambiguity_downgraded=False))
        opp = _make_opp(status="BUY", downgrade_reasons=["SHARED_LAST_NAME"])
        assert manager._passes_filters(opp) is True

    def test_non_ambiguity_downgrade_passes(self):
        manager, _, _ = _make_manager(_make_config(exclude_ambiguity_downgraded=True))
        opp = _make_opp(status="BUY", downgrade_reasons=["EDGE_BELOW_THRESHOLD"])
        assert manager._passes_filters(opp) is True


# ---------------------------------------------------------------------------
# _decide
# ---------------------------------------------------------------------------

class TestDecide:
    def test_first_time_is_new(self):
        manager, _, _ = _make_manager()
        rec = AlertRecord(key="test", alert_count=0)
        opp = _make_opp(edge=0.08)
        assert manager._decide(rec, opp, time.time()) == "NEW"

    def test_within_cooldown_no_improvement(self):
        manager, _, _ = _make_manager(_make_config(cooldown_minutes=30))
        now = time.time()
        rec = AlertRecord(
            key="test",
            alert_count=1,
            last_alert_ts=now - 600,  # 10 min ago (within 30 min cooldown)
            last_alert_edge=0.08,
        )
        opp = _make_opp(edge=0.09)  # only +1%, below 3% threshold
        assert manager._decide(rec, opp, now) == "COOLDOWN"

    def test_within_cooldown_with_improvement(self):
        manager, _, _ = _make_manager(_make_config(cooldown_minutes=30, resend_edge_improvement=0.03))
        now = time.time()
        rec = AlertRecord(
            key="test",
            alert_count=1,
            last_alert_ts=now - 600,
            last_alert_edge=0.05,
        )
        opp = _make_opp(edge=0.09)  # +4%, above 3% threshold
        assert manager._decide(rec, opp, now) == "IMPROVED"

    def test_cooldown_expired_is_new(self):
        manager, _, _ = _make_manager(_make_config(cooldown_minutes=30))
        now = time.time()
        rec = AlertRecord(
            key="test",
            alert_count=1,
            last_alert_ts=now - 2000,  # 33 min ago (past 30 min cooldown)
            last_alert_edge=0.08,
        )
        opp = _make_opp(edge=0.08)
        assert manager._decide(rec, opp, now) == "NEW"

    def test_improvement_exactly_at_threshold(self):
        manager, _, _ = _make_manager(_make_config(resend_edge_improvement=0.03))
        now = time.time()
        rec = AlertRecord(
            key="test",
            alert_count=1,
            last_alert_ts=now - 60,
            last_alert_edge=0.05,
        )
        opp = _make_opp(edge=0.08)  # exactly +3%
        assert manager._decide(rec, opp, now) == "IMPROVED"

    def test_edge_decreased_stays_cooldown(self):
        manager, _, _ = _make_manager(_make_config(cooldown_minutes=30))
        now = time.time()
        rec = AlertRecord(
            key="test",
            alert_count=1,
            last_alert_ts=now - 60,
            last_alert_edge=0.10,
        )
        opp = _make_opp(edge=0.07)  # decreased
        assert manager._decide(rec, opp, now) == "COOLDOWN"


# ---------------------------------------------------------------------------
# run_cycle (async)
# ---------------------------------------------------------------------------

class TestRunCycle:
    def test_new_opportunity_fires_alert(self):
        manager, state, notifier = _make_manager()
        opp = _make_opp(status="BUY", edge=0.08)
        result = asyncio.run(manager.run_cycle([opp]))

        assert result.candidates_evaluated == 1
        assert result.filter_passed == 1
        assert result.new_alerts == 1
        assert result.alerts_sent == 1
        assert len(notifier.sent) == 1

    def test_watch_not_alerted(self):
        manager, _, notifier = _make_manager()
        opp = _make_opp(status="WATCH", edge=0.08)
        result = asyncio.run(manager.run_cycle([opp]))

        assert result.filter_passed == 0
        assert result.alerts_sent == 0
        assert len(notifier.sent) == 0

    def test_cooldown_suppresses_repeat(self):
        async def _run():
            manager, state, notifier = _make_manager(_make_config(cooldown_minutes=30))
            opp = _make_opp(status="BUY", edge=0.08)

            # First cycle — fires
            r1 = await manager.run_cycle([opp])
            assert r1.alerts_sent == 1

            # Second cycle — same opp, within cooldown
            r2 = await manager.run_cycle([opp])
            assert r2.suppressed_cooldown == 1
            assert r2.alerts_sent == 0
            assert len(notifier.sent) == 1  # still just the first one

        asyncio.run(_run())

    def test_rate_limit_caps_alerts(self):
        manager, _, notifier = _make_manager(_make_config(max_alerts_per_hour=2))

        opps = [
            _make_opp(event=f"Event {i}", side=f"Player {i}", edge=0.05 + i * 0.01)
            for i in range(5)
        ]
        result = asyncio.run(manager.run_cycle(opps))

        assert result.alerts_sent == 2
        assert result.suppressed_rate_limit == 3
        assert len(notifier.sent) == 2

    def test_rate_limit_cap_lowered_below_already_sent_sends_nothing(self):
        """Regression (moni0): cap lowered below alerts already sent this hour.

        10 alerts already sent, cap now 5 -> hourly_remaining is -5.  A
        negative slice previously kept len+(-5) items; the cap must instead
        suppress every candidate.
        """
        manager, state, notifier = _make_manager(_make_config(max_alerts_per_hour=5))
        now = time.time()
        state._alerts_this_hour = [now - 60 - i for i in range(10)]

        opps = [
            _make_opp(event=f"Event {i}", side=f"Player {i}", edge=0.06 + i * 0.01)
            for i in range(8)
        ]
        result = asyncio.run(manager.run_cycle(opps))

        assert result.alerts_sent == 0
        assert result.suppressed_rate_limit == 8
        assert len(notifier.sent) == 0
        assert state.alerts_sent_this_hour() == 10

    def test_rate_limit_remaining_exactly_zero_sends_nothing(self):
        """Boundary: alerts already sent == cap -> nothing sent, all suppressed."""
        manager, state, notifier = _make_manager(_make_config(max_alerts_per_hour=5))
        now = time.time()
        state._alerts_this_hour = [now - 60 - i for i in range(5)]

        opps = [
            _make_opp(event=f"Event {i}", side=f"Player {i}", edge=0.06 + i * 0.01)
            for i in range(3)
        ]
        result = asyncio.run(manager.run_cycle(opps))

        assert result.alerts_sent == 0
        assert result.suppressed_rate_limit == 3
        assert len(notifier.sent) == 0

    def test_disabled_monitor_does_nothing(self):
        manager, _, notifier = _make_manager(_make_config(enabled=False, dry_run=False))
        opp = _make_opp(status="BUY", edge=0.08)
        result = asyncio.run(manager.run_cycle([opp]))

        assert result.alerts_sent == 0
        assert len(notifier.sent) == 0

    def test_improved_edge_breaks_cooldown(self):
        async def _run():
            manager, _, notifier = _make_manager(
                _make_config(cooldown_minutes=60, resend_edge_improvement=0.03)
            )

            opp1 = _make_opp(status="BUY", edge=0.06)
            await manager.run_cycle([opp1])
            assert len(notifier.sent) == 1

            # Same opp, edge jumped from 0.06 to 0.10 (+4%, above 3% threshold)
            opp2 = _make_opp(status="BUY", edge=0.10)
            r2 = await manager.run_cycle([opp2])
            assert r2.re_alerts == 1
            assert r2.alerts_sent == 1
            assert len(notifier.sent) == 2

        asyncio.run(_run())

    def test_multiple_platforms_independent(self):
        manager, _, notifier = _make_manager()

        opp_pm = _make_opp(platform="polymarket", edge=0.08)
        opp_k = _make_opp(platform="kalshi", edge=0.07)
        result = asyncio.run(manager.run_cycle([opp_pm, opp_k]))

        assert result.alerts_sent == 2
        assert len(notifier.sent) == 2


# ---------------------------------------------------------------------------
# AlertStateStore
# ---------------------------------------------------------------------------

class TestAlertStateStore:
    def test_upsert_creates_record(self):
        store = AlertStateStore()
        rec = store.upsert("test_key", platform="polymarket", event="Test")
        assert rec.key == "test_key"
        assert rec.platform == "polymarket"

    def test_upsert_updates_existing(self):
        store = AlertStateStore()
        store.upsert("test_key", platform="polymarket", current_edge=0.05)
        rec = store.upsert("test_key", current_edge=0.10)
        assert rec.current_edge == 0.10
        assert rec.platform == "polymarket"  # preserved from first upsert

    def test_alerts_sent_this_hour_prunes_old(self):
        store = AlertStateStore()
        # Add an alert timestamp from 2 hours ago
        store._alerts_this_hour = [time.time() - 7200]
        assert store.alerts_sent_this_hour() == 0

    def test_alerts_sent_this_hour_counts_recent(self):
        store = AlertStateStore()
        store._alerts_this_hour = [time.time() - 100, time.time() - 50]
        assert store.alerts_sent_this_hour() == 2

    def test_mark_all_inactive(self):
        store = AlertStateStore()
        store.upsert("k1", is_active=True)
        store.upsert("k2", is_active=True)
        store.mark_all_inactive()
        for rec in store.all_records():
            assert rec.is_active is False

    def test_prune_stale(self, tmp_path):
        # Use an isolated temp path so this test does not load any
        # pre-existing local backend/data/alert_state.json records.
        store = AlertStateStore(path=str(tmp_path / "state.json"))
        store.upsert("old", last_seen_ts=time.time() - 400000)
        store.upsert("recent", last_seen_ts=time.time())
        pruned = store.prune_stale(max_age_hours=72)
        assert pruned == 1
        assert store.get("old") is None
        assert store.get("recent") is not None


class TestAlertStatePathIsolation:
    """Guard (testL0): tests must never touch backend/data/alert_state.json."""

    def test_default_path_redirected_to_tmp(self, tmp_path):
        import os
        import services.monitor.state as alert_state

        real = os.path.realpath(os.path.join(
            os.path.dirname(alert_state.__file__), "..", "..", "data", "alert_state.json",
        ))
        store = AlertStateStore()
        assert os.path.realpath(store._path) != real
        assert os.path.realpath(store._path).startswith(os.path.realpath(str(tmp_path)))

    def test_scheduler_state_uses_isolated_path(self, tmp_path):
        import os
        from services.monitor.scheduler import MonitorScheduler

        s = MonitorScheduler(monitor_config=MonitorConfig(enabled=True, dry_run=True))
        assert os.path.realpath(s.state._path).startswith(os.path.realpath(str(tmp_path)))
