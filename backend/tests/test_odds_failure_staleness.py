"""
Fail-closed handling of per-sport Odds API refetch failures.

Covers (audit exte0 / snap0 / exte1):
  - an expired cache entry whose refetch fails (HTTP 429 / other error) is
    evicted, so expired FanDuel odds never reach Pass A as pricing truth
  - the failure is reported in meta["sports_failed"] with a short,
    secret-free error and the stale age
  - a still-fresh entry for another key is untouched
  - disabled-sport cached data behaviour is unchanged
  - refresh_snapshot keeps sports_failed in snapshot meta and
    GET /api/opportunities/status exposes odds_failures / degraded
"""
import asyncio
import time

import httpx
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

import services.odds_provider as odds_provider_module
from services import odds_cache
from services.odds_cache import store_cached, clear_cache, get_fetched_at
from services.odds_provider import fetch_odds, TennisOddsEvent
from services.scan_config import reset_scan_config, update_scan_config
from services import snapshot as _snap_mod
from services.snapshot import clear_snapshot


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    reset_scan_config()
    clear_cache()
    clear_snapshot()
    odds_provider_module._discovery_cache = None
    odds_provider_module._discovery_cache_ts = 0.0
    yield
    reset_scan_config()
    clear_cache()
    clear_snapshot()
    odds_provider_module._discovery_cache = None
    odds_provider_module._discovery_cache_ts = 0.0


def _make_event(event_id: str, sport_key: str) -> TennisOddsEvent:
    return TennisOddsEvent(
        event_id=event_id,
        sport_key=sport_key,
        tournament="Test",
        home_player="Team A",
        away_player="Team B",
        home_player_norm="team a",
        away_player_norm="team b",
        commence_time="2026-06-01T12:00:00Z",
    )


_KEY_MAP = {"baseball_kbo": "baseball_kbo", "baseball_mlb": "baseball_mlb"}


async def _mock_resolve(api_key: str) -> dict[str, str]:
    return dict(_KEY_MAP)


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request(
        "GET", "https://api.the-odds-api.com/v4/sports/x/odds?apiKey=SENTINELKEY",
    )
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(
        f"error {status} for url {req.url}", request=req, response=resp,
    )


def _age_entry(sport_key: str, seconds: float) -> None:
    odds_cache._cache[sport_key].fetched_at = time.time() - seconds


def _run_fetch(fetch_side_effect):
    with patch("services.odds_provider._resolve_all_sport_keys", side_effect=_mock_resolve):
        with patch("services.odds_provider._fetch_sport_odds", side_effect=fetch_side_effect):
            return asyncio.run(fetch_odds())


class TestExpiredEntryEvictedOnFailedRefetch:
    def test_http_429_evicts_expired_entry_and_reports_failure(self):
        store_cached("baseball_mlb", [_make_event("STALE_MLB", "baseball_mlb")])
        _age_entry("baseball_mlb", 6 * 3600)   # far past default 900s TTL
        store_cached("baseball_kbo", [_make_event("FRESH_KBO", "baseball_kbo")])

        async def _fetch(key, *a, **kw):
            raise _http_error(429)

        events, meta = _run_fetch(_fetch)

        ids = [e.event_id for e in events]
        assert "STALE_MLB" not in ids
        assert "FRESH_KBO" in ids                       # fresh entry untouched
        assert get_fetched_at("baseball_mlb") == 0.0     # evicted
        assert get_fetched_at("baseball_kbo") > 0.0
        assert meta["sports_cached"] == ["baseball_kbo"]
        assert "baseball_mlb" not in meta["sports_fetched"]

        failed = meta["sports_failed"]
        assert len(failed) == 1
        rec = failed[0]
        assert rec["sport_key"] == "baseball_mlb"
        assert rec["error"] == "HTTP 429"
        assert rec["stale_age_seconds"] is not None
        assert rec["stale_age_seconds"] >= 6 * 3600 - 5
        assert "SENTINELKEY" not in repr(meta)

    def test_generic_exception_evicts_and_reports_type_only(self):
        store_cached("baseball_mlb", [_make_event("STALE_MLB", "baseball_mlb")])
        _age_entry("baseball_mlb", 2000)
        store_cached("baseball_kbo", [_make_event("FRESH_KBO", "baseball_kbo")])

        async def _fetch(key, *a, **kw):
            raise httpx.ConnectTimeout("timed out ?apiKey=SENTINELKEY")

        events, meta = _run_fetch(_fetch)

        assert "STALE_MLB" not in [e.event_id for e in events]
        assert "FRESH_KBO" in [e.event_id for e in events]
        assert len(meta["sports_failed"]) == 1
        rec = meta["sports_failed"][0]
        assert rec["sport_key"] == "baseball_mlb"
        assert rec["error"] == "ConnectTimeout"
        assert rec["stale_age_seconds"] >= 2000 - 5
        assert "SENTINELKEY" not in repr(meta)

    def test_failure_without_prior_cache_entry_reports_none_age(self):
        async def _fetch(key, *a, **kw):
            if key == "baseball_mlb":
                raise _http_error(503)
            return [], {"remaining": "100", "used": "1"}

        events, meta = _run_fetch(_fetch)

        assert events == []
        assert meta["sports_fetched"] == ["baseball_kbo"]
        assert meta["sports_failed"] == [
            {"sport_key": "baseball_mlb", "error": "HTTP 503", "stale_age_seconds": None},
        ]

    def test_successful_fetch_has_empty_sports_failed(self):
        async def _fetch(key, *a, **kw):
            return [], {}

        _, meta = _run_fetch(_fetch)
        assert meta["sports_failed"] == []

    def test_disabled_sport_cached_data_still_available(self):
        """Unchanged: disabled sports keep cached data (no refetch, no eviction)."""
        store_cached("baseball_kbo", [_make_event("cached_kbo", "baseball_kbo")])
        _age_entry("baseball_kbo", 6 * 3600)
        update_scan_config({"sports": {"baseball_kbo": {"enabled": False}}})

        async def _fetch(key, *a, **kw):
            raise _http_error(429)

        events, meta = _run_fetch(_fetch)
        assert "cached_kbo" in [e.event_id for e in events]
        assert "baseball_kbo" in meta["sports_skipped"]
        assert [f["sport_key"] for f in meta["sports_failed"]] == ["baseball_mlb"]


class TestSnapshotSurfacesOddsFailures:
    @staticmethod
    def _failed():
        return [{"sport_key": "baseball_mlb", "error": "HTTP 429", "stale_age_seconds": 1200.0}]

    @staticmethod
    def _refresh(meta):
        async def _mock_fetch(cfg=None):
            return [], meta

        async def _go():
            # Fresh lock bound to this test's event loop.
            _snap_mod._refresh_lock = asyncio.Lock()
            return await _snap_mod.refresh_snapshot(trigger="manual")

        with patch("services.opportunities.fetch_opportunities", side_effect=_mock_fetch):
            return asyncio.run(_go())

    def test_refresh_snapshot_keeps_sports_failed_in_meta(self):
        snap = self._refresh(
            {"status_counts": {}, "platforms_fetched": ["kalshi"], "sports_failed": self._failed()},
        )
        assert snap.meta["sports_failed"] == self._failed()

    def test_refresh_snapshot_defaults_sports_failed_to_empty(self):
        snap = self._refresh({"status_counts": {}})
        assert snap.meta["sports_failed"] == []

    def test_status_endpoint_exposes_degraded(self):
        from main import app
        client = TestClient(app)

        st = client.get("/api/opportunities/status").json()
        assert st["has_snapshot"] is False
        assert st["degraded"] is False
        assert st["odds_failures"] == []

        _snap_mod.store_snapshot(
            [],
            {"status_counts": {}, "platforms_fetched": ["kalshi"], "sports_failed": self._failed()},
            trigger="manual",
        )
        st = client.get("/api/opportunities/status").json()
        assert st["degraded"] is True
        assert st["odds_failures"] == self._failed()
        for k in ("has_snapshot", "updated_at", "last_refresh_error", "platforms_fetched",
                  "status_counts", "opportunity_count"):
            assert k in st

        _snap_mod.store_snapshot([], {"status_counts": {}, "sports_failed": []}, trigger="manual")
        st = client.get("/api/opportunities/status").json()
        assert st["degraded"] is False
        assert st["odds_failures"] == []
