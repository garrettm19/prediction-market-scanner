// ---------------------------------------------------------------------------
// Opportunities — matched prediction markets vs sportsbook (normalised)
// ---------------------------------------------------------------------------

export interface RuleEvaluation {
  rule: string;
  passed: boolean;
  severity: string;                 // "CRITICAL" | "DOWNGRADE" | "INFO"
  reason: string;
}

export interface Opportunity {
  // Platform & sport
  platform: string;                 // "polymarket" | "kalshi" | ...
  sport: string;                    // odds API sport_key

  // Event
  event: string;                    // "Player A vs Player B"
  event_url: string | null;
  tournament: string;
  start_time: string;               // ISO-8601 UTC

  // Market
  market_id: string;
  market_type: string;              // "h2h" | "handicap" | "totals" | "first_set"
  side: string;                     // player name (h2h) or "Over"/"Under"
  line: number | null;              // handicap/totals line; null for h2h

  // Pricing — all for the SAME normalised side
  pm_price: number;
  fd_odds: number;                  // FanDuel American odds for this side
  p_true: number;                   // devigged true probability
  edge: number;                     // p_true - pm_price_effective
  recommended_kelly: number;
  kelly_full: number;

  // FanDuel metrics (FanDuel only)
  fanduel_overround: number;
  fanduel_line_width: number;
  fanduel_line_width_label: string; // "Tight" | "Moderate" | "Wide"
  fanduel_confidence_label: string; // "High" | "Medium" | "Low"

  // Match quality
  event_match_confidence: number;
  match_quality: string;            // "verified" | "unverified"

  // Ambiguity
  matched_event_id: string;
  second_best_event_id: string;
  confidence_gap: number;
  competing_matches: number;
  has_shared_last_name: boolean;

  // Classification
  status: string;                   // "BUY" | "WATCH" | "SKIP"
  reject_reasons: string[];         // CRITICAL rule failures
  downgrade_reasons: string[];      // DOWNGRADE rule failures

  // Data quality
  bid_ask_spread: number | null;      // yes_ask - yes_bid; null if unavailable

  // Staleness
  price_fetched_at: number;           // Unix seconds — when platform price was obtained

  // Observability
  home_tokens: string[];
  away_tokens: string[];
  name_match_score: number;
  date_score: number;
  date_delta_hours: number | null;
  rule_evaluations: RuleEvaluation[];
}

// ---------------------------------------------------------------------------
// Tracked positions — manually marked opportunities (localStorage-persisted)
// ---------------------------------------------------------------------------

export interface TrackedPosition {
  id: string;                       // unique key: `${platform}:${market_id}:${side}`
  market_id: string;
  platform: string;
  sport: string;
  event: string;
  event_url: string | null;
  market_type: string;
  side: string;
  line: number | null;
  entry_price: number;              // pm_price at time of marking
  entry_edge: number;               // edge at time of marking
  entry_kelly: number;              // recommended_kelly at time of marking
  fd_odds: number;
  p_true: number;
  matched_event_id: string;         // Odds API event ID (for historical odds)
  taken_at: string;                 // ISO-8601 timestamp
  start_time: string | null;        // event start time (ISO-8601); CLV cutoff
  status: "open" | "closed";
  close_price: number | null;       // prediction market close (legacy, kept for reference)
  entry_ev: number | null;           // p_true - entry_price (FD entry edge)
  fd_close_odds: number | null;     // FanDuel closing American odds for this side
  fd_close_prob: number | null;     // FanDuel closing devigged probability
  clv_prob: number | null;          // fd_close_prob - entry_price (closing line value)
}

export interface OpportunitiesResponse {
  opportunities: Opportunity[];
  total: number;
  status_counts: Record<string, number>;
  quota_remaining: string | null;
  sportsbook_markets_fetched: string[];
  markets_dropped_by_type: Record<string, number>;
  platforms_fetched: string[];
  updated_at: number | null;
  is_refreshing: boolean;
}

export interface BookStatus {
  has_data: boolean;
  event_count: number;
  cache_age_seconds: number | null;
  raw_count?: number | null;           // Kalshi only: markets from API before matching
}

export interface SportDataStatus {
  key: string;
  label: string;
  fanduel: BookStatus;
  polymarket: BookStatus;
  kalshi: BookStatus;
}

export interface PlatformScanConfig {
  name: string;
  label: string;
  enabled: boolean;
}

export interface ScanConfigResponse {
  // Sport entries are not exhaustively typed here — the dashboard only
  // needs the platforms map and global budget for the Books toggles.
  sports: Record<string, { key: string; label: string; enabled: boolean; odds_ttl_seconds: number; market_types: string[] }>;
  platforms: Record<string, PlatformScanConfig>;
  global_max_odds_api_per_day: number;
  odds_cache: Record<string, Record<string, unknown>>;
  // Read-only mirror of EngineConfig.enable_props. Optional for backwards
  // compatibility with older backends that don't return the field.
  props_enabled?: boolean;
}

// ---------------------------------------------------------------------------
// Maker proposals — paper-only audit records exposed via /api/maker/proposals
// ---------------------------------------------------------------------------

export interface MakerRuleEvaluation {
  rule_name: string;
  passed: boolean;
  severity: string;                  // "CRITICAL" | "DOWNGRADE" | "INFO"
  reason_code: string;
  description: string;
}

export interface MakerProposal {
  schema_version: number;
  status: string;                     // "paper_active" | "paper_rejected" | ...

  // Identity
  proposal_id: string;
  platform: string;
  market_id: string;
  market_type: string;
  side: string;
  event_label: string;
  event_start: string;

  // Snapshot
  p_true: number;
  required_edge: number;
  cost_buffer: number;
  best_bid: number | null;
  best_ask: number | null;
  book_fetched_at: number;
  fd_fetched_at: number;

  // Math.  ``null`` when the planner cannot produce a realizable bid
  // (low p_true, missing book, crossed book).  Eligible proposals always
  // have non-null values; the policy enforces this invariant.
  maker_max_bid: number | null;
  proposed_price: number | null;
  tick_size: number;
  estimated_maker_edge: number | null;

  // Eligibility
  eligible: boolean;
  rejection_reasons: string[];
  rule_evaluations: MakerRuleEvaluation[];

  // Provenance — taker classification at planning time (audit only)
  taker_status_at_planning: string;
  taker_reject_reasons: string[];
  taker_downgrade_reasons: string[];
  taker_edge_at_planning: number;

  // Audit
  created_at: number;
  notes: string[];

  // Refresh-cycle UUID shared by every proposal in a single scan.  Optional
  // because legacy records persisted before the field existed lack it; the
  // ``latest_run=true`` server-side filter handles the legacy fallback.
  run_id?: string | null;

  // Display + execution route — disambiguates legacy market_id/side for
  // 2-way Kalshi where the cheapest YES exposure may route through the
  // opposing market's NO contract.  Optional for backwards compatibility
  // with older persisted records.
  display_side?: string;
  execution_market_id?: string;
  execution_contract_side?: "yes" | "no";
  execution_route?: "direct_yes" | "equivalent_no";
}

export interface MakerProposalsResponse {
  days: number;
  count: number;
  proposals: MakerProposal[];
}

// Runtime maker config — mirrors backend MakerConfigResponse.  Safety
// invariants (paper_only, platforms, market_types) are surfaced read-only
// so the UI can label everything PAPER ONLY and confirm scope.
export interface MakerConfig {
  enabled: boolean;
  paper_only: boolean;
  platforms: string[];
  market_types: string[];
  min_estimated_maker_edge: number;
  require_min_spread: number;
  max_book_age_seconds: number;
  max_fd_age_seconds: number;
  max_contracts_per_order: number;
  max_notional_per_order_usd: number;
  max_notional_per_market_usd: number;
  max_open_orders_total: number;
  max_open_orders_per_market: number;
  max_orders_per_hour: number;
  max_daily_notional_usd: number;
  default_ttl_seconds: number;
  cancel_before_event_offset_s: number;
}

// Subset of MakerConfig the UI is allowed to mutate via POST.  ``paper_only``,
// ``platforms``, and ``market_types`` are intentionally absent — the backend
// clamps them server-side regardless of input.
export interface MakerConfigUpdate {
  enabled?: boolean;
  min_estimated_maker_edge?: number;
}

export interface MakerSummaryRejectionReason {
  reason: string;
  count: number;
}

// Compact view of the rejected record with the highest estimated_maker_edge
// among records that had real book data.  Surfaces "how close did the
// scan get to producing an eligible bid?" in the dashboard.
export interface ClosestRejectedRecord {
  side: string;
  event_label: string;
  best_bid: number | null;
  best_ask: number | null;
  proposed_price: number | null;
  maker_max_bid: number | null;
  estimated_maker_edge: number | null;
  rejection_reasons: string[];
}

export interface MakerSummary {
  days: number;
  total: number;
  eligible: number;
  rejected: number;
  by_status: Record<string, number>;
  by_platform: Record<string, number>;
  top_rejection_reasons: MakerSummaryRejectionReason[];
  average_estimated_maker_edge: number | null;
  // Near-miss diagnostics — answer "did the scan have real near-misses
  // or just unusable input?"  All counts apply to whatever slice the
  // endpoint returned (full audit or latest run).
  records_with_real_book: number;
  records_with_valid_inside_spread_possible: number;
  rejected_for_edge_only: number;
  closest_rejected_edge: number | null;
  closest_rejected_record: ClosestRejectedRecord | null;
  // First failing reason per record only (collapses the multi-reason
  // noise of top_rejection_reasons, which is preserved for debug).
  primary_rejection_reasons: MakerSummaryRejectionReason[];
}

export interface SnapshotStatus {
  has_snapshot: boolean;
  updated_at: number | null;
  is_refreshing: boolean;
  refresh_started_at?: number | null;
  last_refresh_error?: string | null;
  last_refresh_duration_seconds?: number | null;
  trigger: string | null;
  last_trigger?: string | null;
  opportunity_count: number;
  status_counts: Record<string, number>;
  platforms_fetched: string[];
  // Optional partial-failure signals. The backend may not emit these yet;
  // the UI only displays them when present.
  degraded?: boolean | null;
  odds_failures?: string[] | number | null;
}
