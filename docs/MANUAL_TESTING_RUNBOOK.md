# MasG EV Tool — Manual Testing Runbook

> Practical operator guide for running the scanner, interpreting output,
> and deciding whether to act on a BUY. Companion to `README.md` (setup),
> `CLAUDE.md` (project rules), and `PROJECT_CONTEXT.md` (architecture).
>
> **Validated operating state (current default):**
> - **Books**: Kalshi ON, Polymarket OFF. Polymarket is opt-in only via the
>   dashboard "Books" toggles or `POST /api/scan/config`. Default scans never
>   call the Polymarket adapter — no Gamma fetch, no CLOB calls.
> - **Alerts**: Monitor scheduler is OFF by default. When started, defaults
>   to `dry_run=true` and `MonitorConfig.platforms=["kalshi"]`.
> - **Pushover end-to-end**: verified — `POST /api/monitor/test` was sent
>   successfully. Credentials and notification delivery confirmed working.
> - **Live alerts**: still off until a real dry-run BUY payload is observed.
>   See section 9 — `POST /api/monitor/test` is the ONLY real-Pushover send
>   that should happen until that checkpoint passes.
> - **Soccer missing draw odds**: feature extractor SKIPs candidates as
>   `NO_BOOKMAKER_DATA` to prevent 2-way-devig phantom EV.
> - **Player props**: disabled (`enable_props=False`).

---

## 1. Starting the backend and frontend

### Backend
```bash
cd backend
.venv\Scripts\activate                    # Windows
# source .venv/bin/activate                # macOS/Linux
uvicorn main:app --port 8000              # add --reload for auto-restart on edits
```
Wait until you see `Uvicorn running on http://127.0.0.1:8000`. Probe:
```bash
curl -s http://localhost:8000/api/opportunities/status
# → {"has_snapshot":false,"updated_at":null,...}    (cold start, expected)
```

### Frontend
```bash
cd frontend
npm install                               # first time only
npm run dev
```
Open http://localhost:3000.

### Both together (Windows convenience)
```cmd
start-dev.bat
```
Two terminals open, backend on :8000, frontend on :3000.

### Required env vars (in `backend/.env`)
```
ODDS_API_KEY        = <your-key>          # the-odds-api.com — required for any scan
GAMMA_API_BASE      = https://gamma-api.polymarket.com
KALSHI_API_KEY      = <your-key>          # optional but recommended
PUSHOVER_USER_KEY   = <your-key>          # optional, only if you ever enable live alerts
PUSHOVER_API_TOKEN  = <your-token>
```
Without `ODDS_API_KEY`, the pipeline fails with 503 because there is no FanDuel truth source.

### Stopping
- Frontend: `Ctrl+C` in the npm terminal.
- Backend: `Ctrl+C` in the uvicorn terminal. On Windows, if uvicorn was background-launched, stop with PowerShell `Stop-Process -Id <pid>` or `taskkill /F /PID <pid>`. Confirm with `netstat -ano | findstr :8000`.

---

## 2. Running a manual scan

Two paths.

### Books toggles (Kalshi / Polymarket)
The toolbar exposes a **Books** control with one checkbox per platform.
Defaults read from `GET /api/scan/config`:
- **Kalshi: ON**
- **Polymarket: OFF**

Toggling a checkbox optimistically updates the UI, posts to
`POST /api/scan/config` with `{"platforms": {"<name>": {"enabled": <bool>}}}`,
and immediately filters out the disabled platform's rows from the table.
The next Scan respects the new state — disabled platforms are not fetched
at all (no API calls). Disabling both books greys out the Scan button with
a tooltip; the cold-start panel shows "Enable at least one book…".

Polymarket is opt-in: enable it only when you specifically want CLOB-priced
Polymarket opportunities for that session. Re-disable when done so future
scans default back to Kalshi-only.

### From the dashboard (preferred)
1. Open http://localhost:3000.
2. Confirm the Books toggles match what you intend to scan.
3. Click **Scan** in the toolbar (top-right). The button switches to "Scanning..." and the dashboard polls `/api/opportunities/status` every 2 seconds while waiting. Background refresh started in commit `c4d9035` — POST returns 202 immediately and the scan runs in the background.
4. Wait 5–60 seconds depending on cache state and which books are enabled (Kalshi-only ≈ 5s; Kalshi + Polymarket ≈ 45s due to CLOB hydration).
5. When the snapshot lands, the toolbar status switches to "Last updated Xs ago" (local time) and the table populates. The Age column briefly shows "…" while the new snapshot is being applied.

### From the API directly
```bash
# Trigger a fresh scan (invalidates the FD odds cache).
curl -X POST -H "Content-Type: application/json" \
  -d '{"scope":"all"}' \
  http://localhost:8000/api/opportunities/refresh

# Read the latest snapshot.
curl -s http://localhost:8000/api/opportunities/snapshot | python -m json.tool
```

`scope` accepts:
- `"stale"` (default) — only re-fetch sports whose TTL expired. Cheapest.
- `"all"` — invalidate every cached sport, fetch everything fresh. Use sparingly.
- `"<sport_key>"` — invalidate one sport (e.g. `"baseball_mlb"`).

### Snapshot envelope at a glance
```json
{
  "opportunities": [...],          // post-dedup BUY+WATCH only
  "total": 54,
  "status_counts": {               // pre-dedup feature counts
    "BUY": 4, "WATCH": 55, "SKIP": 720
  },
  "quota_remaining": "17502",      // Odds API budget left
  "platforms_fetched": ["polymarket", "kalshi"],
  "markets_dropped_by_type": {     // adapter-side classifier filters
    "outright": 4477, "unknown": 4399, "prop": 962, ...
  },
  "updated_at": 1777590358.94,
  "is_refreshing": false
}
```

---

## 3. Interpreting BUY vs WATCH

Every opportunity is classified by `evaluate_rules` against the centralized policy table in `backend/services/rule_engine.py`.

| Status | Meaning | Reaches dashboard? | Reaches alerts? |
|---|---|---|---|
| **BUY** | Every rule passes | Yes | Yes (when alerts enabled) |
| **WATCH** | At least one DOWNGRADE rule failed; no CRITICAL failures | Yes | **No — never** |
| **SKIP** | At least one CRITICAL rule failed | No (filtered out) | No |

### CRITICAL rules (failing one → SKIP)
- `price_range`, `not_live`, `has_bookmaker`
- `name_match`, `outcome_alignment`, `confidence_survival`
- `line_match`, `unit_match`, `side_match` (totals/handicap only)
- `date_not_stale` (per-sport limit; team sports = 12h, tennis/MMA = 72h)
- `price_prob_coherence`, `positive_edge`, `min_true_prob`

### DOWNGRADE rules (failing one → WATCH instead of BUY)
- `confidence_buy`, `edge_threshold`, `fd_confidence`, `edge_plausible`
- `confidence_gap`, `shared_last_name`, `competing_matches`, `last_name_collision`
- `metadata_complete`, `prices_consistent`

**WATCH is not a soft-BUY.** It is the system telling you "I'd flag this if you asked, but at least one safety filter failed." Never act on a WATCH without first understanding the specific reason in `downgrade_reasons`.

Read each opportunity's `rule_evaluations` array (returned by the API and visible in the detail panel) to see exactly which rules passed/failed.

---

## 4. Manual verification checklist before acting on a BUY

A BUY is necessary but not sufficient. Always confirm in this order:

1. **Click `View on Polymarket` / `View on Kalshi`** in the dashboard detail panel. The platform should load the actual market.

2. **Read the platform question text yourself.**
   - Does it match the event in the snapshot (e.g. `Toronto Raptors vs Cleveland Cavaliers`)?
   - Is it really a moneyline / h2h question, not a spread, total, alt-line, futures, or prop?
   - Does the question subject (the YES side) match `side` in the snapshot?

3. **Confirm the date.**
   - The platform market should resolve on the same calendar day (or near-same-day) as the FanDuel event start.
   - For Polymarket, check the event slug — `mlb-ari-nym-2026-04-07` should match the scheduled game.
   - If `date_delta_hours > 12` for an MLB/NBA/NHL/etc. team-sport BUY, the system should already have SKIP'd it. If you see one, that's a regression — file it.

4. **Confirm the price still matches.**
   - Polymarket and Kalshi prices move continuously. The snapshot is at most 15 minutes old (default scan cadence) and may be staler if you haven't refreshed.
   - Click **Scan** to refresh, or check `price_fetched_at` on the opportunity.
   - If the platform price has moved meaningfully (e.g. > 1¢) since the snapshot, the listed edge is wrong.

5. **Check `event_match_confidence` ≥ 0.90.**
   - 1.000 = perfect same-day name + date match.
   - 0.948 = name match perfect, date in 1–7 day window (lenient sports only).
   - Below 0.90 will already be WATCH or SKIP, but always glance at it.

6. **Check `competing_matches`, `confidence_gap`, `has_shared_last_name`, `has_*_collision`.**
   - All should be benign on a BUY (the rules already filter), but the rule trace makes the reasoning auditable.

7. **Check the FanDuel side of the trade.**
   - `fd_odds` and `p_true` are listed in the detail panel. Confirm they are reasonable for the matchup.
   - If `p_true` looks wrong (e.g. an obvious favorite has p_true < 0.5), suspect a side-inversion. The `price_prob_coherence` rule guards against the worst cases but isn't infallible.

8. **Sanity-check the edge size.**
   - 3–8% on liquid sports (NBA/MLB/NHL) is plausible Polymarket inefficiency.
   - 10%+ on a same-day team-sport h2h is suspicious. Verify everything above twice.
   - 15%+ should already be DOWNGRADE'd by `edge_plausible` to WATCH for liquid sports. If a BUY shows >15% edge in MLB or >12% in NBA/NHL, that's a regression — file it.

If any check fails, do not bet. Re-scan, re-verify, and if uncertain, skip.

---

## 5. Red flags ("do not bet")

Stop and **do not place the trade** if any of the following are true:

- The platform's question text describes a different market type (spread/total/prop/futures) than h2h.
- The platform's question subject names the **opposite** team/player from `side`.
- The platform's market date is a different calendar day than the FanDuel event start, *unless* the sport is tennis/MMA where settlement timing legitimately varies.
- The platform price has moved more than ~2¢ since `price_fetched_at`.
- The event has already started (live betting is unsupported and should already be SKIP'd; if you see a live event in BUY, that's a bug).
- The edge looks "too good" — anything ≥ 20% across any sport, anything ≥ 12% on NBA/NHL, anything ≥ 15% on MLB/NFL. The rule engine downgrades these, but if one slips through to BUY status, do not bet — file the regression.
- `match_quality` is "unverified".
- The PM or Kalshi market is illiquid (low volume, wide bid/ask, near-zero recent trades). Liquidity isn't yet a hard rule but is a real risk; check the platform UI before committing.
- `competing_matches > 1` and `confidence_gap < 0.10`. Indicates the matcher saw two close-quality FD events; the rules already DOWNGRADE these to WATCH, but if one reaches BUY by some other path, treat as suspicious.
- Anything in `downgrade_reasons` or `reject_reasons` for a BUY (should be empty by definition; non-empty = bug).
- The dashboard shows "Updated 30m ago" or older. Re-scan first.

---

## 6. Checking data status

```bash
curl -s http://localhost:8000/api/data-status | python -m json.tool
```

Or visually: the **Data Status** matrix on the dashboard.

What to expect, per sport, per book:
- **FanDuel**: `event_count > 0` for in-season sports. Cache age should be under ~15 minutes after a scan. If FD `event_count = 0`, that sport will produce zero opportunities (no truth source).
- **Polymarket**: only sports Polymarket actually lists. Currently MLB, NBA, NHL, KBO, Tennis, MMA tend to have data; soccer leagues and WNBA usually do not.
- **Kalshi**: `event_count` is the **normalized** count actually entering the pipeline. `raw_count` is what Kalshi's API returned before normalization. A nonzero gap (`raw_count > 0`, `event_count = 0`) for soccer leagues is the **known soccer Kalshi normalization gap** — see section 10.

If a sport you expect data for shows zero across all three books, check:
1. Is that sport in `enabled=True` in `backend/services/sports_config.py`?
2. Is `ODDS_API_KEY` valid and not quota-exhausted? See section 8.
3. For Polymarket, does the relevant `pm_tag` exist? Some sports just don't have Polymarket markets at any given moment.
4. For Kalshi, does `KALSHI_API_KEY` work for the v2 trade-api markets endpoint?

---

## 7. Debugging a suspicious opportunity

The richest source is `GET /api/opportunities/debug`. It runs a fresh pipeline and returns every candidate's full rule trace, including SKIPs.

```bash
curl -s "http://localhost:8000/api/opportunities/debug" | python -m json.tool > /tmp/debug.json
# Then grep / jq through /tmp/debug.json
```

Useful slices:
```bash
# All features for a specific event
jq '.pass_b_classification.evaluated[] | select(.event | contains("Cleveland"))' /tmp/debug.json

# Status counts pre-dedup
jq '.pass_b_classification.status_counts' /tmp/debug.json

# Stage 1 fetch totals (raw markets per platform, FD events, drops)
jq '.stage_1_fetch' /tmp/debug.json

# All FD event IDs that matched a given player or team
jq '[.pass_b_classification.evaluated[] | select(.event | contains("Tampa Bay"))] | .[] | {event,side,delta:.date_delta_hours,event_id:.matched_event_id,status,reasons:.reject_reasons}' /tmp/debug.json
```

The debug endpoint also returns `policy_table` (every rule's stage/severity/reason_code/description) and `config` (the EngineConfig snapshot used for this run).

For a single opportunity already in the snapshot, the dashboard detail panel shows the rule-by-rule trace.

### Common diagnoses
- "Why is this WATCH?" → look at `downgrade_reasons`. Most common: `EDGE_BELOW_THRESHOLD`, `FD_CONFIDENCE_LOW`, `EDGE_IMPLAUSIBLE`, the four ambiguity rules.
- "Why is this SKIP?" → look at `reject_reasons`. Most common: `NO_EDGE`, `NAME_MISMATCH`, `OUTCOME_NOT_ALIGNED`, `CONFIDENCE_BELOW_SURVIVAL`, `DATE_TOO_FAR`, `PRICE_PROB_DIVERGENCE`, `EVENT_LIVE`.
- "Why is this BUY when I think it shouldn't be?" → check `home_tokens`, `away_tokens`, `name_match_score`, `event_match_confidence`, `date_delta_hours`, `competing_matches`, `confidence_gap`. If everything looks legitimately clean, the system is doing its job and the BUY is the system's best estimate — your job is the section 4 verification before acting.

---

## 8. Keeping Odds API usage under control

The free The Odds API tier is **500 requests/month**. Every cycle that fetches FanDuel odds spends roughly 1 credit per `(sport_key, market_type)` combination. With 16 enabled sports, a full scope=all scan costs ~16–20 credits.

### Watching the budget
- The snapshot envelope contains `quota_remaining` (response header `X-Odds-Quota-Remaining` echoes the same).
- Dashboard toolbar shows "X API req left" when present.
- `GET /api/monitor/status` includes `odds_poller.polls_today` and `polls_remaining` (daily budget guard).

### Levers
| Want to spend less | What to do |
|---|---|
| Stop fetching a sport you don't bet | `POST /api/scan/config {"sports": {"<sport_key>": {"enabled": false}}}` |
| Increase TTL on a sport | `POST /api/scan/config {"sports": {"<sport_key>": {"odds_ttl_seconds": 1800}}}` (default 900s) |
| Reduce monitor cadence | `POST /api/monitor/config {"refresh_interval_minutes": 30}` (Conservative default) |
| Reduce daily Odds API ceiling | `POST /api/monitor/config {"odds_api_max_polls_per_day": 10}` |
| Avoid manual `scope=all` | Use `scope=stale` (the default) so cached sports are skipped |

### Expected ranges
- **Manual `scope=stale`**: 0 credits if everything is fresh; up to ~16 if all caches expired.
- **Manual `scope=all`**: ~16–20 credits.
- **Monitor on Conservative (30 min)**: ~32 credits/hour worst case = ~770/day at full uptime.
- **Monitor on Standard (15 min)**: ~64 credits/hour = ~1500/day. Will exhaust the free tier quickly. Use only if you have a paid plan.

Inspect cache age via `GET /api/scan/config` — it returns the `odds_cache` dict with per-sport `age_seconds` and quota snapshot.

---

## 9. Alert workflow: Conservative dry-run is the standing default

**Live phone alerts remain off in regular operation.** The only live Pushover
sends that have happened are deliberate `POST /api/monitor/test` calls for
credential verification — those bypass `dry_run` by design and are the
intended way to confirm the notification path before flipping for real.

### Current verified state
- **Pushover credential / network path**: verified end-to-end via
  `POST /api/monitor/test` → `{"sent": true}`, HTTP 200, log:
  `Pushover: sent 'MasG EV Tool — Test' (priority=0)`. Phone receipt
  confirmed.
- **Conservative dry-run loop**: validated across multi-cycle windows.
  AlertManager filtering, cooldown, hourly cap, BUY-only gate, ambiguity
  exclusion, and the soccer missing-draw guard all behave correctly.
  Phantom MLS alerts are no longer producible.
- **Defaults aligned**: `MonitorConfig.platforms = ["kalshi"]` mirrors
  `ScanConfig.platforms` so the alert allowlist cannot drift from the scan
  layer (commit `31d13b6`).
- **Backend test suite**: 752 passed (full backend `pytest tests/`).
- **Soccer phantom-EV class**: structurally blocked. Soccer candidates with
  `fd.draw_odds=None` SKIP via `NO_BOOKMAKER_DATA` at the feature layer
  (commit `04e9013`). Both teams of an MLS match cannot both surface BUY.

### How to dry-run
```bash
# 1. Set safe config (Conservative + dry_run + Kalshi-only)
curl -X POST -H "Content-Type: application/json" \
  -d '{"preset":"conservative","dry_run":true,"platforms":["kalshi"]}' \
  http://localhost:8000/api/monitor/config

# 2. Confirm before starting — explicitly check dry_run is true
curl -s http://localhost:8000/api/monitor/config | python -m json.tool

# 3. Start (runs an immediate cycle plus future scheduled cycles)
curl -X POST http://localhost:8000/api/monitor/start

# 4. Inspect
curl -s http://localhost:8000/api/monitor/status   | python -m json.tool
curl -s http://localhost:8000/api/monitor/history  | python -m json.tool

# 5. Stop when done
curl -X POST http://localhost:8000/api/monitor/stop

# 6. Restore safe config (Conservative + dry_run + Kalshi-only + max=5)
curl -X POST -H "Content-Type: application/json" \
  -d '{"preset":"conservative","dry_run":true,"platforms":["kalshi"],"max_alerts_per_hour":5}' \
  http://localhost:8000/api/monitor/config
```

Dry-run alerts appear in the uvicorn log as:
```
INFO:services.monitor.notifier:[DRY RUN] Would send: ⚡ +10.7% EV — Cleveland Cavaliers …
INFO:services.monitor.manager:AlertManager cycle: 53 evaluated → 2 passed filters → 2 sent (2 new, 0 re) · 0 cooldown · ...
```

### `POST /api/monitor/test` — IMPORTANT BEHAVIOR
This endpoint **bypasses `dry_run`** by design and sends a real Pushover
notification regardless of the current `dry_run` flag. Use it to validate
credentials and the notification path. It does NOT mutate any state
(no scheduler start, no config change). Calling it when you don't want a
real notification on your phone is the only way to misuse it.

### What must NOT be done casually
- Do **not** set `dry_run=false` and start the monitor until at least
  **one real dry-run BUY payload has been observed in the backend log** —
  i.e., a `[DRY RUN] Would send: …` line for an organic BUY (not test
  payload). This confirms the payload format / deep link / Kelly figures
  on a real candidate before any alert hits your phone autonomously.
- Do **not** disable the BUY-only filter, the ambiguity-exclusion filter,
  or the cooldown.
- Do **not** raise `max_alerts_per_hour` above 5 while still in Conservative.
- Do **not** enable Polymarket via `MonitorConfig.platforms` without also
  enabling it in `ScanConfig` (otherwise the alert allowlist accepts a
  platform that's never scanned — harmless, but indicates drift).

### Path to autonomous live alerts (gated)
1. Run Conservative dry-run across a Kalshi-active window. Wait for at
   least one organic BUY payload to surface in the log
   (`[DRY RUN] Would send: …` for a real opportunity, not test).
2. Manual spot-check on Kalshi of every flagged BUY's price, FD odds,
   and game timing — confirm dashboard data matches the platform UI.
3. Only after step 2 succeeds: `POST /api/monitor/config {"dry_run": false}`
   while keeping Conservative + Kalshi-only.
4. Verify the first 1–2 live alerts deliver and content matches the dashboard.
5. Only then consider Standard preset, Polymarket inclusion, or relaxed
   thresholds.

### Limited live test pattern (pre-flight + restore)
For a bounded one-hour real-Pushover window (e.g., to validate the live
path with a tighter rate cap):
1. Pre-flight: `running=false`, `enabled=false`,
   `has_pushover_credentials=true`, `ScanConfig.polymarket.enabled=false`.
2. Apply `{"preset":"conservative","dry_run":false,"platforms":["kalshi"],
   "max_alerts_per_hour":2}` to limit blast radius.
3. `POST /monitor/start` and watch the backend log for `Pushover: sent` /
   errors / `Monitor cycle […]` lines.
4. After the window, `POST /monitor/stop` then restore the safe Conservative
   dry-run config (step 6 of the dry-run procedure above).

### Current safe manual scan checklist
Before clicking Scan or trusting any BUY, verify each of these holds:

**Operating state**
- [ ] `GET /api/scan/config` shows `kalshi.enabled=true` and
      `polymarket.enabled=false` (unless you intentionally enabled
      Polymarket for this session).
- [ ] `GET /api/monitor/config` shows `dry_run=true` and `enabled=false`
      (unless you're inside a deliberate live-test window).
- [ ] `GET /api/opportunities/status` shows `last_refresh_error=null`
      after the most recent refresh.
- [ ] `platforms_fetched` in the snapshot matches what you intended
      (Kalshi-only by default; both books only if Polymarket was toggled on).

**Snapshot sanity**
- [ ] No team-sport BUY/WATCH has `date_delta_hours > 12` (per-sport cap).
- [ ] No exact 0.50¢ Polymarket row that lacks `bid_ask_spread` (CLOB books
      should populate spread; Gamma 0.5/0.5 stale rows are dropped).
- [ ] No row with `bid_ask_spread > 0.05` (5¢) on Polymarket (the adapter
      should reject these).
- [ ] No soccer BUY with both teams of the same match showing positive EV
      (the soccer missing-draw guard should make this impossible).
- [ ] No `EDGE_IMPLAUSIBLE` BUY (DOWNGRADE rule should turn these into
      WATCH; a BUY past it would indicate sport-cap mislookup regression).

**Per-row before betting** — see section 4's full checklist.

---

## 10. Current known limitations

### Player props are disabled
- `EngineConfig.enable_props = False` by default.
- The full prop pipeline (Polymarket prop markets ↔ FanDuel player props) is implemented and tested but gated.
- Action: do not flip `enable_props=True` for ad-hoc testing. Plan first.
- `fetch_props` does not currently honor `ScanConfig.sports[key].enabled`; tracked as a follow-up to wire up before any rollout.

### Soccer requires draw odds for actionable signal
- Soccer h2h is a 3-way market (home/away/draw). When FanDuel's Odds API
  returns only home/away (no `Draw` outcome), `feature_extractor.py` SKIPs
  the candidate via `NO_BOOKMAKER_DATA` rather than 2-way-devigging
  (which would inflate both teams' p_true by the missing draw mass).
- Effect: matches without published FD draw odds simply don't appear in
  BUY/WATCH. They count toward `SKIP` in `status_counts`. This is correct
  behavior — see the phantom-EV history in `commit 04e9013`.
- For comparison: tennis, MMA, MLB, NBA, NHL, NFL, etc. are 2-way and
  unaffected by this guard.

### Polymarket scanning is opt-in
- ScanConfig defaults to `polymarket.enabled=False`. Default scans never
  call PolymarketAdapter — no Gamma fetch, no CLOB calls, no Polymarket
  rows in the snapshot.
- Enable via the dashboard "Books" toggle or `POST /api/scan/config
  {"platforms": {"polymarket": {"enabled": true}}}`.
- When enabled, refresh time grows from ~5s (Kalshi-only) to ~45s due
  to CLOB book hydration with bounded concurrency (50 in-flight).

### Kalshi 429 rate-limit during long polling windows
- Observed during the 90-minute live test: Kalshi returns HTTP 429 for
  some series tickers (KXEPLGAME, KXLIGUE1GAME, KXLALIGAGAME, etc.) when
  the adapter polls many series in quick succession.
- The adapter handles this gracefully: per-series error sets
  `raw_per_series[ticker] = -1`, the rest of the scan proceeds, and
  `last_refresh_error` stays null. Soft-failure semantics are correct.
- Effect: occasional gaps in coverage for specific series during peak
  polling. Tomorrow's scan typically picks up those series cleanly once
  the rate-limit window resets.
- Action: nothing. If the issue grows (more 429s, more series affected),
  consider reducing concurrency or adding per-series exponential backoff.

### Stale alert-state records
- `backend/data/alert_state.json` accumulates records across sessions.
  Both live AND dry-run sends increment `alerts_sent_this_hour`, so
  records from a recent dry-run session can briefly consume the rolling
  hourly cap budget after switching to live.
- Self-heals after 60 minutes (records age out of the rolling window).
- `AlertStateStore.prune_stale(max_age_hours)` exists but is not
  scheduled to run automatically.

### Polymarket and Kalshi prices move after the snapshot
- Snapshot is a frozen-in-time view. PM and Kalshi orderbooks update continuously.
- The `price_fetched_at` field on each opportunity records when its price was captured.
- Default monitor cadence (Conservative) refreshes every 30 minutes; Standard every 15 minutes.
- The Polymarket WebSocket consumer subscribes to watched market price ticks and triggers debounced re-evaluations on ≥1¢ moves, but there is still a window where the snapshot is stale.
- Action: always re-scan immediately before manually verifying a BUY, and confirm the platform's current price matches the snapshot's `pm_price` before betting.

### Manual verification is required, period
- The system is a discovery tool. It surfaces candidates for human review.
- Every BUY needs the full section 4 checklist before any real-money commitment.
- The system has caught the false-positive classes it knows about (live events, name mismatches, 3-way devig errors, last-name collisions, series-game mismatches, side inversions, implausible edges, ambiguous matches). It cannot catch:
  - Platform-side question-text drift (e.g. a market that *looks* like h2h but resolves on a different criterion).
  - Real-time injuries or news that move FD odds before the next scheduled poll.
  - Polymarket markets whose YES/NO outcomes describe something subtly different from the event title (rare but observed historically).
  - Kalshi orderbook asymmetry where the displayed yes_ask doesn't reflect actual fillable liquidity.
- For these, only your eyes on the platform UI count.

### Other notes
- `routers/odds.py` (`/api/odds/tennis`, `/api/odds/matches`) is legacy tennis-only debug surface. Not used by the dashboard. Recently fixed for a NameError but otherwise dormant.
- The Kalshi WebSocket consumer authenticates with `KALSHI_API_KEY` as the bearer header; if Kalshi rejects the auth (HTTP 401/403), the consumer stops trying. Check `monitor/status` if you expect WS-driven re-evaluation and don't see it.
- `backend/data/alert_state.json` persists alert-state records across restarts. This is intentional (so cooldown survives) but means stale records accumulate over time. The store has a `prune_stale(max_age_hours)` method but it is not currently called automatically.

---

## Quick reference: endpoints

| Method + path | Purpose |
|---|---|
| `GET /api/opportunities` | Read snapshot (cold-start runs pipeline once) |
| `GET /api/opportunities/snapshot` | Read snapshot (never triggers pipeline; 204 if empty) |
| `GET /api/opportunities/status` | Lightweight status + `is_refreshing` |
| `POST /api/opportunities/refresh` | Trigger pipeline; body `{"scope":"stale"\|"all"\|"<sport_key>"}` |
| `GET /api/opportunities/debug` | Full rule-by-rule trace |
| `GET /api/opportunities/policy` | Live policy table |
| `GET /api/opportunities/history?platform=&market_id=` | Price history points |
| `GET /api/opportunities/historical-odds?sport_key=&event_id=&date=` | FD snapshot at/before timestamp (CLV) |
| `GET /api/data-status` | Per-sport per-book matrix |
| `GET /api/sports` | Sport registry |
| `GET /api/scan/config` / `POST /api/scan/config` | Per-sport scan settings + Odds-API daily cap |
| `GET /api/monitor/config` / `POST /api/monitor/config` | Monitor settings (preset, dry_run, cooldown, etc.) |
| `POST /api/monitor/start` / `POST /api/monitor/stop` | Run / halt the scheduler |
| `GET /api/monitor/status` | Scheduler + WS + poller status |
| `GET /api/monitor/history` | Recent alert records |
| `POST /api/monitor/test` | **Bypasses dry_run** — sends a real Pushover. Do not call until live alerts are authorized. |

---

End of runbook. Update this file as the system evolves.
