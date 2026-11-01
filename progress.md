# Product improvement progress

Branch: `ux-polish-and-pr-flow` (no pushes to `main`, no deploys).

## Ground rules for this effort
- Local only: `scripts/dev_server.py` against a local libSQL file; never the production Turso in `.env`.
- Simulation backend blanks payment, GitHub OAuth and service-role secrets, so nothing can be charged, pushed or administered for real.
- Separate accounts: loopback-only local auth accepts `Bearer local:<id>`; the test UI build picks the account from `localStorage['ci-e2e-user']`.
- Customer findings below are **simulated** (agents driving the running UI/API). They are evidence of usability problems, not validated market demand.

## Real feedback and analytics
None available (checked 2026-11-02): GitHub repo `ayushman46/CodeGlyph` has 0 stars, 0 forks, one closed issue filed by the owner (#3, ingestion reliability); the app does not persist answer ratings or usage analytics.

## Baseline (2026-11-02, commit e18eea5)
- Backend: 904 tests OK (2 skipped). Frontend unit: 316 pass. `vite build` OK. Playwright e2e: 22 passed, 3 skipped (screenshot-only).
- Core journey (API, local): index `sindresorhus/ky` → keyword-searchable in ~5 s, fully indexed in 36 s (413 sections); question "How does ky decide whether to retry…" answered in 9.5 s with 8 line-range citations.

## Environment
- Scripts in the session scratchpad `sim/`: `backend.sh` (API :8000), `frontend.sh` (test UI :5180).

## Findings
_(pending customer simulations)_

## Backlog
_(pending)_

## Changes
- Dev tooling: `scripts/dev_server.py` accepts `local:<id>` bearer tokens; the e2e/test build reads `ci-e2e-user` (frontend/src/api/supabase.js, test builds only).

## Verification log
_(pending)_

## Next step
Run the six customer simulations against :5180 / :8000.
