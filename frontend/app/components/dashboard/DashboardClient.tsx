"use client";

import { useState, useCallback, useEffect, useRef, useMemo } from "react";
import { Opportunity, OpportunitiesResponse } from "@/lib/types";
import { fetchSnapshotStatus, fetchSnapshot, refreshOpportunities, fetchSportsRegistry, fetchScanConfig, updateScanConfig } from "@/lib/api";
import { startMonitoring, stopMonitoring } from "@/lib/monitor-api";
import { sportLabel, marketTypeLabel, isPropType, SportRegistryEntry } from "@/lib/sport-labels";
import { useTrackedPositions } from "@/lib/useTrackedPositions";
import { OpportunitiesTable } from "./OpportunitiesTable";
import { MarketDetailPanel } from "./MarketDetailPanel";
import { TrackedPositions } from "./TrackedPositions";
import { NotificationSettings, useMonitorState } from "./NotificationSettings";
import { DataStatus } from "./DataStatus";
import { StatusStrip, isSnapshotStale, fmtAgo } from "./StatusStrip";
import { PaperMakerControls } from "./PaperMakerControls";

// Consecutive /status poll failures before the UI declares the backend
// unreachable. Polling keeps retrying regardless.
const BACKEND_UNREACHABLE_AFTER_FAILURES = 2;

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : "unknown error";
}

interface Props {
  initialData: OpportunitiesResponse | null;
}

const EMPTY_DATA: OpportunitiesResponse = {
  opportunities: [],
  total: 0,
  status_counts: {},
  quota_remaining: null,
  sportsbook_markets_fetched: [],
  markets_dropped_by_type: {},
  platforms_fetched: [],
  updated_at: null,
  is_refreshing: false,
};

function timeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

// Stable key for matching an opportunity across data updates
function oppKey(o: Opportunity): string {
  return `${o.platform}:${o.market_id}:${o.side}`;
}

export function DashboardClient({ initialData }: Props) {
  const init = initialData ?? EMPTY_DATA;
  const [edgeInput, setEdgeInput] = useState("4");
  const [bankrollInput, setBankrollInput] = useState("1000");
  const [bankroll, setBankroll] = useState(1000);
  const [data, setData] = useState<OpportunitiesResponse>(init);
  const [selected, setSelected] = useState<Opportunity | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(init.updated_at ?? null);
  const [backendRefreshing, setBackendRefreshing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [, setTick] = useState(0);
  // New status fields surfaced by /opportunities/status
  const [refreshStartedAt, setRefreshStartedAt] = useState<number | null>(null);
  const [lastRefreshError, setLastRefreshError] = useState<string | null>(null);
  const [lastRefreshDuration, setLastRefreshDuration] = useState<number | null>(null);
  const [firstPollComplete, setFirstPollComplete] = useState(false);
  // Backend reachability: set (unix seconds) once /status polling has failed
  // BACKEND_UNREACHABLE_AFTER_FAILURES times in a row; cleared on success.
  const [backendUnreachableSince, setBackendUnreachableSince] = useState<number | null>(null);
  const pollFailuresRef = useRef(0);
  // Optional partial-failure signals (only rendered if the backend sends them).
  const [degraded, setDegraded] = useState<boolean | null>(null);
  const [oddsFailures, setOddsFailures] = useState<string[] | number | null>(null);
  // Visible error for user-initiated actions (scan, books toggle, auto monitor).
  const [actionError, setActionError] = useState<string | null>(null);
  // Bumped each time a scan transitions from is_refreshing=true to false.
  // PaperMakerControls watches it to refetch the latest-run summary so the
  // counts reflect the freshest run without polling on its own.
  const [scanRefreshKey, setScanRefreshKey] = useState(0);
  const lastSeenUpdatedAt = useRef<number | null>(init.updated_at ?? null);
  const pollIntervalRef = useRef(initialData ? 10_000 : 2_000); // fast poll if no initial data
  const hasAutoFiredRef = useRef(false);  // cold-start auto-refresh once-only guard

  const { positions, isTaken, takePosition, closePosition, removePosition } =
    useTrackedPositions();

  const monitor = useMonitorState();

  // Sport registry — fetched once, used for labels everywhere
  const [sportsRegistry, setSportsRegistry] = useState<Record<string, SportRegistryEntry> | undefined>(undefined);
  useEffect(() => {
    fetchSportsRegistry().then(setSportsRegistry).catch(() => {});
  }, []);

  // Books / platform toggles — backend ScanConfig is source of truth.
  // Defaults: Kalshi=true, Polymarket=false. Initialized from server on mount.
  const [platformEnabled, setPlatformEnabled] = useState<Record<string, boolean>>({
    kalshi: true,
    polymarket: false,
  });
  const [platformLabels, setPlatformLabels] = useState<Record<string, string>>({
    kalshi: "Kalshi",
    polymarket: "Polymarket",
  });
  // Mirror of EngineConfig.enable_props sourced from /api/scan/config.
  // Read-only: there is no UI control to flip props from the dashboard.
  const [propsEnabled, setPropsEnabled] = useState<boolean>(false);
  useEffect(() => {
    fetchScanConfig()
      .then((cfg) => {
        const enabled: Record<string, boolean> = {};
        const labels: Record<string, string> = {};
        for (const [name, p] of Object.entries(cfg.platforms || {})) {
          enabled[name] = p.enabled;
          labels[name] = p.label || name;
        }
        if (Object.keys(enabled).length > 0) {
          setPlatformEnabled(enabled);
          setPlatformLabels(labels);
        }
        if (typeof cfg.props_enabled === "boolean") {
          setPropsEnabled(cfg.props_enabled);
        }
      })
      .catch((e: unknown) => {
        setActionError(
          `Couldn't load scan config from backend (${errMsg(e)}) — Books toggles show defaults and may not match the backend.`,
        );
      });
  }, []);

  const togglePlatform = useCallback((name: string, enabled: boolean) => {
    // Optimistic UI flip first; revert on failure and say why.
    setActionError(null);
    setPlatformEnabled((prev) => ({ ...prev, [name]: enabled }));
    updateScanConfig({ platforms: { [name]: { enabled } } }).catch((e: unknown) => {
      setPlatformEnabled((prev) => ({ ...prev, [name]: !enabled }));
      setActionError(`Couldn't ${enabled ? "enable" : "disable"} ${name}: ${errMsg(e)}. Toggle reverted.`);
    });
  }, []);

  const anyPlatformEnabled = Object.values(platformEnabled).some(Boolean);

  // Tick every second so "Updated Xs ago" counts up live
  useEffect(() => { setMounted(true); }, []);

  useEffect(() => {
    const interval = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(interval);
  }, []);

  // --- Snapshot polling: check /status, fetch /snapshot when updated_at changes ---
  // Cadence is driven by backend state: 2s while refreshing or no snapshot, 10s otherwise.
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      let nextInterval = pollIntervalRef.current;
      try {
        const status = await fetchSnapshotStatus();
        if (!active) return;

        // Backend answered → reachable again.
        pollFailuresRef.current = 0;
        setBackendUnreachableSince(null);
        setDegraded(status.degraded ?? null);
        setOddsFailures(status.odds_failures ?? null);

        // Surface the full backend status to the UI
        setBackendRefreshing(status.is_refreshing);
        setRefreshStartedAt(status.refresh_started_at ?? null);
        setLastRefreshError(status.last_refresh_error ?? null);
        setLastRefreshDuration(status.last_refresh_duration_seconds ?? null);
        setFirstPollComplete(true);

        // Adapt polling rate from backend truth
        nextInterval = (status.is_refreshing || !status.has_snapshot) ? 2_000 : 10_000;

        if (
          status.has_snapshot &&
          status.updated_at != null &&
          status.updated_at !== lastSeenUpdatedAt.current
        ) {
          const snap = await fetchSnapshot();
          if (!active) return;
          // A 204 (no snapshot after all) must not end the poll loop —
          // skip the update and let the next tick retry.
          if (snap) {
            const updatedAtSec = status.updated_at;
            lastSeenUpdatedAt.current = updatedAtSec;
            setUpdatedAt(updatedAtSec);
            setData(() => {
              if (selected) {
                const key = oppKey(selected);
                const match = snap.opportunities.find((o) => oppKey(o) === key);
                if (match) setSelected(match);
              }
              return snap;
            });
          }
        }

        // Backend confirms not refreshing → clear the optimistic local flag
        if (scanning && !status.is_refreshing) {
          setScanning(false);
          // Signal scan completion to PaperMakerControls so it refetches the
          // latest-run maker summary.  Bumping a scalar avoids prop drilling
          // a callback through StatusStrip.
          setScanRefreshKey((k) => k + 1);
        }
      } catch {
        // Network error, non-OK status, or request timeout (apiFetch).
        // Keep retrying on the next tick; after repeated failures, show
        // the backend as unreachable instead of presenting cached data as live.
        if (!active) return;
        pollFailuresRef.current += 1;
        if (pollFailuresRef.current >= BACKEND_UNREACHABLE_AFTER_FAILURES) {
          setBackendUnreachableSince((prev) => prev ?? Date.now() / 1000);
        }
      }
      pollIntervalRef.current = nextInterval;
      if (active) timer = setTimeout(poll, nextInterval);
    };

    timer = setTimeout(poll, pollIntervalRef.current);
    return () => { active = false; clearTimeout(timer); };
  }, [selected, scanning]);

  // --- Run Scan: fire POST /refresh; backend returns 202 immediately, polling delivers results ---
  const runScan = useCallback(() => {
    if (scanning || backendRefreshing) return;
    // Belt-and-suspenders: even if the form is somehow submitted while no
    // book is enabled, do not fire a refresh.
    if (!Object.values(platformEnabled).some(Boolean)) return;
    setActionError(null);
    setScanning(true);
    pollIntervalRef.current = 2_000;
    refreshOpportunities().catch((e: unknown) => {
      // POST itself failed (e.g. backend unreachable). Drop optimistic flag
      // and say so — backend last_refresh_error is never set in this case.
      setScanning(false);
      setActionError(`Scan request failed: ${errMsg(e)}`);
    });
  }, [scanning, backendRefreshing, platformEnabled]);

  // --- Cold-start auto-refresh: fire exactly once when the first /status
  //     response shows no snapshot and no refresh in flight ---
  const runScanRef = useRef<() => void>(() => {});
  runScanRef.current = runScan;
  useEffect(() => {
    if (
      firstPollComplete &&
      !hasAutoFiredRef.current &&
      !updatedAt &&
      !backendRefreshing
    ) {
      hasAutoFiredRef.current = true;
      runScanRef.current();
    }
  }, [firstPollComplete, updatedAt, backendRefreshing]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const br = parseFloat(bankrollInput);
    if (!isNaN(br) && br > 0) {
      setBankroll(br);
    }
    runScan();
  };

  // --- Client-side filters ---
  const [filterStatus, setFilterStatus] = useState<"all" | "BUY" | "WATCH">("all");
  const [filterPlatform, setFilterPlatform] = useState<string>("all");
  const [filterSport, setFilterSport] = useState<string>("all");
  const [filterMarketType, setFilterMarketType] = useState<string>("all");
  const [filterSearch, setFilterSearch] = useState("");

  const { opportunities: allOpps } = data;

  // Derive available filter options from all opportunities
  const availablePlatforms = useMemo(() => [...new Set(allOpps.map((o) => o.platform))].sort(), [allOpps]);
  const availableSports = useMemo(() => [...new Set(allOpps.map((o) => o.sport))].sort(), [allOpps]);
  const availableMarketTypes = useMemo(() => [...new Set(allOpps.map((o) => o.market_type))].sort(), [allOpps]);

  // Apply filters
  const opportunities = useMemo(() => {
    const minEdge = parseFloat(edgeInput);
    const minEdgeFrac = !isNaN(minEdge) && minEdge > 0 ? minEdge / 100 : 0;
    const searchLower = filterSearch.toLowerCase().trim();

    return allOpps.filter((o) => {
      // Books toggles — hide rows from disabled platforms immediately, even
      // if they're still in the snapshot from a previous scan.
      if (platformEnabled[o.platform] === false) return false;
      if (filterStatus !== "all" && o.status !== filterStatus) return false;
      if (filterPlatform !== "all" && o.platform !== filterPlatform) return false;
      if (filterSport !== "all" && o.sport !== filterSport) return false;
      if (filterMarketType === "props" && !isPropType(o.market_type)) return false;
      if (filterMarketType === "main" && isPropType(o.market_type)) return false;
      if (filterMarketType !== "all" && filterMarketType !== "props" && filterMarketType !== "main" && o.market_type !== filterMarketType) return false;
      if (minEdgeFrac > 0 && o.edge < minEdgeFrac) return false;
      if (searchLower && !o.event.toLowerCase().includes(searchLower) && !o.side.toLowerCase().includes(searchLower)) return false;
      return true;
    });
  }, [allOpps, filterStatus, filterPlatform, filterSport, filterMarketType, filterSearch, edgeInput, platformEnabled]);

  const activeFilterCount = [
    filterStatus !== "all",
    filterPlatform !== "all",
    filterSport !== "all",
    filterMarketType !== "all",
    filterSearch.trim().length > 0,
  ].filter(Boolean).length;

  const displayed = selected && opportunities.some((o) => oppKey(o) === oppKey(selected))
    ? selected
    : opportunities[0] ?? null;
  const displayedIsTaken = displayed ? isTaken(displayed) : false;

  const buyCount = allOpps.filter((o) => o.status === "BUY").length;
  const watchCount = allOpps.filter((o) => o.status === "WATCH").length;
  const platforms = availablePlatforms;
  const openPositions = positions.filter((p) => p.status === "open").length;

  // Quota from last response
  const quotaRemaining = data.quota_remaining;

  // True when backend has produced at least one snapshot
  const hasSnapshot = updatedAt != null;

  // Monitor status for toolbar
  const monitorRunning = monitor.status?.running ?? false;
  const monitorEnabled = monitor.config?.enabled ?? false;
  const alertsSent = monitor.status?.total_alerts_sent ?? 0;
  const monitorDryRun = monitor.config?.dry_run ?? true;
  const monitorPreset = monitor.config?.preset ?? null;
  const monitorMinEv = monitor.config?.min_ev ?? null;
  const monitorCooldownMin = monitor.config?.cooldown_minutes ?? null;
  const monitorMaxPerHour = monitor.config?.max_alerts_per_hour ?? null;

  // Freshness: only evaluated after mount (Date.now differs between SSR and client).
  const backendUnreachable = backendUnreachableSince != null;
  const snapshotStale = mounted && isSnapshotStale(updatedAt);
  const freshnessWarning = backendUnreachable || snapshotStale;

  return (
    <main className="max-w-[1600px] mx-auto px-6 py-5 space-y-3">
      <StatusStrip
        isRefreshing={scanning || backendRefreshing}
        refreshStartedAt={refreshStartedAt}
        updatedAt={updatedAt}
        lastRefreshDuration={lastRefreshDuration}
        lastRefreshError={lastRefreshError}
        platformEnabled={platformEnabled}
        platformLabels={platformLabels}
        monitorRunning={monitorRunning}
        monitorEnabled={monitorEnabled}
        monitorDryRun={monitorDryRun}
        monitorPreset={monitorPreset}
        monitorMinEv={monitorMinEv}
        monitorCooldownMin={monitorCooldownMin}
        monitorMaxPerHour={monitorMaxPerHour}
        propsEnabled={propsEnabled}
        backendUnreachableSince={backendUnreachableSince}
        degraded={degraded}
        oddsFailures={oddsFailures}
      />

      {/* Toolbar */}
      <div
        className="flex items-center justify-between px-4 py-2.5 rounded-lg border"
        style={{ background: "#0c1315", borderColor: "rgba(19,78,74,0.35)" }}
      >
        <form onSubmit={handleSubmit} className="flex items-center gap-4">
          <div className="flex items-center gap-1.5">
            <label htmlFor="min-edge" className="font-mono text-[9px]" style={{ color: "#4b5563" }}>
              Min Edge
            </label>
            <div className="relative">
              <input
                id="min-edge"
                type="number"
                step="0.5"
                min="0"
                max="100"
                value={edgeInput}
                onChange={(e) => setEdgeInput(e.target.value)}
                className="w-14 pl-2 pr-5 py-1 rounded-md border font-mono text-[11px] text-right focus:outline-none focus:border-[rgba(45,212,191,0.5)]"
                style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
              />
              <span className="absolute right-1.5 top-1/2 -translate-y-1/2 font-mono text-[9px]" style={{ color: "#374151" }}>%</span>
            </div>
          </div>

          <div className="h-3.5 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />

          <div className="flex items-center gap-1.5">
            <label htmlFor="bankroll" className="font-mono text-[9px]" style={{ color: "#4b5563" }}>
              Bankroll
            </label>
            <div className="relative">
              <span className="absolute left-2 top-1/2 -translate-y-1/2 font-mono text-[9px]" style={{ color: "#374151" }}>$</span>
              <input
                id="bankroll"
                type="number"
                step="100"
                min="0"
                value={bankrollInput}
                onChange={(e) => setBankrollInput(e.target.value)}
                className="w-20 pl-5 pr-2 py-1 rounded-md border font-mono text-[11px] text-right focus:outline-none focus:border-[rgba(45,212,191,0.5)]"
                style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
              />
            </div>
          </div>

          <div className="h-3.5 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />

          {/* Books / platform toggles */}
          <div className="flex items-center gap-2">
            <span className="font-mono text-[9px]" style={{ color: "#4b5563" }}>Books</span>
            {Object.keys(platformEnabled).length > 0 ? (
              Object.keys(platformEnabled)
                .sort()
                .map((name) => (
                  <label
                    key={name}
                    className="flex items-center gap-1 font-mono text-[10px] cursor-pointer select-none"
                    style={{ color: platformEnabled[name] ? "#e2e8f0" : "#4b5563" }}
                    title={`Toggle ${platformLabels[name] ?? name} scanning`}
                  >
                    <input
                      type="checkbox"
                      checked={!!platformEnabled[name]}
                      onChange={(e) => togglePlatform(name, e.target.checked)}
                      className="cursor-pointer"
                      style={{ accentColor: "#2dd4bf" }}
                    />
                    {platformLabels[name] ?? name}
                  </label>
                ))
            ) : null}
          </div>

          <button
            type="submit"
            disabled={scanning || !anyPlatformEnabled}
            title={!anyPlatformEnabled ? "Enable at least one book to scan" : undefined}
            className="px-4 py-1 rounded-md border font-mono text-[10px] tracking-wider uppercase transition-all duration-150"
            style={{
              color: (scanning || !anyPlatformEnabled) ? "#374151" : "#2dd4bf",
              background: (scanning || !anyPlatformEnabled) ? "rgba(75,85,99,0.05)" : "rgba(45,212,191,0.06)",
              borderColor: (scanning || !anyPlatformEnabled) ? "rgba(75,85,99,0.15)" : "rgba(45,212,191,0.2)",
            }}
          >
            {scanning ? "Scanning..." : "Scan"}
          </button>
        </form>

        <div className="flex items-center gap-4 font-mono text-[9px]" style={{ color: "#374151" }}>
          <span>
            {activeFilterCount > 0
              ? `${opportunities.length}/${allOpps.length} shown`
              : `${allOpps.length} results`}{" "}
            ({buyCount} buy, {watchCount} watch)
          </span>
          {openPositions > 0 && (
            <>
              <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
              <span style={{ color: "#f59e0b" }}>
                {openPositions} tracked
              </span>
            </>
          )}
          {quotaRemaining && (
            <>
              <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
              <span>{quotaRemaining} API req left</span>
            </>
          )}
          <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
          <span>
            {platforms.length > 0 ? platforms.map(p => p === "polymarket" ? "PM" : p === "kalshi" ? "Kalshi" : p).join(" + ") : "No platforms"}
          </span>
          <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
          <button
            onClick={async () => {
              setActionError(null);
              try {
                if (monitorRunning) await stopMonitoring();
                else await startMonitoring();
                monitor.reload();
              } catch (e: unknown) {
                setActionError(
                  `Couldn't ${monitorRunning ? "stop" : "start"} auto monitor: ${errMsg(e)}`,
                );
              }
            }}
            className="flex items-center gap-1.5 transition-all"
            title={monitorRunning ? "Stop auto-update" : "Start auto-update"}
          >
            <span
              className="w-1.5 h-1.5 rounded-full transition-all"
              style={{
                background: monitorRunning ? "#4ade80" : "#374151",
                boxShadow: monitorRunning ? "0 0 4px #4ade80" : "none",
              }}
            />
            <span style={{ color: monitorRunning ? "#4ade80" : "#374151" }}>
              Auto
            </span>
          </button>
          <div className="h-3 w-px" style={{ background: "rgba(75,85,99,0.15)" }} />
          <div className="flex items-center gap-1.5">
            <span
              className="w-1.5 h-1.5 rounded-full"
              style={{
                background: backendUnreachable ? "#f87171" : (scanning || backendRefreshing) ? "#f59e0b" : snapshotStale ? "#f59e0b" : updatedAt ? "#4ade80" : "#374151",
                boxShadow: backendUnreachable ? "0 0 4px #f87171" : (scanning || backendRefreshing) ? "0 0 4px #f59e0b" : snapshotStale ? "0 0 4px #f59e0b" : updatedAt ? "0 0 4px #4ade80" : "none",
              }}
            />
            <span
              suppressHydrationWarning
              style={freshnessWarning ? { color: backendUnreachable ? "#f87171" : "#f59e0b" } : undefined}
            >
              {backendUnreachable
                ? "Backend unreachable"
                : (scanning || backendRefreshing)
                ? "Refreshing live prices…"
                : !mounted
                  ? ""
                  : updatedAt
                    ? `${snapshotStale ? "Stale · " : ""}Last updated ${timeAgo(new Date(updatedAt * 1000))}${lastRefreshDuration != null ? ` · last scan ${lastRefreshDuration.toFixed(0)}s` : ""}`
                    : "Waiting for data..."}
            </span>
          </div>
        </div>
      </div>

      {/* Filter bar */}
      <div
        className="flex items-center gap-3 px-4 py-2 rounded-lg border"
        style={{ background: "#0c1315", borderColor: "rgba(19,78,74,0.25)" }}
      >
        <span className="font-mono text-[8px] tracking-[0.15em] uppercase shrink-0" style={{ color: "#374151" }}>
          Filter
        </span>

        {/* Search */}
        <input
          type="text"
          placeholder="Player / team..."
          value={filterSearch}
          onChange={(e) => setFilterSearch(e.target.value)}
          className="w-32 px-2 py-1 rounded border font-mono text-[11px] focus:outline-none focus:border-[rgba(45,212,191,0.5)]"
          style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
        />

        <div className="h-3.5 w-px shrink-0" style={{ background: "rgba(75,85,99,0.15)" }} />

        {/* Status */}
        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value as "all" | "BUY" | "WATCH")}
          className="px-2 py-1 rounded border font-mono text-[11px] focus:outline-none"
          style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
        >
          <option value="all">All Status</option>
          <option value="BUY">BUY only</option>
          <option value="WATCH">WATCH only</option>
        </select>

        {/* Platform */}
        {availablePlatforms.length > 1 && (
          <select
            value={filterPlatform}
            onChange={(e) => setFilterPlatform(e.target.value)}
            className="px-2 py-1 rounded border font-mono text-[11px] focus:outline-none"
            style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
          >
            <option value="all">All Platforms</option>
            {availablePlatforms.map((p) => (
              <option key={p} value={p}>{p === "polymarket" ? "Polymarket" : p === "kalshi" ? "Kalshi" : p}</option>
            ))}
          </select>
        )}

        {/* Sport */}
        {availableSports.length > 1 && (
          <select
            value={filterSport}
            onChange={(e) => setFilterSport(e.target.value)}
            className="px-2 py-1 rounded border font-mono text-[11px] focus:outline-none"
            style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
          >
            <option value="all">All Sports</option>
            {availableSports.map((s) => (
              <option key={s} value={s}>{sportLabel(s, sportsRegistry)}</option>
            ))}
          </select>
        )}

        {/* Market type */}
        {availableMarketTypes.length > 1 && (() => {
          const hasProps = availableMarketTypes.some(isPropType);
          const mainTypes = availableMarketTypes.filter((t) => !isPropType(t));
          const propTypes = availableMarketTypes.filter(isPropType);
          return (
            <select
              value={filterMarketType}
              onChange={(e) => setFilterMarketType(e.target.value)}
              className="px-2 py-1 rounded border font-mono text-[11px] focus:outline-none"
              style={{ background: "rgba(13,20,22,0.8)", borderColor: "rgba(19,78,74,0.3)", color: "#e2e8f0" }}
            >
              <option value="all">All Types</option>
              {hasProps && <option value="props">All Props</option>}
              {hasProps && <option value="main">Main Lines</option>}
              {mainTypes.length > 0 && <option disabled>───────</option>}
              {mainTypes.map((t) => (
                <option key={t} value={t}>{marketTypeLabel(t)}</option>
              ))}
              {propTypes.length > 0 && <option disabled>── Props ──</option>}
              {propTypes.map((t) => (
                <option key={t} value={t}>{marketTypeLabel(t)}</option>
              ))}
            </select>
          );
        })()}

        {/* Clear filters */}
        {activeFilterCount > 0 && (
          <button
            onClick={() => {
              setFilterStatus("all");
              setFilterPlatform("all");
              setFilterSport("all");
              setFilterMarketType("all");
              setFilterSearch("");
            }}
            title="Clear all filters"
            className="px-2 py-1 rounded border font-mono text-[10px] transition-colors"
            style={{ color: "#94a3b8", borderColor: "rgba(75,85,99,0.2)", background: "rgba(75,85,99,0.05)" }}
          >
            Clear ({activeFilterCount})
          </button>
        )}
      </div>

      {/* Backend unreachable / stale snapshot — data shown may not be live */}
      {(backendUnreachable || snapshotStale) && (
        <div
          role="alert"
          className="flex items-center gap-2 px-4 py-2 rounded-lg border font-mono text-[10px]"
          style={{
            background: backendUnreachable ? "rgba(248,113,113,0.05)" : "rgba(245,158,11,0.05)",
            borderColor: backendUnreachable ? "rgba(248,113,113,0.3)" : "rgba(245,158,11,0.3)",
            color: backendUnreachable ? "#f87171" : "#f59e0b",
          }}
          suppressHydrationWarning
        >
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: backendUnreachable ? "#f87171" : "#f59e0b" }}
            aria-hidden="true"
          />
          <span>
            {backendUnreachable
              ? `Backend unreachable since ${new Date((backendUnreachableSince ?? 0) * 1000).toLocaleTimeString()} — retrying.`
              : "Snapshot is stale."}
            {hasSnapshot && updatedAt != null
              ? ` Showing snapshot from ${fmtAgo(updatedAt)}; prices may be outdated.`
              : ""}
          </span>
        </div>
      )}

      {/* Action error (scan / books toggle / auto monitor / scan-config load) */}
      {actionError && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 px-4 py-2 rounded-lg border font-mono text-[10px]"
          style={{ background: "rgba(248,113,113,0.05)", borderColor: "rgba(248,113,113,0.3)", color: "#f87171" }}
        >
          <span>{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError(null)}
            aria-label="Dismiss error"
            className="font-mono text-[11px]"
            style={{ color: "#94a3b8" }}
          >
            ×
          </button>
        </div>
      )}

      {/* Error banner — keep table visible above/below if a snapshot exists */}
      {lastRefreshError && (
        <div
          className="flex items-center justify-between gap-3 px-4 py-2 rounded-lg border"
          style={{ background: "rgba(248,113,113,0.05)", borderColor: "rgba(248,113,113,0.3)" }}
        >
          <div className="flex items-center gap-2 font-mono text-[10px]" style={{ color: "#f87171" }}>
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: "#f87171" }} />
            <span>
              Last refresh failed: {lastRefreshError}
              {hasSnapshot ? " · Showing previous snapshot." : ""}
            </span>
          </div>
          <button
            type="button"
            onClick={runScan}
            disabled={scanning || backendRefreshing}
            className="font-mono text-[9px] tracking-wider uppercase px-2.5 py-1 rounded border"
            style={{
              color: (scanning || backendRefreshing) ? "#374151" : "#f87171",
              borderColor: "rgba(248,113,113,0.3)",
              background: "rgba(248,113,113,0.06)",
            }}
          >
            Retry
          </button>
        </div>
      )}

      {!hasSnapshot ? (
        /* No snapshot yet — cold start, first scan in flight, or empty idle */
        <div
          className="rounded-lg border px-6 py-16 text-center"
          style={{ background: "#0c1315", borderColor: "rgba(19,78,74,0.35)" }}
        >
          <div className="flex items-center justify-center gap-2 mb-3">
            <span
              className="w-2 h-2 rounded-full animate-pulse"
              style={{ background: "#f59e0b", boxShadow: "0 0 6px #f59e0b" }}
            />
            <span className="font-mono text-[12px]" style={{ color: "#f59e0b" }}>
              {(scanning || backendRefreshing) ? "First scan running" : "Waiting for data..."}
            </span>
          </div>
          {(scanning || backendRefreshing) ? (
            <>
              <p className="font-mono text-[10px]" style={{ color: "#374151" }}>
                First scan running — this can take about a minute.
              </p>
              {refreshStartedAt != null && mounted && (
                <p className="font-mono text-[9px] mt-1" style={{ color: "#4b5563" }} suppressHydrationWarning>
                  Elapsed: {Math.max(0, Math.floor(Date.now() / 1000 - refreshStartedAt))}s
                </p>
              )}
            </>
          ) : !anyPlatformEnabled ? (
            <p className="font-mono text-[10px]" style={{ color: "#f87171" }}>
              Enable at least one book (Kalshi or Polymarket) above to scan.
            </p>
          ) : (
            <p className="font-mono text-[10px]" style={{ color: "#374151" }}>
              Click <strong style={{ color: "#2dd4bf" }}>Scan</strong> above to trigger the first refresh.
            </p>
          )}
        </div>
      ) : (
        <>
          {/* Data source status */}
          <DataStatus />

          {/* Notification settings */}
          <NotificationSettings
            config={monitor.config}
            status={monitor.status}
            onReload={monitor.reload}
          />

          {/* Paper Maker — read-only audit + safe enable/disable */}
          <PaperMakerControls scanRefreshKey={scanRefreshKey} />

          {/* Main grid: table + sidebar */}
          <div className="grid gap-3" style={{ gridTemplateColumns: "1fr 320px" }}>
            <div className="space-y-3">
              <OpportunitiesTable
                opportunities={opportunities}
                selectedId={displayed?.market_id}
                onSelect={(opp) => setSelected(opp)}
                bankroll={bankroll}
                isTaken={isTaken}
                sportsRegistry={sportsRegistry}
                isRefreshing={scanning || backendRefreshing}
              />
            </div>
            <div className="space-y-3">
              <MarketDetailPanel
                opportunity={displayed}
                bankroll={bankroll}
                isTaken={displayedIsTaken}
                onTake={() => displayed && takePosition(displayed)}
                sportsRegistry={sportsRegistry}
              />
            </div>
          </div>

          {/* Tracked trades */}
          {positions.length > 0 && (
            <TrackedPositions
              positions={positions}
              onClose={closePosition}
              onRemove={removePosition}
              sportsRegistry={sportsRegistry}
            />
          )}
        </>
      )}
    </main>
  );
}
