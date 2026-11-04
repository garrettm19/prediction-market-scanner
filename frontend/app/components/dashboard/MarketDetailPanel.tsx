"use client";

import { Opportunity } from "@/lib/types";
import { sportLabel, SportRegistryEntry } from "@/lib/sport-labels";
import { Card } from "../ui/Card";
import { StatusPill, type StatusPillTone, type StatusPillVariant } from "../ui/StatusPill";
import { Badge, type BadgeVariant } from "../ui/Badge";
import { Button } from "../ui/Button";
import { EmptyState } from "../ui/EmptyState";
import { PaperMakerPlan } from "./PaperMakerPlan";
import type { CSSProperties } from "react";

interface Props {
  opportunity?: Opportunity | null;
  bankroll?: number;
  isTaken?: boolean;
  onTake?: () => void;
  sportsRegistry?: Record<string, SportRegistryEntry>;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function Row({
  label,
  value,
  valueColor = "var(--fg-secondary)",
}: {
  label: string;
  value: string;
  valueColor?: string;
}) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
        {label}
      </span>
      <span
        className="font-mono font-medium"
        style={{ fontSize: "11px", color: valueColor }}
      >
        {value}
      </span>
    </div>
  );
}

function Section({ children }: { children: string }) {
  return (
    <p
      className="font-mono uppercase tracking-[0.12em] pt-4 pb-1.5 font-medium"
      style={{ fontSize: "9px", color: "var(--accent)", opacity: 0.6 }}
    >
      {children}
    </p>
  );
}

function fmtOdds(n: number): string {
  return n > 0 ? `+${n}` : `${n}`;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 10);
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${months[d.getMonth()]} ${d.getDate()}, ${hh}:${mm} local`;
}

function platformLabel(p: string): string {
  if (p === "polymarket") return "Polymarket";
  if (p === "kalshi") return "Kalshi";
  return p.charAt(0).toUpperCase() + p.slice(1);
}

function platformColor(p: string): string {
  if (p === "polymarket") return "var(--platform-polymarket)";
  if (p === "kalshi") return "var(--platform-kalshi)";
  return "var(--fg-secondary)";
}

function edgeColor(edge: number): string {
  if (edge >= 0.10) return "var(--edge-elite)";
  if (edge >= 0.05) return "var(--edge-strong)";
  if (edge >= 0.02) return "var(--edge-soft)";
  return "var(--edge-mute)";
}

const MARKET_TYPE_LABEL: Record<string, string> = {
  h2h: "Moneyline H2H",
  handicap: "Handicap",
  totals: "Totals",
  first_set: "First Set",
  unknown: "Unknown",
};

interface StatusTone {
  tone: StatusPillTone;
  variant: StatusPillVariant;
  title: string;
}

const STATUS_TONE: Record<string, StatusTone> = {
  BUY:   { tone: "accent", variant: "solid",  title: "All rules pass — actionable per your criteria" },
  WATCH: { tone: "info",   variant: "dashed", title: "Watch only — not actionable. A downgrade rule failed." },
  SKIP:  { tone: "muted",  variant: "solid",  title: "Skipped — a critical rule failed" },
};

const FD_CONF_VARIANT: Record<string, BadgeVariant> = {
  High: "success",
  Medium: "warn",
  Low: "danger",
};

const LW_VARIANT: Record<string, BadgeVariant> = {
  Tight: "success",
  Moderate: "warn",
  Wide: "danger",
};

const LW_COLOR_VAR: Record<string, string> = {
  Tight: "var(--success)",
  Moderate: "var(--warn-strong)",
  Wide: "var(--danger-strong)",
};

// ---------------------------------------------------------------------------
// Hero — status, edge, bet size, side. Fixed at the top of the panel so the
// most decision-relevant numbers don't get lost in the reference grid below.
// ---------------------------------------------------------------------------

function HeroSection({ opp, bankroll }: { opp: Opportunity; bankroll: number }) {
  const ss = STATUS_TONE[opp.status] ?? STATUS_TONE.SKIP;
  const eColor = edgeColor(opp.edge);
  const hasKelly = opp.recommended_kelly > 0;

  return (
    <div className="px-4 pt-4 pb-4 border-b" style={{ borderColor: "var(--border-subtle)" }}>
      {/* Event title + status pill */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0 flex-1">
          <p
            className="font-mono leading-snug font-medium"
            style={{ fontSize: "13px", color: "var(--fg-primary)" }}
          >
            {opp.event}
          </p>
          <p
            className="font-mono mt-1"
            style={{ fontSize: "9px", color: "var(--fg-faint)" }}
          >
            {opp.tournament} · {fmtTime(opp.start_time)}
          </p>
        </div>
        <StatusPill
          value={opp.status}
          tone={ss.tone}
          variant={ss.variant}
          size="md"
          showDot={false}
          title={ss.title}
        />
      </div>

      {/* Edge + Bet size (dominant numbers) */}
      <div className="grid grid-cols-2 gap-3">
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
              fontSize: "28px",
              color: eColor,
              lineHeight: 1.1,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            +{(opp.edge * 100).toFixed(2)}%
          </p>
          <p
            className="font-mono"
            style={{ fontSize: "9px", color: "var(--fg-muted)" }}
          >
            {(opp.p_true * 100).toFixed(1)}% model / {(opp.pm_price * 100).toFixed(1)}c entry
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
              fontSize: "20px",
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
              {(opp.recommended_kelly * 100).toFixed(2)}% Kelly
            </p>
          )}
        </div>
      </div>

      {/* Side + WATCH/SKIP rationale strip */}
      <div
        className="mt-3 pt-3 flex items-center justify-between gap-2 flex-wrap"
        style={{ borderTop: "1px dashed var(--border-subtle)" }}
      >
        <div className="flex items-center gap-2">
          <span
            className="font-mono uppercase tracking-[0.15em]"
            style={{ fontSize: "8px", color: "var(--fg-faint)" }}
          >
            Side
          </span>
          <span
            className="font-mono font-medium"
            style={{ fontSize: "12px", color: "var(--fg-primary)" }}
          >
            {opp.side}
          </span>
          {opp.line != null && (
            <span
              className="font-mono"
              style={{ fontSize: "10px", color: "var(--fg-muted)" }}
            >
              ({opp.line > 0 ? `+${opp.line}` : opp.line})
            </span>
          )}
        </div>
        {opp.status === "WATCH" && (
          <span
            className="font-mono italic"
            style={{ fontSize: "9px", color: "var(--info)" }}
          >
            Watch only — a downgrade rule failed.
          </span>
        )}
        {opp.status === "SKIP" && (
          <span
            className="font-mono italic"
            style={{ fontSize: "9px", color: "var(--danger-strong)" }}
          >
            Skipped — a critical rule failed.
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rule trace — surfaces reject_reasons and downgrade_reasons that drive
// SKIP / WATCH classifications. Hidden when both arrays are empty (BUY).
// ---------------------------------------------------------------------------

function RuleTraceSection({ opp }: { opp: Opportunity }) {
  const rejects = opp.reject_reasons ?? [];
  const downgrades = opp.downgrade_reasons ?? [];
  if (rejects.length === 0 && downgrades.length === 0) return null;

  return (
    <>
      <Section>Rule Trace</Section>
      <div className="space-y-1.5 pb-1">
        {rejects.map((r, i) => (
          <div key={`x-${i}`} className="flex items-start gap-2">
            <span
              className="font-mono shrink-0"
              style={{ fontSize: "10px", color: "var(--danger-strong)", lineHeight: 1.4 }}
              aria-hidden="true"
            >
              ✗
            </span>
            <span
              className="font-mono"
              style={{ fontSize: "10px", color: "var(--danger-strong)", lineHeight: 1.4 }}
            >
              {r}
            </span>
          </div>
        ))}
        {downgrades.map((r, i) => (
          <div key={`w-${i}`} className="flex items-start gap-2">
            <span
              className="font-mono shrink-0"
              style={{ fontSize: "10px", color: "var(--warn-strong)", lineHeight: 1.4 }}
              aria-hidden="true"
            >
              ⚠
            </span>
            <span
              className="font-mono"
              style={{ fontSize: "10px", color: "var(--warn-strong)", lineHeight: 1.4 }}
            >
              {r}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface PlatformLinkVars extends CSSProperties {
  "--link-hover-bg": string;
  "--link-hover-border": string;
}

export function MarketDetailPanel({
  opportunity: opp,
  bankroll = 1000,
  isTaken = false,
  onTake,
  sportsRegistry,
}: Props) {
  if (!opp) {
    return (
      <Card
        variant="card"
        padding={false}
        header={
          <span
            className="font-mono uppercase tracking-[0.12em] font-medium"
            style={{ fontSize: "10px", color: "var(--accent)", opacity: 0.5 }}
          >
            Details
          </span>
        }
      >
        <EmptyState
          title="Select an opportunity to view details"
          icon={
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path
                d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
                stroke="var(--accent)"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          }
        />
      </Card>
    );
  }

  const typeLabel = MARKET_TYPE_LABEL[opp.market_type] ?? opp.market_type;
  const pColor = platformColor(opp.platform);
  const lwColor = LW_COLOR_VAR[opp.fanduel_line_width_label] ?? "var(--fg-faint)";

  const linkStyle: PlatformLinkVars = {
    color: pColor,
    background: `color-mix(in oklab, ${pColor} 8%, transparent)`,
    borderColor: `color-mix(in oklab, ${pColor} 25%, transparent)`,
    "--link-hover-bg": `color-mix(in oklab, ${pColor} 18%, transparent)`,
    "--link-hover-border": `color-mix(in oklab, ${pColor} 50%, transparent)`,
  };

  return (
    <Card
      variant="card"
      padding={false}
      className="flex flex-col"
      header={
        <span
          className="font-mono uppercase tracking-[0.12em] font-medium"
          style={{ fontSize: "10px", color: "var(--accent)", opacity: 0.7 }}
        >
          Details
        </span>
      }
      headerRight={<Badge label={platformLabel(opp.platform)} color={pColor} />}
    >
      <HeroSection opp={opp} bankroll={bankroll} />

      <div className="px-4 pb-4 flex-1 overflow-y-auto">
        {/* Pricing reference */}
        <Section>Pricing</Section>
        <Row
          label="Market Price"
          value={`${(opp.pm_price * 100).toFixed(1)}c`}
        />
        <Row
          label="Model Probability (FD devig)"
          value={`${(opp.p_true * 100).toFixed(1)}%`}
          valueColor="var(--fg-primary)"
        />

        {/* FanDuel reference */}
        <Section>FanDuel Reference</Section>
        <Row label="Odds" value={fmtOdds(opp.fd_odds)} />
        <Row
          label="Overround"
          value={`${(opp.fanduel_overround * 100).toFixed(1)}%`}
          valueColor="var(--fg-muted)"
        />
        <div className="flex items-center justify-between py-1.5">
          <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
            Line Width
          </span>
          <div className="flex items-center gap-1.5">
            <span
              className="font-mono"
              style={{ fontSize: "10px", color: lwColor }}
            >
              {(opp.fanduel_line_width * 100).toFixed(0)}%
            </span>
            <Badge
              label={opp.fanduel_line_width_label}
              variant={LW_VARIANT[opp.fanduel_line_width_label] ?? "platform"}
            />
          </div>
        </div>
        <div className="flex items-center justify-between py-1.5">
          <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
            Confidence
          </span>
          <Badge
            label={opp.fanduel_confidence_label}
            variant={FD_CONF_VARIANT[opp.fanduel_confidence_label] ?? "platform"}
          />
        </div>

        {/* Match quality */}
        <Section>Match Quality</Section>
        <Row
          label="Event Match"
          value={`${(opp.event_match_confidence * 100).toFixed(0)}%`}
          valueColor={
            opp.event_match_confidence >= 0.9
              ? "var(--accent)"
              : "var(--fg-muted)"
          }
        />
        <Row label="Type" value={typeLabel} valueColor="var(--info)" />
        <Row
          label="Sport"
          value={sportLabel(opp.sport, sportsRegistry)}
          valueColor="var(--platform-fanduel)"
        />
        <div className="flex items-center justify-between py-1.5">
          <span className="font-mono" style={{ fontSize: "10px", color: "var(--fg-faint)" }}>
            Match Quality
          </span>
          <Badge
            label={opp.match_quality === "verified" ? "Verified" : "Unverified"}
            variant={opp.match_quality === "verified" ? "success" : "warn"}
          />
        </div>

        {/* Paper maker plan — read-only paper proposal for this opportunity.
            Hidden silently when maker is disabled or no record exists.  The
            section is intentionally non-actionable: no "Place Order" button
            ever appears here. */}
        <PaperMakerPlan marketId={opp.market_id} side={opp.side} />

        {/* Rule trace — only renders if any reject/downgrade present */}
        <RuleTraceSection opp={opp} />

        {/* Action */}
        <div className="pt-4 space-y-2">
          {opp.event_url ? (
            <a
              href={opp.event_url}
              target="_blank"
              rel="noopener noreferrer"
              className={
                "flex items-center justify-center gap-2 w-full py-2.5 rounded-md border font-mono tracking-wider transition-colors duration-150 " +
                "hover:[background:var(--link-hover-bg)] hover:[border-color:var(--link-hover-border)] " +
                "focus:outline-none focus-visible:[box-shadow:var(--ring-focus)]"
              }
              style={{ ...linkStyle, fontSize: "10px", borderRadius: "var(--radius-md)" }}
            >
              View on {platformLabel(opp.platform)}
              <svg width="9" height="9" viewBox="0 0 10 10" fill="none" style={{ opacity: 0.7 }}>
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
            <div
              className="flex items-center justify-center w-full py-2.5 rounded-md border font-mono"
              style={{
                fontSize: "10px",
                color: "var(--fg-ghost)",
                background: "rgba(75,85,99,0.04)",
                borderColor: "rgba(75,85,99,0.15)",
                borderRadius: "var(--radius-md)",
              }}
            >
              No link available
            </div>
          )}

          {/* Mark Taken — Button primitive (secondary/amber) when actionable.
              Tracked indicator stays a static chip so it doesn't read as a
              dismissible button. */}
          {isTaken ? (
            <div
              className="flex items-center justify-center gap-2 w-full py-2.5 rounded-md border font-mono"
              style={{
                fontSize: "10px",
                color: "var(--warn)",
                background: "var(--warn-soft)",
                borderColor: "var(--warn-border)",
                borderRadius: "var(--radius-md)",
              }}
            >
              Position Tracked
            </div>
          ) : (
            <Button
              variant="secondary"
              size="md"
              onClick={onTake}
              className="w-full"
            >
              Mark Taken
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
