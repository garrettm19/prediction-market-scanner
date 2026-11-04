"""
Offline demo mode (MASG_DEMO_MODE=1).

Covers:
  (a) the synthetic fixtures run through the REAL pipeline and come out with
      the designed BUY / WATCH / SKIP mix -- statuses and reason codes are
      asserted per fixture, at a fixed reference "now";
  (b) refresh_snapshot in demo mode makes zero outbound HTTP / websocket
      connections and never enables the paper maker;
  (c) API surface in demo mode: /status flag, refresh -> snapshot with
      data_source "demo", 403 guards, empty /history, /debug offline;
  (d) with demo mode off the default path is unchanged (real fetch_odds is
      called, guards are no-ops, data_source "live").
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app
from services import snapshot as snap_mod
from services.demo import is_demo_mode
from services.demo.fixtures import (
    build_demo_dataset,
    demo_adapters,
    make_demo_odds_fetcher,
)
from services.devig import devig_3way, devig_multiplicative
from services.maker.config import MakerConfig, get_maker_config, set_maker_config
from services.opportunities import DEFAULT_CONFIG, diagnose_pipeline, fetch_opportunities
from services.scan_config import reset_scan_config

# Fixed reference clock far in the future so no fixture is ever EVENT_LIVE
# regardless of when the suite runs (2099-01-01T00:00:00Z).
NOW = 4070908800.0

client = TestClient(app)


def _wait_until_idle(timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not snap_mod.is_refreshing():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MASG_DEMO_MODE", raising=False)
    reset_scan_config()
    snap_mod.clear_snapshot()
    yield
    _wait_until_idle()
    snap_mod.clear_snapshot()
    reset_scan_config()


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("MASG_DEMO_MODE", "1")
    yield


class _NetworkAttempt(AssertionError):
    pass


def _boom(*args, **kwargs):
    raise _NetworkAttempt("outbound network call attempted in demo mode")


@pytest.fixture
def no_network():
    """Make any httpx.AsyncClient / websockets connection attempt fail loudly.

    (The backend only uses httpx.AsyncClient; the sync httpx.Client is left
    alone because FastAPI's TestClient is built on it.)
    """
    patches = [
        patch.object(httpx.AsyncClient, "send", side_effect=_boom),
        patch.object(httpx.AsyncClient, "get", side_effect=_boom),
        patch("services.opportunities.fetch_odds", side_effect=_boom),
        patch("services.opportunities.fetch_props", side_effect=_boom),
    ]
    try:
        import websockets  # noqa: F401
        patches.append(patch("websockets.connect", side_effect=_boom))
    except ImportError:  # pragma: no cover - websockets is a runtime dep
        pass
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


async def _run_demo_with_trace():
    """fetch_opportunities on the demo inputs, recording every Pass B result
    (post series-dedup) so SKIPs can be asserted too."""
    import services.opportunities as opp_mod

    trace: list = []
    real_eval = opp_mod.evaluate_rules

    def _spy(f, cfg):
        status, results = real_eval(f, cfg)
        trace.append(f)
        return status, results

    with patch.object(opp_mod, "evaluate_rules", side_effect=_spy):
        opps, meta = await fetch_opportunities(
            cfg=DEFAULT_CONFIG,
            adapters=demo_adapters(NOW),
            odds_fetcher=make_demo_odds_fetcher(NOW),
            maker_cfg=MakerConfig(),
        )
    return opps, meta, trace


def _by(trace, market_id, side=None):
    hits = [f for f in trace if f.market_id == market_id and (side is None or f.side == side)]
    assert hits, f"no evaluated candidate for {market_id} / {side}"
    return hits


# ---------------------------------------------------------------------------
# Activation helper
# ---------------------------------------------------------------------------

class TestActivation:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("MASG_DEMO_MODE", value)
        assert is_demo_mode() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "demo"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("MASG_DEMO_MODE", value)
        assert is_demo_mode() is False

    def test_unset_is_off(self):
        assert is_demo_mode() is False


# ---------------------------------------------------------------------------
# (a) Fixtures + real pipeline -> designed status mix
# ---------------------------------------------------------------------------

class TestFixtureShape:
    def test_fixtures_are_clearly_fictional_and_unlinked(self):
        ds = build_demo_dataset(NOW)
        assert ds.markets and ds.events
        for m in ds.markets:
            assert "DEMO" in m.market_id
            assert m.url is None
        for e in ds.events:
            assert "DEMO" in e.tournament
            assert e.event_id.startswith("demo-")
            assert e.bookmakers[0].bookmaker_key == "fanduel"
            # *_norm fields built the same way the odds normalizer does
            assert e.home_player_norm == e.home_player.lower()
            assert e.away_player_norm == e.away_player.lower()

    def test_events_are_in_the_future_relative_to_reference_now(self):
        from datetime import datetime
        for e in build_demo_dataset(NOW).events:
            t = datetime.fromisoformat(e.commence_time.replace("Z", "+00:00")).timestamp()
            assert NOW + 3 * 3600 <= t <= NOW + 60 * 3600

    def test_default_now_keeps_events_in_the_future(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for e in build_demo_dataset().events:
            assert datetime.fromisoformat(e.commence_time.replace("Z", "+00:00")) > now

    def test_dataset_covers_soccer_three_way_and_totals(self):
        ds = build_demo_dataset(NOW)
        soccer = [e for e in ds.events if e.sport_key.startswith("soccer_")]
        assert any(e.bookmakers[0].draw_odds is not None for e in soccer)
        assert any(e.bookmakers[0].draw_odds is None for e in soccer)
        assert any(m.market_type == "totals" for m in ds.markets)
        assert any(e.totals for e in ds.events)


class TestDemoPipelineStatusMix:
    def test_active_list_buy_and_watch(self):
        opps, meta, _ = asyncio.run(_run_demo_with_trace())
        got = {(o.market_id, o.side): (o.status, o.downgrade_reasons) for o in opps}
        assert got == {
            ("KXNBAGAME-DEMO01HCHGLL", "Harbor City Herons"): ("BUY", []),
            ("KXMLBGAME-DEMO02G2CFFPAP", "Cedar Falls Foxes"): ("BUY", []),
            ("DEMO-PM-NBA-TOTAL-2215", "Over"): ("BUY", []),
            ("KXNHLGAME-DEMO03NNASSE", "Northgate Narwhals"): ("WATCH", ["EDGE_IMPLAUSIBLE"]),
            ("KXATPMATCH-DEMO05QUIBRI", "Rowan Quillfeather"): ("WATCH", ["FD_CONFIDENCE_LOW"]),
            ("KXWTAMATCH-DEMO06HOLSTR", "Marin Holloway"): ("WATCH", ["LAST_NAME_COLLISION"]),
            ("KXEPLGAME-DEMO07KHALOL-H", "Kingsbridge Harriers"): ("WATCH", ["EDGE_BELOW_THRESHOLD"]),
        }
        assert meta["status_counts"] == {"BUY": 3, "WATCH": 4, "SKIP": 17}
        assert meta["data_source"] == "demo"
        assert meta["maker_enabled"] is False
        # Every surfaced row has a full rule trace and Kelly sizing for BUY.
        for o in opps:
            assert len(o.rule_evaluations) > 0
            if o.status == "BUY":
                assert o.recommended_kelly > 0
                assert o.reject_reasons == []

    def test_buy_edges_are_p_true_minus_effective_price(self):
        opps, _, _ = asyncio.run(_run_demo_with_trace())
        buys = {o.market_id: o for o in opps if o.status == "BUY"}
        nba = buys["KXNBAGAME-DEMO01HCHGLL"]
        p_home, _ = devig_multiplicative(0.6, 100 / 230)
        assert nba.p_true == p_home
        assert nba.edge == round(p_home - (0.53 + DEFAULT_CONFIG.cost_buffer), 4)
        # Polymarket carries no cost buffer.
        assert buys["DEMO-PM-NBA-TOTAL-2215"].edge == round(0.5 - 0.46, 4)

    def test_safety_skips(self):
        _, _, trace = asyncio.run(_run_demo_with_trace())

        # Totals line mismatch: 226.5 must not be priced off FD's 221.5.
        (f,) = _by(trace, "DEMO-PM-NBA-TOTAL-2265")
        assert f.status == "SKIP" and "LINE_MISMATCH" in f.reject_reasons

        # Soccer 3-way with no FanDuel draw odds -> NO_BOOKMAKER_DATA, both teams.
        for mid in ("KXMLSGAME-DEMO08RFCSMT-H", "KXMLSGAME-DEMO08RFCSMT-A"):
            for f in _by(trace, mid):
                assert f.status == "SKIP"
                assert "NO_BOOKMAKER_DATA" in f.reject_reasons
                assert f.edge == 0.0

        # Side inversion: favourite quoted at 0.30.
        (f,) = _by(trace, "KXNFLGAME-DEMO04BBRIOW", "Bayside Bramblers")
        assert f.status == "SKIP" and "PRICE_PROB_DIVERGENCE" in f.reject_reasons
        assert f.edge > 0.40  # the "edge" that must NOT become a BUY

        # Undated market -> confidence below survival.
        for f in _by(trace, "DEMO-PM-NHL-UNDATED"):
            assert f.status == "SKIP"
            assert "CONFIDENCE_BELOW_SURVIVAL" in f.reject_reasons

        # Opposite sides of BUY markets are NO_EDGE SKIPs.
        (f,) = _by(trace, "KXNBAGAME-DEMO01HCHGLL", "Glenmoor Lanterns")
        assert f.status == "SKIP" and "NO_EDGE" in f.reject_reasons

    def test_series_dedup_and_wrong_game_skip(self):
        _, _, trace = asyncio.run(_run_demo_with_trace())
        # Game 2 market: dedup keeps only the Game 2 FD event.
        g2 = _by(trace, "KXMLBGAME-DEMO02G2CFFPAP")
        assert {f.matched_event_id for f in g2} == {"demo-evt-mlb-foxes-pilots-g2"}
        assert all(f.competing_matches == 1 for f in g2)
        # Game 3 market: closest FD event is Game 2, 24h away -> DATE_TOO_FAR.
        g3 = _by(trace, "KXMLBGAME-DEMO02G3CFFPAP")
        assert {f.matched_event_id for f in g3} == {"demo-evt-mlb-foxes-pilots-g2"}
        for f in g3:
            assert f.status == "SKIP"
            assert "DATE_TOO_FAR" in f.reject_reasons
            assert f.date_delta_hours == pytest.approx(24.0)

    def test_soccer_three_way_uses_draw_in_devig(self):
        _, _, trace = asyncio.run(_run_demo_with_trace())
        (f,) = _by(trace, "KXEPLGAME-DEMO07KHALOL-H", "Kingsbridge Harriers")
        p_home, _, _ = devig_3way(100 / 225, 100 / 330, 100 / 350)
        assert f.p_true == p_home
        two_way_home, _ = devig_multiplicative(100 / 225, 100 / 330)
        assert two_way_home - f.p_true > 0.15  # the phantom edge avoided
        # Away team's own market: priced at its own ask, no edge.
        for f in _by(trace, "KXEPLGAME-DEMO07KHALOL-A"):
            assert f.status == "SKIP"

    def test_deterministic(self):
        a, meta_a, _ = asyncio.run(_run_demo_with_trace())
        b, meta_b, _ = asyncio.run(_run_demo_with_trace())
        strip = lambda opps: [dataclasses.replace(o, price_fetched_at=0.0) for o in opps]  # noqa: E731
        assert strip(a) == strip(b)
        assert meta_a["status_counts"] == meta_b["status_counts"]

    def test_diagnose_pipeline_accepts_demo_inputs(self):
        diag = asyncio.run(diagnose_pipeline(
            adapters=demo_adapters(NOW), odds_fetcher=make_demo_odds_fetcher(NOW),
        ))
        assert diag["stage_1_fetch"]["fd_events"] == len(build_demo_dataset(NOW).events)
        counts = diag["pass_b_classification"]["status_counts"]
        assert counts["BUY"] >= 2 and counts["WATCH"] >= 3 and counts["SKIP"] >= 5


# ---------------------------------------------------------------------------
# (b) refresh_snapshot in demo mode -> zero outbound network
# ---------------------------------------------------------------------------

class TestDemoRefreshIsOffline:
    def test_refresh_snapshot_makes_no_network_calls(self, demo_on, no_network):
        snap = asyncio.run(snap_mod.refresh_snapshot(trigger="manual", scope="all"))
        assert snap.meta["data_source"] == "demo"
        statuses = {o.status for o in snap.opportunities}
        assert statuses == {"BUY", "WATCH"}
        # Default ScanConfig: Kalshi on, Polymarket off -> only Kalshi rows.
        assert snap.meta["platforms_fetched"] == ["kalshi"]
        assert sum(o.status == "BUY" for o in snap.opportunities) >= 2

    def test_scope_does_not_touch_odds_cache(self, demo_on, no_network):
        with patch("services.odds_cache.invalidate_all") as inv_all, \
             patch("services.odds_cache.invalidate") as inv_one:
            asyncio.run(snap_mod.refresh_snapshot(trigger="manual", scope="all"))
            asyncio.run(snap_mod.refresh_snapshot(trigger="manual", scope="tennis"))
        inv_all.assert_not_called()
        inv_one.assert_not_called()

    def test_maker_forced_off_even_if_runtime_enabled(self, demo_on, no_network):
        set_maker_config(dataclasses.replace(get_maker_config(), enabled=True))
        snap = asyncio.run(snap_mod.refresh_snapshot(trigger="manual"))
        assert snap.meta["maker_enabled"] is False
        assert snap.meta["maker_proposals_total"] == 0

    def test_polymarket_toggle_respected(self, demo_on, no_network):
        from services.scan_config import update_scan_config
        update_scan_config({"platforms": {"polymarket": {"enabled": True}}})
        snap = asyncio.run(snap_mod.refresh_snapshot(trigger="manual"))
        assert set(snap.meta["platforms_fetched"]) == {"kalshi", "polymarket"}
        assert any(o.market_type == "totals" and o.status == "BUY" for o in snap.opportunities)


# ---------------------------------------------------------------------------
# (c) API in demo mode
# ---------------------------------------------------------------------------

class TestDemoApi:
    def test_status_flag(self, demo_on):
        body = client.get("/api/opportunities/status").json()
        assert body["demo_mode"] is True
        assert body["has_snapshot"] is False

    def test_refresh_then_snapshot(self, demo_on, no_network):
        resp = client.post("/api/opportunities/refresh", json={"scope": "all"})
        assert resp.status_code == 202
        assert _wait_until_idle()
        status = client.get("/api/opportunities/status").json()
        assert status["last_refresh_error"] is None
        assert status["demo_mode"] is True
        assert status["data_source"] == "demo"
        snap = client.get("/api/opportunities/snapshot")
        assert snap.status_code == 200
        body = snap.json()
        assert body["data_source"] == "demo"
        assert body["total"] > 0
        assert {o["status"] for o in body["opportunities"]} <= {"BUY", "WATCH"}
        assert all(o["event_url"] is None for o in body["opportunities"])
        assert all("DEMO" in o["market_id"] for o in body["opportunities"])

    def test_cold_start_get_runs_demo_pipeline(self, demo_on, no_network):
        resp = client.get("/api/opportunities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_source"] == "demo"
        assert body["status_counts"]["BUY"] >= 2

    @pytest.mark.parametrize("method,path,kwargs", [
        ("post", "/api/monitor/start", {}),
        ("post", "/api/monitor/test", {"json": {"message": "x"}}),
        ("post", "/api/maker/config", {"json": {"enabled": True}}),
        ("get", "/api/opportunities/historical-odds",
         {"params": {"sport_key": "basketball_nba", "event_id": "e", "date": "2099-01-01T00:00:00Z"}}),
        ("get", "/api/odds/tennis", {}),
        ("get", "/api/odds/matches", {}),
    ])
    def test_guarded_endpoints_return_403(self, demo_on, no_network, method, path, kwargs):
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"].startswith("Disabled in demo mode: ")

    def test_monitor_not_started_and_maker_unchanged(self, demo_on, no_network, monkeypatch):
        from routers import monitor
        # Isolate the router's lazy singletons (restored by monkeypatch).
        monkeypatch.setattr(monitor, "_scheduler", None)
        monkeypatch.setattr(monitor, "_config", None)
        client.post("/api/monitor/start")
        # The guard runs before the handler: no scheduler/config was created.
        assert monitor._scheduler is None
        assert monitor._config is None
        client.post("/api/maker/config", json={"enabled": True})
        assert get_maker_config().enabled is False

    def test_maker_config_non_enabling_update_allowed(self, demo_on):
        resp = client.post("/api/maker/config", json={"enabled": False, "max_orders_per_hour": 5})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_history_is_empty_without_network(self, demo_on, no_network):
        with patch("routers.opportunities.fetch_price_history", side_effect=_boom):
            resp = client.get(
                "/api/opportunities/history",
                params={"platform": "kalshi", "market_id": "KXNBAGAME-DEMO01HCHGLL"},
            )
        assert resp.status_code == 200
        assert resp.json()["points"] == [] and resp.json()["count"] == 0

    def test_debug_works_offline(self, demo_on, no_network):
        resp = client.get("/api/opportunities/debug")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data_source"] == "demo"
        reasons = {
            r for e in body["pass_b_classification"]["evaluated"] for r in e["reject_reasons"]
        }
        assert {"LINE_MISMATCH", "NO_BOOKMAKER_DATA", "PRICE_PROB_DIVERGENCE",
                "DATE_TOO_FAR", "NO_EDGE"} <= reasons

    def test_data_status_and_scan_config_offline(self, demo_on, no_network):
        client.post("/api/opportunities/refresh")
        assert _wait_until_idle()
        assert client.get("/api/data-status").status_code == 200
        assert client.get("/api/scan/config").status_code == 200


# ---------------------------------------------------------------------------
# (d) Demo mode off -> default path unchanged
# ---------------------------------------------------------------------------

class TestDemoOffUnchanged:
    def test_fetch_opportunities_default_uses_real_fetch_odds(self):
        fake = AsyncMock(return_value=([], {"sports_fetched": []}))
        with patch("services.opportunities.fetch_odds", fake):
            opps, meta = asyncio.run(fetch_opportunities(adapters=[], maker_cfg=MakerConfig()))
        fake.assert_awaited_once_with(bookmaker="fanduel", max_sports=25)
        assert opps == []
        assert "data_source" not in meta  # set by refresh_snapshot, not the pipeline

    def test_diagnose_default_uses_real_fetch_odds(self):
        fake = AsyncMock(return_value=([], {"sports_fetched": []}))
        with patch("services.opportunities.fetch_odds", fake):
            asyncio.run(diagnose_pipeline(adapters=[]))
        fake.assert_awaited_once_with(bookmaker="fanduel", max_sports=25)

    def test_refresh_snapshot_live_path(self):
        calls = []

        async def _fake_fetch(cfg=None, **kwargs):
            calls.append(kwargs)
            return [], {"status_counts": {}}

        with patch("services.opportunities.fetch_opportunities", side_effect=_fake_fetch), \
             patch("services.odds_cache.invalidate_all") as inv_all:
            snap = asyncio.run(snap_mod.refresh_snapshot(trigger="manual", scope="all"))
        assert calls == [{}]  # called with cfg only -- no demo injection
        inv_all.assert_called_once()
        assert snap.meta["data_source"] == "live"

    def test_status_reports_demo_off(self):
        assert client.get("/api/opportunities/status").json()["demo_mode"] is False

    def test_guards_are_noops(self):
        fake_hist = AsyncMock(return_value=[])
        with patch("routers.opportunities.fetch_price_history", fake_hist):
            resp = client.get(
                "/api/opportunities/history",
                params={"platform": "kalshi", "market_id": "X"},
            )
        assert resp.status_code == 200
        fake_hist.assert_awaited_once()
        with patch("routers.opportunities.fetch_historical_odds", AsyncMock(return_value=None)):
            resp = client.get(
                "/api/opportunities/historical-odds",
                params={"sport_key": "k", "event_id": "e", "date": "2099-01-01T00:00:00Z"},
            )
        assert resp.status_code == 200
