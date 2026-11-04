"use client";

import type { CSSProperties, KeyboardEvent } from "react";
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

interface CardVars extends CSSProperties {
  "--link-hover-bg"?: string;
  "--link-hover-border"?: string;
}

interface OpportunityCardProps {
  opp: Opportunity;
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

interface MetricProps {
  label: string;
  value: string;
  valueColor?: string;
  title?: string;
}

function Metric({ label, value, valueColor = "var(--fg-primary)", title }: MetricProps) {
  return (
    <div title={title}>
      <p
        className="font-mono uppercase tracking-[0.12em]"
        style={{ fontSize: "8px", color: "var(--fg-faint)" }}
      >
        {label}
      </p>
      <p
        className="font-mono"
        style={{ fontSize: "11px", color: valueColor, fontVariantNumeric: "tabular-nums" }}
      >
        {value}
      </p>
    </div>
  );
}

export function OpportunityCard({
  opp,
  isSelected,
  isTaken,
  bankroll,
  sportsRegistry,
  nowSec,
  isRefreshing,
  onSelect,
}: OpportunityCardProps) {
  const ss = STATUS_TONE[opp.status] ?? STATUS_TONE.SKIP;
  const pColor = platformColor(opp.platform);
  const isProp = isPropType(opp.market_type);
  const isTotal = opp.market_type === "totals";
  const hasKelly = opp.recommended_kelly > 0;

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(opp);
    }
  };

  // Detail string (per market type)
  const detailString = isProp
    ? `${opp.side.replace(/\s+(Over|Under)$/i, "")} · ${marketTypeLabel(opp.market_type)} ${opp.line ?? ""}`
    : isTotal
      ? `O/U ${opp.line ?? ""}`
      : "Moneyline";

  // Side display + color (Over/Under for prop+total markets)
  const sideLower = opp.side.toLowerCase();
  const sideDisplay = isProp || isTotal
    ? sideLower.includes("over")
      ? "Over"
      : sideLower.includes("under")
        ? "Under"
        : opp.side
    : opp.side;
  const sideColor = isProp || isTotal
    ? sideLower.includes("over")
      ? "var(--accent)"
      : "var(--danger-strong)"
    : "var(--fg-primary)";

  // Background / border treatment — selected wins over taken wins over default
  const bg = isSelected
    ? "var(--accent-soft)"
    : isTaken
      ? "rgba(245,158,11,0.04)"
      : "var(--bg-overlay)";
  const borderColor = isSelected
    ? "var(--accent-border)"
    : isTaken
      ? "var(--warn-border)"
      : "var(--border-subtle)";

  return (
    <div
      onClick={() => onSelect(opp)}
      onKeyDown={handleKeyDown}
      tabIndex={0}
      role="button"
      aria-pressed={isSelected}
      aria-label={`Select ${opp.event} · ${opp.side} · ${opp.status}`}
      className={
        "border cursor-pointer transition-colors duration-100 " +
        "hover:bg-[var(--accent-soft)] " +
        "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
      }
      style={{
        background: bg,
        borderTopColor: borderColor,
        borderRightColor: borderColor,
        borderBottomColor: borderColor,
        borderLeftColor: isTaken ? "var(--warn)" : borderColor,
        borderLeftWidth: isTaken ? "2px" : undefined,
        borderRadius: "var(--radius-lg)",
        padding: "12px",
      }}
    >
      {/* Header: tags + status pill */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <Badge label={sportLabel(opp.sport, sportsRegistry)} color="var(--platform-fanduel)" />
            <Badge label={platformLabel(opp.platform)} color={pColor} />
            {isTaken && <Badge label="Taken" variant="warn" />}
          </div>
          <p
            className="font-mono leading-snug"
            style={{ fontSize: "12px", color: "var(--fg-primary)" }}
          >
            {opp.event}
          </p>
          <p
            className="font-mono mt-0.5"
            style={{ fontSize: "10px", color: "var(--fg-faint)" }}
          >
            {opp.tournament} · {fmtTime(opp.start_time)}
          </p>
        </div>
        <StatusPill
          value={opp.status}
          tone={ss.tone}
          variant={ss.variant}
          size="sm"
          showDot={false}
          title={ss.title}
        />
      </div>

      {/* Hero metrics: Edge + Bet Size */}
      <div
        className="grid grid-cols-2 gap-3 py-2 border-y"
        style={{ borderColor: "var(--border-subtle)" }}
      >
        <div>
          <p
            className="font-mono uppercase tracking-[0.15em]"
            style={{ fontSize: "8px", color: "var(--fg-faint)" }}
          >
            Edge
          </p>
          <p
            className="font-mono font-bold"
            style={{
              fontSize: "20px",
              color: edgeColor(opp.edge),
              lineHeight: 1.1,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            +{fmtPct(opp.edge)}
          </p>
        </div>
        <div className="text-right">
          <p
            className="font-mono uppercase tracking-[0.15em]"
            style={{ fontSize: "8px", color: "var(--fg-faint)" }}
          >
            Bet Size
          </p>
          <p
            className="font-mono font-bold"
            style={{
              fontSize: "16px",
              color: hasKelly ? "var(--fg-primary)" : "var(--fg-ghost)",
              lineHeight: 1.2,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {hasKelly
              ? `$${Math.round(bankroll * opp.recommended_kelly).toLocaleString()}`
              : "—"}
          </p>
          {hasKelly && (
            <p className="font-mono" style={{ fontSize: "9px", color: "var(--info)" }}>
              {fmtPct(opp.recommended_kelly, 2)} Kelly
            </p>
          )}
        </div>
      </div>

      {/* Side + market type */}
      <div className="py-2 flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          {isProp ? (
            <Badge label="Prop" color="var(--platform-polymarket)" />
          ) : isTotal ? (
            <Badge label="Total" variant="warn" />
          ) : (
            <Badge label="H2H" color="var(--fg-muted)" />
          )}
          <span
            className="font-mono truncate"
            style={{ fontSize: "11px", color: "var(--fg-secondary)" }}
          >
            {detailString}
          </span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span
            className="font-mono uppercase tracking-[0.15em]"
            style={{ fontSize: "8px", color: "var(--fg-faint)" }}
          >
            Side
          </span>
          <span
            className="font-mono font-medium"
            style={{ fontSize: "12px", color: sideColor }}
          >
            {sideDisplay}
          </span>
        </div>
      </div>

      {/* Reference grid: 6 metrics in 3 columns */}
      <div
        className="grid grid-cols-3 gap-2 pt-2 border-t"
        style={{ borderColor: "var(--border-subtle)" }}
      >
        <Metric
          label="Price"
          value={`${(opp.pm_price * 100).toFixed(1)}¢`}
          valueColor="var(--fg-secondary)"
        />
        <Metric
          label="Spread"
          value={opp.bid_ask_spread != null ? `${(opp.bid_ask_spread * 100).toFixed(1)}¢` : "—"}
          valueColor={opp.bid_ask_spread != null ? "var(--fg-muted)" : "var(--fg-ghost)"}
        />
        <Metric
          label="Age"
          value={isRefreshing ? "…" : fmtAge(opp.price_fetched_at, nowSec)}
          valueColor={
            isRefreshing ? "var(--fg-faint)" : ageColor(opp.price_fetched_at, nowSec)
          }
          title={isRefreshing ? "Price age hidden while scan is running" : undefined}
        />
        <Metric
          label="FD Odds"
          value={fmtOdds(opp.fd_odds)}
          valueColor="var(--fg-muted)"
        />
        <Metric
          label="Model Prob"
          value={fmtPct(opp.p_true)}
          valueColor="var(--fg-primary)"
        />
        <Metric
          label="Kelly"
          value={hasKelly ? fmtPct(opp.recommended_kelly, 2) : "—"}
          valueColor={hasKelly ? "var(--info)" : "var(--fg-ghost)"}
        />
      </div>

      {/* WATCH/SKIP rationale + View link */}
      {(opp.status === "WATCH" || opp.status === "SKIP") && (
        <p
          className="font-mono italic mt-2"
          style={{
            fontSize: "10px",
            color:
              opp.status === "WATCH" ? "var(--info)" : "var(--danger-strong)",
          }}
        >
          {opp.status === "WATCH"
            ? "Watch only — a downgrade rule failed."
            : "Skipped — a critical rule failed."}
        </p>
      )}

      {opp.event_url && (
        <a
          href={opp.event_url}
          target="_blank"
          rel="noopener noreferrer"
          // Stop both click + keypress so activating the link doesn't also
          // trigger the card's onSelect / onKeyDown handlers.
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          className={
            "inline-flex items-center justify-center gap-2 w-full py-2 rounded-md border font-mono tracking-wider transition-colors duration-150 mt-3 " +
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
          } as CardVars}
        >
          View on {platformLabel(opp.platform)}
          <svg
            width="9"
            height="9"
            viewBox="0 0 10 10"
            fill="none"
            style={{ opacity: 0.7 }}
            aria-hidden="true"
          >
            <path
              d="M1 9L9 1M9 1H3M9 1V7"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </a>
      )}
    </div>
  );
}
