"use client";

import type { CSSProperties } from "react";
import { Opportunity } from "@/lib/types";
import { sportLabel, marketTypeLabel, isPropType, SportRegistryEntry } from "@/lib/sport-labels";
import {
  fmtPct,
  fmtAge,
  ageColor,
  fmtOdds,
  fmtTime,
  platformLabel,
  platformColor,
  edgeColor,
  STATUS_TONE,
} from "@/lib/opportunity-format";
import { Badge } from "../ui/Badge";
import { StatusPill } from "../ui/StatusPill";

// Hover tints applied via per-row CSS custom properties so the row's
// hover/focus styles stay in CSS (no React mouseenter handlers).
interface RowVars extends CSSProperties {
  "--link-hover-bg"?: string;
  "--link-hover-border"?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface OpportunityRowProps {
  opp: Opportunity;
  index: number;
  isSelected: boolean;
  isTaken: boolean;
  bankroll: number;
  sportsRegistry?: Record<string, SportRegistryEntry>;
  /** Backend-driven nowSec for age; 0 before mount (renders "?"). */
  nowSec: number;
  /** Refresh in flight — age column shows "…" instead of stale seconds. */
  isRefreshing: boolean;
  onSelect: (opp: Opportunity) => void;
}

export function OpportunityRow({
  opp,
  index,
  isSelected,
  isTaken,
  bankroll,
  sportsRegistry,
  nowSec,
  isRefreshing,
  onSelect,
}: OpportunityRowProps) {
  const ss = STATUS_TONE[opp.status] ?? STATUS_TONE.SKIP;
  const pColor = platformColor(opp.platform);
  const isProp = isPropType(opp.market_type);
  const isTotal = opp.market_type === "totals";

  const rowBg = isSelected
    ? "var(--accent-soft)"
    : isTaken
      ? "rgba(245,158,11,0.03)"
      : index % 2 === 0
        ? "transparent"
        : "var(--bg-overlay)";

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(opp);
    }
  };

  return (
    <tr
      onClick={() => onSelect(opp)}
      onKeyDown={handleKeyDown}
      tabIndex={0}
      role="button"
      aria-label={`Select ${opp.event} · ${opp.side} · ${opp.status}`}
      aria-pressed={isSelected}
      className={
        "cursor-pointer transition-colors duration-100 " +
        "hover:bg-[var(--accent-soft)] " +
        "focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:[outline-color:var(--accent)] focus-visible:[outline-offset:-2px]"
      }
      style={{
        // Row divider intentionally fainter than var(--border-subtle) so
        // the data grid doesn't get over-segmented.
        borderBottom: "1px solid rgba(19,78,74,0.08)",
        borderLeft: isTaken ? "2px solid var(--warn)" : "2px solid transparent",
        background: rowBg,
      }}
    >
      {/* Event */}
      <td className="px-3 py-3" style={{ minWidth: 220 }}>
        <p
          className="font-mono leading-snug"
          style={{ fontSize: "11px", color: "var(--fg-primary)" }}
        >
          {opp.event}
        </p>
        <div className="flex items-center gap-2 mt-1">
          <Badge label={sportLabel(opp.sport, sportsRegistry)} color="var(--platform-fanduel)" />
          <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-ghost)" }}>
            {opp.tournament}
          </span>
          <Badge label={platformLabel(opp.platform)} color={pColor} />
        </div>
      </td>

      {/* Type */}
      <td className="px-3 py-3">
        {isProp ? (
          <Badge label="Prop" color="var(--platform-polymarket)" />
        ) : isTotal ? (
          <Badge label="Total" variant="warn" />
        ) : (
          <Badge label="H2H" color="var(--fg-muted)" />
        )}
      </td>

      {/* Detail */}
      <td className="px-3 py-3">
        {isProp ? (
          <span className="font-mono" style={{ fontSize: "11px", color: "var(--fg-primary)" }}>
            {opp.side.replace(/\s+(Over|Under)$/i, "")}{" "}
            <span style={{ color: "var(--platform-polymarket)" }}>{marketTypeLabel(opp.market_type)}</span>{" "}
            <span style={{ color: "var(--fg-secondary)" }}>{opp.line}</span>
          </span>
        ) : isTotal ? (
          <span className="font-mono" style={{ fontSize: "11px", color: "var(--fg-primary)" }}>
            O/U <span style={{ color: "var(--warn-strong)" }}>{opp.line}</span>
          </span>
        ) : (
          <span className="font-mono" style={{ fontSize: "11px", color: "var(--fg-faint)" }}>
            Moneyline
          </span>
        )}
      </td>

      {/* Side */}
      <td className="px-3 py-3">
        {isProp || isTotal ? (
          <span
            className="font-mono font-medium"
            style={{
              fontSize: "11px",
              color: opp.side.toLowerCase().includes("over")
                ? "var(--accent)"
                : "var(--danger-strong)",
            }}
          >
            {opp.side.toLowerCase().includes("over")
              ? "Over"
              : opp.side.toLowerCase().includes("under")
                ? "Under"
                : opp.side}
          </span>
        ) : (
          <>
            <span
              className="font-mono font-medium"
              style={{ fontSize: "11px", color: "var(--fg-primary)" }}
            >
              {opp.side}
            </span>
            {opp.line != null && (
              <span
                className="font-mono ml-1"
                style={{ fontSize: "10px", color: "var(--fg-muted)" }}
              >
                {opp.line > 0 ? `+${opp.line}` : opp.line}
              </span>
            )}
          </>
        )}
      </td>

      {/* Start */}
      <td
        className="px-3 py-3 font-mono whitespace-nowrap"
        style={{ fontSize: "11px", color: "var(--fg-faint)" }}
      >
        {fmtTime(opp.start_time)}
      </td>

      {/* Price */}
      <td
        className="px-3 py-3 text-right font-mono"
        style={{ fontSize: "11px", color: "var(--fg-secondary)" }}
      >
        {(opp.pm_price * 100).toFixed(1)}¢
      </td>

      {/* Age */}
      <td
        className="px-3 py-3 text-center font-mono"
        style={{
          fontSize: "10px",
          color: isRefreshing ? "var(--fg-faint)" : ageColor(opp.price_fetched_at, nowSec),
        }}
        title={isRefreshing ? "Price age hidden while scan is running" : undefined}
      >
        {isRefreshing ? "…" : fmtAge(opp.price_fetched_at, nowSec)}
      </td>

      {/* FD Odds */}
      <td
        className="px-3 py-3 text-right font-mono"
        style={{ fontSize: "11px", color: "var(--fg-muted)" }}
      >
        {fmtOdds(opp.fd_odds)}
      </td>

      {/* Model probability (devigged FanDuel) */}
      <td
        className="px-3 py-3 text-right font-mono"
        style={{ fontSize: "11px", color: "var(--fg-primary)" }}
      >
        {fmtPct(opp.p_true)}
      </td>

      {/* Edge */}
      <td className="px-3 py-3 text-right">
        <span
          className="font-mono font-semibold"
          style={{ fontSize: "12px", color: edgeColor(opp.edge) }}
        >
          +{fmtPct(opp.edge)}
        </span>
      </td>

      {/* Kelly */}
      <td
        className="px-3 py-3 text-right font-mono"
        style={{ fontSize: "11px", color: "var(--info)" }}
      >
        {opp.recommended_kelly > 0 ? fmtPct(opp.recommended_kelly) : "--"}
      </td>

      {/* Bet Size */}
      <td
        className="px-3 py-3 text-right font-mono"
        style={{
          fontSize: "11px",
          color: opp.recommended_kelly > 0 ? "var(--fg-primary)" : "var(--fg-ghost)",
        }}
      >
        {opp.recommended_kelly > 0
          ? `$${Math.round(bankroll * opp.recommended_kelly).toLocaleString()}`
          : "--"}
      </td>

      {/* Status */}
      <td className="px-3 py-3 text-center">
        <div className="inline-flex items-center gap-1">
          <StatusPill
            value={opp.status}
            tone={ss.tone}
            variant={ss.variant}
            size="xs"
            showDot={false}
            title={ss.title}
          />
          {isTaken && <Badge label="Taken" variant="warn" />}
        </div>
      </td>

      {/* Trade link */}
      <td className="px-3 py-3 text-center">
        {opp.event_url ? (
          <a
            href={opp.event_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className={
              "inline-flex items-center gap-1.5 font-mono px-2.5 py-1 rounded-md border transition-colors duration-150 " +
              "hover:[background:var(--link-hover-bg)] hover:[border-color:var(--link-hover-border)] " +
              "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
            }
            style={{
              fontSize: "10px",
              color: pColor,
              background: `color-mix(in oklab, ${pColor} 8%, transparent)`,
              borderColor: `color-mix(in oklab, ${pColor} 25%, transparent)`,
              borderRadius: "var(--radius-md)",
              "--link-hover-bg": `color-mix(in oklab, ${pColor} 18%, transparent)`,
              "--link-hover-border": `color-mix(in oklab, ${pColor} 50%, transparent)`,
            } as RowVars}
          >
            View
            <svg width="8" height="8" viewBox="0 0 10 10" fill="none" style={{ opacity: 0.7 }} aria-hidden="true">
              <path
                d="M1 9L9 1M9 1H3M9 1V7"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </a>
        ) : (
          <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-disabled)" }}>
            --
          </span>
        )}
      </td>
    </tr>
  );
}
