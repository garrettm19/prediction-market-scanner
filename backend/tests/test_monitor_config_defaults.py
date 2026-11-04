"""
moni5: monitor alerts must default to dry run.

README / runbook / CLAUDE.md document dry-run as the default and require
live alerts to stay off until an organic dry-run BUY payload is observed.
MonitorConfig is in-memory only, so the default is what a fresh backend
process (e.g. after a restart) uses.  These tests never start the monitor
loop, never call /api/monitor/test, and use fake credentials only.
"""
import pytest

from routers import monitor as monitor_router
from services.monitor.config import AlertPreset, MonitorConfig, apply_preset
from services.monitor.notifier import DryRunNotifier, PushoverNotifier
from services.monitor.scheduler import MonitorScheduler


def test_monitor_config_defaults_to_dry_run():
    assert MonitorConfig().dry_run is True


@pytest.mark.parametrize("preset", list(AlertPreset))
def test_presets_do_not_disable_dry_run(preset):
    cfg = apply_preset(MonitorConfig(), preset)
    assert cfg.dry_run is True


def test_default_scheduler_uses_dry_run_notifier():
    s = MonitorScheduler(monitor_config=MonitorConfig())
    assert isinstance(s.notifier, DryRunNotifier)
    assert not isinstance(s.notifier, PushoverNotifier)


def test_fresh_router_config_with_env_creds_is_dry_run(monkeypatch):
    """Even with Pushover creds in the environment, a fresh process is dry run."""
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake-user-key-for-test")
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "fake-api-token-for-test")
    monkeypatch.setattr(monitor_router, "_config", None)
    monkeypatch.setattr(monitor_router, "_scheduler", None)

    cfg = monitor_router._get_config()
    assert cfg.pushover_user_key == "fake-user-key-for-test"
    assert cfg.dry_run is True

    scheduler = monitor_router._get_scheduler()
    assert isinstance(scheduler.notifier, DryRunNotifier)
    assert isinstance(scheduler.alert_manager.notifier, DryRunNotifier)


def test_explicit_live_opt_in_still_possible():
    """Opting out of dry run remains an explicit choice."""
    cfg = MonitorConfig(dry_run=False, pushover_user_key="u", pushover_api_token="t")
    s = MonitorScheduler(monitor_config=cfg)
    assert isinstance(s.notifier, PushoverNotifier)
