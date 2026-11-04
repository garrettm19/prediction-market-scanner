"""
API keys must not appear in logs or API error responses (audit secu0/secu1).
"""
import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from services.redaction import (
    REDACTED,
    SecretRedactionFilter,
    configure_log_redaction,
    redact_secrets,
)
from services import snapshot as _snap_mod
from services.snapshot import clear_snapshot

SENTINEL = "SENTINELKEY"


@pytest.fixture(autouse=True)
def _clean():
    clear_snapshot()
    yield
    clear_snapshot()


class TestRedactSecrets:
    @pytest.mark.parametrize("text", [
        f"https://api.the-odds-api.com/v4/sports/?apiKey={SENTINEL}",
        f"https://api.the-odds-api.com/v4/sports/x/odds?regions=us&apiKey={SENTINEL}&markets=h2h",
        f"https://x/?APIKEY={SENTINEL}",
        f"https://x/?apikey={SENTINEL}",
        f"https://x/?api_key={SENTINEL}",
        f"https://api.pushover.net/1/messages.json?token={SENTINEL}&user={SENTINEL}",
        f"Client error '401 Unauthorized' for url 'https://x/v4/sports/?apiKey={SENTINEL}'",
        f"GET https://x/?apiKey={SENTINEL} \"HTTP/1.1 200 OK\"",
    ])
    def test_masks_credential_params(self, text):
        out = redact_secrets(text)
        assert SENTINEL not in out
        assert REDACTED in out

    def test_preserves_non_secret_params_and_structure(self):
        url = f"https://x/v4/sports/baseball_mlb/odds?regions=us&apiKey={SENTINEL}&markets=h2h"
        assert redact_secrets(url) == (
            "https://x/v4/sports/baseball_mlb/odds?regions=us&apiKey=REDACTED&markets=h2h"
        )

    def test_does_not_mask_lookalike_names(self):
        s = "sport_key=baseball_mlb monkey=1 username_x"
        assert redact_secrets(s) == s

    def test_accepts_exceptions_and_plain_text(self):
        assert redact_secrets(ValueError("no secrets here")) == "no secrets here"
        assert redact_secrets("") == ""


class TestLogRedaction:
    def test_httpx_request_log_is_redacted(self, caplog):
        """Same setup function main.py calls; httpx INFO request log keeps the
        URL but masks the key."""
        import main  # noqa: F401  (ensures main's configuration ran)
        configure_log_redaction()
        assert any(isinstance(f, SecretRedactionFilter)
                   for f in logging.getLogger("httpx").filters)

        transport = httpx.MockTransport(lambda req: httpx.Response(401, json={}))

        async def _go():
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get(
                    "https://api.the-odds-api.com/v4/sports/",
                    params={"apiKey": SENTINEL},
                )

        with caplog.at_level(logging.INFO, logger="httpx"):
            asyncio.run(_go())

        assert "HTTP Request" in caplog.text
        assert "api.the-odds-api.com" in caplog.text
        assert SENTINEL not in caplog.text
        assert "apiKey=REDACTED" in caplog.text

    def test_configure_is_idempotent(self):
        configure_log_redaction()
        configure_log_redaction()
        n = sum(isinstance(f, SecretRedactionFilter)
                for f in logging.getLogger("httpx").filters)
        assert n == 1

    def test_filter_rewrites_formatted_args(self):
        rec = logging.LogRecord(
            "services.x", logging.ERROR, __file__, 1,
            "failed: %s", (f"url ?apiKey={SENTINEL}",), None,
        )
        assert SecretRedactionFilter().filter(rec) is True
        assert SENTINEL not in rec.getMessage()

    def test_odds_provider_error_log_is_redacted(self, caplog, monkeypatch):
        """A discovery failure whose exception text contains the URL is logged redacted."""
        from services.odds_provider import discover_sport_markets
        configure_log_redaction()

        def _handler(request):
            assert SENTINEL in str(request.url)   # key really is in the URL
            return httpx.Response(401)

        async def _go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
                return await discover_sport_markets("baseball_mlb", SENTINEL, client, "fanduel")

        with caplog.at_level(logging.INFO):
            result = asyncio.run(_go())
        assert result is None
        assert "discovery: event fetch failed" in caplog.text
        assert SENTINEL not in caplog.text


class TestRefreshErrorRedaction:
    def test_last_refresh_error_is_redacted(self):
        req = httpx.Request("GET", f"https://api.the-odds-api.com/v4/sports/?apiKey={SENTINEL}")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{req.url}'", request=req, response=resp,
        )

        async def _mock_fetch(cfg=None):
            raise exc

        async def _go():
            _snap_mod._refresh_lock = asyncio.Lock()
            await _snap_mod.refresh_snapshot(trigger="manual")

        with patch("services.opportunities.fetch_opportunities", side_effect=_mock_fetch):
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(_go())

        err = _snap_mod.get_refresh_state()["last_refresh_error"]
        assert err is not None
        assert err.startswith("HTTPStatusError: ")
        assert "401" in err
        assert SENTINEL not in err

    def test_debug_endpoint_error_detail_is_redacted(self):
        from main import app
        client = TestClient(app)

        async def _boom(cfg=None):
            raise RuntimeError(f"failed for url https://x/?apiKey={SENTINEL}")

        with patch("routers.opportunities.diagnose_pipeline", side_effect=_boom):
            resp = client.get("/api/opportunities/debug")
        assert resp.status_code == 500
        assert SENTINEL not in resp.text
        assert "apiKey=REDACTED" in resp.text

    def test_cold_start_value_error_detail_is_redacted(self):
        from main import app
        client = TestClient(app)

        async def _fresh_lock_refresh(*a, **kw):
            raise ValueError(f"bad config ?apiKey={SENTINEL}")

        with patch("routers.opportunities.refresh_snapshot", side_effect=_fresh_lock_refresh):
            resp = client.get("/api/opportunities")
        assert resp.status_code == 503
        assert SENTINEL not in resp.text
