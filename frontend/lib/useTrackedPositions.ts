"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { Opportunity, TrackedPosition } from "./types";
import { fetchPriceHistory, fetchHistoricalOdds } from "./api";

// Storage keys keep their original prefix so existing tracked positions in
// users' browsers are not lost.
const STORAGE_KEY = "masg_tracked_positions";
// Bump this when CLV calculation logic changes to force recomputation.
const CLV_VERSION = 4;  // v4: entry_ev + CLV = fd_close_prob - entry_price
const CLV_VERSION_KEY = "masg_clv_version";

/**
 * Find the closing-line price from a price history, adjusting for side.
 *
 * CLV = close_price - entry_price.  The "closing line" is the last market
 * price BEFORE the event starts — not in-game prices, not settlement prices.
 *
 * When start_time is available: filters to points strictly before event start,
 * then takes the last one.  This excludes in-game and settlement prices.
 *
 * When start_time is null (legacy positions): falls back to skipping
 * settlement-level prices (>= 0.95 / <= 0.05) and taking the last
 * remaining point — an approximation that may include late in-game prices.
 */
function resolveClosePrice(
  points: { t: number; p: number }[],
  pos: TrackedPosition,
): number | null {
  if (points.length === 0) return null;

  let price: number | null = null;

  // Parse start_time once
  const startUnix = pos.start_time
    ? Math.floor(new Date(pos.start_time).getTime() / 1000)
    : null;

  if (startUnix && startUnix > 0) {
    // Primary path: use only pre-event prices
    // Walk backward to find the last point strictly before event start
    for (let i = points.length - 1; i >= 0; i--) {
      if (points[i].t < startUnix) {
        price = points[i].p;
        break;
      }
    }
  }

  if (price == null) {
    // Fallback: no start_time or no pre-start points found.
    // Find the last stable pre-game price by detecting where the
    // in-game/settlement price drift begins.
    // Walk backward: a "stable" point is one in valid range (0.05-0.95)
    // that is NOT followed by a large jump (>10%) to the next candle.
    // This skips the entire in-game sequence, not just settlement.
    for (let i = points.length - 2; i >= 0; i--) {
      const p = points[i].p;
      const next = points[i + 1].p;
      if (p > 0.05 && p < 0.95 && Math.abs(next - p) <= 0.10) {
        price = p;
        break;
      }
    }
    // If every consecutive pair has a big jump (unlikely), take the first valid point
    if (price == null) {
      for (let i = 0; i < points.length; i++) {
        if (points[i].p > 0.05 && points[i].p < 0.95) {
          price = points[i].p;
          break;
        }
      }
    }
  }

  // Last resort fallbacks
  if (price == null && points.length >= 2) {
    price = points[points.length - 2].p;
  }
  if (price == null) {
    price = points[points.length - 1].p;
  }

  // For Kalshi 2-way markets, market_id is always M1's ticker.
  // The price history is for M1's outcome. If the tracked side is M2,
  // the history price is the complement.
  if (needsInversion(pos, price)) {
    price = 1.0 - price;
  }

  return round4(price);
}

/**
 * Detect if the tracked position is the opposite side of the stored market_id.
 *
 * The market_id ticker suffix (e.g., "ATL", "NYI") identifies which team the
 * price history tracks. We check if the suffix matches the tracked side name:
 *
 *   1. Word-prefix: "dal" → "dallas" (startsWith)
 *   2. Initials: "nyi" → first letters of "New York Islanders"
 *
 * If either matches → same side (no inversion).
 * If neither matches → opposite side → invert.
 */
function needsInversion(
  pos: TrackedPosition,
  historyClosingPrice: number,
): boolean {
  if (pos.platform !== "kalshi") return false;

  const parts = pos.market_id.split("-");
  if (parts.length < 2) return false;
  const suffix = parts[parts.length - 1].toLowerCase();
  const sideWords = pos.side.toLowerCase().split(/\s+/);

  // Strategy 1: word-prefix match
  // "dal" → "dallas", "blo" → "blockx", "atl" → "atlanta"
  const wordMatch = sideWords.some(
    (w) => w.startsWith(suffix) || suffix.startsWith(w.slice(0, 3)),
  );
  if (wordMatch) return false;

  // Strategy 2: initials match — suffix matches first letters of consecutive words
  // "nyi" → N(ew) Y(ork) I(slanders), "nyr" → N(ew) Y(ork) R(angers)
  for (let start = 0; start <= sideWords.length - suffix.length; start++) {
    const initials = sideWords
      .slice(start, start + suffix.length)
      .map((w) => w[0])
      .join("");
    if (initials === suffix) return false;
  }

  // No match → this market tracks the opponent → invert
  return true;
}

function round4(n: number): number {
  return Math.round(n * 10000) / 10000;
}

function americanToImplied(odds: number): number {
  if (odds >= 0) return 100.0 / (odds + 100.0);
  return Math.abs(odds) / (Math.abs(odds) + 100.0);
}

function devigTwo(homeImpl: number, awayImpl: number): [number, number] {
  const or_ = homeImpl + awayImpl;
  if (or_ <= 0) return [0.5, 0.5];
  return [round4(homeImpl / or_), round4(awayImpl / or_)];
}

function devigThree(hImpl: number, aImpl: number, dImpl: number): [number, number, number] {
  const or_ = hImpl + aImpl + dImpl;
  if (or_ <= 0) return [0.3333, 0.3333, 0.3334];
  return [round4(hImpl / or_), round4(aImpl / or_), round4(dImpl / or_)];
}

/**
 * Resolve FanDuel closing-line CLV for a tracked position.
 *
 * Fetches the historical FanDuel odds snapshot at or before start_time,
 * finds the correct side, devigs, and returns { fd_close_odds, fd_close_prob, clv_prob }.
 */
async function resolveFdClv(
  pos: TrackedPosition,
): Promise<{ fd_close_odds: number; fd_close_prob: number; clv_prob: number } | null> {
  if (!pos.start_time || !pos.sport || !pos.matched_event_id) return null;

  const snapshot = await fetchHistoricalOdds(pos.sport, pos.matched_event_id, pos.start_time);
  if (!snapshot) return null;

  // Determine which side of the snapshot matches this position
  const sideLower = pos.side.toLowerCase();
  const homeLower = snapshot.home_team.toLowerCase();
  const awayLower = snapshot.away_team.toLowerCase();

  let isHome: boolean | null = null;
  if (sideLower.includes(homeLower) || homeLower.includes(sideLower)) {
    isHome = true;
  } else if (sideLower.includes(awayLower) || awayLower.includes(sideLower)) {
    isHome = false;
  } else {
    // Try word matching
    const sideWords = sideLower.split(/\s+/);
    const homeWords = homeLower.split(/\s+/);
    const awayWords = awayLower.split(/\s+/);
    const homeOverlap = sideWords.filter((w) => w.length >= 4 && homeWords.some((hw) => hw.startsWith(w) || w.startsWith(hw))).length;
    const awayOverlap = sideWords.filter((w) => w.length >= 4 && awayWords.some((aw) => aw.startsWith(w) || w.startsWith(aw))).length;
    if (homeOverlap > awayOverlap) isHome = true;
    else if (awayOverlap > homeOverlap) isHome = false;
  }

  if (isHome == null) return null;

  const closeOdds = isHome ? snapshot.home_odds : snapshot.away_odds;

  // Devig to get true closing probability
  let closeProb: number;
  if (snapshot.draw_odds != null) {
    const [pH, pA] = devigThree(
      americanToImplied(snapshot.home_odds),
      americanToImplied(snapshot.away_odds),
      americanToImplied(snapshot.draw_odds),
    );
    closeProb = isHome ? pH : pA;
  } else {
    const [pH, pA] = devigTwo(
      americanToImplied(snapshot.home_odds),
      americanToImplied(snapshot.away_odds),
    );
    closeProb = isHome ? pH : pA;
  }

  // CLV = FD closing devigged prob - prediction market entry price
  // Positive = you bought cheaper than the sharp closing line
  const clvProb = round4(closeProb - pos.entry_price);

  return {
    fd_close_odds: closeOdds,
    fd_close_prob: closeProb,
    clv_prob: clvProb,
  };
}

function loadPositions(): TrackedPosition[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    let positions: TrackedPosition[] = raw ? JSON.parse(raw) : [];

    // CLV version migration: when calculation logic changes, clear
    // close_price on all positions so the backfill recomputes them.
    const storedVersion = Number(localStorage.getItem(CLV_VERSION_KEY) || "0");
    if (storedVersion < CLV_VERSION && positions.length > 0) {
      positions = positions.map((p) => ({
        ...p,
        close_price: null,
        entry_ev: p.p_true && p.entry_price ? round4(p.p_true - p.entry_price) : null,
        fd_close_odds: null,
        fd_close_prob: null,
        clv_prob: null,
      }));
      localStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
      localStorage.setItem(CLV_VERSION_KEY, String(CLV_VERSION));
    }

    return positions;
  } catch {
    return [];
  }
}

function savePositions(positions: TrackedPosition[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
    localStorage.setItem(CLV_VERSION_KEY, String(CLV_VERSION));
  } catch {
    // storage full or unavailable
  }
}

function positionId(opp: Opportunity): string {
  return `${opp.platform}:${opp.market_id}:${opp.side}`;
}

export function useTrackedPositions() {
  const [positions, setPositions] = useState<TrackedPosition[]>([]);

  // Load from localStorage on mount
  useEffect(() => {
    setPositions(loadPositions());
  }, []);

  // One-time backfill: recover close_price for finished positions.
  // Targets two cases:
  //   1. Closed positions missing close_price (pre-CLV or failed fetch)
  //   2. Open positions whose market has settled (auto-close)
  const backfillRan = useRef(false);
  useEffect(() => {
    if (backfillRan.current || positions.length === 0) return;
    const candidates = positions.filter(
      (p) =>
        (p.status === "closed" && p.close_price == null) ||
        p.status === "open",
    );
    if (candidates.length === 0) return;
    backfillRan.current = true;

    (async () => {
      type PosUpdate = {
        close_price?: number;
        fd_close_odds?: number;
        fd_close_prob?: number;
        clv_prob?: number;
        autoClose?: boolean;
      };
      const updates = new Map<string, PosUpdate>();

      for (const pos of candidates) {
        const upd: PosUpdate = {};

        // Platform close_price (existing behavior)
        try {
          const history = await fetchPriceHistory(pos.platform, pos.market_id);
          if (history.points.length > 0) {
            const lastPrice = history.points[history.points.length - 1].p;
            const isSettled = lastPrice >= 0.95 || lastPrice <= 0.05;

            if (pos.status === "open" && isSettled) {
              const cp = resolveClosePrice(history.points, pos);
              if (cp != null) { upd.close_price = cp; upd.autoClose = true; }
            } else if (pos.status === "closed" && pos.close_price == null) {
              const cp = resolveClosePrice(history.points, pos);
              if (cp != null) upd.close_price = cp;
            }
          }
        } catch { /* skip platform history */ }

        // FanDuel closing-line CLV (new)
        if (pos.fd_close_prob == null) {
          try {
            const fdClv = await resolveFdClv(pos);
            if (fdClv) {
              upd.fd_close_odds = fdClv.fd_close_odds;
              upd.fd_close_prob = fdClv.fd_close_prob;
              upd.clv_prob = fdClv.clv_prob;
            }
          } catch { /* skip FD history */ }
        }

        if (Object.keys(upd).length > 0) {
          updates.set(pos.id, upd);
        }
      }

      if (updates.size === 0) return;
      setPositions((prev) => {
        const next = prev.map((p) => {
          const upd = updates.get(p.id);
          if (!upd) return p;
          const merged = { ...p };
          if (upd.close_price != null) merged.close_price = upd.close_price;
          if (upd.fd_close_odds != null) merged.fd_close_odds = upd.fd_close_odds;
          if (upd.fd_close_prob != null) merged.fd_close_prob = upd.fd_close_prob;
          if (upd.clv_prob != null) merged.clv_prob = upd.clv_prob;
          if (upd.autoClose) merged.status = "closed";
          return merged;
        });
        savePositions(next);
        return next;
      });
    })();
  }, [positions]);

  const takenIds = new Set(positions.map((p) => p.id));

  const isTaken = useCallback(
    (opp: Opportunity) => takenIds.has(positionId(opp)),
    [takenIds],
  );

  const takePosition = useCallback(
    (opp: Opportunity) => {
      const id = positionId(opp);
      if (takenIds.has(id)) return;

      const pos: TrackedPosition = {
        id,
        market_id: opp.market_id,
        platform: opp.platform,
        sport: opp.sport,
        event: opp.event,
        event_url: opp.event_url,
        market_type: opp.market_type,
        side: opp.side,
        line: opp.line,
        entry_price: opp.pm_price,
        entry_edge: opp.edge,
        entry_kelly: opp.recommended_kelly,
        fd_odds: opp.fd_odds,
        p_true: opp.p_true,
        matched_event_id: opp.matched_event_id || "",
        taken_at: new Date().toISOString(),
        start_time: opp.start_time || null,
        status: "open",
        close_price: null,
        entry_ev: round4(opp.p_true - opp.pm_price),
        fd_close_odds: null,
        fd_close_prob: null,
        clv_prob: null,
      };

      const next = [pos, ...positions];
      setPositions(next);
      savePositions(next);
    },
    [positions, takenIds],
  );

  const closePosition = useCallback(
    async (id: string, force?: boolean): Promise<{ ok: boolean }> => {
      const pos = positions.find((p) => p.id === id);
      if (!pos || pos.status !== "open") return { ok: false };

      // Fetch platform close price
      let closePrice: number | null = null;
      let fetchFailed = false;
      try {
        const history = await fetchPriceHistory(pos.platform, pos.market_id);
        closePrice = resolveClosePrice(history.points, pos);
      } catch {
        fetchFailed = true;
      }

      // Fetch FanDuel closing-line CLV
      let fdCloseOdds: number | null = null;
      let fdCloseProb: number | null = null;
      let clvProb: number | null = null;
      try {
        const fdClv = await resolveFdClv(pos);
        if (fdClv) {
          fdCloseOdds = fdClv.fd_close_odds;
          fdCloseProb = fdClv.fd_close_prob;
          clvProb = fdClv.clv_prob;
        }
      } catch { /* skip */ }

      if (fetchFailed && !force) {
        return { ok: false };
      }

      const next = positions.map((p) =>
        p.id === id
          ? {
              ...p,
              status: "closed" as const,
              close_price: closePrice,
              fd_close_odds: fdCloseOdds,
              fd_close_prob: fdCloseProb,
              clv_prob: clvProb,
            }
          : p,
      );
      setPositions(next);
      savePositions(next);
      return { ok: true };
    },
    [positions],
  );

  const removePosition = useCallback(
    (id: string) => {
      const next = positions.filter((p) => p.id !== id);
      setPositions(next);
      savePositions(next);
    },
    [positions],
  );

  return { positions, isTaken, takePosition, closePosition, removePosition };
}
