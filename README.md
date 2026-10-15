# Codebase Intel

Codebase Intel turns any public GitHub repository into a private, searchable workspace. Ask questions and get answers that cite the exact files and lines they came from, browse and search the code, see what a change would affect, and turn a request (or a GitHub issue) into a validated multi-file patch that you review as a green and red diff before a pull request is opened.

It is built for the first hours in an unfamiliar codebase: new engineers onboarding, senior engineers giving knowledge transfer, open-source contributors finding where to make a change, and reviewers doing a security or due-diligence pass.

## What it does

- **Indexes huge repositories without hitting limits.** Files are ranked by importance (source, schema and configuration first; tests, fixtures, generated code and data last) and the most important are indexed within a budget instead of the job failing. Non-English translation catalogues and build artefacts are skipped. Every omission is listed with its reason in a coverage report. A repository like `calcom/cal.com` (7,700 files) indexes its core code fully.
- **Searchable in moments.** Keyword search is ready as soon as the first sections are written; the semantic index (NVIDIA Nemotron embeddings) builds in the background with live progress. Refreshing a repository only re-processes changed files, and the previous index keeps answering questions until the new one is ready.
- **Exact citations.** Answers stream in real time and cite code as `path/to/file.ts:L42-L118`. Every citation opens the exact lines in a built-in file viewer and links to GitHub at the indexed commit. Sources are grouped by folder.
- **Accurate retrieval.** File-level semantic search, keyword search with prefix matching and path weighting, path-aware ranking for component questions, the import graph for impact questions, and follow-up awareness for short questions like "where is it called?".
- **Code editing and pull requests.** Describe a change or paste a GitHub issue URL. The agent plans the change across files (call sites, types, styles, tests, new files when needed), reads the current files from GitHub, writes exact search/replace hunks, validates every hunk against the real file, and shows the result inline in the chat. The review screen has unified and split diffs, in-place editing with a live diff, a prefilled pull request, `.patch` download, and one-click PR creation (a branch in the repository, or a fork when you lack write access). Nothing is pushed without your review.
- **Exploration tools.** Repository overview (languages, folder map, key files, suggested questions), instant code search, and change-impact analysis.
- Light and dark themes, a command palette (`⌘K` / `Ctrl+K`), keyboard-first chat, and a responsive layout down to phone width.

## Architecture

```
Browser (React SPA on Vercel or served by the API)
   │  Supabase Google sign-in (session token)
   ▼
FastAPI API (Render)  ──── Server-sent events for streamed answers
   ├── Ingestion worker (in-process, durable Turso queue)
   │      GitHub archive (codeload) → importance budget → chunker → NVIDIA embeddings
   ├── Retrieval: FTS5 + file-level vectors + chunk rerank + import graph
   ├── Answers: NVIDIA Nemotron (streamed)
   └── Code editor agent: plan → read files (GitHub raw) → hunks → validate → diff
   ▼
Turso / libSQL (async HTTP client): repositories, chunks, FTS, vectors, coverage,
conversations, proposals, billing
```

### Why the backend runs on Render

The frontend is a static single-page app and deploys perfectly to Vercel. The backend is not a good fit for serverless functions: indexing a large repository streams a ~150 MB archive and makes hundreds of rate-limited embedding requests, which takes minutes and needs a persistent worker. Vercel's function time limits (and daily-only cron on the Hobby plan) cannot run that reliably. The recommended topology is therefore:

- **Frontend on Vercel** for instant CDN page loads (`frontend/vercel.json`).
- **API and worker on Render** (`render.yaml`). The same Render service also serves the built frontend, so a single Render service is a complete deployment on its own.

On Render's free tier the instance sleeps after 15 idle minutes. The app shows a "waking up" notice on the first request, and while an indexing job is running the worker pings its own health endpoint so the job is never frozen mid-way.

### Database access

Remote Turso is accessed through an asynchronous Hrana-over-HTTP client (`httpx`). The embedded `libsql` Python client holds the GIL for the duration of every statement, which froze the whole server during network round trips and made the app feel slow while a repository was indexing; it is now used only for local `file:` databases. Vectors are written as packed float32 blobs, batches are atomic, and the additive SQL migrations in `turso/` are applied automatically at startup (`AUTO_MIGRATE=true`).

## Indexing in detail

1. **Download.** The worker streams `codeload.github.com/<owner>/<repo>/tar.gz/HEAD` (or the requested ref: a `github.com/<owner>/<repo>/tree/<branch>` or `/commit/<sha>` URL indexes that branch or commit) and extracts only candidate text files (no `git` binary, binaries never touch the disk). The archive's commit SHA is recorded so citations link to the exact revision. `git clone` remains as a fallback.
2. **Select.** Files are filtered (lockfiles, minified files, build artefacts, translations, binaries, hidden files except `.github/` workflows and `.env.example`) and ranked by importance. The worker keeps the highest-priority files within the file, byte, and section budgets and the remaining plan quota. Nothing fails because a repository is big; the coverage report explains every omission.
3. **Compare.** SHA-256 hashes skip unchanged files on refresh. Removed files are deleted.
4. **Chunk.** Declaration-aware chunking for JS/TS, Python, Go, Rust, Kotlin, Java, Ruby, SQL, Prisma, GraphQL and Markdown merges small declarations into coherent sections with exact line ranges and extracted symbols. Files over 2 MB stream through a bounded producer.
5. **Keyword search.** Sections are written in batches; the repository becomes queryable immediately.
6. **Semantic index.** Sections are embedded in adaptive batches with a shared rate limiter that reserves capacity for interactive questions. Per-file centroid vectors power file-level semantic search. If the embedding provider is down the index degrades to keyword search with a clear message.
7. **Refresh safety.** A refresh writes replacement rows under a job-scoped prefix that retrieval hides until an atomic swap. Stopping or failing a refresh restores the previous index.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_REPOSITORY_FILES` | 10,000 | Files indexed per repository (importance order) |
| `MAX_REPOSITORY_BYTES` | 100 MB | Source bytes indexed per repository |
| `MAX_REPOSITORY_CHUNKS` | 15,000 | Sections per repository |
| `MAX_FILE_SIZE_BYTES` | 50 MB | Largest single file considered |
| `INDEX_TRANSLATIONS` | false | Include non-English locale catalogues |
| `FREE_CODEBASE_BYTES` / `TEAM_CODEBASE_BYTES` | 200 MB / 800 MB | Indexed-source quota per plan |
| `NVIDIA_CALLS_PER_MINUTE` | 40 | Set to your NVIDIA key's limit |

## Answers and citations

Each question is classified (explicit file, component, location, API, impact, overview, issue) and retrieved with a bounded evidence set: keyword (FTS5 with prefix matching and 4× weight on path matches), file-level semantic search followed by an exact chunk rerank, a semantic rerank of keyword candidates, path-hint targets, and the import graph for impact questions. Results are fused with reciprocal-rank fusion, kept file-diverse, and passed to the model with real line numbers and a compact repository map so it can name exact folders. Short follow-up questions inherit terms and cited files from the previous turn. Citations the answer actually uses are ranked first. When the model is unavailable the retrieved evidence is returned instead of a guess.

Workflows: general, new-engineer onboarding, architecture, security review, open-source contributor, and technical due diligence.

## Code editing and pull requests

1. Switch the composer to **Edit & PR** and describe the change, or paste a GitHub issue URL (its title and body become acceptance criteria and the PR says `Fixes #N`).
2. The agent ranks candidate files, plans the change (files to modify or create, symbols to trace), finds usages of those symbols, and reads the complete current files from GitHub.
3. The code model returns exact search/replace hunks. Every hunk must match its file exactly once (a trailing-whitespace-tolerant match is accepted only when unique); one repair round fixes imperfect copies. Unvalidated output is never shown as a patch.
4. The proposal appears in the chat with per-file green/red diffs and is saved, so it survives a reload.
5. **Review & create PR** opens the review screen: file list with stats, unified or split diff with word-level highlights, edit mode with a live diff, title/description/branch/commit fields, GitHub connection, `.patch` download and copy.
6. On push, the server verifies the short-lived, file-scoped editing ticket and the reviewed blob SHA of every modified file (and that new files do not already exist), creates one atomic commit on a new `codebase-intel/` branch, and opens the pull request. A stale file returns a conflict and the review screen can re-apply the change to the latest revision.

## Deployment

### 1. Turso, Supabase, NVIDIA

- Create a Turso database and token (`turso db create`, `turso db tokens create`). The schema is created automatically on first start; you can also apply `turso/00_init.sql` … `turso/06_scale_and_editing.sql` manually.
- In Supabase, enable the Google provider and add your frontend origins (Vercel and/or Render URL, and `http://localhost:5173`) to the redirect allow list.
- Create an NVIDIA API key with access to the configured Nemotron models.

### 2. Backend on Render

Create a service from `render.yaml` (Blueprint) and fill in the secrets marked `sync: false`. The full variable list with comments is in `render.env.example`. Important values:

- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `NVIDIA_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`.
- `CORS_ORIGINS`: the frontend origin(s), e.g. `https://your-app.vercel.app,https://your-service.onrender.com`. CORS exposes the `X-Error-Code` and `Retry-After` response headers to the SPA.
- `GITHUB_FRONTEND_ORIGIN` (**required**): the single origin that serves the SPA. After GitHub authorization the API redirects the browser to `{GITHUB_FRONTEND_ORIGIN}/github/callback` with the one-time code in the URL fragment; the SPA then POSTs `/api/github/complete` with the user's bearer token. Use the Vercel origin for split deploys (e.g. `https://your-app.vercel.app`) or the Render URL for a single-service deploy.
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI=https://your-service.onrender.com/api/github/callback` (register the same callback in the GitHub OAuth App; it stays on the Render API host even when the frontend is on Vercel), `GITHUB_TOKEN_ENCRYPTION_KEY` (a stable Fernet key or a random secret of at least 32 characters), `EDITING_TICKET_SECRET`.
- If your Render environment still sets `MAX_REPOSITORY_FILES=5000`, raise it to `10000` (or remove it) to use the new default of 10,000.
- Request bodies over 256 KB are rejected with `413` (the GitHub push endpoint allows `2 × MAX_GITHUB_CHANGE_BYTES + 1 MB`).

Health check: `/api/health`. Dependency readiness (Turso query and NVIDIA probe): `/api/ready`.

### 3. Frontend on Vercel (recommended)

Import the repository in Vercel with **Root Directory = `frontend`** (framework Vite; `frontend/vercel.json` configures SPA routing, caching and security headers). Set:

- `VITE_API_BASE_URL=https://your-service.onrender.com/api`
- `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, optional `VITE_RAZORPAY_KEY_ID`

Then add the Vercel origin to `CORS_ORIGINS` and set `GITHUB_FRONTEND_ORIGIN` on Render to the Vercel origin (the GitHub OAuth callback URL stays `https://your-service.onrender.com/api/github/callback`). See `frontend/.env.example`.

## Local development

```bash
python3 -m venv backend/venv
backend/venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend install
cp .env.example .env   # fill in the values
```

Run the backend and frontend:

```bash
backend/venv/bin/python -m uvicorn main:app --app-dir backend --reload --port 8000
```

```bash
npm --prefix frontend run dev
```

Open http://localhost:5173.

To develop without touching a shared database, run a local libSQL server (`turso dev` or `sqld --http-listen-addr 127.0.0.1:18090`) and start the API with `scripts/dev_server.py`. It refuses non-local databases; `--insecure-local-auth` accepts a fixed local user from loopback only so the UI can be exercised without Google sign-in:

```bash
backend/venv/bin/python scripts/dev_server.py --insecure-local-auth
```

## Testing

```bash
cd backend && ./venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

```bash
npm --prefix frontend run test:unit
```

```bash
npm --prefix frontend run build && npm --prefix frontend run test:e2e
```

The backend suite isolates itself from real services (it never reads production Turso or calls NVIDIA) and covers the async Turso client against libSQL, migrations, importance-budget selection and archive extraction (including hostile archives), chunking invariants, the ingestion lifecycle (refresh staging, cancellation, deletion, budgets, quotas), retrieval at scale, streaming answers, the code editor, GitHub OAuth and PR creation, billing and auth caching. Frontend unit tests cover the diff engine (verified with `git apply`), citation parsing and patch generation. Playwright tests drive the marketing pages and the full workspace (streamed answers, citation viewer, code search, edit proposal, review and PR) against mocked APIs.

## API

All routes require a Supabase bearer token except health/readiness and the GitHub OAuth callback.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/health`, `/api/ready` | Liveness and dependency readiness |
| POST | `/api/ingest` | Queue a repository (URL or `owner/repo`) |
| GET | `/api/repos`, `/api/repos/statuses`, `/api/status/{repo}` | Repository state (`is_indexing`, `queryable`, phase, progress, coverage summary) |
| GET | `/api/repos/{repo}/coverage` | Omitted files with reasons and the indexing budget |
| GET | `/api/repos/{repo}/overview` | Languages, folder map, key files, suggested questions |
| GET | `/api/repos/{repo}/search?q=` | Instant code search |
| GET | `/api/repos/{repo}/file?path=` | One file at the indexed commit |
| GET | `/api/repos/{repo}/impact?file_path=` | Files that import a file, and what it imports |
| POST | `/api/repos/{repo}/reindex`, `/api/repos/{repo}/cancel-indexing` | Refresh or stop indexing |
| PATCH / DELETE | `/api/repos/{repo}` | Rename or delete |
| POST | `/api/query` | Grounded answer (JSON) |
| POST | `/api/query/stream` | Grounded answer as server-sent events (`stage`, `citations`, `token`, `done`, `error`) |
| GET / DELETE | `/api/conversations/{repo}` | Conversation history / new chat |
| GET | `/api/edits/{id}` | Saved edit proposal with a fresh editing ticket |
| GET | `/api/github/status`, `/api/github/login`, `/api/github/callback` | GitHub connection (callback redirects to `{GITHUB_FRONTEND_ORIGIN}/github/callback`) |
| POST | `/api/github/complete` | SPA finishes the OAuth connection with the fragment code and the user's bearer token |
| GET | `/api/github/file` | Current file revision for review (works before connecting GitHub) |
| POST | `/api/github/push-pr`, `/api/github/disconnect` | Create the branch, commit and PR / disconnect |
| GET | `/api/account/usage` | Indexed-source usage and quota |
| POST | `/api/create-order`, `/api/verify-payment` | Team plan checkout (Razorpay) |

## Security

- Supabase validates every session; verified sessions are cached briefly (bounded by token expiry). Every query is scoped to the authenticated user.
- Repository content, issues and conversation history are treated as untrusted data in every prompt.
- GitHub tokens are encrypted at rest; the OAuth state is single-use and short-lived. PR creation requires a signed, short-lived ticket scoped to the user, repository and exact file set, plus the reviewed blob SHA of every modified file.
- Archive extraction rejects path traversal, links and devices, bounds download size and time, and never uses `tar.extract`.
- SQL is parameterised; payment signatures are verified on the server; API errors never expose credentials or stack traces.

## Troubleshooting

- **The first request is slow** — a free Render instance is waking up; the UI shows a notice.
- **GitHub popup says the connection expired** — check that `GITHUB_REDIRECT_URI` matches the OAuth App callback exactly and that `GITHUB_TOKEN_ENCRYPTION_KEY` has not changed between deploys. The review screen polls the connection, so it also works when the browser blocks the popup from messaging back.
- **A file is "partially indexed" or "lower priority"** — the repository exceeded a budget; raise the limits above or upgrade the plan quota. The coverage report lists every skipped path.
- **Semantic search unavailable** — the embedding provider did not respond; keyword search keeps working and a re-index adds semantic search later.

## Contributing

1. Run the backend tests, frontend unit tests and build before changing anything.
2. Keep changes focused and add a regression test for the behaviour you change.
3. Changes to retrieval should show why the selected evidence is more relevant; changes to indexing should keep line ranges, budgets, cancellation and refresh behaviour correct; changes to security boundaries need tests proving one user cannot read or change another user's data.
4. Open a pull request with the problem, the solution, test output, and any configuration change.

## License

See the repository for license information.
