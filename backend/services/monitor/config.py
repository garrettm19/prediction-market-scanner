"""
Monitoring and alert configuration.

Simplified: always instant push, always BUY only.
User controls: min EV, refresh interval, cooldown, platforms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AlertPreset(str, Enum):
    CONSERVATIVE = "conservative"
    STANDARD = "standard"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


@dataclass
class MonitorConfig:
    """All monitoring and alert thresholds."""

    # --- Global toggle ---
    enabled: bool = False

    # --- Preset ---
    preset: AlertPreset = AlertPreset.STANDARD

    # --- Filters ---
    min_ev: float = 0.05                           # minimum edge to alert (5%)
    # Alert-side platform allowlist.  Default mirrors ScanConfig's default
    # (Kalshi enabled, Polymarket disabled).  Operator can re-add "polymarket"
    # via POST /api/monitor/config when explicitly enabling that book.
    platforms: list[str] = field(default_factory=lambda: ["kalshi"])
    market_types: list[str] = field(default_factory=lambda: ["h2h"])
    exclude_ambiguity_downgraded: bool = True

    # --- Anti-spam ---
    cooldown_minutes: int = 30                     # per-opportunity cooldown
    resend_edge_improvement: float = 0.03          # re-alert if edge improves by 3%+
    max_alerts_per_hour: int = 10

    # --- Source refresh ---
    refresh_interval_minutes: int = 15             # how often to scan
    odds_api_max_polls_per_day: int = 20           # budget guard

    # --- Notification provider ---
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    priority_high_min_edge: float = 0.10           # edge >= 10% → high priority push

    # --- Dry run ---
    # Fail-closed default: live Pushover delivery must be an explicit opt-in
    # (POST /api/monitor/config dry_run=false).  Config is in-memory only, so
    # a backend restart must come back in dry-run, never silently live.
    dry_run: bool = True


PRESETS: dict[AlertPreset, dict] = {
    AlertPreset.CONSERVATIVE: {
        "min_ev": 0.08,
        "cooldown_minutes": 60,
        "max_alerts_per_hour": 5,
        "refresh_interval_minutes": 30,
    },
    AlertPreset.STANDARD: {
        "min_ev": 0.05,
        "cooldown_minutes": 30,
        "max_alerts_per_hour": 10,
        "refresh_interval_minutes": 15,
    },
    AlertPreset.AGGRESSIVE: {
        "min_ev": 0.03,
        "cooldown_minutes": 15,
        "max_alerts_per_hour": 20,
        "refresh_interval_minutes": 10,
    },
}


def apply_preset(config: MonitorConfig, preset: AlertPreset) -> MonitorConfig:
    if preset == AlertPreset.CUSTOM:
        config.preset = preset
        return config
    overrides = PRESETS.get(preset, {})
    for k, v in overrides.items():
        setattr(config, k, v)
    config.preset = preset
    return config
