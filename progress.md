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

## Findings — simulation round 1 (2026-11-02, simulated, 6 personas, 28 min)
Raw reports + synthesized backlog: scratchpad `sim/round1.json`; screenshots in `sim/agents/<persona>/`.
- Verdicts: first-time user "would come back"; developer, maintainer, onboarding engineer "side tool, not trusted"; tech lead "would not use for a go/no-go"; admin "would not adopt for the team: there is nothing to administer".
- Trust: long answers cut off mid-sentence under an all-clear badge (answer cap 1,400 tokens, `length` ignored); invented methods/imports shown as "references checked"; real indexed lines flagged "not in the indexed snapshot"; Detailed model leaked reasoning / took ~90 s.
- Retrieval: naming a big file retrieves only its first ~700 lines (`path_hint_chunks` orders by line); natural questions miss obvious files, then answers claim the code/tests don't exist.
- Finding code: "exact" search matches any token, caps 3 lines/section; ⌘K can't jump to files or symbols; no file tree; @-mention can't find files by name; search line clicks open the wrong line; no deep links.
- Impact: `import flask` / src-layout packages unresolved, so tests never appear; ambiguous names merged; no transitive or affected-tests view.
- Conversations: "New chat" permanently deletes the only thread (no confirm/undo/history).
- Access: no members, sharing, roles or removal; the "Team" plan only adds storage.
- Onboarding/indexing: overview stale after indexing (cache never invalidated), contradictory progress numbers, no queue position, typo'd repos leave failed entries.
- Edit & PR: "checks passed" shown for patches whose tests were never run.

## Backlog (49 items; top of list, value/effort)
Bugs: BUG-01 New chat deletes (5/S) · BUG-02 truncation (5/M) · BUG-03 search exactness (5/M) · BUG-04 file-head retrieval (5/M) · BUG-05 unverified identifiers (5/M) · BUG-06 wrong evidence + false absence (5/L) · BUG-07 wrong warning text (4/S) · BUG-08 "checks passed" (4/S) · BUG-09 Detailed reasoning leak (4/M) · BUG-10 package imports (4/M) · BUG-11..15 (3/S).
Features: FEAT-01 ⌘K files/symbols (5/M) · FEAT-02 enclosing symbol (4/M) · FEAT-03 file tree (4/M) · FEAT-04 multiple conversations (4/L) · FEAT-05 transitive impact + affected tests (4/L) · FEAT-06 team access (4/XL) · FEAT-08 queue ETA · FEAT-09 copy with permalinks · FEAT-10 deep links · FEAT-14 Start here · FEAT-15 share link.
Deferred (need owner input or infrastructure): FEAT-07 sandboxed test runs (cost/security), seat-based billing for teams, SSO, FEAT-18 compare refs, FEAT-19 ownership/blame.

## Batch 1 plan (parallel engineering tracks, one worktree each)
T1 answer trust (BUG-02/05/07/09 + absence wording) · T2 retrieval (BUG-04/06) · T3 navigation (BUG-03/11/12, FEAT-01/03/10/02) · T4 conversations (BUG-01, FEAT-04/09) · T5 impact (BUG-10/15, FEAT-05) · T6 team access (FEAT-06/15) · T7 onboarding + indexing UX (FEAT-14/08, BUG-13/14, UX-07/04) · T8 Edit & PR honesty (BUG-08/16, FEAT-13).

## Changes
- Batch 1 merged into `ux-polish-and-pr-flow` (each track implemented → adversarially reviewed → fixed): T7 onboarding/indexing, T5 impact, T3 navigation, T1 answer trust, T6 team access. Integration fixes: new `/files`, `/symbols` endpoints use the shared-access lookup and are in the team-access route-coverage test (with `/impact/suggestions`); sidebar file count uses searchable files inside the new shared/owned list.
- Dev tooling: `scripts/dev_server.py` accepts `local:<id>` bearer tokens; the e2e/test build reads `ci-e2e-user` (frontend/src/api/supabase.js, test builds only).

## Verification log
- After merging T7, T5, T3, T1, T6: backend 1077 tests OK (2 skipped); frontend unit 352 pass; `vite build` OK. Playwright e2e not yet run on the merged branch (held until the remaining fix agents stop using ports 4173/4174).

## Next step
Fix step for T2 retrieval, T4 conversations and T8 Edit & PR is running as workflow run `wf_175f4b69-751` (script `scratchpad/merge/finish-fixes.js`; worktrees `.claude/worktrees/wf_f9352695-2a0-{2,4,8}`). The first attempt stopped at a usage limit; do NOT resume `wf_f9352695-2a0` (resume re-runs every fix agent after the first changed one).
When it finishes: merge `track/t2-retrieval`, `track/t4-conversations`, `track/t8-edit-honesty` (T4 then T8: T8's tests call `get_conversation_history`, which T4 gives a required `conversation_id`; T4 conversation routes must use the shared-access lookup and be added to `repo_routes()` in backend/tests/test_team_access.py; MIGRATIONS order 07 then 08 and the migration-list test). Then run all suites including `cd frontend && CI=1 npx playwright test`, restart `sim/backend.sh` + `sim/frontend.sh`, and run customer round 2.
