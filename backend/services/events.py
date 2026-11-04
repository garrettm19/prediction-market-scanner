"""
Gamma API events flow — active, open, sports events only.
Primary data source replacing the broad /markets fetch.
"""
import httpx
import logging
import os

from services.redaction import redact_secrets

logger = logging.getLogger(__name__)

GAMMA_BASE = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com")


async def fetch_active_events(
    limit: int = 100,
    offset: int = 0,
    tag_slug: str | None = None,
) -> list[dict]:
    """Fetch active, non-closed events from Gamma API, optionally filtered by tag slug."""
    params: dict[str, str | int] = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{GAMMA_BASE}/events", params=params)
        response.raise_for_status()
        data = response.json()
    # Gamma may return a plain list or a paginated envelope
    return data if isinstance(data, list) else data.get("data", [])


def extract_markets(events: list[dict]) -> list[dict]:
    """
    Flatten the markets nested inside each event.
    Inherits event-level category and slug if the market has none.
    event_slug is the canonical Polymarket URL slug (polymarket.com/event/{event_slug}).
    """
    markets = []
    for event in events:
        event_category = event.get("category")
        event_slug = event.get("slug")
        event_name = event.get("title") or event.get("name")
        for market in event.get("markets") or []:
            if not market.get("category") and event_category:
                market = {**market, "category": event_category}
            if event_slug:
                market = {**market, "event_slug": event_slug}
            if event_name:
                market = {**market, "event_name": event_name}
            markets.append(market)
    return markets


async def _fetch_paginated(tag_slug: str, limit: int = 500) -> list[dict]:
    """Paginate through all events for a single tag."""
    all_events: list[dict] = []
    offset = 0
    page_size = 100

    while len(all_events) < limit:
        batch = await fetch_active_events(
            limit=page_size, offset=offset, tag_slug=tag_slug,
        )
        if not batch:
            break
        all_events.extend(batch)
        offset += len(batch)
        if len(batch) < page_size:
            break
    return all_events


async def fetch_tennis_markets(limit: int = 500, offset: int = 0) -> list[dict]:
    """Backward-compatible: fetch tennis markets only."""
    return await fetch_markets_by_tags(["tennis"], limit=limit)


async def fetch_markets_by_tags(tags: list[str], limit: int = 500) -> list[dict]:
    """
    Fetch active markets from Polymarket for multiple sport tags.
    Deduplicates by market id across tags.

    A failure fetching one tag (HTTP error, timeout, bad payload) is
    isolated: it is logged and that tag contributes no markets, while the
    other tags' results are still returned.  This fails closed (fewer
    markets, never fabricated ones) and keeps one flaky Polymarket tag from
    aborting the whole scan, including the Kalshi and Odds fetches.
    No retries are added.
    """
    import asyncio

    tag_results = await asyncio.gather(
        *[_fetch_paginated(tag, limit=limit) for tag in tags],
        return_exceptions=True,
    )

    # Deduplicate events by id
    seen_event_ids: set[str] = set()
    unique_events: list[dict] = []
    for tag, events in zip(tags, tag_results):
        if isinstance(events, BaseException):
            if not isinstance(events, Exception):
                raise events  # e.g. CancelledError — do not swallow
            logger.warning(
                "Polymarket tag %r fetch failed, skipping tag: %s: %s",
                tag, type(events).__name__, redact_secrets(events)[:200],
            )
            continue
        for ev in events:
            eid = ev.get("id") or ev.get("slug") or id(ev)
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                unique_events.append(ev)

    return extract_markets(unique_events)
