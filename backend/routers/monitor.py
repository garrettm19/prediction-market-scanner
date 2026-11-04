"""
Monitoring and alert API routes.

Config changes never start/stop monitoring.
Only POST /monitor/start and /monitor/stop control the loop.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.demo.guards import block_in_demo
from services.monitor.config import MonitorConfig, AlertPreset, apply_preset
from services.monitor.scheduler import MonitorScheduler

router = APIRouter()
logger = logging.getLogger(__name__)

_scheduler: MonitorScheduler | None = None
_config: MonitorConfig | None = None


def _get_config() -> MonitorConfig:
    global _config
    if _config is None:
        _config = MonitorConfig(
            pushover_user_key=os.getenv("PUSHOVER_USER_KEY", ""),
            pushover_api_token=os.getenv("PUSHOVER_API_TOKEN", ""),
        )
    return _config


def _get_scheduler() -> MonitorScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MonitorScheduler(monitor_config=_get_config())
    return _scheduler


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MonitorConfigRequest(BaseModel):
    preset: str | None = None
    min_ev: float | None = None
    platforms: list[str] | None = None
    market_types: list[str] | None = None
    cooldown_minutes: int | None = None
    max_alerts_per_hour: int | None = None
    refresh_interval_minutes: int | None = None
    dry_run: bool | None = None
    pushover_user_key: str | None = None
    pushover_api_token: str | None = None


class MonitorConfigResponse(BaseModel):
    enabled: bool
    preset: str
    min_ev: float
    platforms: list[str]
    market_types: list[str]
    cooldown_minutes: int
    max_alerts_per_hour: int
    refresh_interval_minutes: int
    dry_run: bool
    has_pushover_credentials: bool


class MonitorStatusResponse(BaseModel):
    running: bool
    enabled: bool
    last_cycle_ts: float
    total_cycles: int
    total_alerts_sent: int
    odds_poller: dict
    polymarket_ws: dict
    kalshi_ws: dict
    last_cycle_result: dict | None


class TestNotificationRequest(BaseModel):
    message: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/monitor/config", response_model=MonitorConfigResponse)
async def get_monitor_config() -> MonitorConfigResponse:
    cfg = _get_config()
    return MonitorConfigResponse(
        enabled=cfg.enabled,
        preset=cfg.preset.value,
        min_ev=cfg.min_ev,
        platforms=cfg.platforms,
        market_types=cfg.market_types,
        cooldown_minutes=cfg.cooldown_minutes,
        max_alerts_per_hour=cfg.max_alerts_per_hour,
        refresh_interval_minutes=cfg.refresh_interval_minutes,
        dry_run=cfg.dry_run,
        has_pushover_credentials=bool(cfg.pushover_user_key and cfg.pushover_api_token),
    )


@router.post("/monitor/config", response_model=MonitorConfigResponse)
async def update_monitor_config(req: MonitorConfigRequest) -> MonitorConfigResponse:
    """Update settings. Does NOT start or stop monitoring."""
    global _config
    cfg = _get_config()

    if req.preset is not None:
        try:
            preset = AlertPreset(req.preset)
            cfg = apply_preset(cfg, preset)
        except ValueError:
            raise HTTPException(400, f"Invalid preset: {req.preset}")

    for field_name in [
        "min_ev", "platforms", "market_types", "cooldown_minutes",
        "max_alerts_per_hour", "refresh_interval_minutes",
        "dry_run", "pushover_user_key", "pushover_api_token",
    ]:
        val = getattr(req, field_name, None)
        if val is not None:
            setattr(cfg, field_name, val)
            if req.preset is None:
                cfg.preset = AlertPreset.CUSTOM

    _config = cfg

    # If scheduler exists, update its config (but don't start/stop it)
    if _scheduler is not None:
        _scheduler.update_config(cfg)

    return await get_monitor_config()


@router.post(
    "/monitor/start",
    dependencies=[Depends(block_in_demo("the monitor polls live markets and sends alerts"))],
)
async def start_monitoring() -> dict:
    """Start the monitoring loop. Stops any existing loop first."""
    cfg = _get_config()
    if not cfg.pushover_user_key or not cfg.pushover_api_token:
        if not cfg.dry_run:
            raise HTTPException(400, "Configure Pushover credentials first, or enable dry run")

    cfg.enabled = True
    scheduler = _get_scheduler()
    scheduler.update_config(cfg)

    # Stop existing loop if running
    if scheduler.status().running:
        await scheduler.stop()

    await scheduler.start()
    return {"status": "started", "preset": cfg.preset.value}


@router.post("/monitor/stop")
async def stop_monitoring() -> dict:
    """Stop the monitoring loop."""
    cfg = _get_config()
    cfg.enabled = False
    scheduler = _get_scheduler()
    scheduler.update_config(cfg)
    await scheduler.stop()
    return {"status": "stopped"}


@router.get("/monitor/status", response_model=MonitorStatusResponse)
async def get_monitor_status() -> MonitorStatusResponse:
    scheduler = _get_scheduler()
    s = scheduler.status()
    return MonitorStatusResponse(
        running=s.running,
        enabled=s.enabled,
        last_cycle_ts=s.last_cycle_ts,
        total_cycles=s.total_cycles,
        total_alerts_sent=s.total_alerts_sent,
        odds_poller=s.odds_poller,
        polymarket_ws=s.polymarket_ws,
        kalshi_ws=s.kalshi_ws,
        last_cycle_result=s.last_cycle_result,
    )


@router.get("/monitor/history")
async def get_alert_history() -> list[dict]:
    scheduler = _get_scheduler()
    records = scheduler.state.recent_alerts(limit=50)
    return [
        {
            "key": r.key,
            "platform": r.platform,
            "event": r.event,
            "side": r.side,
            "last_alert_edge": r.last_alert_edge,
            "current_edge": r.current_edge,
            "current_status": r.current_status,
            "is_active": r.is_active,
            "alert_count": r.alert_count,
            "last_alert_ts": r.last_alert_ts,
        }
        for r in records
    ]


@router.post(
    "/monitor/test",
    dependencies=[Depends(block_in_demo("test notifications send a real Pushover message"))],
)
async def test_notification(req: TestNotificationRequest | None = None) -> dict:
    """Send a real test notification — always bypasses dry run."""
    from services.monitor.notifier import AlertPayload, PushoverNotifier

    cfg = _get_config()
    if not cfg.pushover_user_key or not cfg.pushover_api_token:
        raise HTTPException(400, "Pushover credentials not configured")

    notifier = PushoverNotifier(
        user_key=cfg.pushover_user_key,
        api_token=cfg.pushover_api_token,
    )
    payload = AlertPayload(
        title="MasG EV Tool \u2014 Test",
        body=req.message if req and req.message else "Push notifications are working.",
        priority=0,
    )
    success = await notifier.send(payload)
    return {"sent": success}
