"use client";

import { useEffect, useState } from "react";
import { StatusPill, type StatusPillTone } from "../ui/StatusPill";
import type { OddsFailure } from "@/lib/types";

interface StatusStripProps {
  // Refresh state
  isRefreshing: boolean;
  refreshStartedAt: number | null;
  updatedAt: number | null;
  lastRefreshDuration: number | null;
  lastRefreshError: string | null;
  // Books
  platformEnabled: Record<string, boolean>;
  platformLabels: Record<string, string>;
  // Alerts
  monitorRunning: boolean;
  monitorEnabled: boolean;
  monitorDryRun: boolean;
  monitorPreset: string | null;
  monitorMinEv: number | null;
  monitorCooldownMin: number | null;
  monitorMaxPerHour: number | null;
  // Props
  propsEnabled: boolean;
  // Backend reachability / data freshness
  /** Unix seconds since /status polling started failing; null when reachable. */
  backendUnreachableSince?: number | null;
  /** Optional partial-failure flags from /status (only shown when present). */
  degraded?: boolean | null;
  oddsFailures?: OddsFailure[] | null;
}

// Snapshot staleness window. The longest monitor refresh interval selectable
// in NotificationSettings ("Refresh every", max 60 min) is the slowest normal
// cadence; a snapshot older than 2x that cannot be explained by the schedule,
// so the UI flags it as stale rather than showing it as fresh.
export const MAX_REFRESH_INTERVAL_MINUTES = 60;
export const SNAPSHOT_STALE_AFTER_SECONDS = 2 * MAX_REFRESH_INTERVAL_MINUTES * 60;

export function isSnapshotStale(updatedAt: number | null, nowSec: number = Date.now() / 1000): boolean {
  return updatedAt != null && nowSec - updatedAt > SNAPSHOT_STALE_AFTER_SECONDS;
}

export function fmtAgo(unixSec: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - unixSec));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function fmtElapsed(startedAt: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  return `${s}s`;
}

interface PillSpec {
  label: string;
  value: string;
  tone?: StatusPillTone;
  pulse?: boolean;
  title?: string;
  detail?: string | null;
}

export function StatusStrip(props: StatusStripProps) {
  // Tick every second so elapsed counters and "X ago" stay live.
  const [, setTick] = useState(0);
  useEffect(() => {
    const iv = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(iv);
  }, []);

  // --- Scan pill ---
  let scanPill: PillSpec;
  if (props.backendUnreachableSince != null) {
    scanPill = {
      label: "Backend",
      value: "Unreachable",
      tone: "danger",
      title: "Status polling is failing; retrying. Data shown may be outdated.",
      detail: props.updatedAt ? `cached ${fmtAgo(props.updatedAt)}` : null,
    };
  } else if (props.lastRefreshError) {
    scanPill = {
      label: "Scan",
      value: "Error",
      tone: "danger",
      title: props.lastRefreshError,
      detail: props.updatedAt ? `last good ${fmtAgo(props.updatedAt)}` : null,
    };
  } else if (props.isRefreshing) {
    scanPill = {
      label: "Scan",
      value: "Refreshing",
      tone: "warn",
      pulse: true,
      detail: props.refreshStartedAt ? `${fmtElapsed(props.refreshStartedAt)} elapsed` : null,
    };
  } else if (props.updatedAt && isSnapshotStale(props.updatedAt)) {
    scanPill = {
      label: "Scan",
      value: `Stale · ${fmtAgo(props.updatedAt)}`,
      tone: "warn",
      title: `Snapshot is older than ${SNAPSHOT_STALE_AFTER_SECONDS / 60} minutes; prices may be outdated`,
    };
  } else if (props.updatedAt) {
    scanPill = {
      label: "Scan",
      value: fmtAgo(props.updatedAt),
      tone: "success",
      detail:
        props.lastRefreshDuration != null
          ? `${props.lastRefreshDuration.toFixed(0)}s last scan`
          : null,
    };
  } else {
    scanPill = { label: "Scan", value: "Idle", tone: "muted" };
  }

  // --- Books pill ---
  const enabledBooks = Object.entries(props.platformEnabled)
    .filter(([, v]) => v)
    .map(([k]) => props.platformLabels[k] ?? k);
  let booksPill: PillSpec;
  if (enabledBooks.length === 0) {
    booksPill = {
      label: "Books",
      value: "None",
      tone: "danger",
      title: "Enable at least one book to scan",
    };
  } else if (props.platformEnabled.polymarket) {
    booksPill = {
      label: "Books",
      value: enabledBooks.join(" + "),
      tone: "warn",
      title: "Polymarket is opt-in; default is Kalshi-only",
      detail: "PM opt-in",
    };
  } else {
    booksPill = { label: "Books", value: enabledBooks.join(" + "), tone: "success" };
  }

  // --- Alerts pill ---
  let alertsPill: PillSpec;
  if (props.monitorRunning && !props.monitorDryRun) {
    alertsPill = {
      label: "Alerts",
      value: "LIVE",
      tone: "danger",
      pulse: true,
      title: "Live Pushover alerts are active",
      detail: props.monitorPreset ?? null,
    };
  } else if (props.monitorRunning && props.monitorDryRun) {
    alertsPill = {
      label: "Alerts",
      value: "Dry-run",
      tone: "warn",
      pulse: true,
      title: "Monitor running, payloads logged not sent",
      detail: props.monitorPreset ?? null,
    };
  } else if (props.monitorDryRun) {
    alertsPill = {
      label: "Alerts",
      value: "Off (dry-run)",
      tone: "muted",
      title: "Monitor stopped; would dry-run if started",
    };
  } else {
    alertsPill = {
      label: "Alerts",
      value: "Off",
      tone: "muted",
      title: "Monitor stopped",
    };
  }

  // --- Odds health pill (only when the backend reports it) ---
  let oddsPill: PillSpec | null = null;
  const failures = props.oddsFailures;
  const failureCount = Array.isArray(failures) ? failures.length : 0;
  if (props.degraded || failureCount > 0) {
    oddsPill = {
      label: "Odds",
      value: "Degraded",
      tone: "warn",
      title: Array.isArray(failures) && failures.length > 0
        ? `Odds fetch failures: ${failures.map((f) => `${f.sport_key} (${f.error})`).join(", ")}`
        : "Backend reports a partial odds failure",
      detail: failureCount > 0 ? `${failureCount} failed` : null,
    };
  }

  // --- Props pill ---
  const propsPill: PillSpec = props.propsEnabled
    ? { label: "Props", value: "On", tone: "warn" }
    : { label: "Props", value: "Off", tone: "muted" };

  return (
    <div
      className="flex flex-wrap items-center gap-2 px-4 py-2.5 rounded-lg border"
      style={{
        background: "var(--bg-surface)",
        borderColor: "var(--border-default)",
      }}
    >
      <StatusPill {...scanPill} />
      {oddsPill && <StatusPill {...oddsPill} />}
      <StatusPill {...booksPill} />
      <StatusPill {...alertsPill} />
      <StatusPill {...propsPill} />
      {props.monitorPreset && props.monitorMinEv != null && (
        <div
          className="ml-auto flex items-center gap-3 font-mono text-[9px]"
          style={{ color: "var(--fg-faint)" }}
          title="Active monitor preset settings"
        >
          <span>min EV {(props.monitorMinEv * 100).toFixed(1)}%</span>
          {props.monitorCooldownMin != null && (
            <span>· cooldown {props.monitorCooldownMin}m</span>
          )}
          {props.monitorMaxPerHour != null && (
            <span>· max {props.monitorMaxPerHour}/hr</span>
          )}
        </div>
      )}
    </div>
  );
}
