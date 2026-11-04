import {
  MakerConfig,
  MakerConfigUpdate,
  MakerProposalsResponse,
  MakerSummary,
  OpportunitiesResponse,
  ScanConfigResponse,
  SnapshotStatus,
  SportDataStatus,
} from "./types";
import type { SportRegistryEntry } from "./sport-labels";

export const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

// Default per-request timeout. One hung request (backend accepted the TCP
// connection but never answers) must not stall the dashboard poll loop
// forever: the request is aborted and the caller's catch path runs.
export const API_TIMEOUT_MS = 15_000;

/**
 * fetch() with an AbortController-based timeout. If the caller passes its
 * own ``signal``, aborting either one aborts the request. On timeout the
 * returned promise rejects with an Error whose message says so.
 */
export async function apiFetch(
  input: string,
  init: RequestInit = {},
  timeoutMs: number = API_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const outer = init.signal;
  const onOuterAbort = () => controller.abort();
  if (outer) {
    if (outer.aborted) controller.abort();
    else outer.addEventListener("abort", onOuterAbort, { once: true });
  }
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (e) {
    if (timedOut) {
      throw new Error(`Request timed out after ${timeoutMs / 1000}s`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
    if (outer) outer.removeEventListener("abort", onOuterAbort);
  }
}

export interface PriceHistoryResponse {
  platform: string;
  market_id: string;
  points: { t: number; p: number }[];
  count: number;
}

export async function fetchPriceHistory(
  platform: string,
  marketId: string,
): Promise<PriceHistoryResponse> {
  const url = new URL(`${BACKEND_URL}/api/opportunities/history`);
  url.searchParams.set("platform", platform);
  url.searchParams.set("market_id", marketId);
  const res = await apiFetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchOpportunities(params: {
  platform?: string;
  include_watch?: boolean;
} = {}): Promise<OpportunitiesResponse> {
  const url = new URL(`${BACKEND_URL}/api/opportunities`);
  if (params.platform != null) url.searchParams.set("platform", params.platform);
  if (params.include_watch != null) url.searchParams.set("include_watch", String(params.include_watch));
  const res = await apiFetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchSnapshotStatus(): Promise<SnapshotStatus> {
  const res = await apiFetch(`${BACKEND_URL}/api/opportunities/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchSnapshot(): Promise<OpportunitiesResponse | null> {
  const res = await apiFetch(`${BACKEND_URL}/api/opportunities/snapshot`, { cache: "no-store" });
  if (res.status === 204) return null;
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function refreshOpportunities(): Promise<SnapshotStatus> {
  const res = await apiFetch(`${BACKEND_URL}/api/opportunities/refresh`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Backend error: ${res.status}`);
  }
  return res.json();
}

export async function fetchSportsRegistry(): Promise<Record<string, SportRegistryEntry>> {
  const res = await apiFetch(`${BACKEND_URL}/api/sports`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchDataStatus(): Promise<SportDataStatus[]> {
  const res = await apiFetch(`${BACKEND_URL}/api/data-status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchScanConfig(): Promise<ScanConfigResponse> {
  const res = await apiFetch(`${BACKEND_URL}/api/scan/config`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function updateScanConfig(updates: Record<string, unknown>): Promise<ScanConfigResponse> {
  const res = await apiFetch(`${BACKEND_URL}/api/scan/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export interface HistoricalOddsSnapshot {
  event_id: string;
  home_team: string;
  away_team: string;
  home_odds: number;
  away_odds: number;
  draw_odds: number | null;
  snapshot_time: string;
  home_implied: number;
  away_implied: number;
}

// ---------------------------------------------------------------------------
// Maker proposals — read-only paper audit records.
// The backend never exposes order placement; these endpoints surface the
// paper proposals produced by the gated paper-maker pipeline.
// ---------------------------------------------------------------------------

export async function fetchMakerProposals(params: {
  days?: number;
  market_id?: string;
  eligible?: boolean;
  // When true, the server narrows to the most recent scan's run_id only.
  // Use this for live dashboard views where the audit-trail noise from
  // multiple scans is undesirable.
  latest_run?: boolean;
  // Server clamps to [1, 1000]; default 100.
  limit?: number;
} = {}): Promise<MakerProposalsResponse> {
  const url = new URL(`${BACKEND_URL}/api/maker/proposals`);
  if (params.days != null) url.searchParams.set("days", String(params.days));
  if (params.market_id != null) url.searchParams.set("market_id", params.market_id);
  if (params.eligible != null) url.searchParams.set("eligible", String(params.eligible));
  if (params.latest_run != null) url.searchParams.set("latest_run", String(params.latest_run));
  if (params.limit != null) url.searchParams.set("limit", String(params.limit));
  const res = await apiFetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

// Runtime maker config — read.  Always returns the current safety-clamped
// values regardless of who set them.
export async function fetchMakerConfig(): Promise<MakerConfig> {
  const res = await apiFetch(`${BACKEND_URL}/api/maker/config`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

// Runtime maker config — write.  Only ``enabled`` and
// ``min_estimated_maker_edge`` are accepted by the typed update; the
// backend additionally enforces paper-mode safety invariants on every
// write so this endpoint cannot enable live trading or Polymarket.
export async function updateMakerConfig(update: MakerConfigUpdate): Promise<MakerConfig> {
  const res = await apiFetch(`${BACKEND_URL}/api/maker/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

// Aggregated maker summary.  ``latest_run=true`` narrows to the most
// recent scan's records only — what the dashboard wants by default.
export async function fetchMakerSummary(params: {
  days?: number;
  latest_run?: boolean;
} = {}): Promise<MakerSummary> {
  const url = new URL(`${BACKEND_URL}/api/maker/summary`);
  if (params.days != null) url.searchParams.set("days", String(params.days));
  if (params.latest_run != null) url.searchParams.set("latest_run", String(params.latest_run));
  const res = await apiFetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchHistoricalOdds(
  sportKey: string,
  eventId: string,
  date: string,
): Promise<HistoricalOddsSnapshot | null> {
  const url = new URL(`${BACKEND_URL}/api/opportunities/historical-odds`);
  url.searchParams.set("sport_key", sportKey);
  url.searchParams.set("event_id", eventId);
  url.searchParams.set("date", date);
  const res = await apiFetch(url.toString(), { cache: "no-store" });
  if (!res.ok) return null;
  const body = await res.json();
  return body.snapshot ?? null;
}

