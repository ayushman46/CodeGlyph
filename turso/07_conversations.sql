-- Additive migration: several saved conversations per repository.
-- The backend applies this automatically at startup (AUTO_MIGRATE=true).
-- Manual alternative: turso db shell YOUR_DATABASE < turso/07_conversations.sql
--
-- Each chat message now belongs to one conversation. Existing messages become
-- one conversation per repository and user, titled from its first question,
-- so nothing saved before this migration is lost or hidden. Conversations are
-- private to the user who asked (user_id), also in a shared repository.
-- Deleting one only sets deleted_at, so it can be restored.

PRAGMA foreign_keys = ON;

-- Runs first: if it fails, nothing else has changed. If a run stops after
-- it, the next start applies the file again and the runner skips this
-- statement because the column exists (backend/database.py); the rest of
-- the file is idempotent.
ALTER TABLE chat_messages ADD COLUMN conversation_id TEXT;

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS conversations_by_repo_user ON conversations(repo_id, user_id, deleted_at, updated_at, id);
-- Every message query names the repository, user and conversation. With
-- all three leading, SQLite prefers this index to chat_messages_by_repo_user
-- (which would read every message the user has in the repository, including
-- the large citations of each answer), and created_at, id serve the order.
CREATE INDEX IF NOT EXISTS chat_messages_by_conversation ON chat_messages(repo_id, user_id, conversation_id, created_at, id);

-- Repository deletion removes its rows table by table; this keeps working
-- when foreign keys are not enforced on the connection.
CREATE TRIGGER IF NOT EXISTS conversations_after_repo_delete AFTER DELETE ON repos BEGIN
  DELETE FROM conversations WHERE repo_id = old.id;
END;

-- One "legacy" conversation per repository and user for every message saved
-- before conversations existed.
INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at)
SELECT lower(hex(randomblob(16))), m.repo_id, m.user_id, '', MIN(m.created_at), MAX(m.created_at)
FROM chat_messages m
WHERE m.conversation_id IS NULL AND EXISTS (SELECT 1 FROM repos r WHERE r.id = m.repo_id)
GROUP BY m.repo_id, m.user_id;

UPDATE chat_messages SET conversation_id = (
  SELECT c.id FROM conversations c
  WHERE c.repo_id = chat_messages.repo_id AND c.user_id = chat_messages.user_id
  ORDER BY c.created_at LIMIT 1
)
WHERE conversation_id IS NULL;

-- Title: the first question on one line, at most 80 characters. Each
-- replace of two spaces by one halves a run of spaces.
UPDATE conversations SET title = COALESCE((
  SELECT trim(replace(replace(replace(replace(
    replace(replace(replace(f.content, char(13), ' '), char(10), ' '), char(9), ' '),
    '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' '))
  FROM chat_messages f
  WHERE f.repo_id = conversations.repo_id AND f.user_id = conversations.user_id
    AND f.conversation_id = conversations.id AND f.role = 'user'
  ORDER BY f.created_at, f.id LIMIT 1
), '')
WHERE title = '';

UPDATE conversations SET title = rtrim(substr(title, 1, 79)) || '…' WHERE length(title) > 80;

UPDATE conversations SET title = 'Earlier conversation' WHERE title = '';
