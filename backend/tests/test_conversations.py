"""Several saved conversations per repository (turso/07_conversations.sql).

"New chat" used to erase a repository's only conversation. These tests pin
the replacement: every message belongs to a conversation, a new chat never
touches an old one, follow-ups read only their own conversation, deleting is
reversible, and conversations stay private to the user who asked.

Stores are embedded libsql files with the real migrated schema; the model
and retrieval boundaries are patched.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

import database
from api.query_router import QueryRequest, conversation_title, persist_proposal, save_message
from api.repository_state import fetch_owned_repository
from database import TursoStore, apply_migrations
from ingest.pipeline import delete_repository
from test_answers_and_editing import (
    AUTH_CHUNK, OTHER, REPO_ID, REPO_NAME, SESSION_CHUNK, USER, DatabaseTestCase, EndpointTestCase, parse_sse, run,
    sample_proposal, scripted_stream, seed_repo,
)

ISSUE_QUESTION = "Bug report #7634: non-string form field names raise AttributeError when files are attached"


class ConversationTitleTests(unittest.TestCase):
    def test_title_is_the_first_question_on_one_line_cut_at_a_word(self):
        self.assertEqual(conversation_title("  How does\nlogin   work?  "), "How does login work?")
        long = "Explain how the request preparation pipeline encodes multipart bodies when files and data are both given"
        title = conversation_title(long)
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("…"))
        self.assertTrue(long.startswith(title[:-1]))
        self.assertFalse(title[:-1].endswith(" "))
        self.assertEqual(conversation_title("   "), "Untitled conversation")

    def test_request_takes_either_a_conversation_or_a_new_one(self):
        conversation_id = "a" * 32
        self.assertEqual(QueryRequest(repo_name="r", question="q", conversation_id=conversation_id).conversation_id, conversation_id)
        self.assertTrue(QueryRequest(repo_name="r", question="q", new_conversation=True).new_conversation)
        for bad in ({"conversation_id": "not-an-id"}, {"conversation_id": conversation_id, "new_conversation": True}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                QueryRequest(repo_name="r", question="q", **bad)


class LegacyMigrationTests(unittest.TestCase):
    """A database from before conversations existed keeps every message."""

    def test_existing_messages_become_one_titled_conversation_per_repository_and_user(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(f"file:{Path(directory) / 'legacy.db'}", "local-test-token")
            try:
                before = tuple(item for item in database.MIGRATIONS if item[0] != "07_conversations.sql")
                with patch.object(database, "MIGRATIONS", before):
                    run(apply_migrations(store))
                run(seed_repo(store))
                run(seed_repo(store, repo_id="repo-other", user_id=OTHER, repo_name=REPO_NAME))
                rows = [
                    ("m1", REPO_ID, USER, "user", "  First question\nabout   login", "2026-10-01T09:00:00+00:00"),
                    ("m2", REPO_ID, USER, "assistant", "An answer", "2026-10-01T09:00:01+00:00"),
                    ("m3", REPO_ID, USER, "user", "x" * 200, "2026-10-02T09:00:00+00:00"),
                    ("m4", "repo-other", OTHER, "user", "Their own question", "2026-10-03T09:00:00+00:00"),
                ]
                for message_id, repo_id, user_id, role, content, created in rows:
                    run(store.execute(
                        "INSERT INTO chat_messages (id, repo_id, user_id, role, content, citations, tool_calls, created_at) "
                        "VALUES (?, ?, ?, ?, ?, '[]', '[]', ?)",
                        [message_id, repo_id, user_id, role, content, created],
                    ))

                self.assertEqual(run(apply_migrations(store)), ["07_conversations.sql"])
                conversations = {row["user_id"]: row for row in run(store.fetch_all(
                    "SELECT id, repo_id, user_id, title, created_at, updated_at, deleted_at FROM conversations"
                ))}
                self.assertEqual(sorted(conversations), sorted([USER, OTHER]))
                mine, theirs = conversations[USER], conversations[OTHER]
                self.assertEqual((mine["repo_id"], theirs["repo_id"]), (REPO_ID, "repo-other"))
                self.assertEqual(mine["title"], "First question about login")
                self.assertEqual((mine["created_at"], mine["updated_at"]), ("2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00"))
                self.assertIsNone(mine["deleted_at"])
                self.assertRegex(mine["id"], r"^[0-9a-f]{32}$")
                self.assertEqual(theirs["title"], "Their own question")
                threads = {row["id"]: row["conversation_id"] for row in run(store.fetch_all("SELECT id, conversation_id FROM chat_messages"))}
                self.assertEqual(threads, {"m1": mine["id"], "m2": mine["id"], "m3": mine["id"], "m4": theirs["id"]})
                # Applying again changes nothing.
                self.assertEqual(run(apply_migrations(store)), [])
            finally:
                run(store.close())

    def test_long_first_questions_are_shortened_in_sql_too(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(f"file:{Path(directory) / 'legacy.db'}", "local-test-token")
            try:
                before = tuple(item for item in database.MIGRATIONS if item[0] != "07_conversations.sql")
                with patch.object(database, "MIGRATIONS", before):
                    run(apply_migrations(store))
                run(seed_repo(store))
                run(store.execute(
                    "INSERT INTO chat_messages (id, repo_id, user_id, role, content, created_at) VALUES ('m1', ?, ?, 'user', ?, ?)",
                    [REPO_ID, USER, "word " * 60, "2026-10-01T09:00:00+00:00"],
                ))
                run(apply_migrations(store))
                title = run(store.fetch_one("SELECT title FROM conversations"))["title"]
                self.assertEqual(len(title), 80)
                self.assertTrue(title.endswith("…"))
            finally:
                run(store.close())

    def test_a_run_that_stopped_after_adding_the_column_completes_on_the_next_start(self):
        # Writes over HTTP are not retried when their outcome is unknown, so
        # the column can exist while the rest of the file never ran. That
        # used to fail every start with "duplicate column name".
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(f"file:{Path(directory) / 'partial.db'}", "local-test-token")
            try:
                before = tuple(item for item in database.MIGRATIONS if item[0] != "07_conversations.sql")
                with patch.object(database, "MIGRATIONS", before):
                    run(apply_migrations(store))
                run(seed_repo(store))
                run(store.execute(
                    "INSERT INTO chat_messages (id, repo_id, user_id, role, content, created_at) VALUES ('m1', ?, ?, 'user', ?, ?)",
                    [REPO_ID, USER, "How does login work?", "2026-10-01T09:00:00+00:00"],
                ))
                run(store.execute("ALTER TABLE chat_messages ADD COLUMN conversation_id TEXT"))

                self.assertEqual(run(apply_migrations(store)), ["07_conversations.sql"])
                conversation = run(store.fetch_one("SELECT id, title FROM conversations"))
                self.assertEqual(conversation["title"], "How does login work?")
                self.assertEqual(run(store.fetch_one("SELECT conversation_id FROM chat_messages"))["conversation_id"], conversation["id"])
                self.assertEqual(run(apply_migrations(store)), [])
            finally:
                run(store.close())

    def test_a_database_without_the_conversation_migration_is_reported_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TursoStore(f"file:{Path(directory) / 'old.db'}", "local-test-token")
            database.reset_schema_verification()
            try:
                before = tuple(item for item in database.MIGRATIONS if item[0] != "07_conversations.sql")
                with patch.object(database, "MIGRATIONS", before):
                    run(apply_migrations(store))
                with patch.object(database, "get_turso_store", return_value=store), \
                     patch.object(database.settings, "auto_migrate", False), \
                     self.assertRaises(database.DatabaseConfigurationError) as raised:
                    run(database.assert_turso_schema())
                self.assertIn("missing required tables: conversations", str(raised.exception))
            finally:
                database.reset_schema_verification()
                run(store.close())


class ConversationApiTests(EndpointTestCase):
    def ask(self, question, *, conversation_id=None, new=False, deltas=("An answer.",), calls=None, retrieved=(AUTH_CHUNK,)):
        payload = {"repo_name": REPO_NAME, "question": question}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if new:
            payload["new_conversation"] = True
        retrieve = AsyncMock(return_value=list(retrieved))
        with patch("api.query_router.retrieve_context", retrieve), \
             patch("api.query_router.stream_agent_answer", scripted_stream(list(deltas), calls=calls)):
            response = self.client.post("/api/query/stream", json=payload)
        return response, retrieve

    def done(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        events, _ = parse_sse(response.text)
        self.assertEqual(events[-1][0], "done", events)
        return events[-1][1]

    def threads(self, user=None):
        if user:
            self.current_user = SimpleNamespace(id=user)
        try:
            response = self.client.get(f"/api/conversations/{REPO_NAME}/threads")
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        finally:
            self.current_user = SimpleNamespace(id=USER)

    def test_new_chat_keeps_the_old_conversation_and_both_are_listed(self):
        first = self.done(self.ask(ISSUE_QUESTION, new=True)[0])["conversation"]
        self.assertRegex(first["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["title"], conversation_title(ISSUE_QUESTION))
        self.done(self.ask("Does the fix need a test?", conversation_id=first["id"])[0])

        second = self.done(self.ask("Where are redirects handled?", new=True)[0])["conversation"]
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["title"], "Where are redirects handled?")

        listed = self.threads()
        self.assertFalse(listed["has_more"])
        self.assertEqual([item["id"] for item in listed["conversations"]], [second["id"], first["id"]])
        self.assertEqual([item["message_count"] for item in listed["conversations"]], [2, 4])
        for item in listed["conversations"]:
            self.assertTrue(item["created_at"] and item["updated_at"])

        old = self.client.get(f"/api/conversations/{REPO_NAME}", params={"conversation_id": first["id"]}).json()
        self.assertEqual(old["conversation"]["id"], first["id"])
        self.assertEqual([message["content"] for message in old["messages"] if message["role"] == "user"],
                         [ISSUE_QUESTION, "Does the fix need a test?"])
        # Without an id the most recently active conversation opens.
        latest = self.client.get(f"/api/conversations/{REPO_NAME}").json()
        self.assertEqual(latest["conversation"]["id"], second["id"])
        self.assertEqual(len(latest["messages"]), 2)

    def test_follow_ups_read_only_their_own_conversation(self):
        first_answer = "Validation happens in `src/auth.py:L1-L6`."
        first = self.done(self.ask("How does session validation work?", new=True, deltas=[first_answer])[0])["conversation"]
        other = self.done(self.ask("Explain the database migration runner", new=True, deltas=["Elsewhere."])[0])["conversation"]

        calls = []
        response, retrieve = self.ask("Where is it called?", conversation_id=first["id"], calls=calls)
        self.done(response)
        history = calls[0][0][2]
        self.assertEqual([item["content"] for item in history], ["How does session validation work?", first_answer])
        self.assertEqual(retrieve.await_args.kwargs["boost_paths"], ["src/auth.py"])

        calls = []
        self.done(self.ask("Where is it called?", conversation_id=other["id"], calls=calls)[0])
        self.assertEqual([item["content"] for item in calls[0][0][2]], ["Explain the database migration runner", "Elsewhere."])

        # A new chat starts with no history at all.
        calls = []
        response, retrieve = self.ask("Where is it called?", new=True, calls=calls)
        self.done(response)
        self.assertEqual(calls[0][0][2], [])
        self.assertIsNone(retrieve.await_args.kwargs["retrieval_query"])

    def test_clients_that_send_no_conversation_continue_the_latest_one(self):
        first = self.done(self.ask("How does login work?")[0])["conversation"]
        again = self.done(self.ask("And logout?")[0])["conversation"]
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.threads()["conversations"][0]["message_count"], 4)

    def test_asking_in_a_deleted_or_unknown_conversation_is_a_plain_404(self):
        conversation = self.done(self.ask("How does login work?", new=True)[0])["conversation"]
        self.assertEqual(self.client.delete(f"/api/conversations/{REPO_NAME}/threads/{conversation['id']}").status_code, 200)
        for conversation_id in (conversation["id"], "f" * 32):
            with self.subTest(conversation_id=conversation_id):
                response, retrieve = self.ask("And logout?", conversation_id=conversation_id)
                self.assertEqual(response.status_code, 404)
                self.assertIn("deleted or does not exist", response.json()["detail"])
                retrieve.assert_not_awaited()
                self.assertNotIn(USER, self.limiter._active)
        json_response = self.client.post(
            "/api/query", json={"repo_name": REPO_NAME, "question": "And logout?", "conversation_id": conversation["id"]},
        )
        self.assertEqual(json_response.status_code, 404)
        self.assertEqual(len(self.chat_rows()), 2)

    def test_delete_hides_and_restore_brings_back_in_the_same_place(self):
        older = self.done(self.ask("How does login work?", new=True)[0])["conversation"]
        newer = self.done(self.ask("How does logout work?", new=True)[0])["conversation"]

        deleted = self.client.delete(f"/api/conversations/{REPO_NAME}/threads/{older['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["conversation"]["id"], older["id"])
        self.assertEqual([item["id"] for item in self.threads()["conversations"]], [newer["id"]])
        self.assertEqual(
            self.client.get(f"/api/conversations/{REPO_NAME}", params={"conversation_id": older["id"]}).status_code, 404,
        )
        # Deleting twice is harmless; nothing was erased.
        self.assertEqual(self.client.delete(f"/api/conversations/{REPO_NAME}/threads/{older['id']}").status_code, 200)
        self.assertEqual(len(self.chat_rows()), 4)

        # Deleting the open (latest) conversation opens the next one.
        self.client.delete(f"/api/conversations/{REPO_NAME}/threads/{newer['id']}")
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}").json()["conversation"], None)

        restored = self.client.post(f"/api/conversations/{REPO_NAME}/threads/{older['id']}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["conversation"]["message_count"], 2)
        self.client.post(f"/api/conversations/{REPO_NAME}/threads/{newer['id']}/restore")
        self.assertEqual([item["id"] for item in self.threads()["conversations"]], [newer["id"], older["id"]])
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}").json()["conversation"]["id"], newer["id"])

    def test_rename_trims_the_title_and_rejects_empty_or_long_ones(self):
        conversation = self.done(self.ask(ISSUE_QUESTION, new=True)[0])["conversation"]
        url = f"/api/conversations/{REPO_NAME}/threads/{conversation['id']}"
        response = self.client.patch(url, json={"title": "  Triage   #7634\n"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation"]["title"], "Triage #7634")
        self.assertEqual(self.threads()["conversations"][0]["title"], "Triage #7634")
        self.assertEqual(self.client.patch(url, json={"title": "   "}).json()["detail"], "Enter a title.")
        self.assertEqual(self.client.patch(url, json={"title": "x" * 121}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/conversations/{REPO_NAME}/threads/not-an-id", json={"title": "x"}).status_code, 404)

    def test_conversations_are_private_to_the_user_who_asked(self):
        mine = self.done(self.ask("How does login work?", new=True)[0])["conversation"]
        # Another account with a repository of the same name.
        run(seed_repo(self.store, repo_id="repo-other", user_id=OTHER, repo_name=REPO_NAME))
        self.current_user = SimpleNamespace(id=OTHER)
        url = f"/api/conversations/{REPO_NAME}/threads/{mine['id']}"
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}", params={"conversation_id": mine["id"]}).status_code, 404)
        self.assertEqual(self.client.patch(url, json={"title": "Taken"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self.client.post(f"{url}/restore").status_code, 404)
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}/threads").json()["conversations"], [])
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}").json()["conversation"], None)
        self.current_user = SimpleNamespace(id=USER)
        self.assertEqual(self.threads()["conversations"][0]["title"], "How does login work?")

    def test_history_pages_stay_inside_one_conversation(self):
        first = self.done(self.ask("q0", new=True)[0])["conversation"]
        for index in range(1, 4):
            self.done(self.ask(f"q{index}", conversation_id=first["id"])[0])
        self.done(self.ask("other conversation", new=True)[0])

        seen, before = [], None
        while True:
            params = {"conversation_id": first["id"], "limit": 3, **({"before": before} if before else {})}
            body = self.client.get(f"/api/conversations/{REPO_NAME}", params=params).json()
            seen = [message["content"] for message in body["messages"]] + seen
            if not body["has_more"]:
                break
            before = body["next_before"]
        self.assertEqual([content for content in seen if content.startswith("q") or content.startswith("other")],
                         ["q0", "q1", "q2", "q3"])
        self.assertEqual(len(seen), 8)

    def test_messages_saved_without_a_conversation_are_adopted_not_hidden(self):
        # An older server (mid-deploy) writes messages without a conversation.
        run(save_message(self.store, repo_id=REPO_ID, user_id=USER, role="user", content="Written by an older server"))
        run(save_message(self.store, repo_id=REPO_ID, user_id=USER, role="assistant", content="Its answer"))
        listed = self.threads()["conversations"]
        self.assertEqual([(item["title"], item["message_count"]) for item in listed], [("Written by an older server", 2)])
        # Adopting again (a second request at the same time) creates nothing new.
        self.assertEqual([item["id"] for item in self.threads()["conversations"]], [listed[0]["id"]])
        self.assertEqual(run(self.store.fetch_one("SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id IS NULL"))["n"], 0)

    def test_an_empty_conversation_is_never_listed_or_opened(self):
        # A new chat whose messages could not be saved leaves only the row.
        run(self.store.execute(
            "INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ["e" * 32, REPO_ID, USER, "Lost", "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00"],
        ))
        kept = self.done(self.ask("How does login work?", new=True)[0])["conversation"]
        self.assertEqual([item["id"] for item in self.threads()["conversations"]], [kept["id"]])
        run(self.store.execute("UPDATE conversations SET updated_at = '2099-01-01T00:00:00+00:00' WHERE id = ?", ["e" * 32]))
        self.assertEqual(self.client.get(f"/api/conversations/{REPO_NAME}").json()["conversation"]["id"], kept["id"])

    def test_a_proposal_stays_reachable_from_its_conversation(self):
        proposal = sample_proposal()
        repo = run(fetch_owned_repository(self.store, USER, REPO_NAME))
        run(persist_proposal(self.store, proposal, repo=repo, repo_name=REPO_NAME, user_id=USER))

        async def fake_edit(request):
            return "## Clear the session on logout", [], proposal

        with patch("api.query_router.retrieve_context", AsyncMock(return_value=[AUTH_CHUNK, SESSION_CHUNK])), \
             patch("api.query_router.run_code_edit", fake_edit), \
             patch("api.query_router.persist_proposal", AsyncMock()):
            response = self.client.post("/api/query/stream", json={
                "repo_name": REPO_NAME, "question": "Fix logout so it clears the session", "workflow": "editing",
                "new_conversation": True,
            })
        triage = self.done(response)["conversation"]
        self.done(self.ask("Where are redirects handled?", new=True)[0])

        listed = {item["id"]: item for item in self.threads()["conversations"]}
        self.assertEqual(listed[triage["id"]]["proposal_count"], 1)
        reopened = self.client.get(f"/api/conversations/{REPO_NAME}", params={"conversation_id": triage["id"]}).json()
        reply = reopened["messages"][-1]
        self.assertEqual(reply["mode"], "editing")
        proposal_ids = [call["input"]["proposal_id"] for call in reply["tool_calls"] if call.get("tool") == "code_edit"]
        self.assertEqual(proposal_ids, [proposal["id"]])
        reopened_proposal = self.client.get(f"/api/edits/{proposal_ids[0]}")
        self.assertEqual(reopened_proposal.status_code, 200)
        self.assertEqual(reopened_proposal.json()["proposal"]["title"], proposal["title"])

    def test_list_pages_with_a_cursor(self):
        ids = [self.done(self.ask(f"Question {index}", new=True)[0])["conversation"]["id"] for index in range(5)]
        seen, before = [], None
        while True:
            params = {"limit": 2, **({"before": before} if before else {})}
            body = self.client.get(f"/api/conversations/{REPO_NAME}/threads", params=params).json()
            seen += [item["id"] for item in body["conversations"]]
            if not body["has_more"]:
                self.assertIsNone(body["next_before"])
                break
            before = body["next_before"]
        self.assertEqual(seen, list(reversed(ids)))

    def test_unknown_repository_is_404_everywhere(self):
        for method, url in (
            ("get", "/api/conversations/unknown/threads"),
            ("delete", f"/api/conversations/unknown/threads/{'a' * 32}"),
            ("post", f"/api/conversations/unknown/threads/{'a' * 32}/restore"),
        ):
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 404)


    def test_conversation_queries_read_only_that_conversation_through_its_index(self):
        # Production has no planner statistics (no ANALYZE), like this file.
        # Without the conversation among the leading index columns SQLite
        # picked chat_messages_by_repo_user and read every message the user
        # has in the repository, large citations included, for each row of
        # the History list and for each page of an older conversation.
        first = self.done(self.ask("q0", new=True)[0])["conversation"]
        for index in range(1, 3):
            self.done(self.ask(f"q{index}", conversation_id=first["id"])[0])
        self.done(self.ask("another conversation", new=True)[0])
        run(save_message(self.store, repo_id=REPO_ID, user_id=USER, role="user", content="Written by an older server"))

        recorded = []
        fetch_all, fetch_one = self.store.fetch_all, self.store.fetch_one

        async def recording_all(sql, args=None):
            recorded.append((sql, list(args or [])))
            return await fetch_all(sql, args)

        async def recording_one(sql, args=None):
            recorded.append((sql, list(args or [])))
            return await fetch_one(sql, args)

        with patch.object(self.store, "fetch_all", recording_all), patch.object(self.store, "fetch_one", recording_one):
            self.threads()
            url = f"/api/conversations/{REPO_NAME}"
            page = self.client.get(url, params={"conversation_id": first["id"], "limit": 2}).json()
            self.client.get(url, params={"conversation_id": first["id"], "limit": 2, "before": page["next_before"]})
            self.client.get(url)
            self.done(self.ask("Where is it called?", conversation_id=first["id"])[0])

        kinds = {
            "history list": "AS message_count", "page": "SELECT id, role, content", "follow-up history": "SELECT role, content, citations",
            "unthreaded check": "conversation_id IS NULL", "latest": "ORDER BY c.updated_at DESC, c.id DESC LIMIT 1",
        }
        seen = set()
        for sql, args in {(sql, tuple(args)) for sql, args in recorded if "chat_messages" in sql}:
            plan = [row["detail"] for row in run(fetch_all(f"EXPLAIN QUERY PLAN {sql}", list(args)))]
            with self.subTest(sql=sql[:100], plan=plan):
                self.assertFalse([line for line in plan if "chat_messages_by_repo_user" in line or line.startswith("SCAN")])
                self.assertFalse([line for line in plan if "TEMP B-TREE" in line])
                self.assertTrue([line for line in plan if "INDEX chat_messages_by_conversation (repo_id=? AND user_id=? AND conversation_id=?)" in line])
                if "AS message_count" in sql:
                    # The count and the has-messages check never read a message row.
                    self.assertEqual(len([line for line in plan if "COVERING INDEX chat_messages_by_conversation" in line]), 2)
            seen |= {kind for kind, marker in kinds.items() if marker in sql}
        self.assertEqual(seen, set(kinds))


class RepositoryDeletionTests(DatabaseTestCase):
    def test_deleting_a_repository_removes_its_conversations(self):
        run(seed_repo(self.store))
        run(self.store.execute(
            "INSERT INTO conversations (id, repo_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, 't', 'x', 'x')",
            ["c" * 32, REPO_ID, USER],
        ))
        run(save_message(self.store, repo_id=REPO_ID, user_id=USER, role="user", content="q", conversation_id="c" * 32))
        run(delete_repository(self.store, REPO_ID, USER))
        self.assertIsNone(run(self.store.fetch_one("SELECT id FROM conversations")))
        self.assertIsNone(run(self.store.fetch_one("SELECT id FROM chat_messages")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
