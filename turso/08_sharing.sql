-- Additive migration for sharing a repository with teammates.
-- The backend applies this automatically at startup (AUTO_MIGRATE=true).
-- Manual alternative: turso db shell YOUR_DATABASE < turso/08_sharing.sql

PRAGMA foreign_keys = ON;

-- People an owner shared a repository with. The owner is never a row here:
-- ownership stays repos.user_id. ``alias`` is the member's own name for the
-- repository, unique among their repositories and other shares, so
-- /app/r/<alias> and every repository route keep working by name.
-- A member the owner removed keeps a row with ``removed_at`` set, so they are
-- told the repository is no longer shared instead of "not found".
CREATE TABLE IF NOT EXISTS repo_members (
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('editor', 'viewer')),
  alias TEXT NOT NULL,
  member_label TEXT NOT NULL DEFAULT '',
  owner_label TEXT NOT NULL DEFAULT '',
  invite_id TEXT,
  joined_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  removed_at TEXT,
  PRIMARY KEY (repo_id, user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS repo_members_alias ON repo_members(user_id, alias) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS repo_members_by_user ON repo_members(user_id, joined_at);

-- Invite links. Only a SHA-256 of the token is stored: the link is shown
-- once, when it is created. A link lets any signed-in user join with its
-- role until it expires or the owner revokes it.
CREATE TABLE IF NOT EXISTS repo_invites (
  id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token_hint TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('editor', 'viewer')),
  owner_label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  accepted_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS repo_invites_by_repo ON repo_invites(repo_id, created_at);

-- Read-only links to one answer. The question, answer and citations
-- (commit-pinned GitHub links) are copied when the link is made, so later
-- conversation or index changes do not alter it. Only a SHA-256 of the token
-- is stored. A link stops working when it is revoked, or when the person who
-- made it can no longer open the repository.
CREATE TABLE IF NOT EXISTS answer_shares (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  repo_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS answer_shares_by_user ON answer_shares(user_id, created_at);
