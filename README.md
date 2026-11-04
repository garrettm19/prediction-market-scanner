# MasG EV Tool

Cross-platform EV scanner for prediction market opportunities. Compares prices on Polymarket and Kalshi against FanDuel sportsbook odds to find mispriced markets, with phone push notifications for new opportunities.

> **Current operating defaults (validated):**
> - **Books**: Kalshi enabled, Polymarket disabled. Polymarket is opt-in via the dashboard "Books" toggles or `POST /api/scan/config`.
> - **Alerts**: monitor scheduler off; defaults to `dry_run=true` and `MonitorConfig.platforms=["kalshi"]` when started. Live Pushover alerts stay off until at least one organic dry-run BUY payload has been observed.
> - **Player props**: disabled (`enable_props=False`) — full pipeline exists but gated.
> - **Soccer 3-way safety**: matches with no published FanDuel Draw odds are SKIPped at the feature layer; phantom EV from 2-way-devig fallback cannot occur.
> - **Pushover credential check**: `POST /api/monitor/test` sends a real Pushover **even when `dry_run=true`**. It is the deliberate credential-verification path; don't call it unless you want a real notification on your phone.
>
> See `MANUAL_TESTING_RUNBOOK.md` for the safe operating procedure and `PROJECT_CONTEXT.md` for full architecture context.

## Quick Start

### Prerequisites

- Python 3.12
- Node.js 18+
- API keys:
  - [The Odds API](https://the-odds-api.com/) (free tier: 500 req/month)
  - [Kalshi](https://kalshi.com/) API key (optional)
  - [Pushover](https://pushover.net/) for phone notifications (optional)

### 1. Clone and set up the backend

```bash
cd masg-ev-tool/backend
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Set up the frontend

```bash
cd masg-ev-tool/frontend
npm install
```

### 4. Run

**Option A** -- Use the start script (Windows):
```bash
start-dev.bat
```

**Option B** -- Manual (two terminals):
```bash
# Terminal 1: backend
cd backend
.venv\Scripts\activate
uvicorn main:app --reload --port 8000

# Terminal 2: frontend
cd frontend
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

### Offline demo mode (no API keys)

`MASG_DEMO_MODE=1` runs the real pipeline (adapters → `NormalizedMarket` → Pass A → series dedup / ambiguity → Pass B → Kelly) on a deterministic, clearly fictional dataset (`backend/services/demo/fixtures.py`). There are no requests to Kalshi, Polymarket or The Odds API, no notifications and no orders. The dashboard shows a "Demo mode" banner. Monitor start/test, enabling the paper maker, historical odds and the legacy `/api/odds/*` endpoints return 403. Price history comes back empty.

```bash
./start-demo.sh                                    # backend + dashboard (Windows equivalents are in the script header)
cd backend && python -m services.demo --all        # no Node: run the pipeline once, print a table
```

Loading the dashboard (or clicking Scan) fills the demo snapshot. SKIP rows are listed by `GET /api/opportunities/debug`. Polymarket demo markets (totals) show up only when the Polymarket "Books" toggle is on, the same as in live mode.

## How It Works

### Rule Engine Pipeline

Every opportunity goes through a two-pass evaluation:

**Pass A -- Feature Extraction**
- Fetches markets from Polymarket and Kalshi
- Fetches FanDuel odds via The Odds API
- Matches events across platforms by player/team names + date proximity
- Computes all pricing, confidence, and ambiguity metrics

**Pass B -- Rule Engine**
- Evaluates a centralized policy table (defined in `backend/services/rule_engine.py`)
- Each rule is CRITICAL (fail = SKIP) or DOWNGRADE (fail = WATCH)
- Classifies every opportunity as BUY, WATCH, or SKIP with full reasoning

### Classification

| Status | Meaning |
|--------|---------|
| **BUY** | All rules pass: positive edge above threshold, high confidence match, no ambiguity |
| **WATCH** | Positive edge but one or more quality/ambiguity concerns (e.g., edge too thin, confidence gap) |
| **SKIP** | Critical failure: no edge, live event, name mismatch, line mismatch, etc. |

### Key Metrics

- **Edge** = devigged true probability - market price - 1c cost buffer
- **True probability** = FanDuel odds after removing the bookmaker's vig (multiplicative devig)
- **Kelly** = fractional Kelly criterion (1/5 Kelly, capped at 25%) for position sizing
- **FD Confidence** = High/Medium/Low based on overround and line width

## Phone Alerts

The monitoring system scans for new BUY opportunities on a configurable interval and sends push notifications via Pushover. **Default state is `dry_run=true`** — the scheduler logs payloads to the backend instead of sending them. Live alerts are gated; see "Going live" below.

### Setup

1. Create a [Pushover](https://pushover.net/) account and install the app on your phone
2. Add your user key and app token to `backend/.env`
3. Open the dashboard and expand the **Phone Alerts** panel
4. Choose a preset (Conservative recommended for first runs)
5. Confirm the config shows `dry_run=true` before pressing **Start Monitoring**
6. Optional credential check (sends one real notification): `POST /api/monitor/test` — this endpoint **bypasses `dry_run` by design** and verifies your Pushover keys end-to-end. It does not start the scheduler or change config.

### How Alerts Work

- The system scans the enabled books (default Kalshi only) on the configured refresh interval (Conservative: every 30 minutes; WS-driven re-evaluations on price moves with 10s debounce)
- When a new BUY opportunity is found, the AlertManager builds a payload with the event, side, EV%, price, FanDuel odds, true probability, Kelly, and a deep link to the platform. In dry-run mode the payload is logged as `[DRY RUN] Would send: …` instead of sent.
- **Cooldown** prevents the same opportunity from buzzing your phone repeatedly. Once alerted about "Alcaraz +6.2% EV", that specific opportunity is silenced for the cooldown window (Conservative default: 60 minutes). Edge improvement ≥3% breaks through cooldown.
- **Hourly cap** limits the maximum number of alerts per rolling hour (Conservative: 5).
- Soccer 3-way matches with no FanDuel Draw odds SKIP at the feature layer and never reach AlertManager.

### Going live (gated)

Live alerts (`dry_run=false`) should remain off until:
1. At least one organic dry-run BUY payload has been logged so you can verify the format and deep link.
2. Manual spot-check on the platform of every flagged BUY's price, FD odds, and game timing.

When ready: `POST /api/monitor/config {"dry_run": false}` while keeping Conservative + Kalshi-only. See `MANUAL_TESTING_RUNBOOK.md` section 9 for the full procedure.

### Presets

| Preset | Min EV | Refresh | Cooldown | Max Alerts/hr |
|--------|--------|---------|----------|---------------|
| Conservative | 8% | 30 min | 60 min | 5 |
| Standard | 5% | 15 min | 30 min | 10 |
| Aggressive | 3% | 10 min | 15 min | 20 |

### API Budget

The Odds API free tier allows 500 requests/month. Each scan uses ~5 requests (one per sport). At 15-minute intervals that's ~20 requests/hour. Monitor the **API Budget** counter in the alerts panel to track usage.

## Supported Sports

The full list is the source of truth in `backend/services/sports_config.py`.
Sport availability depends on whether FanDuel publishes odds via the Odds API
on a given day; soccer rows additionally require FD draw odds (see soccer
guard below).

US team sports (2-way h2h):
- NBA, WNBA
- MLB
- NHL, AHL
- NFL, UFL
- Tennis (ATP/WTA tournaments)
- UFC / MMA
- Cricket IPL
- Rugby NRL
- AFL
- KBO

Soccer (3-way h2h: home / away / draw):
- MLS
- EPL
- Bundesliga
- La Liga
- Serie A
- Ligue 1
- UEFA Champions League

> Soccer rows with no FanDuel `Draw` odds are SKIPped at the feature layer
> via `NO_BOOKMAKER_DATA` to prevent 2-way-devig phantom EV. This is a
> safety guard, not a configuration choice.

## Project Structure

```
masg-ev-tool/
├── backend/
│   ├── main.py                        # FastAPI app
│   ├── routers/
│   │   ├── opportunities.py           # /api/opportunities
│   │   ├── monitor.py                 # /api/monitor (alerts)
│   │   └── odds.py                    # /api/odds (debug)
│   ├── services/
│   │   ├── engine_config.py           # All engine thresholds
│   │   ├── rule_engine.py             # Policy table + classification
│   │   ├── feature_extractor.py       # Pass A: feature extraction
│   │   ├── opportunities.py           # Two-pass pipeline
│   │   ├── normalizer.py              # Name normalization
│   │   ├── odds_provider.py           # FanDuel odds (The Odds API)
│   │   ├── matcher.py                 # Event matching
│   │   ├── devig.py                   # Vig removal
│   │   ├── sports_config.py           # Supported sports
│   │   ├── events.py                  # Polymarket Gamma API
│   │   ├── price_history.py           # Historical prices
│   │   ├── adapters/
│   │   │   ├── base.py                # MarketAdapter protocol
│   │   │   ├── polymarket.py          # Polymarket adapter
│   │   │   └── kalshi.py              # Kalshi adapter
│   │   └── monitor/
│   │       ├── config.py              # Alert presets + config
│   │       ├── manager.py             # Alert decisions + dedup
│   │       ├── notifier.py            # Pushover notifications
│   │       ├── state.py               # Alert state persistence
│   │       ├── scheduler.py           # Monitoring orchestrator
│   │       ├── odds_poller.py         # Scheduled Odds API poller
│   │       ├── ws_polymarket.py       # Polymarket WebSocket
│   │       └── ws_kalshi.py           # Kalshi WebSocket
│   ├── tests/
│   │   ├── test_rule_engine.py        # 115 rule engine tests
│   │   ├── test_feature_extractor.py  # 46 feature extraction tests
│   │   ├── test_normalizer.py         # 33 normalization tests
│   │   └── test_ambiguity.py          # 10 ambiguity detection tests
│   └── models/
│       └── market.py                  # Market data model
├── frontend/
│   ├── app/
│   │   ├── page.tsx
│   │   └── components/dashboard/
│   │       ├── DashboardClient.tsx
│   │       ├── OpportunitiesTable.tsx
│   │       ├── MarketDetailPanel.tsx
│   │       └── NotificationSettings.tsx
│   └── lib/
│       ├── api.ts                     # Opportunities API client
│       ├── monitor-api.ts             # Monitor API client
│       └── types.ts                   # TypeScript types
└── start-dev.bat
```

## API Endpoints

### Opportunities

| Endpoint | Description |
|----------|-------------|
| `GET /api/opportunities` | Matched opportunities with edge/kelly/status (cold-start runs pipeline once) |
| `GET /api/opportunities/snapshot` | Read latest snapshot (never triggers pipeline; 204 if empty) |
| `GET /api/opportunities/status` | Lightweight status, refresh metadata, error state |
| `POST /api/opportunities/refresh` | Trigger background pipeline run (returns 202 immediately) |
| `GET /api/opportunities?platform=kalshi` | Filter by platform (view-only) |
| `GET /api/opportunities/policy` | Current rule policy table |
| `GET /api/opportunities/history` | Price history for a market (CLV) |
| `GET /api/opportunities/debug` | Full pipeline diagnostic trace |

### Scan / Books

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/scan/config` | GET | Per-sport + per-platform scan config + cache state |
| `/api/scan/config` | POST | Update scan config (sports / platforms / quota) |
| `/api/data-status` | GET | Per-sport per-book data freshness matrix |
| `/api/sports` | GET | Static sport registry |

### Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/monitor/config` | GET | Current alert settings |
| `/api/monitor/config` | POST | Update settings (does not start/stop) |
| `/api/monitor/start` | POST | Start monitoring loop |
| `/api/monitor/stop` | POST | Stop monitoring loop |
| `/api/monitor/status` | GET | Live status, countdown, API budget |
| `/api/monitor/history` | GET | Recent alert history |
| `/api/monitor/test` | POST | Send a real Pushover test notification — **bypasses `dry_run`** |

## Configuration

Engine thresholds live in `backend/services/engine_config.py`. Per-sport
overrides (e.g. tighter `max_date_delta_hours` for liquid team sports,
sport-specific `max_plausible_edge` caps) live in
`backend/services/sports_config.py`. Default values:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_edge` | 0.05 | Minimum edge for BUY (5%) |
| `wide_market_min_edge` | 0.08 | Edge required for wide lines (8%) |
| `survival_threshold` | 0.85 | Match confidence below this = SKIP |
| `buy_threshold` | 0.90 | Match confidence below this = WATCH |
| `min_true_probability` | 0.40 | Devigged probability floor |
| `max_date_delta_hours` | 72.0 (12.0 for team sports) | Max time gap between platform market and FD event |

To add a new sport, edit `backend/services/sports_config.py`. To toggle
which platforms are scanned, use the dashboard "Books" toggles or
`POST /api/scan/config`.

## Tests

```bash
cd backend
.venv\Scripts\activate
python -m pytest tests/ -v
```

The full backend suite covers policy rules, normalization, feature
extraction, ambiguity detection, soccer alignment + missing-draw guard,
platform toggles, snapshot lifecycle / background refresh, scheduler /
manual refresh lock alignment, Polymarket CLOB book parsing, and Kalshi
adapter behaviors. Run `pytest tests/` to see the current passing count;
all suites should be green on `main`.
