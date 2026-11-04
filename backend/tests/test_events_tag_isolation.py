"""
One Polymarket tag/page failure must not abort the whole scan (audit exte5).

fetch_markets_by_tags isolates per-tag failures: the failed tag contributes
no markets (fail closed), the other tags' markets are still returned, and
no retries are made.
"""
import asyncio
import logging

import httpx
import pytest
from unittest.mock import patch

from services.events import fetch_markets_by_tags


def _event(eid: str, market_id: str) -> dict:
    return {"id": eid, "slug": f"slug-{eid}", "title": f"Event {eid}",
            "markets": [{"id": market_id, "question": "Q?"}]}


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://gamma-api.polymarket.com/events?tag_slug=nba&offset=200")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


class TestPerTagFailureIsolation:
    def test_one_tag_failure_keeps_other_tags(self, caplog):
        calls: list[tuple[str, int]] = []

        async def _fetch(limit=100, offset=0, tag_slug=None):
            calls.append((tag_slug, offset))
            if tag_slug == "nba":
                raise _http_error(502)
            if offset:
                return []
            return [_event(f"{tag_slug}-1", f"m-{tag_slug}")]

        with patch("services.events.fetch_active_events", side_effect=_fetch):
            with caplog.at_level(logging.WARNING, logger="services.events"):
                markets = asyncio.run(fetch_markets_by_tags(["mlb", "nba", "nhl"]))

        ids = sorted(m["id"] for m in markets)
        assert ids == ["m-mlb", "m-nhl"]
        assert "nba" in caplog.text
        # No retries: the failing tag was requested exactly once.
        assert [c for c in calls if c[0] == "nba"] == [("nba", 0)]

    def test_mid_pagination_failure_drops_only_that_tag(self):
        async def _fetch(limit=100, offset=0, tag_slug=None):
            if tag_slug == "nba":
                if offset == 0:
                    return [_event(f"nba-{i}", f"m-nba-{i}") for i in range(100)]
                raise httpx.ReadTimeout("timeout")
            return [_event(f"{tag_slug}-1", f"m-{tag_slug}")]

        with patch("services.events.fetch_active_events", side_effect=_fetch):
            markets = asyncio.run(fetch_markets_by_tags(["nba", "mlb"]))

        # The partially paginated tag is dropped entirely (no half-results).
        assert [m["id"] for m in markets] == ["m-mlb"]

    def test_all_tags_fail_returns_empty(self):
        async def _fetch(limit=100, offset=0, tag_slug=None):
            raise _http_error(429)

        with patch("services.events.fetch_active_events", side_effect=_fetch):
            assert asyncio.run(fetch_markets_by_tags(["nba", "mlb"])) == []

    def test_cancellation_is_not_swallowed(self):
        async def _fetch(limit=100, offset=0, tag_slug=None):
            raise asyncio.CancelledError()

        with patch("services.events.fetch_active_events", side_effect=_fetch):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(fetch_markets_by_tags(["nba"]))
