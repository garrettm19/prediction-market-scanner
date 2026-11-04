"""
Alert state store — tracks per-opportunity alert history.

JSON-file backed.  Keyed by (platform, event, market_type, side, line).
Tracks last alert time, last edge, cooldown, and lifecycle state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "alert_state.json"
)


@dataclass
class AlertRecord:
    """Per-opportunity alert tracking."""
    key: str                               # stable key string
    platform: str = ""
    event: str = ""
    market_type: str = ""
    side: str = ""
    line: float | None = None

    last_alert_ts: float = 0.0             # unix timestamp of last alert sent
    last_alert_edge: float = 0.0           # edge at time of last alert
    current_edge: float = 0.0
    current_status: str = ""               # BUY / WATCH / SKIP
    is_active: bool = False                # currently in pipeline results
    first_seen_ts: float = 0.0
    last_seen_ts: float = 0.0
    alert_count: int = 0
    suppressed_count: int = 0


def make_key(
    platform: str,
    event: str,
    market_type: str,
    side: str,
    line: float | None,
) -> str:
    line_s = str(line) if line is not None else "none"
    return f"{platform}|{event}|{market_type}|{side}|{line_s}"


class AlertStateStore:
    """Persistent alert state backed by a JSON file."""

    def __init__(self, path: str | None = None):
        self._path = path or _DEFAULT_PATH
        self._records: dict[str, AlertRecord] = {}
        self._alerts_this_hour: list[float] = []   # timestamps of alerts sent
        self._load()

    # --- Persistence ---

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, "r") as f:
                    raw = json.load(f)
                for key, data in raw.get("records", {}).items():
                    self._records[key] = AlertRecord(**data)
                self._alerts_this_hour = raw.get("alerts_this_hour", [])
                logger.info("AlertStateStore: loaded %d records from %s", len(self._records), self._path)
        except Exception as exc:
            logger.warning("AlertStateStore: failed to load %s: %s", self._path, exc)

    def save(self) -> None:
        """Persist state atomically.

        Writes to a temp file in the same directory, fsyncs it, then
        ``os.replace``s it over the target, so a crash or full disk mid-write
        can never leave a truncated alert_state.json (which would reset
        cooldowns and the hourly cap on the next load).
        """
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "records": {k: asdict(v) for k, v in self._records.items()},
            "alerts_this_hour": self._alerts_this_hour,
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix=".alert_state.", suffix=".tmp", dir=directory,
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- Record access ---

    def get(self, key: str) -> AlertRecord | None:
        return self._records.get(key)

    def upsert(self, key: str, **kwargs: object) -> AlertRecord:
        if key not in self._records:
            self._records[key] = AlertRecord(key=key)
        rec = self._records[key]
        for k, v in kwargs.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        return rec

    def all_records(self) -> list[AlertRecord]:
        return list(self._records.values())

    # --- Hourly rate tracking ---

    def record_alert_sent(self) -> None:
        now = time.time()
        self._alerts_this_hour.append(now)
        # Prune older than 1 hour
        cutoff = now - 3600
        self._alerts_this_hour = [t for t in self._alerts_this_hour if t > cutoff]

    def alerts_sent_this_hour(self) -> int:
        now = time.time()
        cutoff = now - 3600
        self._alerts_this_hour = [t for t in self._alerts_this_hour if t > cutoff]
        return len(self._alerts_this_hour)

    # --- Lifecycle ---

    def mark_all_inactive(self) -> None:
        """Mark all records inactive before a fresh pipeline cycle."""
        for rec in self._records.values():
            rec.is_active = False

    def prune_stale(self, max_age_hours: int = 72) -> int:
        """Remove records not seen in max_age_hours."""
        cutoff = time.time() - max_age_hours * 3600
        stale = [k for k, v in self._records.items() if v.last_seen_ts < cutoff]
        for k in stale:
            del self._records[k]
        return len(stale)

    # --- History ---

    def recent_alerts(self, limit: int = 50) -> list[AlertRecord]:
        """Return the most recently alerted records."""
        alerted = [r for r in self._records.values() if r.alert_count > 0]
        alerted.sort(key=lambda r: r.last_alert_ts, reverse=True)
        return alerted[:limit]
