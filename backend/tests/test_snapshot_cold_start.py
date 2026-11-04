"""
Concurrent cold-start GET /api/opportunities must not run the full pipeline
back to back (audit snap1).

The cold-start path calls refresh_snapshot(only_if_missing=True): after
acquiring the refresh lock, a waiter that finds a snapshot stored while it
was waiting returns it instead of re-running the pipeline.
"""
import asyncio

import pytest
from fastapi import Response
from unittest.mock import patch

from routers.opportunities import get_opportunities
from services import snapshot as _snap_mod
from services.snapshot import clear_snapshot


@pytest.fixture(autouse=True)
def _clean():
    clear_snapshot()
    yield
    clear_snapshot()


def _counting_fetch():
    calls = {"n": 0}

    async def _fetch(cfg=None):
        calls["n"] += 1
        await asyncio.sleep(0.05)   # hold the lock so the others queue behind it
        return [], {"status_counts": {"BUY": 0, "WATCH": 0, "SKIP": 0}, "platforms_fetched": ["kalshi"]}

    return calls, _fetch


def _cold_get():
    return get_opportunities(Response(), platform=None, include_watch=True)


class TestConcurrentColdStart:
    def test_concurrent_cold_start_runs_pipeline_once(self):
        calls, fetch = _counting_fetch()

        async def _go():
            _snap_mod._refresh_lock = asyncio.Lock()
            return await asyncio.gather(*[_cold_get() for _ in range(5)])

        with patch("services.opportunities.fetch_opportunities", side_effect=fetch):
            results = asyncio.run(_go())

        assert calls["n"] == 1
        assert len(results) == 5
        assert all(r.total == 0 for r in results)
        assert _snap_mod.get_snapshot() is not None
        assert _snap_mod.get_snapshot().trigger == "cold_start"

    def test_cold_start_behind_background_refresh_runs_pipeline_once(self):
        calls, fetch = _counting_fetch()

        async def _go():
            _snap_mod._refresh_lock = asyncio.Lock()
            _snap_mod._start_lock = asyncio.Lock()
            status = await _snap_mod.start_background_refresh(trigger="manual")
            assert status["started"] is True
            await asyncio.sleep(0)   # let the background task take the lock
            resp = await _cold_get()
            await _snap_mod._background_task
            return resp

        with patch("services.opportunities.fetch_opportunities", side_effect=fetch):
            asyncio.run(_go())

        assert calls["n"] == 1
        assert _snap_mod.get_snapshot().trigger == "manual"

    def test_default_refresh_still_runs_when_snapshot_exists(self):
        """Non-cold-start callers (scheduler, manual refresh) are unaffected."""
        calls, fetch = _counting_fetch()

        async def _go():
            _snap_mod._refresh_lock = asyncio.Lock()
            await _snap_mod.refresh_snapshot(trigger="manual")
            await _snap_mod.refresh_snapshot(trigger="scheduled")

        with patch("services.opportunities.fetch_opportunities", side_effect=fetch):
            asyncio.run(_go())

        assert calls["n"] == 2
        assert _snap_mod.get_snapshot().trigger == "scheduled"
