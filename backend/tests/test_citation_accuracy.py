"""Answer and citation accuracy: retrieval ranking, follow-ups, and answer
reference validation (QA findings cite-where-*, CITE-HOW-*, RET-*, IH-7,
EXP-7/8, API-SEC-03)."""

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import build_answer_messages  # noqa: E402
from api import query_router  # noqa: E402
from api.query_router import (  # noqa: E402
    build_context_with_coverage,
    build_retrieval_query,
    extract_answer_references,
    is_follow_up_question,
    rank_citations_by_answer,
    validate_answer_references,
)
from ingest import embedder  # noqa: E402
from ingest.embedder import EmbeddingUnavailableError  # noqa: E402
from retrieval import retriever  # noqa: E402
from retrieval.retriever import (  # noqa: E402
    _definition_regex,
    _editing_candidate_paths,
    _path_noise_penalties,
    build_evidence_plan,
    extract_directory_candidates,
    extract_question_identifiers,
    extract_question_path_hints,
    extract_requested_file_paths,
    merge_overlapping_chunk,
    path_matches_hints,
    select_diverse_chunks,
)


def run(coro):
    return asyncio.run(coro)


def chunk(path, start, end=None, content=None, chunk_id=None, symbols=()):
    lines = content.split("\n") if content is not None else [f"line {n}" for n in range(start, (end or start) + 1)]
    return {
        "id": chunk_id or f"{path}:{start}", "file_path": path, "start_line": start,
        "end_line": start + len(lines) - 1, "language": "ts", "symbols": list(symbols), "content": "\n".join(lines),
    }


class FakeStore:
    """Routes SQL to canned rows by a substring of the statement."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.executed = []

    async def fetch_all(self, sql, args=None):
        self.executed.append((sql, list(args or [])))
        for needle, rows in self.routes:
            if needle in sql:
                return rows(sql, args) if callable(rows) else list(rows)
        return []

    async def fetch_one(self, sql, args=None):
        rows = await self.fetch_all(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql, args=None):
        self.executed.append((sql, list(args or [])))


class ExplicitPathExtractionTests(unittest.TestCase):
    def test_dotted_technology_names_are_not_file_paths(self):
        for question in ("How does this project use Next.js middleware?", "Show me the Next.js config",
                         "Do we use Chart.js or Node.js streams?", "Is Express.js used?"):
            with self.subTest(question=question):
                self.assertEqual(extract_requested_file_paths(question), [])
                self.assertNotEqual(build_evidence_plan(question)["query_scope"], "explicit_file")

    def test_more_extensions_and_dot_prefixes_are_recognised(self):
        self.assertEqual(extract_requested_file_paths("In packages/prisma/schema.prisma add a field"), ["packages/prisma/schema.prisma"])
        self.assertEqual(extract_requested_file_paths("In apps/web/next.config.mjs set it"), ["apps/web/next.config.mjs"])
        self.assertEqual(extract_requested_file_paths("Read apps/docs/intro.mdx"), ["apps/docs/intro.mdx"])
        self.assertEqual(extract_requested_file_paths("open .github/workflows/ci.yml"), [".github/workflows/ci.yml"])
        self.assertEqual(extract_requested_file_paths("fix ./src/a.ts"), ["src/a.ts"])

    def test_path_followed_by_a_full_stop(self):
        self.assertEqual(
            extract_requested_file_paths("Fix the bug in packages/features/bookings/lib/service/RegularBookingService.ts."),
            ["packages/features/bookings/lib/service/RegularBookingService.ts"],
        )
        self.assertEqual(extract_requested_file_paths("Open src/a.ts. Then b.py!"), ["src/a.ts", "b.py"])
        self.assertEqual(extract_requested_file_paths("see notes.ts.bak"), [])

    def test_globs_are_never_paths(self):
        self.assertEqual(
            extract_requested_file_paths("Create packages/lib/foo.test.ts like the other *.test.ts files in src/**/x.ts"),
            ["packages/lib/foo.test.ts"],
        )

    def test_unresolved_bare_dotted_name_falls_through_to_hybrid_retrieval(self):
        hit = chunk("apps/web/proxy.ts", 1, content="export function middleware(req) {}")
        store = FakeStore([("chunks_fts MATCH", [hit])])
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")), \
             patch("retrieval.retriever.extract_requested_file_paths", return_value=["Widget.js"]):
            result = run(retriever.retrieve_context(store, "repo-1", "How does Widget.js middleware work?", top_k=4))
        self.assertEqual([item["file_path"] for item in result], ["apps/web/proxy.ts"])
        self.assertEqual(result[0]["_evidence_plan"]["ignored_file_like_terms"], ["Widget.js"])

    def test_missing_folder_path_still_returns_no_evidence(self):
        store = FakeStore()
        with patch("retrieval.retriever.embed_query", side_effect=EmbeddingUnavailableError("offline")):
            result = run(retriever.retrieve_context(store, "repo-1", "Explain packages/payments/escrow.ts", top_k=4))
        self.assertEqual(result, [])


class PathHintAndNoiseTests(unittest.TestCase):
    def test_product_page_wording_does_not_select_frontend_hints(self):
        hints = extract_question_path_hints("How are slots computed when the booker opens the booking page?")
        self.assertNotIn("components", hints)
        self.assertNotIn("app", hints)

    def test_generic_monorepo_tokens_are_not_hints(self):
        for question in ("How does the API authenticate requests?", "Where is the login handled?", "Explain the frontend"):
            hints = extract_question_path_hints(question)
            for generic in ("app", "main", "server", "page"):
                self.assertNotIn(generic, hints, question)
        self.assertFalse(path_matches_hints("apps/web/lib/x.ts", extract_question_path_hints("How does the API work?")))

    def test_tests_and_docs_are_demoted_unless_requested(self):
        def penalised(question, path):
            return any(pattern.search(path.lower()) for pattern, _ in _path_noise_penalties(question))
        for path in ("apps/web/playwright/booking.e2e.ts", "packages/x/__tests__/a.ts", "packages/x/a.test.ts",
                     "agents/skills/calcom-api/references/bookings.md", "apps/docs/content/a.mdx"):
            self.assertTrue(penalised("How are bookings created?", path), path)
        self.assertFalse(penalised("Show me the tests for booking limits", "packages/x/a.test.ts"))
        self.assertFalse(penalised("Where is the documentation for webhooks?", "apps/docs/content/a.mdx"))
        self.assertFalse(penalised("How are bookings created?", "packages/features/bookings/lib/createBooking.ts"))

    def test_editing_plan_does_not_search_generic_process_words(self):
        plan = build_evidence_plan("Add a per-month booking limit check", "editing")
        for word in ("fix", "bug", "test", "review", "patch"):
            self.assertNotIn(word, plan["search_terms"])
        self.assertIn("test", plan["workflow_terms"])

    def test_editing_candidates_prefer_implementation_over_tests(self):
        plan = {"tests_requested": False, "docs_requested": False}
        chunks = [
            {**chunk("packages/x/booking-limits.test.ts", 1), "_evidence_plan": plan},
            {**chunk("apps/web/playwright/booking-limits.e2e.ts", 1), "_evidence_plan": plan},
            {**chunk("packages/x/checkBookingLimits.ts", 1), "_evidence_plan": plan},
        ]
        self.assertEqual(_editing_candidate_paths(chunks, [], 2)[0], "packages/x/checkBookingLimits.ts")


def fused(question, *, sparse=(), file_hits=(), file_chunks=(), rerank=(), symbols=(), plan_rows=(), top_k=8):
    """Run retrieve_context with every strategy stubbed."""
    store = FakeStore()
    with patch.object(retriever, "embed_query", return_value=[0.0] * 4), \
         patch.object(retriever, "sparse_search", AsyncMock(return_value=list(sparse))), \
         patch.object(retriever, "semantic_file_search", AsyncMock(return_value=list(file_hits))), \
         patch.object(retriever, "semantic_chunks_in_files", AsyncMock(return_value=list(file_chunks))), \
         patch.object(retriever, "semantic_rerank_ids", AsyncMock(return_value=list(rerank))), \
         patch.object(retriever, "symbol_definition_chunks", AsyncMock(return_value=list(symbols))), \
         patch.object(retriever, "path_hint_chunks_batch", AsyncMock(return_value=list(plan_rows))), \
         patch.object(retriever, "absent_question_terms", AsyncMock(return_value=[])):
        return run(retriever.retrieve_context(store, "repo-1", question, top_k=top_k))


class FusionTests(unittest.TestCase):
    def test_semantic_only_implementation_beats_keyword_test_files(self):
        impl = chunk("packages/app-store/dailyvideo/lib/VideoApiAdapter.ts", 239, 300)
        tests = [chunk(f"packages/x/test/t{n}.test.ts", 1, 50) for n in range(4)]
        e2e = chunk("apps/web/playwright/video.e2e.ts", 1, 40)
        result = fused(
            "Which code integrates the Daily video provider?",
            sparse=[*tests, e2e], rerank=[*tests, e2e],
            file_hits=[{"file_path": impl["file_path"]}], file_chunks=[impl],
        )
        self.assertEqual(result[0]["file_path"], impl["file_path"])
        self.assertTrue(all("test" in item["file_path"] or "playwright" in item["file_path"] for item in result[1:]))

    def test_semantic_rerank_is_not_a_second_full_vote(self):
        keyword = chunk("packages/a/keyword.ts", 1, 20)
        semantic = chunk("packages/b/semantic.ts", 1, 20)
        result = fused(
            "How does the scheduler compute windows?",
            sparse=[keyword], rerank=[keyword],
            file_hits=[{"file_path": semantic["file_path"]}], file_chunks=[semantic],
        )
        scores = {item["file_path"]: item["_relevance_score"] for item in result}
        # keyword: 1 vote + half an agreement vote; semantic-only: 1 vote + half a file vote.
        self.assertAlmostEqual(scores[keyword["file_path"]], scores[semantic["file_path"]], places=6)

    def test_path_hint_boost_applies_once_and_at_fusion_scale(self):
        auth = chunk("packages/auth/session.ts", 1, 20)
        other = chunk("packages/billing/charge.ts", 1, 20)
        result = fused(
            "How does auth refresh sessions?",
            sparse=[auth, other], rerank=[auth, other],
            file_hits=[{"file_path": auth["file_path"]}, {"file_path": other["file_path"]}], file_chunks=[auth, other],
        )
        scores = {item["file_path"]: item["_relevance_score"] for item in result}
        self.assertLess(scores[auth["file_path"]] - scores[other["file_path"]], 0.03)
        self.assertGreater(scores[auth["file_path"]], scores[other["file_path"]])

    def test_alphabetical_path_samples_do_not_outrank_real_evidence(self):
        sample = {**chunk("agents/rules/auth-guide.ts", 1, 10), "matched_hint": "auth"}
        real = chunk("packages/features/auth/lib/next-auth-options.ts", 1, 40)
        result = fused("How does login work?", sparse=[real], rerank=[real], plan_rows=[sample])
        self.assertEqual(result[0]["file_path"], real["file_path"])

    def test_definition_question_ranks_the_symbol_definition_first(self):
        definition = {**chunk("packages/prisma/schema.prisma", 851, content="model Booking {\n  id Int\n}"), "_defined_symbol": "Booking"}
        noise = [chunk(f"packages/prisma/migrations/{n}/migration.sql", 1, 20) for n in range(3)]
        result = fused("Where is the Booking model defined?", sparse=noise, rerank=noise, symbols=[definition])
        self.assertEqual(result[0]["file_path"], "packages/prisma/schema.prisma")
        self.assertIn("symbol_definition", result[0]["_retrieval_methods"])

    def test_previously_cited_files_join_the_semantic_candidates(self):
        calls = []

        async def chunks_in_files(store, repo_id, embedding, paths, limit, **kwargs):
            calls.append(list(paths))
            return []
        store = FakeStore()
        with patch.object(retriever, "embed_query", return_value=[0.0]), \
             patch.object(retriever, "sparse_search", AsyncMock(return_value=[])), \
             patch.object(retriever, "semantic_file_search", AsyncMock(return_value=[{"file_path": "a.ts"}])), \
             patch.object(retriever, "semantic_chunks_in_files", chunks_in_files), \
             patch.object(retriever, "semantic_rerank_ids", AsyncMock(return_value=[])), \
             patch.object(retriever, "path_hint_chunks_batch", AsyncMock(return_value=[])):
            run(retriever.retrieve_context(store, "r", "and where is that called?", retrieval_query="x\ny", boost_paths=["b.ts"]))
        self.assertEqual(calls, [["a.ts", "b.ts"]])

    def test_follow_up_embeds_the_expanded_retrieval_text(self):
        seen = []

        def embed(text):
            seen.append(text)
            raise EmbeddingUnavailableError("offline")
        with patch.object(retriever, "embed_query", embed):
            run(retriever.retrieve_context(FakeStore(), "r", "where is that called?", retrieval_query="where is that called?\nWhat does getLuckyUser do?"))
        self.assertEqual(seen, ["where is that called?\nWhat does getLuckyUser do?"])


class SymbolTests(unittest.TestCase):
    def test_identifier_extraction(self):
        self.assertEqual(extract_question_identifiers("Where is the Booking model defined?"), ["Booking"])
        self.assertEqual(extract_question_identifiers("Show me the AvailableSlotsService class"), ["AvailableSlotsService"])
        self.assertIn("parse_select", extract_question_identifiers("Where does HogQL parse SELECT (parse_select)?"))
        self.assertEqual(extract_question_identifiers("getCalendar"), ["getCalendar"])
        self.assertEqual(extract_question_identifiers("How does the Stripe app charge a payment?"), [])

    def test_declaration_regex_covers_methods_and_multiline_generics(self):
        regex = _definition_regex("getLuckyUser")
        self.assertTrue(regex.search("  public async getLuckyUser<\n    T extends X\n  >(params) {"))
        self.assertTrue(regex.search("export const getLuckyUser = async (x) => {"))
        self.assertFalse(regex.search("const user = await this.getLuckyUser(params);"))
        self.assertTrue(_definition_regex("parse_select").search("def parse_select(query: str):"))

    def test_symbol_lookup_prefers_the_named_kind_over_local_aliases(self):
        alias = chunk("packages/features/a/createBooking.ts", 168, content="type Booking = Prisma.BookingGetPayload<{}>;", symbols=["Booking"])
        model = chunk("packages/prisma/schema.prisma", 851, content="model Booking {\n  id Int @id\n}", symbols=["Booking", "Tracking"])
        migration = chunk("packages/prisma/migrations/1/migration.sql", 1, content='CREATE TABLE "Booking" (', symbols=["Booking"])
        store = FakeStore([("symbols LIKE", [alias, migration, model])])
        rows = run(retriever.symbol_definition_chunks(store, "r", ["Booking"], kinds={"Booking": "model"}))
        self.assertEqual(rows[0]["file_path"], "packages/prisma/schema.prisma")
        sql, args = next(item for item in store.executed if "symbols LIKE" in item[0])
        self.assertIn('%"Booking"%', args)


class ChunkSelectionTests(unittest.TestCase):
    def test_overlapping_chunks_merge_into_one_span(self):
        first = chunk("a.ts", 133, 282)
        second = chunk("a.ts", 243, 392)
        merged = merge_overlapping_chunk(first, second)
        self.assertEqual((merged["start_line"], merged["end_line"]), (133, 392))
        self.assertEqual(merged["content"].split("\n")[0], "line 133")
        self.assertEqual(merged["content"].split("\n")[-1], "line 392")
        selected = select_diverse_chunks([first, chunk("b.ts", 1, 5), second, chunk("c.ts", 1, 5)], 3)
        self.assertEqual([(item["file_path"], item["start_line"], item["end_line"]) for item in selected],
                         [("a.ts", 133, 392), ("b.ts", 1, 5), ("c.ts", 1, 5)])

    def test_misaligned_content_is_not_merged(self):
        bad = {**chunk("a.ts", 1, 10), "content": "only one line"}
        self.assertIsNone(merge_overlapping_chunk(bad, chunk("a.ts", 5, 15)))


class FollowUpTests(unittest.TestCase):
    def test_standalone_questions_with_pronouns_are_not_follow_ups(self):
        for question in ("Where is the function that sends booking emails?",
                         "Where is the code that computes available slots?",
                         "What does handlePaymentSuccess do and where is it used?",
                         "How does this project handle webhooks?"):
            self.assertFalse(is_follow_up_question(question), question)

    def test_real_follow_ups(self):
        for question in ("and where is that called?", "show me the tests for it", "Where is it called?", "What about the API?"):
            self.assertTrue(is_follow_up_question(question), question)

    def test_unrelated_question_does_not_borrow_previous_context(self):
        history = [
            {"role": "user", "content": "How does the Google Calendar integration create events?", "cited_paths": []},
            {"role": "assistant", "content": "See `CalendarService.ts`.", "cited_paths": ["packages/googlecalendar/CalendarService.ts"]},
        ]
        self.assertEqual(build_retrieval_query("What does handlePaymentSuccess do and where is it used?", history), (None, []))

    def test_follow_up_chain_keeps_the_original_topic_and_answer_identifiers(self):
        history = [
            {"role": "user", "content": "What does the getLuckyUser method of LuckyUserService do?", "cited_paths": []},
            {"role": "assistant", "content": "`getLuckyUser` picks a host.", "cited_paths": ["packages/getLuckyUser.ts"]},
            {"role": "user", "content": "and where is that called?", "cited_paths": []},
            {"role": "assistant", "content": "It is called from `createBooking` in `RegularBookingService.ts`.",
             "cited_paths": ["packages/RegularBookingService.ts"]},
        ]
        text, boost = build_retrieval_query("show me the tests for it", history)
        self.assertIn("What does the getLuckyUser method of LuckyUserService do?", text)
        self.assertIn("createBooking", text)
        self.assertNotIn("RegularBookingService.ts", text.split("\n")[-1])
        self.assertEqual(boost, ["packages/RegularBookingService.ts"])

    def test_history_boosts_only_files_the_answer_cited(self):
        citations = [
            {"file_path": "a.ts", "cited_in_answer": True},
            {"file_path": "b.ts", "cited_in_answer": False},
        ]
        self.assertEqual(query_router._answer_cited_paths(citations), ["a.ts"])
        self.assertEqual(query_router._answer_cited_paths([{"file_path": "a.ts"}, {"file_path": "b.ts"}]), ["a.ts", "b.ts"])


class ContextCoverageTests(unittest.TestCase):
    def test_only_rendered_chunks_are_returned_and_truncation_is_reported(self):
        big = chunk("a.ts", 1, 400)
        small = chunk("b.ts", 1, 3)
        text, shown = build_context_with_coverage([big, small], max_characters=1200)
        self.assertEqual([item["file_path"] for item in shown], ["a.ts"])
        self.assertLess(shown[0]["end_line"], 400)
        self.assertIn(f"(L1-L{shown[0]['end_line']})", text)
        self.assertEqual(len(shown[0]["content"].split("\n")), shown[0]["end_line"])


class ReferenceValidationTests(unittest.TestCase):
    EVIDENCE = [
        chunk("packages/features/bookings/lib/service/RegularBookingService.ts", 900, 1075),
        chunk("packages/trpc/server/routers/viewer/bookings/find.handler.ts", 1, 36),
        chunk("packages/prisma/schema.prisma", 851, 959),
    ]

    def validate(self, answer, indexed=()):
        store = FakeStore([("FROM repo_files", [{"file_path": path} for path in indexed])])
        return run(validate_answer_references(store, "repo-1", answer, self.EVIDENCE))

    def test_reference_grammar(self):
        refs = extract_answer_references(
            "See `a/b.ts:L1-L9`, `find.handler.ts`, c/d.tsx:L5-9, **e/f.py (L3-L4)** and `g/h.ts` lines 7-9.\n"
            "```ts\nconst x = `no/ref.ts:L1`\n```\n`prisma.booking.update` and `handleThing`"
        )
        self.assertEqual([(r["path"], r["start"], r["end"]) for r in refs], [
            ("a/b.ts", 1, 9), ("find.handler.ts", None, None), ("c/d.tsx", 5, 9), ("e/f.py", 3, 4), ("g/h.ts", 7, 9),
        ])

    def test_valid_and_invalid_references(self):
        answer = (
            "Model: `packages/prisma/schema.prisma:L851-L930`. Wrong range: `packages/prisma/schema.prisma:L656-L784`. "
            "Missing: `packages/app-store/stripepayment/api/webhook.ts:L70-L74`. Bare: `find.handler.ts`."
        )
        new_answer, refs = self.validate(answer)
        self.assertEqual(new_answer, answer)
        by_text = {item["text"]: item for item in refs}
        self.assertTrue(by_text["packages/prisma/schema.prisma:L851-L930"]["valid"])
        self.assertFalse(by_text["packages/prisma/schema.prisma:L656-L784"]["valid"])
        self.assertEqual(by_text["packages/prisma/schema.prisma:L656-L784"]["reason"], "range_outside_evidence")
        missing = by_text["packages/app-store/stripepayment/api/webhook.ts:L70-L74"]
        self.assertFalse(missing["valid"])
        self.assertIsNone(missing["file_path"])
        bare = by_text["find.handler.ts"]
        self.assertTrue(bare["valid"])
        self.assertEqual(bare["file_path"], "packages/trpc/server/routers/viewer/bookings/find.handler.ts")
        for item in refs:
            self.assertIn(item["text"], new_answer)
            self.assertEqual(set(item) >= {"text", "file_path", "start_line", "end_line", "valid"}, True)

    def test_wrong_folder_with_matching_name_and_range_is_repaired(self):
        answer = "Called at `apps/api/v2/src/lib/service/RegularBookingService.ts:L902-L910`."
        new_answer, refs = self.validate(answer)
        self.assertEqual(new_answer, "Called at `packages/features/bookings/lib/service/RegularBookingService.ts:L902-L910`.")
        self.assertTrue(refs[0]["valid"])
        self.assertTrue(refs[0]["repaired"])
        self.assertIn(refs[0]["text"], new_answer)

    def test_indexed_file_outside_evidence_is_valid_without_range_only(self):
        _, refs = self.validate("See `packages/lib/rateLimit.ts` and `packages/lib/rateLimit.ts:L33-L40`.",
                                indexed=["packages/lib/rateLimit.ts"])
        self.assertEqual([item["valid"] for item in refs], [True, False])

    def test_cited_in_answer_uses_suffix_and_range_overlap(self):
        citations = [
            {"file_path": "x/_router.ts", "start_line": 1, "end_line": 50},
            {"file_path": "x/_router.tsx", "start_line": 1, "end_line": 50},
            {"file_path": "y/CancelBooking.tsx", "start_line": 118, "end_line": 267},
            {"file_path": "y/CancelBooking.tsx", "start_line": 268, "end_line": 400},
        ]
        refs = [
            {"file_path": "x/_router.tsx", "start_line": None, "end_line": None, "valid": True},
            {"file_path": "y/CancelBooking.tsx", "start_line": 286, "end_line": 301, "valid": True},
        ]
        ranked = rank_citations_by_answer("", [dict(item) for item in citations], refs)
        flags = {(item["file_path"], item["start_line"]): item["cited_in_answer"] for item in ranked}
        self.assertEqual(flags, {("x/_router.ts", 1): False, ("x/_router.tsx", 1): True,
                                 ("y/CancelBooking.tsx", 118): False, ("y/CancelBooking.tsx", 268): True})
        # Without server references, basename mentions still count.
        ranked = rank_citations_by_answer("See `_router.tsx`.", [dict(item) for item in citations])
        self.assertEqual([item["file_path"] for item in ranked if item["cited_in_answer"]], ["x/_router.tsx"])


class AnswerPipelineTests(unittest.TestCase):
    def collect(self, answer_text, chunks, *, max_context=None):
        store = FakeStore()
        repo = {"id": "repo-1", "status": "ready", "github_url": "https://github.com/acme/w", "commit_sha": "abc"}

        async def stream(*args, **kwargs):
            yield answer_text

        request = query_router.QueryRequest(repo_name="w", question="Where is the Booking model defined?")
        patches = [
            patch.object(query_router, "retrieve_context", AsyncMock(return_value=chunks)),
            patch.object(query_router, "build_repo_map", AsyncMock(return_value={"text": ""})),
            patch.object(query_router, "stream_agent_answer", stream),
        ]
        if max_context:
            patches.append(patch.object(query_router.settings, "max_context_characters", max_context))
        for item in patches:
            item.start()
        try:
            async def gather():
                return [event async for event in query_router.answer_events(store, repo, request, "u", stream=True)]
            return run(gather())
        finally:
            for item in reversed(patches):
                item.stop()

    def test_done_carries_answer_references_and_only_shown_citations(self):
        shown = chunk("packages/prisma/schema.prisma", 851, 900)
        dropped = chunk("packages/other/huge.ts", 1, 2000)
        answer = "In `schema.prisma:L851-L860` and `packages/nowhere/missing.ts:L1-L2`."
        events = self.collect(answer, [shown, dropped], max_context=6000)
        done = events[-1][1]
        # The large chunk only partly fits: it is cited up to its last shown line.
        self.assertEqual([item["file_path"] for item in done["citations"]], ["packages/prisma/schema.prisma", "packages/other/huge.ts"])
        self.assertLess(done["citations"][1]["end_line"], 2000)
        citations_event = next(data for name, data in events if name == "citations")
        self.assertEqual(citations_event["citations"][1]["end_line"], done["citations"][1]["end_line"])
        self.assertTrue(done["citations"][0]["cited_in_answer"])
        self.assertFalse(done["citations"][1]["cited_in_answer"])
        refs = {item["text"]: item for item in done["answer_references"]}
        self.assertTrue(refs["schema.prisma:L851-L860"]["valid"])
        self.assertEqual(refs["schema.prisma:L851-L860"]["file_path"], "packages/prisma/schema.prisma")
        self.assertFalse(refs["packages/nowhere/missing.ts:L1-L2"]["valid"])
        self.assertEqual(done["evidence_plan"]["reference_check"]["unverified"], 1)

    def test_absent_terms_from_retrieval_reach_the_prompt(self):
        plan = {"absent_terms": ["rabbitmq"], "label": "Repository question"}
        messages = build_answer_messages("Where is RabbitMQ used?", "ctx", [], "general", plan)
        self.assertIn("do not occur anywhere in the indexed repository: rabbitmq", messages[-1]["content"])
        self.assertIn("false premises", messages[0]["content"])
        evidence = {**chunk("a.ts", 1, 3), "_evidence_plan": {"absent_terms": ["rabbitmq"]}}
        merged = {"workflow": "general"}
        query_router._merge_retrieval_plan(merged, [evidence])
        self.assertEqual(merged["absent_terms"], ["rabbitmq"])


class GitHubTokenForEditingTests(unittest.TestCase):
    def test_token_loader_returns_token_or_none(self):
        from fastapi import HTTPException
        with patch("api.github_router._load_token", AsyncMock(return_value=("gho_test", "octo"))):
            self.assertEqual(run(query_router.load_user_github_token(FakeStore(), "u")), "gho_test")
        with patch("api.github_router._load_token", AsyncMock(side_effect=HTTPException(status_code=401, detail="x"))):
            self.assertIsNone(run(query_router.load_user_github_token(FakeStore(), "u")))
        with patch("api.github_router._load_token", AsyncMock(side_effect=RuntimeError("no table"))):
            self.assertIsNone(run(query_router.load_user_github_token(FakeStore(), "u")))

    def test_edit_request_carries_the_users_token(self):
        seen = {}

        async def fake_edit(request):
            seen["token"] = request.github_token
            return "No change", [], None
        repo = {"id": "repo-1", "status": "ready", "github_url": "https://github.com/acme/w", "commit_sha": "abc"}
        request = query_router.QueryRequest(repo_name="w", question="Fix issue https://github.com/acme/w/issues/3", workflow="editing")
        with patch.object(query_router, "retrieve_context", AsyncMock(return_value=[chunk("src/a.ts", 1, 3)])), \
             patch.object(query_router, "build_repo_map", AsyncMock(return_value={"text": ""})), \
             patch.object(query_router, "run_code_edit", fake_edit), \
             patch.object(query_router, "load_user_github_token", AsyncMock(return_value="gho_test")):
            async def gather():
                return [event async for event in query_router.answer_events(FakeStore(), repo, request, "u", stream=True)]
            run(gather())
        self.assertEqual(seen["token"], "gho_test")


class SkippedFileTests(unittest.TestCase):
    def test_named_file_skipped_by_budget_gets_a_specific_answer(self):
        import json as _json
        selection = {"budget_omitted_sample": [{"path": "packages/big/huge.ts", "category": "source", "reason": "chunk_budget"}]}
        store = FakeStore([
            ("FROM repo_snapshots", [{"selection_json": _json.dumps(selection)}]),
            ("FROM repo_coverage", [{"excluded_paths": _json.dumps(["vendor/lib.min.js"])}]),
        ])
        repo = {"id": "repo-1", "status": "ready", "github_url": "https://github.com/acme/w", "commit_sha": "abc"}
        for question, path, phrase in (
            ("Explain packages/big/huge.ts", "packages/big/huge.ts", "chunk budget"),
            ("What is in vendor/lib.min.js?", "vendor/lib.min.js", "indexing filters"),
        ):
            request = query_router.QueryRequest(repo_name="w", question=question)
            with patch.object(query_router, "retrieve_context", AsyncMock(return_value=[])):
                async def gather():
                    return [event async for event in query_router.answer_events(store, repo, request, "u", stream=True)]
                done = run(gather())[-1][1]
            self.assertIn(f"`{path}`", done["answer"])
            self.assertIn(phrase, done["answer"])
            self.assertEqual(done["evidence_plan"]["skipped_files"][0]["path"], path)
        request = query_router.QueryRequest(repo_name="w", question="Explain src/unknown.ts")
        with patch.object(query_router, "retrieve_context", AsyncMock(return_value=[])):
            async def gather_unknown():
                return [event async for event in query_router.answer_events(store, repo, request, "u", stream=True)]
            done = run(gather_unknown())[-1][1]
        self.assertIn("won't guess", done["answer"])
        self.assertNotIn("skipped_files", done["evidence_plan"])


class EditCitationTests(unittest.TestCase):
    def test_edit_citations_point_at_the_changed_region(self):
        chunks = [chunk("big.tsx", 1, 107), chunk("big.tsx", 108, 257),
                  chunk("big.tsx", 1100, 1200, content="\n".join(f"row {n}" for n in range(1100, 1201)))]
        proposal = {"files": [{"file_path": "big.tsx", "changes": [{"old": "row 1116\nrow 1117", "new": "x"}], "before": None}]}
        citations = query_router._edit_region_citations(chunks, proposal, {"github_url": "", "commit_sha": "c"})
        self.assertEqual((citations[0]["start_line"], citations[0]["cited_in_answer"]), (1100, True))
        self.assertTrue(all(not item["cited_in_answer"] for item in citations[1:]))


class SearchAndMapTests(unittest.TestCase):
    def test_code_search_prefers_distinct_files_and_dedupes_lines(self):
        rows = [
            {**chunk("big.test.ts", 1, content="handleNewBooking()\nhandleNewBooking()"), "score": -9},
            {**chunk("big.test.ts", 2, content="handleNewBooking()"), "score": -8},
            {**chunk("big.test.ts", 50, content="handleNewBooking() again"), "score": -7},
            {**chunk("impl.ts", 10, content="export function handleNewBooking() {}"), "score": -6},
        ]
        store = FakeStore([("chunks_fts", rows)])
        results = run(retriever.search_code(store, "r", "handleNewBooking", limit=2))
        self.assertEqual([item["file_path"] for item in results], ["big.test.ts", "impl.ts"])
        sql = store.executed[0][0]
        self.assertIn("PARTITION BY file_path", sql)
        results = run(retriever.search_code(store, "r", "handleNewBooking", limit=10))
        lines = [(item["file_path"], match["line"]) for item in results for match in item["matches"]]
        self.assertEqual(len(lines), len(set(lines)))

    def test_repo_map_keeps_every_extension(self):
        rows = [{"file_path": f"src/f{n}.ext{n}", "byte_size": 1} for n in range(20)] + [{"file_path": "a.yaml", "byte_size": 1}]
        retriever.invalidate_repo_map("map-repo")
        result = run(retriever.build_repo_map(FakeStore([("FROM repo_files", rows)]), "map-repo"))
        self.assertEqual(len(result["extensions"]), 21)
        retriever.invalidate_repo_map("map-repo")

    def test_directory_question_gets_a_generated_listing(self):
        self.assertEqual(extract_directory_candidates("What lives in packages/app-store?"), ["packages/app-store"])
        self.assertEqual(extract_directory_candidates("Explain packages/a/b.ts"), [])
        files = [{"file_path": p} for p in ("packages/app-store/stripe/a.ts", "packages/app-store/stripe/b.ts",
                                             "packages/app-store/zoom/c.ts", "packages/app-store/_appRegistry.ts")]
        listing = run(retriever.directory_listing_chunk(FakeStore([("FROM repo_files", files)]), "r", "packages/app-store"))
        self.assertIn("stripe/ (2)", listing["content"])
        self.assertIn("packages/app-store/_appRegistry.ts", listing["content"])
        text, shown = build_context_with_coverage([listing, chunk("x.ts", 1, 2)])
        self.assertIn("Generated directory listing", text)
        self.assertEqual(query_router.build_citations(shown, {"github_url": "", "commit_sha": "c"})[0]["file_path"], "x.ts")


class EmbeddingDeadlineTests(unittest.TestCase):
    def setUp(self):
        embedder._request_times.clear()
        embedder.embed_query.cache_clear()

    def tearDown(self):
        embedder._request_times.clear()
        embedder.embed_query.cache_clear()

    def test_query_without_key_fails_fast_and_takes_no_limiter_slot(self):
        with patch.object(embedder.settings, "nvidia_api_key", ""):
            started = time.monotonic()
            with self.assertRaises(Exception) as raised:
                embedder.embed_texts(["q"], input_type="query")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(type(raised.exception).__name__, "ModelConfigurationError")
        self.assertEqual(len(embedder._request_times), 0)

    def test_saturated_limiter_does_not_block_a_query(self):
        now = time.monotonic()
        with patch.object(embedder.settings, "nvidia_calls_per_minute", 2):
            embedder._request_times.extend([(now, "query"), (now, "query")])
            started = time.monotonic()
            with self.assertRaises(EmbeddingUnavailableError):
                embedder.wait_for_embedding_slot("query", embedder.QUERY_SLOT_MAX_WAIT_SECONDS)
        self.assertLess(time.monotonic() - started, embedder.QUERY_SLOT_MAX_WAIT_SECONDS + 0.5)

    def test_slow_embedding_falls_back_to_keyword_retrieval_quickly(self):
        release = threading.Event()

        def slow(_):
            release.wait(5)
            raise EmbeddingUnavailableError("late")
        hit = chunk("src/a.ts", 1, content="booking window")
        store = FakeStore([("chunks_fts MATCH", [hit])])
        async def timed():
            started = time.monotonic()
            try:
                return await retriever.retrieve_context(store, "r", "How is the booking window computed?", top_k=4), time.monotonic() - started
            finally:
                release.set()
        with patch.object(retriever, "embed_query", slow), patch.object(retriever, "QUERY_EMBEDDING_TIMEOUT_SECONDS", 0.5):
            result, elapsed = run(timed())
        self.assertLess(elapsed, 2.5)
        self.assertEqual(result[0]["file_path"], "src/a.ts")
        self.assertEqual(result[0]["_evidence_plan"]["semantic_search"], "timed_out")


if __name__ == "__main__":
    unittest.main()
