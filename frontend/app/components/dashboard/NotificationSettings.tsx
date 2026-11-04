"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  MonitorConfig,
  MonitorStatus,
  fetchMonitorConfig,
  updateMonitorConfig,
  fetchMonitorStatus,
  startMonitoring,
  stopMonitoring,
  testNotification,
} from "@/lib/monitor-api";
import { Toggle } from "../ui/Toggle";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { LiveModeBanner } from "./LiveModeBanner";

// ---------------------------------------------------------------------------
// useCountdown — syncs to server value, ticks locally every second
// ---------------------------------------------------------------------------

function useCountdown(serverSeconds: number | null | undefined): number | null {
  const anchorRef = useRef<{ serverVal: number; receivedAt: number } | null>(null);
  const [display, setDisplay] = useState<number | null>(null);

  useEffect(() => {
    if (serverSeconds == null) {
      anchorRef.current = null;
      setDisplay(null);
      return;
    }
    anchorRef.current = { serverVal: serverSeconds, receivedAt: performance.now() / 1000 };
    setDisplay(serverSeconds);
  }, [serverSeconds]);

  useEffect(() => {
    const iv = setInterval(() => {
      const a = anchorRef.current;
      if (!a) { setDisplay(null); return; }
      const elapsed = performance.now() / 1000 - a.receivedAt;
      setDisplay(Math.max(0, Math.round(a.serverVal - elapsed)));
    }, 1000);
    return () => clearInterval(iv);
  }, []);

  return display;
}

function useTimeAgo(unixTs: number): string {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!unixTs) return;
    const iv = setInterval(() => setTick((t) => t + 1), 5000);
    return () => clearInterval(iv);
  }, [unixTs]);
  if (!unixTs) return "never";
  const s = Math.floor(Date.now() / 1000 - unixTs);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

// ---------------------------------------------------------------------------
// Shared hook
// ---------------------------------------------------------------------------

export interface MonitorState { config: MonitorConfig | null; status: MonitorStatus | null; }

export function useMonitorState(): MonitorState & { reload: () => Promise<void> } {
  const [config, setConfig] = useState<MonitorConfig | null>(null);
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const reload = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([fetchMonitorConfig(), fetchMonitorStatus()]);
      setConfig(c); setStatus(s);
    } catch { /* */ }
  }, []);
  useEffect(() => { reload(); const iv = setInterval(reload, 10000); return () => clearInterval(iv); }, [reload]);
  return { config, status, reload };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtCountdown(s: number | null): string {
  if (s == null || s <= 0) return "scanning...";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60); const r = s % 60;
  return r > 0 ? `${m}m ${r}s` : `${m}m`;
}

const PRESETS = [
  { value: "conservative", label: "Conservative", desc: "8% EV, 30m refresh, 1hr cooldown" },
  { value: "standard", label: "Standard", desc: "5% EV, 15m refresh, 30m cooldown" },
  { value: "aggressive", label: "Aggressive", desc: "3% EV, 10m refresh, 15m cooldown" },
];

const iStyle = { background: "rgba(13,20,22,0.8)", borderColor: "var(--border-default)", color: "var(--fg-primary)" };
const iClass = "rounded border font-mono text-[11px] focus:outline-none focus-visible:[box-shadow:var(--ring-focus)] focus:[border-color:var(--accent-border)]";

// ---------------------------------------------------------------------------
// NumericSetting — local draft, committed on blur / Enter only.
// Typing must not POST partial values (e.g. "3" on the way to "30").
// Values outside [min, max], non-numeric, or non-integer (when ``integer``)
// are rejected: nothing is sent and the draft reverts to the server value.
// ---------------------------------------------------------------------------

interface NumericSettingProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  integer: boolean;
  className: string;
  /** Persist the value. Resolves true on success, false on failure. */
  onCommit: (n: number) => Promise<boolean>;
  /** Called with a human-readable message when the draft is invalid. */
  onInvalid: (msg: string) => void;
}

function NumericSetting({
  label, value, min, max, step, integer, className, onCommit, onInvalid,
}: NumericSettingProps) {
  const [draft, setDraft] = useState(String(value));
  const [focused, setFocused] = useState(false);

  // Follow the server value whenever the user is not editing.
  useEffect(() => {
    if (!focused) setDraft(String(value));
  }, [value, focused]);

  const commit = async () => {
    setFocused(false);
    const trimmed = draft.trim();
    const n = Number(trimmed);
    if (
      trimmed === "" ||
      !Number.isFinite(n) ||
      (integer && !Number.isInteger(n)) ||
      n < min ||
      n > max
    ) {
      setDraft(String(value));
      onInvalid(`${label} must be ${integer ? "a whole number " : ""}between ${min} and ${max}`);
      return;
    }
    if (n === value) return;
    const ok = await onCommit(n);
    if (!ok) setDraft(String(value));
  };

  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      aria-label={label}
      value={draft}
      onFocus={() => setFocused(true)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => { void commit(); }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.currentTarget.blur();
        }
      }}
      className={className}
      style={iStyle}
    />
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface Props { config: MonitorConfig | null; status: MonitorStatus | null; onReload: () => Promise<void>; }

export function NotificationSettings({ config, status, onReload }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [testState, setTestState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [error, setError] = useState<string | null>(null);
  const [confirmLiveOpen, setConfirmLiveOpen] = useState(false);

  const poller = status?.odds_poller as Record<string, number | null> | undefined;
  const serverNext = poller?.seconds_until_next ?? null;
  const refreshMin = poller?.interval_minutes ?? config?.refresh_interval_minutes ?? 15;
  const pollsMax = poller?.max_polls_per_day ?? 20;
  const pollsRemaining = poller?.polls_remaining ?? pollsMax;
  const eventCount = poller?.latest_event_count ?? 0;
  const lastPollTs = (poller?.last_poll as number) ?? 0;

  const countdown = useCountdown(serverNext);
  const lastScanAgo = useTimeAgo(lastPollTs);

  const running = status?.running ?? false;
  const totalSent = status?.total_alerts_sent ?? 0;
  const totalCycles = status?.total_cycles ?? 0;
  const lastCycle = status?.last_cycle_result as Record<string, number> | null;

  if (!config) return null;

  const liveActive = running && !config.dry_run;

  // Never rejects: failures are surfaced via ``error`` (role="alert") so
  // e.g. a failed "Switch to Dry-run" is visible instead of silent.
  // Resolves true on success, false on failure.
  const save = async (updates: Record<string, unknown>): Promise<boolean> => {
    setSaving(true); setError(null);
    try {
      await updateMonitorConfig(updates);
      await onReload();
      return true;
    } catch (e: unknown) {
      setError(`Save failed: ${e instanceof Error ? e.message : "unknown error"}`);
      return false;
    } finally { setSaving(false); }
  };

  const handleStartStop = async () => {
    setStarting(true); setError(null);
    try {
      if (running) { await stopMonitoring(); }
      else { await startMonitoring(); }
      await onReload();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally { setStarting(false); }
  };

  const handleTest = async () => {
    setTestState("sending"); setError(null);
    try {
      const r = await testNotification();
      setTestState(r.sent ? "sent" : "failed");
    } catch (e: unknown) {
      setTestState("failed");
      setError(e instanceof Error ? e.message : "Failed");
    }
    setTimeout(() => setTestState("idle"), 4000);
  };

  // Toggle change handler. Going dry-run -> live REQUIRES confirmation;
  // going live -> dry-run is safer and saves immediately.
  const handleNotificationToggle = (next: boolean) => {
    if (next) {
      // Proposed: enable live alerts (dry_run = false). Open confirm.
      setConfirmLiveOpen(true);
    } else {
      // Proposed: return to dry-run (dry_run = true). Safe, save now.
      void save({ dry_run: true });
    }
  };

  const countdownColor = countdown == null
    ? "var(--fg-ghost)"
    : countdown <= 0
      ? "var(--accent)"
      : countdown <= 30
        ? "var(--warn)"
        : "var(--fg-muted)";

  return (
    <>
      <LiveModeBanner
        show={liveActive}
        cooldownMinutes={config.cooldown_minutes}
        maxAlertsPerHour={config.max_alerts_per_hour}
        onDisable={() => save({ dry_run: true })}
      />

      <div
        className="rounded-lg border"
        style={{
          background: "var(--bg-surface)",
          borderColor: liveActive ? "var(--danger-border)" : "var(--border-default)",
          borderRadius: "var(--radius-lg)",
        }}
      >
        {/* ── COLLAPSED BAR ── */}
        <div
          className="flex items-center justify-between px-4 py-2 cursor-pointer select-none"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <span
              className="font-mono uppercase tracking-[0.12em] font-medium"
              style={{
                fontSize: "10px",
                color: liveActive ? "var(--danger-strong)" : running ? "var(--accent)" : "var(--fg-faint)",
              }}
            >
              Phone Alerts
            </span>
            {running && config.preset !== "custom" && (
              <span
                className="font-mono uppercase tracking-wider px-1.5 py-0.5 rounded-md border"
                style={{
                  fontSize: "8px",
                  color: "var(--accent)",
                  background: "var(--accent-soft)",
                  borderColor: "var(--accent-border)",
                }}
              >
                {config.preset}
              </span>
            )}
          </div>
          <div
            className="flex items-center gap-3 font-mono"
            style={{ fontSize: "10px", color: "var(--fg-ghost)" }}
          >
            {running && countdown != null && (
              <>
                <span>
                  Next scan{" "}
                  <span style={{ color: countdownColor, fontVariantNumeric: "tabular-nums" }}>
                    {fmtCountdown(countdown)}
                  </span>
                </span>
                <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
                <span>{(config.min_ev * 100).toFixed(0)}%+ EV</span>
                <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
              </>
            )}
            {totalSent > 0 && (
              <>
                <span>{totalSent} sent</span>
                <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
              </>
            )}
            {running ? (
              liveActive ? (
                <div className="flex items-center gap-1.5" title="LIVE Pushover alerts active">
                  <span
                    className="w-1.5 h-1.5 rounded-full animate-pulse"
                    style={{ background: "var(--danger)", boxShadow: "0 0 6px var(--danger)" }}
                  />
                  <span style={{ color: "var(--danger-strong)", fontWeight: 600 }}>LIVE</span>
                </div>
              ) : (
                <div className="flex items-center gap-1.5" title="Monitor running, dry-run on (no real alerts)">
                  <span
                    className="w-1.5 h-1.5 rounded-full animate-pulse"
                    style={{ background: "var(--warn)", boxShadow: "0 0 4px var(--warn)" }}
                  />
                  <span style={{ color: "var(--warn-strong)" }}>Dry-run</span>
                </div>
              )
            ) : (
              <span style={{ color: "var(--fg-ghost)" }}>Off</span>
            )}
            <span style={{ color: "var(--fg-disabled)" }}>{expanded ? "▲" : "▼"}</span>
          </div>
        </div>

        {/* Error row — visible even when collapsed, so a failed save (e.g.
            "Switch to Dry-run" from the LIVE banner) is never silent. */}
        {error && (
          <div
            role="alert"
            className="flex items-center justify-between gap-3 px-4 pb-2 font-mono"
            style={{ fontSize: "10px", color: "var(--danger-strong)" }}
          >
            <span>{error}</span>
            <button
              type="button"
              onClick={() => setError(null)}
              aria-label="Dismiss error"
              className="focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
              style={{ color: "var(--fg-faint)" }}
            >
              ×
            </button>
          </div>
        )}

        {/* ── EXPANDED ── */}
        {expanded && (
          <div
            className="px-4 pb-4 space-y-4"
            style={{ borderTop: "1px solid var(--border-subtle)" }}
          >
            {/* Start / Stop button */}
            <div className="pt-3 flex items-center gap-3 flex-wrap">
              <button
                onClick={handleStartStop}
                disabled={starting}
                className={
                  "px-5 py-2 rounded-md border font-mono font-semibold tracking-wider uppercase transition-colors duration-150 " +
                  "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)] " +
                  "disabled:opacity-40 disabled:cursor-not-allowed"
                }
                style={{
                  fontSize: "11px",
                  color: running ? "var(--danger-strong)" : "var(--accent)",
                  background: running ? "var(--danger-soft)" : "var(--accent-soft)",
                  borderColor: running ? "var(--danger-border)" : "var(--accent-border)",
                }}
              >
                {starting ? "..." : running ? "Stop Monitoring" : "Start Monitoring"}
              </button>
              {!config.has_pushover_credentials && !running && (
                <span className="font-mono" style={{ fontSize: "10px", color: "var(--warn-strong)" }}>
                  Set PUSHOVER keys in .env first
                </span>
              )}
            </div>

            {/* Live dashboard — only when running */}
            {running && (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
                <div
                  className="rounded-md border px-3 py-2"
                  style={{ borderColor: "var(--border-subtle)", background: "var(--bg-overlay)" }}
                >
                  <div className="font-mono uppercase tracking-wider" style={{ fontSize: "8px", color: "var(--fg-faint)" }}>
                    Next Scan
                  </div>
                  <div
                    className="font-mono font-bold mt-0.5"
                    style={{ fontSize: "14px", color: countdownColor, fontVariantNumeric: "tabular-nums" }}
                  >
                    {fmtCountdown(countdown)}
                  </div>
                  <div className="font-mono mt-0.5" style={{ fontSize: "9px", color: "var(--fg-ghost)" }}>
                    every {refreshMin}m
                  </div>
                </div>
                <div
                  className="rounded-md border px-3 py-2"
                  style={{ borderColor: "var(--border-subtle)", background: "var(--bg-overlay)" }}
                >
                  <div className="font-mono uppercase tracking-wider" style={{ fontSize: "8px", color: "var(--fg-faint)" }}>
                    API Budget
                  </div>
                  <div
                    className="font-mono font-bold mt-0.5"
                    style={{
                      fontSize: "14px",
                      color:
                        pollsRemaining <= 3
                          ? "var(--danger)"
                          : pollsRemaining <= 10
                            ? "var(--warn)"
                            : "var(--fg-primary)",
                      fontVariantNumeric: "tabular-nums",
                    }}
                  >
                    {pollsRemaining}/{pollsMax}
                  </div>
                  <div className="font-mono mt-0.5" style={{ fontSize: "9px", color: "var(--fg-ghost)" }}>
                    polls left today
                  </div>
                </div>
                <div
                  className="rounded-md border px-3 py-2"
                  style={{ borderColor: "var(--border-subtle)", background: "var(--bg-overlay)" }}
                >
                  <div className="font-mono uppercase tracking-wider" style={{ fontSize: "8px", color: "var(--fg-faint)" }}>
                    FD Events
                  </div>
                  <div
                    className="font-mono font-bold mt-0.5"
                    style={{ fontSize: "14px", color: "var(--fg-primary)", fontVariantNumeric: "tabular-nums" }}
                  >
                    {eventCount}
                  </div>
                  <div className="font-mono mt-0.5" style={{ fontSize: "9px", color: "var(--fg-ghost)" }}>
                    scanned {lastScanAgo}
                  </div>
                </div>
                <div
                  className="rounded-md border px-3 py-2"
                  style={{ borderColor: "var(--border-subtle)", background: "var(--bg-overlay)" }}
                >
                  <div className="font-mono uppercase tracking-wider" style={{ fontSize: "8px", color: "var(--fg-faint)" }}>
                    Alerts Sent
                  </div>
                  <div
                    className="font-mono font-bold mt-0.5"
                    style={{
                      fontSize: "14px",
                      color: totalSent > 0 ? "var(--platform-polymarket)" : "var(--fg-ghost)",
                      fontVariantNumeric: "tabular-nums",
                    }}
                  >
                    {totalSent}
                  </div>
                  <div className="font-mono mt-0.5" style={{ fontSize: "9px", color: "var(--fg-ghost)" }}>
                    {totalCycles} scans
                  </div>
                </div>
                <div
                  className="rounded-md border px-3 py-2"
                  style={{ borderColor: "var(--border-subtle)", background: "var(--bg-overlay)" }}
                >
                  <div className="font-mono uppercase tracking-wider" style={{ fontSize: "8px", color: "var(--fg-faint)" }}>
                    Last Scan
                  </div>
                  {lastCycle ? (
                    <div className="flex flex-col gap-0.5 mt-1 font-mono" style={{ fontSize: "10px" }}>
                      <span style={{ color: "var(--fg-muted)" }}>{lastCycle.candidates_evaluated ?? 0} evaluated</span>
                      <span style={{ color: "var(--fg-muted)" }}>{lastCycle.filter_passed ?? 0} qualified</span>
                      <span
                        style={{
                          color:
                            (lastCycle.alerts_sent ?? 0) > 0
                              ? "var(--platform-polymarket)"
                              : "var(--fg-muted)",
                        }}
                      >
                        {lastCycle.alerts_sent ?? 0} alerted
                      </span>
                    </div>
                  ) : (
                    <div className="font-mono mt-1" style={{ fontSize: "10px", color: "var(--fg-ghost)" }}>
                      waiting...
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* ── Preset selector ── */}
            <div>
              <div className="font-mono uppercase tracking-wider mb-2" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
                Preset
              </div>
              <div className="flex gap-2">
                {PRESETS.map((p) => {
                  const active = config.preset === p.value;
                  return (
                    <button
                      key={p.value}
                      onClick={() => save({ preset: p.value })}
                      disabled={saving}
                      className={
                        "flex-1 px-3 py-2 rounded-md border text-left transition-colors duration-150 " +
                        "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)] " +
                        "disabled:opacity-40 disabled:cursor-not-allowed"
                      }
                      style={{
                        background: active ? "var(--accent-soft)" : "transparent",
                        borderColor: active ? "var(--accent-border)" : "rgba(75,85,99,0.12)",
                      }}
                    >
                      <div
                        className="font-mono font-semibold"
                        style={{ fontSize: "11px", color: active ? "var(--accent)" : "var(--fg-muted)" }}
                      >
                        {p.label}
                      </div>
                      <div
                        className="font-mono mt-0.5 leading-relaxed"
                        style={{ fontSize: "9px", color: "var(--fg-ghost)" }}
                      >
                        {p.desc}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* ── Settings ── */}
            <div>
              <div className="font-mono uppercase tracking-wider mb-2" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
                Settings
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2">
                <div className="space-y-1">
                  <label className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>Min EV</label>
                  <div className="relative">
                    <NumericSetting
                      label="Min EV"
                      step={1}
                      min={1}
                      max={50}
                      integer={false}
                      value={Math.round(config.min_ev * 100)}
                      onCommit={(n) => save({ min_ev: n / 100 })}
                      onInvalid={setError}
                      className={`w-full pl-2 pr-5 py-1 text-right ${iClass}`}
                    />
                    <span
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 font-mono"
                      style={{ fontSize: "10px", color: "var(--fg-ghost)" }}
                    >
                      %
                    </span>
                  </div>
                </div>
                <div className="space-y-1">
                  <label className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>Refresh every</label>
                  <div className="relative">
                    <NumericSetting
                      label="Refresh every"
                      step={5}
                      min={5}
                      max={60}
                      integer={true}
                      value={config.refresh_interval_minutes}
                      onCommit={(n) => save({ refresh_interval_minutes: n })}
                      onInvalid={setError}
                      className={`w-full pl-2 pr-7 py-1 text-right ${iClass}`}
                    />
                    <span
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 font-mono"
                      style={{ fontSize: "10px", color: "var(--fg-ghost)" }}
                    >
                      min
                    </span>
                  </div>
                </div>
                <div className="space-y-1">
                  <label className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>Cooldown</label>
                  <div className="relative">
                    <NumericSetting
                      label="Cooldown"
                      step={5}
                      min={5}
                      max={120}
                      integer={true}
                      value={config.cooldown_minutes}
                      onCommit={(n) => save({ cooldown_minutes: n })}
                      onInvalid={setError}
                      className={`w-full pl-2 pr-7 py-1 text-right ${iClass}`}
                    />
                    <span
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 font-mono"
                      style={{ fontSize: "10px", color: "var(--fg-ghost)" }}
                    >
                      min
                    </span>
                  </div>
                </div>
                <div className="space-y-1">
                  <label className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>Max alerts/hr</label>
                  <NumericSetting
                    label="Max alerts/hr"
                    step={1}
                    min={1}
                    max={30}
                    integer={true}
                    value={config.max_alerts_per_hour}
                    onCommit={(n) => save({ max_alerts_per_hour: n })}
                    onInvalid={setError}
                    className={`w-full px-2 py-1 text-right ${iClass}`}
                  />
                </div>
              </div>
            </div>

            {/* ── Platforms (alert allowlist; separate from scan-config Books toggles) ── */}
            <div className="flex items-center gap-4 flex-wrap">
              <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>Platforms</span>
              {["polymarket", "kalshi"].map((p) => (
                <label
                  key={p}
                  className="flex items-center gap-1 font-mono cursor-pointer"
                  style={{
                    fontSize: "11px",
                    color: config.platforms.includes(p) ? "var(--fg-primary)" : "var(--fg-faint)",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={config.platforms.includes(p)}
                    onChange={(e) => {
                      const next = e.target.checked
                        ? [...config.platforms, p]
                        : config.platforms.filter((x) => x !== p);
                      if (next.length > 0) save({ platforms: next });
                    }}
                    className="w-3 h-3 rounded accent-teal-400 focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
                  />
                  {p === "polymarket" ? "Polymarket" : "Kalshi"}
                </label>
              ))}
            </div>

            {/* ── Notifications mode (dry-run vs LIVE) ── */}
            <div
              className="rounded-md border p-3 flex items-center justify-between gap-3 flex-wrap"
              style={{
                background: liveActive ? "var(--danger-soft)" : "var(--bg-overlay)",
                borderColor: liveActive ? "var(--danger-border)" : "var(--border-subtle)",
                borderRadius: "var(--radius-md)",
                boxShadow: liveActive ? "var(--glow-danger)" : undefined,
              }}
            >
              <div className="min-w-0">
                <p
                  className="font-mono uppercase tracking-wider font-semibold"
                  style={{
                    fontSize: "10px",
                    color: liveActive ? "var(--danger-strong)" : "var(--fg-secondary)",
                  }}
                >
                  Notifications mode
                </p>
                <p
                  className="font-mono mt-0.5 leading-relaxed"
                  style={{
                    fontSize: "11px",
                    color: liveActive ? "var(--danger-strong)" : "var(--fg-secondary)",
                  }}
                >
                  {config.dry_run
                    ? "Dry-run — payloads logged to backend; never sent to your phone."
                    : "LIVE — real Pushover alerts fire on every BUY that passes the rule engine."}
                </p>
              </div>
              <Toggle
                checked={!config.dry_run}
                dangerWhen={true}
                size="md"
                label={config.dry_run ? "Live alerts" : "LIVE"}
                onCheckedChange={handleNotificationToggle}
              />
            </div>

            {/* ── Footer: Pushover credential status + Test Push ── */}
            <div
              className="flex items-center justify-between gap-3 pt-2 flex-wrap"
              style={{ borderTop: "1px solid var(--border-subtle)" }}
            >
              <div className="font-mono" style={{ fontSize: "10px", color: "var(--fg-ghost)" }}>
                {config.has_pushover_credentials ? (
                  <span style={{ color: "var(--fg-faint)" }}>Pushover connected</span>
                ) : (
                  <span>
                    Set PUSHOVER_USER_KEY + PUSHOVER_API_TOKEN in .env &mdash;{" "}
                    <a
                      href="https://pushover.net"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
                      style={{ color: "var(--fg-faint)" }}
                    >
                      pushover.net
                    </a>
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3">
                <span
                  className="font-mono italic"
                  style={{ fontSize: "10px", color: "var(--warn-strong)" }}
                  title="POST /api/monitor/test bypasses dry_run by design"
                >
                  Sends a real Pushover, even in dry-run
                </span>
                <button
                  onClick={handleTest}
                  disabled={testState === "sending" || !config.has_pushover_credentials}
                  className={
                    "px-3 py-1 rounded-md border font-mono uppercase tracking-wider transition-colors duration-150 " +
                    "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)] " +
                    "disabled:opacity-40 disabled:cursor-not-allowed"
                  }
                  style={{
                    fontSize: "10px",
                    color:
                      testState === "sent"
                        ? "var(--success)"
                        : testState === "failed"
                          ? "var(--danger-strong)"
                          : !config.has_pushover_credentials
                            ? "var(--fg-disabled)"
                            : "var(--warn-strong)",
                    borderColor:
                      testState === "sent"
                        ? "var(--success-border)"
                        : testState === "failed"
                          ? "var(--danger-border)"
                          : "var(--warn-border)",
                    background:
                      testState === "sent"
                        ? "var(--success-soft)"
                        : testState === "failed"
                          ? "var(--danger-soft)"
                          : "var(--warn-soft)",
                  }}
                >
                  {testState === "sending"
                    ? "Sending..."
                    : testState === "sent"
                      ? "Sent to phone"
                      : testState === "failed"
                        ? "Failed"
                        : "Send Test Push"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmLiveOpen}
        title="Enable LIVE Pushover alerts?"
        description={
          <div className="space-y-2">
            <p>
              Real Pushover notifications will fire on your phone for every BUY
              opportunity that passes the rule engine.
            </p>
            <p>
              Rate limits still apply: alerts are gated by your cooldown
              ({config.cooldown_minutes}m) and hourly cap
              ({config.max_alerts_per_hour}/hr).
            </p>
            <p>
              You can return to Dry-run any time. The Send Test Push button is
              independent of this setting — it always sends a real Pushover.
            </p>
          </div>
        }
        confirmLabel="Enable Live Alerts"
        confirmVariant="danger"
        cancelLabel="Stay in Dry-run"
        onCancel={() => setConfirmLiveOpen(false)}
        onConfirm={async () => {
          setConfirmLiveOpen(false);
          await save({ dry_run: false });
        }}
      />
    </>
  );
}
