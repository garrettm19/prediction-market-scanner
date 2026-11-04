import { BACKEND_URL, apiFetch } from "./api";

export interface MonitorConfig {
  enabled: boolean;
  preset: string;
  min_ev: number;
  platforms: string[];
  market_types: string[];
  cooldown_minutes: number;
  max_alerts_per_hour: number;
  refresh_interval_minutes: number;
  dry_run: boolean;
  has_pushover_credentials: boolean;
}

export interface MonitorStatus {
  running: boolean;
  enabled: boolean;
  last_cycle_ts: number;
  total_cycles: number;
  total_alerts_sent: number;
  odds_poller: Record<string, unknown>;
  polymarket_ws: Record<string, unknown>;
  kalshi_ws: Record<string, unknown>;
  last_cycle_result: Record<string, unknown> | null;
}

export async function fetchMonitorConfig(): Promise<MonitorConfig> {
  const res = await apiFetch(`${BACKEND_URL}/api/monitor/config`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function updateMonitorConfig(
  updates: Record<string, unknown>,
): Promise<MonitorConfig> {
  const res = await apiFetch(`${BACKEND_URL}/api/monitor/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Backend error: ${res.status}`);
  }
  return res.json();
}

// POST /api/monitor/start does not respond until the scheduler's initial
// "startup" pipeline run (a full snapshot refresh, possibly queued behind an
// in-flight manual scan) has finished, so the default 15s timeout would abort
// a start that the backend still completes. Allow it much longer.
export const MONITOR_START_TIMEOUT_MS = 300_000;

export async function startMonitoring(): Promise<{ status: string }> {
  const res = await apiFetch(
    `${BACKEND_URL}/api/monitor/start`,
    { method: "POST" },
    MONITOR_START_TIMEOUT_MS,
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Backend error: ${res.status}`);
  }
  return res.json();
}

export async function stopMonitoring(): Promise<{ status: string }> {
  const res = await apiFetch(`${BACKEND_URL}/api/monitor/stop`, { method: "POST" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function fetchMonitorStatus(): Promise<MonitorStatus> {
  const res = await apiFetch(`${BACKEND_URL}/api/monitor/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Backend error: ${res.status}`);
  return res.json();
}

export async function testNotification(): Promise<{ sent: boolean }> {
  const res = await apiFetch(`${BACKEND_URL}/api/monitor/test`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Backend error: ${res.status}`);
  }
  return res.json();
}
