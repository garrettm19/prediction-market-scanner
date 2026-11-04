"""
Cross-site request guard (services/origin_guard.py).

CORS only prevents a browser from reading cross-origin responses; a page on
another site can still *send* a bodyless POST to the local API.  These tests
pin that state-changing requests from foreign browser origins are rejected
before reaching any handler, while same-origin dashboard calls, non-browser
clients (no Origin header) and read-only requests keep working.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_monitor_router_state(monkeypatch):
    """Calls that reach /api/monitor/* lazily create the router's module-level
    scheduler/config singletons; restore them so no state leaks into other
    test modules."""
    import routers.monitor as monitor_router
    monkeypatch.setattr(monitor_router, "_scheduler", None)
    monkeypatch.setattr(monitor_router, "_config", None)
    yield


class TestForeignOriginRejected:
    @pytest.mark.parametrize("path", [
        "/api/monitor/test",
        "/api/monitor/stop",
        "/api/monitor/start",
        "/api/opportunities/refresh",
    ])
    def test_bodyless_cross_site_post_is_403(self, path):
        # The handler must never run: patch the notifier/refresh so a
        # regression would be visible as a call rather than a real side effect.
        with patch(
            "services.monitor.notifier.PushoverNotifier.send",
            new=AsyncMock(side_effect=AssertionError("notifier reached")),
        ), patch(
            "services.snapshot.start_background_refresh",
            new=AsyncMock(side_effect=AssertionError("refresh reached")),
        ):
            res = client.post(path, headers={"Origin": "https://evil.example"})
        assert res.status_code == 403
        assert "evil.example" in res.json()["detail"]

    def test_text_plain_cross_site_post_is_403(self):
        res = client.post(
            "/api/scan/config",
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
            content='{"platforms": {"polymarket": {"enabled": true}}}',
        )
        assert res.status_code == 403

    def test_null_origin_is_403(self):
        res = client.post("/api/monitor/stop", headers={"Origin": "null"})
        assert res.status_code == 403

    def test_lookalike_origin_is_403(self):
        res = client.post(
            "/api/monitor/stop",
            headers={"Origin": "http://localhost:3000.evil.example"},
        )
        assert res.status_code == 403


class TestAllowedRequestsPass:
    def test_dashboard_origin_post_reaches_handler(self):
        res = client.post(
            "/api/monitor/stop", headers={"Origin": "http://localhost:3000"}
        )
        assert res.status_code == 200

    def test_trailing_slash_origin_allowed(self):
        res = client.post(
            "/api/monitor/stop", headers={"Origin": "http://127.0.0.1:3000/"}
        )
        assert res.status_code == 200

    def test_no_origin_header_allowed(self):
        # curl / scripts / server-side rendering send no Origin header.
        res = client.post("/api/monitor/stop")
        assert res.status_code == 200

    def test_cross_site_get_not_blocked_by_guard(self):
        # Safe methods are left to CORS; the guard only covers unsafe methods.
        res = client.get(
            "/api/monitor/config", headers={"Origin": "https://evil.example"}
        )
        assert res.status_code == 200

    def test_cors_preflight_still_answered(self):
        res = client.options(
            "/api/monitor/config",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert res.status_code == 200
        assert res.headers["access-control-allow-origin"] == "http://localhost:3000"
