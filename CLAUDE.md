# Prediction Market Scanner — Claude Project Instructions

## Purpose

Prediction Market Scanner is a local, single-user, read-only EV scanner for prediction market mispricing.

It compares Polymarket and Kalshi prices against FanDuel-derived true probabilities from The Odds API to identify positive expected value opportunities.

This tool is decision support only.

Do not add trade execution.
Do not automate betting.
Do not add wallet, order placement, or transaction-signing behavior.
Final marketplace action must remain manual by the user.

---

## Core Product Model

FanDuel is the pricing truth source unless Mason explicitly asks to refactor the model.

The core pipeline is:

1. Platform adapters fetch and normalize Polymarket/Kalshi markets.
2. FanDuel odds are fetched through The Odds API.
3. Pass A extracts candidate features.
4. Pass B evaluates policy rules.
5. BUY/WATCH/SKIP opportunities are surfaced to the frontend.
6. Alerts flow only through the monitor/AlertManager system.

Preserve this architecture.

---

## Non-Negotiable Architecture Rules

- Preserve the separation between Pass A feature extraction and Pass B rule evaluation.
- Keep platform-specific ingestion behind adapter abstractions.
- Do not bypass `NormalizedMarket`.
- Do not create ad hoc pricing or matching paths outside the existing pipeline.
- Do not duplicate thresholds in random files.
- Thresholds belong in `EngineConfig`, `SportConfig`, or `MonitorConfig`.
- Prefer minimal diffs over broad rewrites.
- Do not perform broad refactors unless explicitly requested.

---

## Pass A: Feature Extraction

Pass A extracts features without making final BUY/WATCH/SKIP decisions.

Pass A may compute:

- name matching
- date proximity
- event match confidence
- devigged FanDuel probabilities
- outcome alignment
- line matching
- side matching
- unit matching
- edge calculation
- FanDuel confidence metrics
- ambiguity metrics
- competing match counts
- shared last-name indicators
- last-name collision indicators

Do not collapse Pass A into rule filtering.

Do not silently drop candidates in Pass A unless the existing architecture already does so for a clearly documented reason.

---

## Pass B: Rule Evaluation

Pass B evaluates candidates through the policy table.

Preserve these semantics:

- `BUY`: all required rules pass.
- `WATCH`: at least one downgrade rule fails, with no critical failures.
- `SKIP`: at least one critical rule fails.

Rules must remain centralized in the policy/rule engine.

Do not bypass policy rules with ad hoc checks.

Do not make frontend-only BUY/WATCH/SKIP decisions.

---

## Matching and Ambiguity

This repo is highly sensitive to false positives.

Do not weaken:

- name matching
- full-name matching
- last-name matching safeguards
- outcome alignment
- confidence thresholds
- confidence survival threshold
- buy confidence threshold
- competing match detection
- shared last-name checks
- last-name collision detection
- date proximity checks
- series deduplication
- line matching
- side matching
- unit matching
- price/probability coherence checks

Any change to matching, alignment, ambiguity, date logic, or deduplication must include adversarial tests.

Adversarial tests should cover cases such as:

- same last name on both teams or players
- generic city/team names
- team-name Polymarket outcomes instead of Yes/No outcomes
- Kalshi paired two-way markets
- Kalshi soccer three-way markets with draw
- MLB/NBA/NHL series ambiguity
- totals line mismatch
- Over/Under side mismatch
- prop line/unit/side mismatch

Correctness is more important than finding more opportunities.

False positives are worse than missed opportunities.

---

## Pricing Logic

Do not change the following unless Mason explicitly asks and tests are updated:

- devigging approach
- binary devig math
- three-way devig math
- edge formula
- cost buffer behavior
- Polymarket zero-cost-buffer behavior
- Kelly sizing formula
- Kelly cap
- Kelly fraction
- minimum edge thresholds
- minimum true probability
- plausible edge caps
- price/probability divergence guard

FanDuel-derived devigged probability is the truth source.

Prediction-market price is the entry price.

Edge must remain conceptually:

`p_true - effective_market_price`

Do not introduce alternate EV formulas without explicit approval.

---

## Platform Adapter Rules

Platform-specific logic belongs in platform adapters.

Adapters must emit `NormalizedMarket`.

Do not make downstream services depend directly on raw Polymarket, Kalshi, or Odds API response shapes unless that is already the established pattern.

When changing an adapter:

1. Read the adapter.
2. Read the relevant tests.
3. Trace how its normalized output is used.
4. Add or update adapter tests.
5. Add downstream feature/rule tests if behavior changes.

---

## Alerts and Monitoring

Preserve the alert concepts:

- NEW
- IMPROVED
- COOLDOWN
- ACTIVE/rate-limited state
- hourly cap
- cooldown window
- edge improvement resend threshold

Notifications must go through the intended monitor and AlertManager flow.

Do not trigger notifications from random endpoints, frontend components, scripts, or tests.

Do not send real Pushover notifications unless Mason explicitly asks.

Prefer dry-run validation before live alert behavior.

WATCH and SKIP opportunities should not trigger live alerts.

Ambiguity-downgraded opportunities should remain excluded when configured.

`POST /api/monitor/test` **bypasses `dry_run`** by design and sends a real Pushover. Do not call it casually. It is reserved for explicit credential / delivery verification when Mason asks.

Live alerts (`dry_run=false` in production cycles) should remain off until at least one organic dry-run BUY payload has been observed in the backend log (`[DRY RUN] Would send: …` for a real opportunity). Verify payload format and deep-link content before flipping live.

The current scan/alert defaults are Kalshi-only:
- `ScanConfig.platforms`: kalshi enabled, polymarket disabled.
- `MonitorConfig.platforms`: `["kalshi"]`.
Polymarket is opt-in via the dashboard "Books" toggle or `POST /api/scan/config`. Do not flip Polymarket on without Mason's explicit instruction. Keep the alert allowlist (`MonitorConfig.platforms`) aligned with the scan allowlist.

For soccer (3-way markets), candidates with `fd.draw_odds=None` are SKIPped at the feature layer via `NO_BOOKMAKER_DATA` to prevent 2-way-devig phantom EV. Do not bypass this guard. If extending to other 3-way sports, add them to `_is_three_way_sport` rather than removing the check.

---

## Paper Maker Subsystem

The current maker layer records simulated proposals only. During the paper-testing phase, it must not grow live execution behavior by accident.

- The maker system is PAPER ONLY.
- Do not add real order placement.
- Do not add live trading endpoints.
- Do not add wallet, transaction signing, or authenticated order-submission behavior.
- Do not add frontend buttons or labels such as "Place Order," "Submit Bid," or anything that implies live execution.
- `MakerConfig.enabled` must default to `False`.
- `MakerConfig.paper_only` must remain `True`.
- Maker platform scope remains Kalshi-only unless Mason explicitly approves a separate rollout.
- Maker market-type scope remains H2H-only unless Mason explicitly approves a separate rollout.
- Maker proposals are decision support / simulated proposal records only.
- Maker planning must not loosen EV-core matching, date, side, confidence, ambiguity, or price/probability safety rules.
- If a change touches maker pricing, proposal eligibility, route selection, persistence, frontend display, or config toggles, add or update tests, or clearly state why tests were not run.

Future live execution is not forbidden as a product direction, but it must be implemented later as a separate explicitly approved execution layer with its own design, tests, safety gates, manual controls, limits, and rollback plan. Do not mix live execution into paper-maker planning, diagnostics, UI, config, or persistence work.

---

## Secrets and Credentials

Do not read, print, modify, or commit secrets.

Do not modify:

- `.env`
- `backend/.env`
- API keys
- Pushover credentials
- Kalshi credentials
- Odds API keys
- local credential files
- alert state files unless specifically asked

Do not add secrets to logs, tests, docs, screenshots, or example output.

Use `.env.example` for documentation only.

---

## Git and File Safety

Do not run destructive git commands unless Mason explicitly requests them.

Avoid:

- `git reset --hard`
- `git clean`
- force push
- branch deletion
- mass file deletion
- broad formatting-only rewrites
- dependency upgrades unrelated to the task

Before making changes, check current state with:

- `git status`
- targeted file inspection
- relevant test inspection

After making changes, summarize:

- files changed
- reason for each change
- tests run
- remaining risks

---

## Frontend Expectations

Do not break:

- opportunities table
- market detail panel
- rule trace visibility
- notification settings
- tracked positions
- CLV display/backfill
- data status matrix
- scan button behavior
- auto monitor toggle

Frontend API calls should stay centralized in:

- `frontend/lib/api.ts`
- `frontend/lib/monitor-api.ts`

Do not add inline fetches inside components unless the project already uses that pattern and there is a clear reason.

Frontend should not silently expose backend parameters that the backend ignores.

---

## Testing Discipline

The test suite is the spec.

Before editing:

1. Search for the existing implementation.
2. Search for related tests.
3. Read relevant code paths.
4. Reuse existing patterns before introducing new ones.

After editing:

1. Add or update tests when behavior changes.
2. Run targeted tests first.
3. Run broader tests when the change touches core flow.
4. Verify no regression in EV, matching, classification, adapters, or alerting.

For matching/pricing/alignment/adapter/monitoring changes, tests are required.

Do not claim a change is safe without running relevant tests or clearly stating that tests were not run.

---

## Research-First Workflow

Always follow a research-first approach for non-trivial changes.

For complex or multi-file logic, perform a full data-flow trace before editing.

Complex areas include:

- matching
- pricing
- devigging
- outcome alignment
- platform adapters
- props
- monitoring
- alerts
- scan config
- snapshot refresh
- CLV backfill

Before editing complex logic, report:

1. Files inspected.
2. Current data flow.
3. Root cause or suspected issue.
4. Proposed minimal fix.
5. Tests that should cover the change.

Avoid edit-first behavior.

Do not guess fixes.
Do not patch symptoms without understanding cause.
Do not modify code that has not been inspected.

---

## Task Scope Rules

Work one bounded task at a time.

Do not opportunistically refactor nearby code.

Do not mix unrelated changes in one pass.

Do not combine bug fixes, cleanup, UI changes, and feature work unless Mason explicitly asks.

When a task reveals additional issues, document them separately instead of fixing them immediately.

---

## Real Marketplace Safety

This project may inform real-money decisions.

Therefore:

- Prefer false negatives over false positives.
- Treat suspicious large edges as likely bugs until verified.
- Preserve price/probability coherence checks.
- Preserve max plausible edge checks.
- Preserve ambiguity downgrades.
- Preserve manual user review before any market action.
- Do not hide rule failures from the UI/debug output.
- Do not make BUY labels easier to trigger without explicit approval and tests.

Any opportunity shown as BUY should be traceable through:

1. matched event
2. matched side
3. matched line/unit if relevant
4. FanDuel odds
5. devigged true probability
6. market price
7. edge calculation
8. policy rule results

---

## Known Current Priorities

When no other task is specified, prioritize stability and correctness in this order:

1. Fix known low-risk bugs.
2. Align stale docs with actual code.
3. Improve tests around false positives.
4. Validate marketplace correctness.
5. Validate dry-run alert behavior.
6. Improve observability/debuggability.
7. Only then consider feature expansion.

Do not enable props globally without a specific rollout plan.

Do not delete legacy endpoints unless Mason explicitly chooses that path.

---

## Communication Requirements

Before edits, summarize the plan briefly.

After edits, summarize:

- what changed
- why it changed
- tests run
- test results
- risks or follow-ups

If uncertain, inspect first and summarize findings before editing.

If tests cannot be run, say that clearly.

Do not overstate confidence.