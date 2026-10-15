-- Additive migration for large-repository indexing, file-level semantic
-- search, safe re-index staging, and persisted code-edit proposals.
-- The backend applies this automatically at startup (AUTO_MIGRATE=true).
-- Manual alternative: turso db shell YOUR_DATABASE < turso/06_scale_and_editing.sql

PRAGMA foreign_keys = ON;

-- The exact commit an index was built from. Citations deep-link to this
-- revision and the coverage panel reports the importance budget.
CREATE TABLE IF NOT EXISTS repo_snapshots (
  repo_id TEXT PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
  commit_sha TEXT,
  source TEXT NOT NULL DEFAULT 'archive',
  default_branch TEXT,
  selection_json TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL
);

-- One centroid vector per indexed file. Semantic retrieval ranks files first
-- and then reranks chunks inside the best files, which keeps each question
-- bounded for repositories with tens of thousands of chunks.
CREATE TABLE IF NOT EXISTS file_vectors (
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  embedding F32_BLOB(2048),
  chunk_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, file_path)
);

-- A refresh writes replacement rows beside the published index. Retrieval
-- hides rows with this run's id prefix until the swap completes, so a
-- repository stays fully queryable while it is being re-indexed.
CREATE TABLE IF NOT EXISTS repo_index_runs (
  repo_id TEXT PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE,
  job_id TEXT,
  chunk_prefix TEXT NOT NULL,
  started_at TEXT NOT NULL
);

-- Validated code-edit proposals (before/after content and hunks) so a review
-- survives a page reload. Tickets are re-issued on read and still expire.
CREATE TABLE IF NOT EXISTS edit_proposals (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  repo_name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS edit_proposals_by_user ON edit_proposals(user_id, created_at);
CREATE INDEX IF NOT EXISTS edit_proposals_by_repo ON edit_proposals(repo_id, created_at);
