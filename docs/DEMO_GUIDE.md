# Offline Demo Guide

This guide is a 3–5 minute technical walkthrough of MasG EV Tool that needs no
accounts, API keys or paid requests.

## What demo mode is

Setting `MASG_DEMO_MODE=1` swaps the **data sources** and nothing else. The
code path is the production one:

```
DemoMarketAdapter ──► NormalizedMarket ──┐
                                         ├─► Pass A ─► series dedup ─► ambiguity ─► Pass B ─► Kelly ─► snapshot ─► REST API ─► dashboard
demo odds fetcher ──► FanDuel events ────┘     (same functions the live scanner uses)
```

- **Injection points.** `fetch_opportunities()` takes an `adapters` list and
  an `odds_fetcher`, and `services/snapshot.refresh_snapshot()` passes the
  demo versions of both when demo mode is on. No BUY/WATCH/SKIP status is hard-coded: the real rule engine
  produces every one of them.
- **Synthetic data.** All teams and players are invented, every market id
  contains `DEMO`, and no market links to a real exchange page. Prices and odds
  are fixed. Event times are set relative to the current time, so the games are
  always in the future and the results are the same on every run.
- **Isolation.**
  - No outbound requests. The tests patch `httpx`, `websockets` and the odds
    fetchers to raise, and the demo still runs.
  - No notifications: `POST /api/monitor/start` and `/api/monitor/test` return
    `403`.
  - The paper maker cannot be enabled.
  - Legacy and historical-odds endpoints are disabled.
  - Your real alert history and paper-maker records under `backend/data/` are
    never shown or written.
- **Labeling.** The API reports `demo_mode: true` and `data_source: "demo"`,
  and the dashboard shows a permanent **DEMO MODE** banner.

Source: [`backend/services/demo/`](../backend/services/demo/). The fixtures are
in `fixtures.py`, and each one carries a comment naming the rule it exercises.
The guards are in `guards.py`, and the CLI is in `__main__.py`. Tests are in
[`backend/tests/test_demo_mode.py`](../backend/tests/test_demo_mode.py).

## Start it

**macOS / Linux / Git Bash.** Requires Python 3.12+ and Node 18.18+.

```bash
cd backend && pip install -r requirements.txt && cd ..
cd frontend && npm install && cd ..
./start-demo.sh
```

The script starts the backend on `127.0.0.1:8000` and the dashboard on
<http://localhost:3000>. Opening the dashboard runs the first scan
automatically; the **Scan** button re-runs it.

**Windows (cmd).** Use two terminals (this assumes a `backend\.venv`
virtualenv with `requirements.txt` installed):

```bat
cd backend && set "MASG_DEMO_MODE=1" && .venv\Scripts\activate && uvicorn main:app --host 127.0.0.1 --port 8000
cd frontend && set "NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:8000" && npm run dev
```

**No Node.js?** Print the pipeline output in the terminal:

```bash
cd backend
MASG_DEMO_MODE=1 python -m services.demo --all
```

## Walkthrough (about 4 minutes)

### 1. The dashboard (30 s)

Open <http://localhost:3000>. The amber banner confirms demo mode. The status
strip shows the following:

- **Scan** shows the snapshot's age.
- **Books** shows `Kalshi`, because Kalshi is the only platform enabled by
  default.
- **Alerts** shows `Off (dry-run)`. Dry-run is the default.

The opportunities table lists BUY and WATCH rows sorted by edge. SKIPs are
left out of the snapshot; they are counted only in the API's `status_counts`
and shown in the diagnostic trace (step 4).

> The default **Min Edge 4%** view filter hides the two rows whose edge is
> below 4%. Set it to `0` to see all six Kalshi rows (the filter
> applies immediately; no rescan is needed).

### 2. A BUY, traced end to end (60 s)

Click **Cedar Falls Foxes vs Port Avalon Pilots**. The detail panel shows the
chain of evidence that every BUY has to carry:

| Step | What you see | Where it comes from |
|---|---|---|
| Matched event and side | Foxes, MLB, 100% event match | Pass A name and date scoring (`feature_extractor.py`) |
| FanDuel reference | −135, 4.0% overround, line width | Sportsbook odds; the overround and line width feed a confidence label |
| Model probability | 55.3% | Multiplicative devig of the FanDuel pair (`devig.py`) |
| Entry price | 50.0¢ (+1¢ Kalshi cost buffer) | `NormalizedMarket.price` from the adapter |
| Edge | +4.26% = 0.553 − (0.50 + 0.01) | `edge = p_model − effective_price` |
| Size | 1.74% Kelly | Kelly fraction `(p − pₑ)/(1 − pₑ)`, capped at 0.25, then scaled by 0.20 |
| Policy result | BUY, no failed rules | `rule_engine.POLICY_TABLE` |

This market is also a **series-dedup** case. FanDuel lists Game 1 and Game 2
of the series, and the market's name matches both. `_dedup_to_best_date_match`
keeps Game 2, the game at the market's date.

### 3. Why a positive edge is *not* a BUY (60 s)

Click the WATCH rows. Each fails a different downgrade rule, and the **Rule
Trace** names it:

| Row | Edge | Rule | Why the pipeline refuses to call it a BUY |
|---|---|---|---|
| Marin Holloway vs Holloway Strand | +6.0% | `LAST_NAME_COLLISION` | One player's last name appears in the opponent's name, so a last-name mapping could select the wrong player. |
| Northgate Narwhals (NHL) | +12.6% | `EDGE_IMPLAUSIBLE` | The edge is above the NHL plausibility cap (12%). Large edges in liquid markets are treated as probable data errors. |
| Rowan Quillfeather (ATP) | +6.0% | `FD_CONFIDENCE_LOW` | FanDuel's own line carries about 6.8% margin, so the reference price itself is low-confidence. |
| Kingsbridge Harriers (EPL) | +3.5% | `EDGE_BELOW_THRESHOLD` | This is a 3-way soccer market. Devigging with the draw gives 43.0%. A 2-way devig would have claimed about 59.5%, a phantom 20% edge. |

### 4. What gets rejected outright (45 s)

SKIPs are visible through the diagnostic trace: run
`python -m services.demo --all`, or call `GET /api/opportunities/debug`. The
following are worth pointing out:

- **`NO_BOOKMAKER_DATA`.** The MLS 3-way game has no published draw price. It
  is skipped rather than priced with a 2-way devig.
- **`DATE_TOO_FAR`.** The Game 3 market's nearest FanDuel game is 24 hours
  away, so its apparent 9% edge is a wrong-game comparison.
- **`PRICE_PROB_DIVERGENCE`.** A market quotes the heavy favourite at 0.30.
  A gap that large between price and probability suggests the YES/NO sides are
  inverted.
- **`LINE_MISMATCH`.** A totals market at 226.5 must not be priced from
  FanDuel's 221.5 line. This and the next case are Polymarket fixtures; the
  CLI and the debug endpoint include them whatever the **Books** setting.
- **`CONFIDENCE_BELOW_SURVIVAL`.** An undated Polymarket market reaches only
  0.825 match confidence (0.65 × name + 0.35 × date, with date scored 0.5),
  below the 0.85 survival threshold.

The debug endpoint skips series dedup, so it also shows the Game 1 pairings
that the production path drops.

### 5. Platform toggle and API surface (30 s)

- **Platform toggle.** Tick **Polymarket** under **Books** and press **Scan**.
  A Polymarket totals BUY (Over 221.5) appears in the table. (The Polymarket
  SKIPs stay out of the table, as all SKIPs do; see step 4.) The backend honours the toggle, so the UI
  never shows a setting the backend ignores.
- **API surface.** The same data is available from the API:

```bash
curl -s localhost:8000/api/opportunities/status   # demo_mode, refresh state, degraded flag
curl -s localhost:8000/api/opportunities/snapshot # opportunities + per-rule results
```

### 6. Tests behind it (30 s)

```bash
cd backend && python -m pytest tests/test_demo_mode.py -q
```

The demo tests pin each fixture's status and reason codes. They also prove
that a demo refresh makes no network calls, and that the live code path is
unchanged when demo mode is off.

## Known demo-mode limitations

- The **Data Sources** matrix shows Kalshi counts but no FanDuel freshness. The
  demo odds fetcher deliberately bypasses the shared odds cache so that demo
  data can never mix with cached live odds.
- The demo odds fetcher ignores per-sport scan toggles. Platform toggles are
  honoured.
- `POST /api/scan/config` and `POST /api/monitor/config` still work in demo
  mode, because they only change in-memory settings.
