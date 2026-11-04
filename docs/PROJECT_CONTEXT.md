# MasG EV Tool — Full Project Context

> **Note:** [`ARCHITECTURE.md`](./ARCHITECTURE.md) is the maintained architecture overview; this file is a historical handoff snapshot (facts refreshed 2026-11-04).

> Context document for handing the project off to a new LLM conversation.
> Snapshot date: 2026-04-30. Codebase last touched ~2026-04-12.
> Read this top-to-bottom before making changes. It captures the *why* and the
> *what isn't yet finished*, not just the *what*.

---

## 1. What this product is

MasG EV Tool is a **read-only cross-platform EV (expected value) scanner** for
prediction markets. It compares live prices on **Polymarket** and **Kalshi**
against **FanDuel-derived "true" probabilities** (sourced through The Odds API)
to find positive-EV mispricings, and pushes phone alerts when a new BUY
opportunity appears.

It does **not** execute trades. It is a discovery + decision-support tool.
Final betting is manual via the platform link.

The owner-operator is the user (Mason), running it
locally on Windows 11. There is no deployment, no auth, no multi-tenant
concern. CORS is locked to `localhost:3000` / `127.0.0.1:3000`, and an origin
guard (`services/origin_guard.py`) rejects cross-site state-changing requests.

### Current operating defaults
- **Books**: Kalshi enabled, Polymarket disabled. Polymarket is opt-in via
  the dashboard "Books" toggles or `POST /api/scan/config`.
- **Alerts**: monitor scheduler off; defaults to `dry_run=true` (the code
  default since `b18615b`) and
  `MonitorConfig.platforms=["kalshi"]` when started.
- **Player props**: disabled (`enable_props=False`).
- **Pushover**: credentials configured and end-to-end verified via
  `POST /api/monitor/test`. Live autonomous alerts gated on observing at
  least one organic dry-run BUY payload (see runbook section 9).
- **Soccer missing-draw odds**: SKIPped at the feature layer
  (`NO_BOOKMAKER_DATA`) to prevent 2-way-devig phantom EV.

### Core thesis
- FanDuel sets sharp lines on liquid sports.
- Polymarket and Kalshi are thinner / less efficient on those same matches.
- After removing FanDuel's vig (devigging), the implied probability is treated
  as ground truth.
- If `p_true (FD devigged) - pm_price - cost_buffer > threshold`, that's an EV
  opportunity to BUY on the prediction market.

### Discipline (from CLAUDE.md)
- Pass A (feature extraction) and Pass B (rule classification) must stay
  separate. Don't collapse Pass A into rule filtering.
- FanDuel is the truth source. Don't change devig, edge formula, cost buffer,
  or Kelly sizing without explicit intent + tests.
- Matching is highly tuned against false positives. Any change to name match,
  outcome alignment, confidence thresholds, last-name collision, line/side/unit
  matching needs adversarial tests added.
- Alert flow uses NEW / IMPROVED / COOLDOWN / rate limit. Notifications must go
  through AlertManager, not ad hoc code paths.
- Prefer minimal diffs. No broad refactors unless asked.
- Research-first: read all relevant code paths before editing. For matching /
  pricing / alignment / adapters / monitoring, do a full data flow trace before
  editing.

---

## 2. Repo layout

```
masg-ev-tool/
├── README.md                  # Project overview + getting started
├── CLAUDE.md                  # Project rules + change discipline (small file)
├── SECURITY.md
├── docs/
│   ├── PROJECT_CONTEXT.md     # ← THIS FILE
│   ├── ARCHITECTURE.md        # Maintained architecture overview
│   ├── OPERATIONS.md          # Env vars, REST API, config defaults, presets, go-live
│   ├── KNOWN_ISSUES.md        # Current verified backlog
│   ├── MANUAL_TESTING_RUNBOOK.md
│   ├── DEMO_GUIDE.md          # Offline demo mode
│   └── screenshots/
├── start-dev.bat              # Windows launcher for both servers
├── start-demo.sh              # Offline demo launcher (MASG_DEMO_MODE=1)
├── .github/workflows/ci.yml   # Backend pytest (3.12/3.13) + frontend typecheck/build
├── .gitignore
├── backend/
│   ├── main.py                # FastAPI app + CORS + origin guard + 5 routers
│   ├── requirements.txt       # fastapi, uvicorn, httpx, pydantic, dotenv
│   ├── requirements-dev.txt   # requirements.txt + pytest
│   ├── pytest.ini             # testpaths/pythonpath; run `python -m pytest` from backend/
│   ├── .env / .env.example    # API keys (gitignored)
│   ├── data/
│   │   └── alert_state.json   # Persisted AlertRecord dict (gitignored)
│   ├── models/
│   │   └── market.py          # Polymarket Market pydantic schema
│   ├── routers/
│   │   ├── opportunities.py   # /api/opportunities (snapshot, refresh, debug, history)
│   │   ├── monitor.py         # /api/monitor (alert config + start/stop + status)
│   │   ├── odds.py            # /api/odds (legacy debug — see "gaps" §13)
│   │   ├── scan.py            # /api/scan/config, /api/data-status, /api/sports
│   │   └── maker.py           # /api/maker (paper-only maker proposals)
│   ├── services/
│   │   ├── origin_guard.py        # OriginGuardMiddleware — 403 for cross-site state-changing requests
│   │   ├── redaction.py           # redact_secrets + log filter masking apiKey/token params
│   │   ├── demo/                  # Offline demo mode (fixtures, guards, CLI)
│   │   ├── maker/                 # Paper maker subsystem (PAPER ONLY)
│   │   ├── engine_config.py       # EngineConfig — every tuneable threshold (no magic numbers elsewhere)
│   │   ├── rule_engine.py         # MarketFeatures + POLICY_TABLE (23 rules) + evaluate_rules() + Kelly
│   │   ├── feature_extractor.py   # Pass A — h2h/totals/handicap features per (market, event)
│   │   ├── prop_extractor.py      # Pass A for player props (gated by enable_props)
│   │   ├── opportunities.py       # End-to-end Pass A → Pass B pipeline + dedup + sort
│   │   ├── matcher.py             # Legacy matcher + classify_pm_market_type() (still used by adapters)
│   │   ├── normalizer.py          # normalize_name / last_name / tokenize / appears_in_text
│   │   ├── devig.py               # devig_multiplicative + devig_3way
│   │   ├── sports_config.py       # SPORTS registry + SportConfig dataclass + helpers
│   │   ├── scan_config.py         # ScanConfig (per-sport TTL + enabled toggle)
│   │   ├── odds_provider.py       # The Odds API client (h2h + totals + props + historical)
│   │   ├── odds_cache.py          # Per-sport TTL cache for FanDuel events
│   │   ├── prop_cache.py          # Per-sport TTL cache for prop lines
│   │   ├── discovery_cache.py     # Per-sport "what markets does FanDuel offer" cache
│   │   ├── snapshot.py            # In-memory OpportunitySnapshot (last pipeline result)
│   │   ├── price_history.py       # Polymarket CLOB + Kalshi candlestick history
│   │   ├── events.py              # Polymarket Gamma API (events → markets)
│   │   ├── adapters/
│   │   │   ├── base.py            # NormalizedMarket + MarketAdapter protocol
│   │   │   ├── polymarket.py      # PolymarketAdapter — h2h/totals only, drops futures/handicap/props
│   │   │   └── kalshi.py          # KalshiAdapter — pairs 2-way events, handles 3-way (soccer)
│   │   └── monitor/
│   │       ├── config.py          # MonitorConfig + AlertPreset enum + presets
│   │       ├── manager.py         # AlertManager — NEW/IMPROVED/COOLDOWN/rate-limit decisions
│   │       ├── notifier.py        # PushoverNotifier + DryRunNotifier + format_instant_alert
│   │       ├── state.py           # AlertStateStore — JSON-persisted AlertRecord dict
│   │       ├── scheduler.py       # MonitorScheduler — orchestrates poller + WS + AlertManager
│   │       ├── odds_poller.py     # Scheduled Odds API poller with daily budget
│   │       ├── ws_polymarket.py   # Polymarket CLOB WebSocket
│   │       └── ws_kalshi.py       # Kalshi WebSocket
│   └── tests/                     # 44 pytest modules — see §10
└── frontend/
    ├── package.json               # Next 15 + React 19 + Tailwind 4 + TS 5
    ├── next.config.ts
    ├── tsconfig.json
    ├── postcss.config.mjs
    ├── tailwind.config.ts
    ├── app/
    │   ├── layout.tsx             # Top nav + global styles
    │   ├── page.tsx               # Server entry — pre-fetches /snapshot, hands to client
    │   ├── globals.css
    │   ├── model/page.tsx         # "Model" tab (separate page; not central to scanner)
    │   └── components/
    │       ├── ui/                        # Shared UI primitives
    │       └── dashboard/
    │           ├── DashboardClient.tsx        # Top-level client — toolbar, filters, polls /status, deep-link
    │           ├── OpportunitiesTable.tsx     # Sortable opportunities grid
    │           ├── MarketDetailPanel.tsx      # Right sidebar — single-opp detail
    │           ├── TrackedPositions.tsx       # Manually marked positions w/ CLV
    │           ├── NotificationSettings.tsx   # Phone alert config + start/stop
    │           ├── DataStatus.tsx             # Per-sport per-book matrix
    │           └── …                          # StatusStrip, DemoModeBanner, PaperMaker*, etc.
    │                                          # (NavWalletButton stub removed)
    └── lib/                       # NOTE: frontend/lib/, not frontend/app/lib/
        ├── types.ts               # Opportunity / TrackedPosition / response shapes
        ├── api.ts                 # Snapshot, refresh, status, history, registry, data-status
        ├── monitor-api.ts         # Monitor config + start/stop + status + test push
        ├── sport-labels.ts        # sportLabel(), marketTypeLabel(), isPropType()
        └── useTrackedPositions.ts # localStorage tracking + CLV backfill (FanDuel + platform close)
```

---

## 3. Two-pass pipeline (the core)

```
┌──────────────────────── Pass A: feature extraction ────────────────────────┐
│                                                                            │
│  PolymarketAdapter ─┐                                                      │
│                     ├──► all_markets (NormalizedMarket[])                  │
│  KalshiAdapter ─────┘                                                      │
│                                                                            │
│  fetch_odds(FanDuel) ──► odds_events (TennisOddsEvent[]) ──► odds_cache    │
│  fetch_props (opt)   ──► prop_events (PropEvent[])      ──► prop_cache     │
│                                                                            │
│  for market in all_markets:                                                │
│      for event in odds_events:                                             │
│          features += extract_features(market, event, cfg)  # 0–2 sides     │
│                                                                            │
│  for market in all_markets:                                                │
│      for prop_event in prop_events:                                        │
│          features += extract_prop_features(...)            # 0–1 features  │
│                                                                            │
│  enrich_ambiguity_metrics(features)   # competing_matches, confidence_gap, │
│                                       # shared_last_name                   │
│  features = dedup_to_best_date_match(features)  # series dedup             │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘

┌────────────────────── Pass B: rule engine + Kelly ─────────────────────────┐
│                                                                            │
│  for f in features:                                                        │
│      status, rule_results = evaluate_rules(f, cfg)  # walks POLICY_TABLE   │
│      compute_kelly(f, cfg)                                                 │
│                                                                            │
│  active = [o for o in evaluated if o.status != "SKIP"]                     │
│  active = deduplicate(active)  # by (platform, event, market_type, side, line) │
│  active.sort(key=lambda o: o.edge, reverse=True)                           │
│                                                                            │
│  store_snapshot(active, meta, trigger)                                     │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### Classification semantics
- **BUY** — every rule passes
- **WATCH** — at least one DOWNGRADE rule fails, no CRITICAL fails
- **SKIP** — at least one CRITICAL rule fails (dropped from API response by default)

### POLICY_TABLE (23 rules, defined once in `rule_engine.py`)

| Stage      | Rule                  | Severity   | Reason code                |
|------------|-----------------------|------------|----------------------------|
| fetch      | price_range           | CRITICAL   | PRICE_OUT_OF_RANGE         |
| fetch      | not_live              | CRITICAL   | EVENT_LIVE                 |
| fetch      | has_bookmaker         | CRITICAL   | NO_BOOKMAKER_DATA          |
| match      | name_match            | CRITICAL   | NAME_MISMATCH              |
| match      | outcome_alignment     | CRITICAL   | OUTCOME_NOT_ALIGNED        |
| match      | confidence_survival   | CRITICAL   | CONFIDENCE_BELOW_SURVIVAL  |
| alignment  | line_match            | CRITICAL   | LINE_MISMATCH              |
| alignment  | unit_match            | CRITICAL   | UNIT_MISMATCH              |
| alignment  | side_match            | CRITICAL   | SIDE_MISMATCH              |
| date       | date_not_stale        | CRITICAL   | DATE_TOO_FAR               |
| pricing    | price_prob_coherence  | CRITICAL   | PRICE_PROB_DIVERGENCE      |
| pricing    | positive_edge         | CRITICAL   | NO_EDGE                    |
| pricing    | min_true_prob         | CRITICAL   | TRUE_PROB_TOO_LOW          |
| quality    | confidence_buy        | DOWNGRADE  | CONFIDENCE_BELOW_BUY       |
| quality    | edge_threshold        | DOWNGRADE  | EDGE_BELOW_THRESHOLD       |
| quality    | fd_confidence         | DOWNGRADE  | FD_CONFIDENCE_LOW          |
| quality    | edge_plausible        | DOWNGRADE  | EDGE_IMPLAUSIBLE           |
| ambiguity  | confidence_gap        | DOWNGRADE  | AMBIGUOUS_MATCH_GAP        |
| ambiguity  | shared_last_name      | DOWNGRADE  | SHARED_LAST_NAME           |
| ambiguity  | competing_matches     | DOWNGRADE  | EXCESS_COMPETING_MATCHES   |
| ambiguity  | last_name_collision   | DOWNGRADE  | LAST_NAME_COLLISION        |
| metadata   | metadata_complete     | DOWNGRADE  | INCOMPLETE_METADATA        |
| metadata   | prices_consistent     | DOWNGRADE  | PRICES_INCONSISTENT        |

(23 entries, matching the README. Treat the table in `rule_engine.py` as
authoritative.)

### Key formulas
- `pm_price_effective = pm_price + cost_buffer`  (cost_buffer = $0.01 except
  Polymarket where it is $0.00 — see `_cost_buffer` in feature_extractor.py)
- `p_true = devig_multiplicative(fd_yes_implied, fd_no_implied)`
  for binary; 3-way uses `devig_3way` including draw probability
- `edge = p_true - pm_price_effective`
- Kelly full = `(p_true - pm_price_effective) / (1 - pm_price_effective)`
  capped at `kelly_cap = 0.25`. Recommended Kelly = full × `kelly_fraction = 0.20`
  (i.e. 1/5 Kelly).
- FanDuel confidence label: High / Medium / Low based on overround tiers,
  downgraded one tier when line width is "Wide" (>0.35).

---

## 4. EngineConfig (single source of truth for thresholds)

`backend/services/engine_config.py`. Edit *here*, nowhere else, when retuning.

```
min_edge                   = 0.05    # BUY threshold for normal-width markets (5%)
wide_market_min_edge       = 0.08    # BUY threshold when line width > 0.35 (8%)
min_true_probability       = 0.40    # drop p_true < 40% (long-shot floor)
survival_threshold         = 0.85    # below = SKIP (CRITICAL)
buy_threshold              = 0.90    # 0.85–0.90 = WATCH, ≥ 0.90 = BUY-eligible
max_line_width_for_normal_threshold = 0.35
overround_high_max         = 0.03    # FD overround ≤ 3% → "High" confidence
overround_medium_max       = 0.06    # ≤ 6% → "Medium", > 6% → "Low"
kelly_cap                  = 0.25
kelly_fraction             = 0.20
cost_buffer                = 0.01    # platform fee buffer (0 for PM)
min_price                  = 0.02
max_price                  = 0.98
min_name_length            = 3
min_confidence_gap         = 0.05    # ambiguity guard between top-2 events
max_competing_matches      = 1       # > 1 surviving event = ambiguous
max_price_prob_divergence  = 0.40    # |pm_price - p_true| > 0.40 = side inversion
max_date_delta_hours       = 72.0    # > 72h with no rival = wrong game
max_plausible_edge         = 0.20    # global cap; sport configs override
require_end_date           = False
enable_props               = False   # feature flag for player props (OFF by default)
max_props_per_event        = 20      # cap to prevent feature explosion
```

`SportConfig.min_edge` and `SportConfig.max_plausible_edge` override the global
EngineConfig per sport. Liquid sports (NBA, MLB, NHL) use `min_edge=0.03`.

---

## 5. Adapters and the NormalizedMarket contract

Every platform adapter MUST emit `NormalizedMarket` objects:

```python
@dataclass(frozen=True)
class NormalizedMarket:
    platform: str               # "polymarket" | "kalshi"
    market_id: str              # platform-specific unique ID
    event: str                  # human-readable event name
    market_type: str            # "h2h" | "handicap" | "totals" | "first_set"
    side: str                   # YES-side player/team or "Over"/"Under"; "" if no hint
    line: float | None          # handicap/totals line; None for h2h
    price: float                # YES price 0–1
    liquidity: float | None
    url: str | None
    timestamp: str | None       # market end/resolution time
    question: str               # full question text
    end_date: str | None        # for date proximity
    outcome_prices: list[str] | None  # ["yes_price", "no_price"]
    event_slug: str | None
    bid_ask_spread: float | None = None
    fetched_at: float = 0.0
```

### Polymarket adapter
- Source: Gamma API (`https://gamma-api.polymarket.com/events`).
- Tags pulled from `all_pm_tags()` — derived from `SPORTS` registry.
- Drops everything except `h2h` and `totals` after running
  `classify_pm_market_type(question)` from `matcher.py`.
- Two question formats handled:
  1. `outcomes=["Yes","No"]` — `outcomePrices[0]=YES`.
  2. `outcomes=["Team A","Team B"]` — sets `side=outcomes[0]` so the feature
     extractor knows which team's price is at index 0.
- Slug-based date extraction: e.g., `mlb-ari-nym-2026-04-07` → that date as
  `effective_end_date`. Used because Polymarket's `endDate` is the *settlement*
  date, not the game date. Live/past games filtered out via slug date.
- No fees → `_cost_buffer("polymarket", cfg) = 0`.

### Kalshi adapter
- Source: `api.elections.kalshi.com/trade-api/v2/markets`.
- Each H2H **event** has 2 markets (one per team). Adapter pairs them and
  emits ONE `NormalizedMarket` with cross-market best prices:
  - `best_m1 = min(M1.yes_ask, M2.no_ask)`
  - `best_m2 = min(M2.yes_ask, M1.no_ask)`
- 3-way detection (soccer): if a "tie"/"draw" market exists, emit TWO
  NormalizedMarkets (one per team) using direct yes_ask prices instead of
  cross-market — because "No Home" ≠ "Buy Away" when a draw is possible.
- Subject extraction order:
  1. Title `"Will X win"` regex.
  2. Ticker suffix matching ("BHM" → "Birmingham Stallions").
- Date extraction: `KXATPMATCH-26APR08MOURUU` → 2026-04-08.
- Auth: `KALSHI_API_KEY` env var, sent as `Authorization: <key>` header.
- Pacing: 100ms between series fetches (10 req/sec; Basic tier limit is 20/sec).

### Sport registry
`backend/services/sports_config.py` is the single source of truth for which
sports are wired up. Currently in the registry:

| Sport key            | Label             | Enabled | PM tag      | Kalshi series                | Markets         |
|----------------------|-------------------|---------|-------------|------------------------------|-----------------|
| tennis               | Tennis            | YES     | tennis      | KXATPMATCH, KXWTAMATCH       | h2h             |
| cricket_ipl          | Cricket IPL       | NO¹     | cricket     | KXIPLGAME                    | h2h             |
| rugby_nrl            | Rugby NRL         | NO¹     | rugby       | KXRUGBYNRLMATCH              | h2h             |
| ufl                  | UFL               | YES     | (none)      | KXUFLGAME                    | h2h             |
| hockey_ahl           | Hockey AHL        | NO¹     | hockey      | KXAHLGAME                    | h2h             |
| basketball_nba       | NBA               | YES     | basketball  | KXNBAGAME                    | h2h, totals, props |
| basketball_wnba      | WNBA              | YES     | basketball  | KXWNBAGAME                   | h2h             |
| baseball_mlb         | MLB               | YES     | baseball    | KXMLBGAME                    | h2h, totals, props |
| hockey_nhl           | NHL               | YES     | hockey      | KXNHLGAME                    | h2h, totals     |
| football_nfl         | NFL               | YES     | (none)      | KXNFLGAME                    | h2h, props      |
| soccer_mls           | MLS               | YES     | soccer      | KXMLSGAME                    | h2h             |
| soccer_epl           | EPL               | YES     | soccer      | KXEPLGAME                    | h2h             |
| afl                  | AFL               | NO¹     | (none)      | KXAFLGAME                    | h2h             |
| baseball_kbo         | KBO               | YES     | baseball    | KXKBOGAME                    | h2h             |
| mma                  | UFC/MMA           | YES     | ufc         | KXUFCFIGHT                   | h2h             |
| soccer_france        | Ligue 1           | YES     | soccer      | KXLIGUE1GAME                 | h2h             |
| soccer_italy         | Serie A           | YES     | soccer      | KXSERIEAGAME                 | h2h             |
| soccer_germany       | Bundesliga        | YES     | soccer      | KXBUNDESLIGAGAME             | h2h             |
| soccer_spain         | La Liga           | YES     | soccer      | KXLALIGAGAME                 | h2h             |
| soccer_ucl           | Champions League  | YES     | soccer      | KXUCLGAME                    | h2h             |

¹ Disabled because The Odds API returns events for the sport but FanDuel has
no bookmaker lines — no truth source means no edge calculation.

`enable_discovery=True` on a SportConfig opts the sport into a 1-credit-per-
discovery probe that asks FanDuel which markets it actually offers; results
land in `discovery_cache`.

---

## 6. Backend HTTP API

CORS allows only `http://localhost:3000` and `http://127.0.0.1:3000`
(`ALLOWED_ORIGINS` in `main.py`). Methods: GET, POST, OPTIONS. The same list
feeds `OriginGuardMiddleware`, which returns 403 for non-GET/HEAD/OPTIONS
requests carrying a foreign `Origin`. The `/api/maker` router (paper maker)
is not listed below; see `docs/OPERATIONS.md` §4 for the full API reference.

### `/api/opportunities` router
| Method + path                          | Notes |
|----------------------------------------|-------|
| GET `/opportunities`                   | Returns latest snapshot. Cold-start runs `refresh_snapshot("cold_start")`. Query: `platform`, `include_watch`. |
| GET `/opportunities/snapshot`          | Read-only — never triggers pipeline. 204 if no snapshot. |
| GET `/opportunities/status`            | Lightweight status (has_snapshot, updated_at, is_refreshing, status_counts). For frontend polling. |
| POST `/opportunities/refresh`          | Triggers pipeline. Body: `{"scope": "stale" \| "all" \| "<sport_key>"}`. Returns lightweight status. |
| GET `/opportunities/policy`            | Returns POLICY_TABLE entries. |
| GET `/opportunities/history`           | `?platform=&market_id=` → price history points. |
| GET `/opportunities/historical-odds`   | `?sport_key=&event_id=&date=` → FanDuel snapshot at/before timestamp. |
| GET `/opportunities/debug`             | Full pipeline diagnostic with rule-by-rule trace. Accepts engine config overrides. |

### `/api/monitor` router (alerts)
| Method + path | Notes |
|---|---|
| GET `/monitor/config`        | Current MonitorConfig (without secrets — only `has_pushover_credentials` bool). |
| POST `/monitor/config`       | Update settings. Does NOT start/stop. Setting any non-preset field switches preset to CUSTOM. |
| POST `/monitor/start`        | Spin up scheduler (Pushover required unless dry_run). |
| POST `/monitor/stop`         | Cancel scheduler tasks, save state. |
| GET `/monitor/status`        | running/enabled, total_cycles, total_alerts_sent, per-source status. |
| GET `/monitor/history`       | Last 50 alerted records. |
| POST `/monitor/test`         | Send real Pushover test (bypasses dry_run). Refused (403) in demo mode. |

### `/api/scan` router
| Method + path | Notes |
|---|---|
| GET `/scan/config`     | Per-sport scan settings (TTL, enabled) + odds_cache state. |
| POST `/scan/config`    | Patch config: `{"sports": {"tennis": {"enabled": false}}}`, `{"global_max_odds_api_per_day": 100}`. |
| GET `/data-status`     | Per-sport per-book matrix (FD/PM/Kalshi has_data + age). |
| GET `/sports`          | Static sport registry (key, label, match_style, market_types). |

### `/api/odds` router (legacy debug)
| Method + path | Notes |
|---|---|
| GET `/odds/tennis`     | Fetch tennis events directly. Tennis-only. |
| GET `/odds/matches`    | Cross-match PM tennis vs FD odds with the legacy matcher. (The old `len(markets)` bug is fixed — it now reports `len(normalized)`; see §13.) |

---

## 7. Monitoring & alerts (the phone push system)

### Layers
- **OddsApiPoller** — runs every `refresh_interval_minutes` (default 15). On
  each successful poll, the full pipeline re-runs and the snapshot is
  re-stored. Daily budget enforced via `max_polls_per_day` (default 20).
- **PolymarketWsConsumer** — subscribes to CLOB WS for watched asset_ids
  (`wss://ws-subscriptions-clob.polymarket.com/ws/market`). On a ≥1¢ price
  change of a watched market, fires a debounced re-evaluation
  (10-second cooldown between WS-triggered evaluations).
- **KalshiWsConsumer** — orderbook_delta channel, same debounce.
  Both WS consumers reconnect with exponential backoff (5→60s cap), including
  after clean server closes (commit `29cd0ac`).
- **MonitorScheduler** orchestrates all three. After each pipeline run it
  calls `_update_watch_lists` so the WS consumers track the *current* set of
  non-SKIP markets. `start()`/`stop()` are serialized by a lifecycle lock so
  overlapping start/stop cannot leave orphan poller/WS tasks (`705169a`).

### Alert decision tree (`AlertManager._decide`)
For each opportunity that passes user filters:

1. `alert_count == 0` → **NEW** (alert).
2. Within cooldown (`now < last_alert_ts + cooldown_minutes*60`):
   - If edge improved by ≥ `resend_edge_improvement` (default 3%) → **IMPROVED** (alert).
   - Otherwise → **COOLDOWN** (suppress).
3. Past cooldown → treat as **NEW** again (alert).

Hourly cap (`max_alerts_per_hour`): if more candidates than slots, sort by
edge desc and take top N. The remaining-slot count is clamped at 0, so
lowering the cap mid-hour never sends extra alerts (commit `848f133`).

### Filter rules (`AlertManager._passes_filters`)
- `status == "BUY"` (WATCH/SKIP never alerted)
- `edge >= cfg.min_ev`
- `platform in cfg.platforms`
- `market_type in cfg.market_types`
- If `exclude_ambiguity_downgraded` (default True): skip if any downgrade
  reason is in `_AMBIGUITY_REASONS = {AMBIGUOUS_MATCH_GAP, SHARED_LAST_NAME,
  EXCESS_COMPETING_MATCHES, LAST_NAME_COLLISION}`. Note that BUY status
  shouldn't have downgrades, but the filter is here as belt-and-braces.

### Presets (`monitor/config.py`)
| Preset       | min_ev | refresh | cooldown | max/hr |
|--------------|--------|---------|----------|--------|
| Conservative | 8%     | 30 min  | 60 min   | 5      |
| Standard     | 5%     | 15 min  | 30 min   | 10     |
| Aggressive   | 3%     | 10 min  | 15 min   | 20     |
| Custom       | (whatever was set last) |

### Notification format (Pushover)
- Title: `⚡ +X.X% EV — <side>`
- Body: 3–4 lines (event, platform+market+price, true_prob+FD odds+confidence, optional Kelly%)
- Priority 1 if edge ≥ `priority_high_min_edge` (10%), else 0.
- Deep links: `polymarket://event/{slug}` (iOS) or web URL fallback.
- Kalshi has no public deep link scheme, so web URL is used.

### State persistence
`backend/data/alert_state.json` is written on every cycle and on stop
(atomically: temp file + fsync + `os.replace`, commit `f73c033`). Tracks
`AlertRecord` per opportunity key (`platform|event|market_type|side|line`)
plus a rolling hourly alert timestamp list.

---

## 8. Frontend

### Stack
- Next.js 15 (App Router) + React 19 + Tailwind 4 + TypeScript 5.
- Single dev server: `npm run dev` from `frontend/`.
- `NEXT_PUBLIC_BACKEND_URL` env (defaults to `http://localhost:8000`).

### Pages
- `/` — Scanner dashboard (default view). Renders `DashboardClient`.
- `/model` — Separate "Model" page. Self-contained; not central to the
  scanner workflow.

### Dashboard data flow
1. Server component `app/page.tsx` does a SSR `fetchSnapshot()` and passes
   `initialData` to `DashboardClient`.
2. `DashboardClient` polls `GET /api/opportunities/status` every 2s while
   scanning, every 10s otherwise. When `updated_at` changes it pulls
   `GET /api/opportunities/snapshot` and replaces the in-memory data.
3. `Scan` button calls `POST /api/opportunities/refresh` then waits for the
   poll to detect the new snapshot.
4. `Auto` toggle calls `POST /api/monitor/start` or `/stop`.
5. Filters (status / platform / sport / market type / search / min_edge) all
   run client-side over `data.opportunities`.
6. `Mark Taken` saves an `Opportunity` snapshot into localStorage (key
   `masg_tracked_positions`). `useTrackedPositions` backfills CLV from
   `/opportunities/history` (platform close price) and
   `/opportunities/historical-odds` (FanDuel devigged closing line).
7. `MarketDetailPanel` shows side, price, true_prob, edge, Kelly $, FanDuel
   reference, match quality, "Mark Taken" / "Position Tracked" CTA.

### Tracked positions / CLV
- `entry_ev = p_true - entry_price` (FD entry edge at the time of marking).
- `clv_prob = fd_close_prob - entry_price` (positive = bought below the
  sharp closing line; this is the gold-standard signal that you got a real edge).
- Versioned via `CLV_VERSION` constant; bumping it triggers a recomputation.
- Kalshi close price needs side inversion when the tracked side is M2 because
  `market_id` always references M1's ticker — `needsInversion()` does
  word-prefix and initials matching to detect this.

---

## 9. Setup / running

### Backend
```
cd backend
python -m venv .venv
.venv\Scripts\activate           # Windows
pip install -r requirements.txt
cp .env.example .env             # then fill in keys
uvicorn main:app --reload --port 8000
```

### Frontend
```
cd frontend
npm install
npm run dev                      # http://localhost:3000
```

### Both at once (Windows)
`start-dev.bat`

### .env keys
```
GAMMA_API_BASE       = https://gamma-api.polymarket.com   # public, no key
ODDS_API_KEY         = <your-key>                          # the-odds-api.com (free 500/mo)
KALSHI_API_KEY       = <your-key>                          # optional
PUSHOVER_USER_KEY    = <your-key>                          # optional, for phone alerts
PUSHOVER_API_TOKEN   = <your-token>
```

`PUSHOVER_USER_KEY` and `PUSHOVER_API_TOKEN` together are required for live
alerts. `dry_run=True` lets you exercise the alert flow without them.

### Tests
```
cd backend
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest                 # config in backend/pytest.ini
```
CI (`.github/workflows/ci.yml`) runs the backend suite on Python 3.12/3.13
with outbound HTTP blackholed, plus the frontend typecheck and build.

### Offline demo
`start-demo.sh` (or `MASG_DEMO_MODE=1`) runs the real pipeline on synthetic
fixtures with no keys or outbound calls; see `docs/DEMO_GUIDE.md`.

---

## 10. Test inventory (`backend/tests/`)

44 test modules, 1303 tests passing as of 2026-11-04 (`7b161a2`). The table
below lists the original core files only; `ls backend/tests/` for the rest
(e.g. `test_origin_guard.py`, `test_secret_redaction.py`,
`test_price_history.py`, `test_scheduler_lifecycle.py`,
`test_ws_reconnect_backoff.py`, `test_odds_failure_staleness.py`,
`test_demo_mode.py`, `test_maker_*.py`).

| File                             | What it covers |
|----------------------------------|----------------|
| `test_rule_engine.py`            | All 23 policy rules, classification, Kelly. Largest file (~36KB). |
| `test_feature_extractor.py`      | h2h / totals / handicap features, name+date scoring, outcome alignment, last-name collision. ~28KB. |
| `test_normalizer.py`             | normalize_name, last_name, tokenize, name_appears_in_text. |
| `test_ambiguity.py`              | confidence_gap, shared_last_name, competing_matches, last_name_collision. |
| `test_3way_devig.py`             | Soccer 3-way devig (home/away/draw). |
| `test_kalshi_adapter.py`         | Pair extraction, ticker→subject identification, 3-way detection, fallbacks. ~45KB. |
| `test_polymarket_adapter.py`     | Yes/No vs team-name outcome formats, slug date extraction, dropped types. |
| `test_new_sports.py`             | Baseball/hockey/basketball/football/soccer cross-platform matches. ~34KB. |
| `test_prop_extractor.py`         | Player prop classification, line parsing, player matching. |
| `test_prop_pipeline.py`          | End-to-end prop flow into the engine. |
| `test_soccer_alignment.py`       | 3-way alignment, draw exclusion, side identification. |
| `test_strict_validation.py`      | Edge-case validation guards. |
| `test_series_dedup.py`           | Multi-game series dedup (closest-date wins). |
| `test_fetch_odds_cache.py`       | Per-sport TTL cache hit/miss. |
| `test_scan_config.py`            | ScanConfig defaults + updates. |
| `test_scan_endpoints.py`         | `/scan/config` GET/POST behavior. |
| `test_snapshot.py`               | Snapshot store/get/clear, refresh lock. |
| `test_snapshot_endpoints.py`     | `/opportunities/snapshot`, `/status`, `/refresh`. |
| `test_monitor_manager.py`        | NEW/IMPROVED/COOLDOWN, hourly cap, filter rules, dry run. |
| `test_scheduler_watch.py`        | WS watch list updates from pipeline. |

The test suite is the spec. When changing matching/pricing/alignment, update
or add tests in the same PR.

---

## 11. Caching layers (important for cost control)

The Odds API has a hard quota (free tier 500/month). The system layers caches
to reuse data:

| Cache              | Key             | TTL      | Invalidate when |
|--------------------|------------------|----------|------------------|
| `odds_cache`       | sport_key       | 900s default per `ScanConfig.odds_ttl_seconds` | manual `invalidate(sport_key)` or `invalidate_all()` |
| `prop_cache`       | sport_key       | 900s      | not currently invalidated programmatically |
| `discovery_cache`  | sport_key       | 14400s (4h) | `clear_cache()` only |
| `_discovery_cache` (sport-key resolution) | global | 3600s (1h) | falls back to stale on API error |

`POST /opportunities/refresh` accepts `scope`:
- `"stale"` (default) — no invalidation, just lets TTLs do their job.
- `"all"` — `odds_cache.invalidate_all()`.
- `"<sport_key>"` — `odds_cache.invalidate(sport_key)`.

---

## 12. Conventions and design rules in force

### Backend
- Python 3.12, type hints on public functions, dataclasses for value types.
- Frozen dataclasses for immutable structures (NormalizedMarket, RuleResult,
  PolicyRule, BookmakerLine, TotalsLine, MatchResult, HistoricalOddsSnapshot).
- Every threshold lives in `EngineConfig`, `MonitorConfig`, or `SportConfig`.
  Magic numbers are a smell.
- Match scoring formula: `confidence = name_score × 0.65 + date_score × 0.35`.
  Both scores live in `feature_extractor.py` as `_name_score` / `_date_score`.
- The matcher's name strategies, in order:
  1. Full-name substring (definitive — no false positives from generic tokens).
  2. Last-name word-boundary, with an `_AMBIGUOUS_SUFFIXES` guard list (city,
     united, tigers, sox…) so generic last names don't cross-team-match.
  3. City-name span expansion when adapter provides `side` hint —
     `_find_best_span` finds the longest contiguous span from FD team names
     that appears in the question, replaces it with the full name, then runs
     Strategy 1 again. Only fires when Kalshi series sport matches FD sport
     (via `_sport_compatible`).

### Frontend
- All API calls in `lib/api.ts` and `lib/monitor-api.ts`. No inline `fetch` in
  components.
- `lib/types.ts` is the schema authority.
- `lib/sport-labels.ts` resolves Odds API keys (e.g. `tennis_atp_french_open`)
  to short labels via the backend `/api/sports` registry, with a hardcoded
  prefix fallback for SSR.
- localStorage is fine for tracked positions (single-user local tool).
- Polling cadence: 2s while scanning, 10s otherwise.

---

## 13. Known gaps / things that are stubby / things to clean up

(Written against `04e9013`; refreshed 2026-11-04 at `7b161a2`. The current,
re-verified backlog is [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md) — prefer it over
this list. Fixed since `03deae7`: Kalshi 3-way price-history math
(`78bad19`), atomic alert-state writes (`f73c033`), CSRF origin guard
(`0443910`), secrets redaction + stale-odds eviction with `degraded` /
`odds_failures` on `/opportunities/status` (`dbb71ce`), scheduler
start/stop lifecycle lock (`705169a`), WS reconnect backoff (`29cd0ac`),
hourly-cap clamp (`848f133`), `MonitorConfig.dry_run` default `True`
(`b18615b`).)

1. **`routers/odds.py` is legacy.** `/odds/tennis` and `/odds/matches` exist
   from the tennis-only era. The frontend never calls them; the dashboard
   uses `/api/opportunities/snapshot`. The `NameError` was fixed in commit
   `c68eca7` but the endpoint remains unused. Decide: keep as a debug
   surface or remove entirely and unregister the router from `main.py`.

2. **`MIN_CONFIDENCE = 0.90` in matcher.py vs `survival_threshold = 0.85` in
   EngineConfig.** The legacy matcher (`/odds/matches`) uses the 0.90 const.
   The current pipeline uses EngineConfig directly. If the legacy endpoint is
   kept, document the discrepancy or align them.

3. **`enable_props` is OFF by default.** Player props (NBA points/rebounds,
   NFL pass yds, MLB hits) have a complete pipeline (`prop_extractor.py`,
   `prop_cache.py`, `fetch_props`) but are gated. `fetch_props` does not
   currently honor `ScanConfig.sports[key].enabled` — wire that up before
   any rollout. Per CLAUDE.md, don't flip `enable_props=True` for ad-hoc
   testing without a deliberate plan.

4. **Polymarket NO-side derivation when book missing**: when the NO token's
   CLOB book is unavailable, the adapter derives `outcome_prices[1]` as
   `1 - best_ask` instead of using stale Gamma metadata (commit `ce6f7ce`).
   This keeps consistency by construction but is a workaround; long-term
   the WS consumer could maintain a live cache so missing-book scenarios
   are rare. Still open — tracked as KNOWN_ISSUES M-11 (understates the
   NO cost by the spread).

5. ~~**Kalshi 3-way price-history math is still 2-way**~~ — **Fixed**
   (`78bad19`): cross-market pairing is now used only for strict 2-way
   events; 3-way (draw) events use each market's own mid.

6. **Kalshi 3-way feature semantics for the NO side**: the adapter emits
   two NormalizedMarkets per soccer event (one per team), each with
   `outcome_prices[1] = 1 - team_yes_ask`. For a 3-way market the
   "complement" is "team does not win" = "opponent OR draw", which is not
   `1 - team_yes`. Only `outcome_prices[0]` (the team's own YES price)
   is used for pricing, so this is observability-only — but adversarial
   cases involving the NO side could still surface incorrect labels.
   Still open — tracked as KNOWN_ISSUES M-14. (Separately, `fded37e`
   stopped the draw/Tie leg being emitted as an h2h team market.)

7. **Disabled sports with no FanDuel lines** — Cricket IPL, Rugby NRL,
   Hockey AHL, AFL. They appear in the registry as `enabled=False` but
   if FanDuel ever starts pricing them, just flip the flag.

8. **No auth** on the backend. Fine for single-user localhost. CSRF is
   now mitigated (`0443910`): `OriginGuardMiddleware` rejects
   state-changing requests with a foreign `Origin`; requests without an
   `Origin` header are still allowed (KNOWN_ISSUES S-1). If anyone ever
   exposes this beyond localhost, add simple bearer auth on the monitor +
   scan endpoints.

9. **Pushover credentials live in plain `.env`.** Consider Windows DPAPI
   or a secret manager if the box is shared.

10. **`AlertStateStore.prune_stale(max_age_hours)` exists but is not
    scheduled** (KNOWN_ISSUES R-13; saves are now atomic, `f73c033`). Records accumulate; both live and dry-run sends count
    toward the rolling hourly cap, so dry-run records can briefly consume
    budget after a switch to live. Self-heals after 60 minutes.

11. **No structured logging.** The codebase uses `logger.info/warning/error`
    with f-strings or %-formatting. Credential query params (`apiKey`,
    `token`, …) are masked in logs by `services/redaction.py` (`dbb71ce`). If you ever need to grep for an alert
    flow, search for `"AlertManager cycle"`, `"Pass A"`, `"Pass B"`,
    `"Series dedup"`, `"Pushover"`, `"DRY RUN"`, etc.

12. **Rate-limiting on the Odds API** is enforced only via `OddsApiPoller`'s
    daily rolling window and TTL caches — there is no global counter.
    If you bypass the poller (e.g., spamming `POST /opportunities/refresh?
    scope=all`), you can blow through the monthly quota.

13. **Kalshi 429 rate-limit during long polling windows.** Observed
    intermittently across some series tickers (KXEPLGAME, KXLIGUE1GAME,
    KXLALIGAGAME) during multi-cycle live runs. The adapter handles
    gracefully (per-series error, pipeline continues, `last_refresh_error`
    stays null). Tomorrow's scan picks up cleanly. If the issue grows,
    consider per-series exponential backoff.

14. **`routers/odds.py` import of `fetch_tennis_odds` is still alive** in
    `odds_provider.py`, so the file works even though it's tennis-only
    logic. Just don't call it for non-tennis sports.

15. **`opportunities.py` has an `_alt_raw` / `_alt_actionable` measurement
    block** that logs alt-demand counts (totals candidates with no
    matching FD line). This is logging-only; the data is not surfaced
    anywhere yet.

---

## 14. How to extend common things

### Add a new sport
1. Add a `SportConfig` to `SPORTS` in `sports_config.py` with the right
   `odds_api_keys`, `kalshi_series`, `pm_tag`, `match_style`, and
   `max_plausible_edge`.
2. Confirm Polymarket has a tag for it; confirm Kalshi has the series ticker.
3. Add tests in `test_new_sports.py`.
4. Liquid sports (tight lines): set `min_edge=0.03` and lower
   `max_plausible_edge`.

### Tweak an alert preset
Edit `PRESETS` in `services/monitor/config.py`. Update the presets table in
`docs/OPERATIONS.md` §8 to match.

### Add a new policy rule
1. Write `_my_rule(features, config) -> bool` in `rule_engine.py`.
2. Add a `PolicyRule(...)` entry to `POLICY_TABLE` with the right severity.
3. Pick a unique `reason_code` and add tests in `test_rule_engine.py` for
   both pass and fail paths.
4. If it's an ambiguity rule (DOWNGRADE) and you want it to suppress phone
   alerts, add the reason code to `_AMBIGUITY_REASONS` in `monitor/manager.py`.

### Add a new platform adapter
1. Implement `MarketAdapter` protocol in `services/adapters/<platform>.py`.
2. Emit `NormalizedMarket` objects with `market_type ∈ {"h2h","totals","handicap"}`.
3. Register it in `services/opportunities.py` `DEFAULT_ADAPTERS`.
4. Add adapter unit tests in `tests/test_<platform>_adapter.py`.

### Change devig / edge formula
Don't, unless intentional. If you must:
- `services/devig.py` is the only place to edit math.
- Update `feature_extractor.py` if the outputs change shape (e.g., 4-way).
- Update `test_3way_devig.py` and any rule/feature tests that hardcode
  expected values.

---

## 15. Snapshot of current runtime state

- **Backend**: FastAPI app exposing 5 routers under `/api` (odds,
  opportunities, monitor, scan, maker). CORS for localhost:3000 /
  127.0.0.1:3000 only, plus the origin guard.
- **Books default**: `ScanConfig.platforms` defaults to Kalshi enabled,
  Polymarket disabled (commit `b6c14ee`). Default scans never call
  `PolymarketAdapter.fetch_markets` — no Gamma fetch, no CLOB calls.
  Polymarket is opt-in via dashboard "Books" toggles or
  `POST /api/scan/config {"platforms": {"polymarket": {"enabled": true}}}`.
- **Refresh**: `POST /api/opportunities/refresh` is non-blocking — returns
  202 immediately and runs the pipeline in a background asyncio task
  (commit `c4d9035`). Manual refresh and the scheduler share the same
  `_refresh_lock` so the expensive pipeline cannot run concurrently
  (commit `93a00ab`). Status / duration / error metadata is centrally
  recorded in `services.snapshot` and surfaced via
  `GET /api/opportunities/status` (`refresh_started_at`, `last_refresh_error`,
  `last_refresh_duration_seconds`, `last_trigger`, and — since `dbb71ce` —
  `odds_failures` / `degraded` when a sport's FanDuel refetch failed and its
  expired odds were evicted).
- **Frontend**: Single dashboard that polls the snapshot endpoint and shows a
  filtered/sorted table of EV opportunities, with a sidebar detail panel,
  a tracked-positions section, alert configuration, and a per-sport data
  status matrix. Toolbar exposes Books toggles (Kalshi / Polymarket) wired
  to `POST /api/scan/config`. Default Min Edge filter is 4%. Time columns
  render in the user's local timezone with explicit "(local)" labels.
- **Monitor**: Off by default. `MonitorConfig.dry_run` defaults to `True`
  (commit `b18615b`; before that the code defaulted to `False`).
  `MonitorConfig.platforms` defaults to
  `["kalshi"]` (commit `31d13b6`) so the alert-side allowlist mirrors the
  scan-side default and cannot drift. Once started via the dashboard or
  `POST /monitor/start`, it goes through `refresh_snapshot()` for every
  cycle, polls FanDuel every 15–30 minutes (preset-dependent), watches
  Polymarket and Kalshi WebSockets for price changes, and pushes Pushover
  alerts based on the cooldown/rate-limit/filter rules.
  - **Pushover**: end-to-end verified via `POST /api/monitor/test` (sends
    a real notification, bypasses `dry_run` by design). Live alerts are
    standing OFF until at least one organic dry-run BUY payload is observed.
  - **Soccer guard**: `feature_extractor.py` SKIPs candidates with
    `fd.draw_odds=None` on `soccer_*` sport keys via
    `NO_BOOKMAKER_DATA` (commit `04e9013`). Phantom EV from the
    2-way-devig fallback class is structurally impossible.
- **Persistence**: alert_state.json on disk; everything else (caches,
  snapshot) is in-memory and rebuilds on restart.
- **Cost**: Each `fetch_odds` cycle costs roughly N credits where N is the
  number of (sport_key, market_type) combinations actively being fetched.
  The poller is bounded to 20 polls/day by default, so the worst case is
  ~600 credits/month — slightly over the free tier. Caches usually keep this
  much lower in practice. Default scans run faster (~3-10s) when only
  Kalshi is enabled because there's no Polymarket CLOB hydration.
- **Tests**: `python -m pytest` (from `backend/`) reports 1303 passed across
  44 test modules (HEAD `7b161a2`, 2026-11-04).

---

## 16. Glossary

- **Edge** — `p_true (FD devigged) - pm_price - cost_buffer`.
- **Devig / Vig removal** — Bookmaker prices include an overround so they
  don't sum to 1. We divide each implied probability by the sum to get
  the "true" probability.
- **Overround** — `sum(implied_probabilities) - 1`. FanDuel's house edge.
- **Line width** — `|p_true_yes - p_true_no|`. A wide line means the favorite
  is heavily favored — markets are noisier here, so we require more edge.
- **Confidence** — match confidence between PM market and FD event.
  `name × 0.65 + date × 0.35`.
- **Kelly** — optimal bet sizing. Capped fractional Kelly (1/5, max 25%).
- **CLV** — Closing Line Value. `fd_close_prob - entry_price`. The gold
  standard for whether you actually had edge.
- **Pass A / Pass B** — feature extraction (no rejections) / rule evaluation
  (BUY/WATCH/SKIP classification).
- **NEW / IMPROVED / COOLDOWN / ACTIVE** — alert decision states.
- **Series dedup** — when one PM market matches multiple FD events (e.g., a
  3-game MLB series), keep only the closest-date FD event.
- **3-way market** — soccer h2h has home/away/DRAW. Devig must include the
  draw probability or team probabilities are inflated.

---

## 17. Things I am uncertain about / open questions for the next session

These are real ambiguities you should decide about, not just things that
weren't read. I flag them so the next conversation can resolve them.

1. **Should `/api/odds/*` be deleted or kept?** It's tennis-only legacy.
   The frontend doesn't call it. Bug fix landed in `c68eca7` but the
   endpoint is otherwise dormant.
2. **Should `enable_props` flip to True now?** The pipeline is complete
   and tested; it's gated for cost and noise control. `fetch_props` also
   doesn't yet honor `ScanConfig.sports[key].enabled`. The user owns
   this decision and the rollout plan.
3. **Live alerts go-live checkpoint.** Pushover end-to-end is verified via
   `POST /api/monitor/test`. The standing pre-flight before flipping
   `dry_run=false` autonomously is: observe at least one organic
   `[DRY RUN] Would send: …` payload in the backend log for a real BUY.
   Currently no organic BUYs surface under Conservative 8% threshold;
   wait for a Kalshi-active slate where edges naturally clear that bar.
4. **CLV backfill aggressiveness.** `useTrackedPositions` runs a backfill
   on every position change. For users with hundreds of tracked positions
   this could be slow. Currently fine.
5. **Snapshot lifetime.** Snapshots live until the backend process
   restarts. For a long-running monitor this is fine, but on restart the
   first request triggers a cold-start refresh which can take 5–60s
   depending on which books are enabled.
6. **`/model` page**. It exists but is detached from the scanner workflow.
   Read it before changing anything that crosses both pages.
7. **Alert allowlist alignment.** `MonitorConfig.platforms` now defaults
   to `["kalshi"]` to mirror `ScanConfig`. If the user re-enables
   Polymarket in `ScanConfig`, they should also add it to
   `MonitorConfig.platforms` (or the alert allowlist will silently drop
   Polymarket BUYs). Worth deriving one from the other automatically.
8. ~~**Kalshi 3-way CLV math**~~ — **Resolved** in `78bad19`: 3-way
   (draw) events now use each market's own mid in `price_history.py`.

---

## 18. One-line "where do I start" guide

- **To change a threshold** → `engine_config.py`.
- **To change a rule** → `rule_engine.py` (POLICY_TABLE).
- **To change matching** → `feature_extractor.py` (`_name_score`, `_date_score`,
  `_identify_yes_player`, `_find_best_span`, `_sport_compatible`).
- **To change platform ingestion** → `services/adapters/<platform>.py`.
- **To change alert behavior** → `services/monitor/manager.py`.
- **To change alert presets / config shape** → `services/monitor/config.py`.
- **To change scheduling** → `services/monitor/scheduler.py`.
- **To change which sports are fetched** → `services/sports_config.py`.
- **To change UI layout** → `frontend/app/components/dashboard/DashboardClient.tsx`.
- **To change tracked positions / CLV** → `frontend/lib/useTrackedPositions.ts`.
- **To trace a bug** → `GET /api/opportunities/debug` returns the full
  rule-by-rule trace for every candidate.
