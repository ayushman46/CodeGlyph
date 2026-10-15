"""Tests for server-side application of reviewed code-edit hunks."""

import difflib
import itertools
import random
import time
import unittest
from unittest import mock

from agent import code_edit
from agent.code_edit import (
    CodeEditValidationError,
    apply_hunks,
    git_blob_sha,
    line_change_counts,
    locate_hunk,
    normalise_repository_path,
    unified_diff_text,
)


def change(old, new):
    return {"old": old, "new": new}


class GitBlobShaTests(unittest.TestCase):
    def test_matches_values_reported_by_git(self):
        self.assertEqual(git_blob_sha(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(git_blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")
        # Computed with ``git hash-object --stdin`` so the length header is
        # verified as a byte count, not a character count.
        self.assertEqual(
            git_blob_sha("caf\u00e9 \U0001f389\r\nline".encode("utf-8")),
            "1248c24788bac2eb895c62bf3dceb55e8cb08521",
        )

    def test_accepts_other_bytes_like_values(self):
        expected = "ce013625030ba8dba906f756967f9e9ca394464a"
        self.assertEqual(git_blob_sha(bytearray(b"hello\n")), expected)
        self.assertEqual(git_blob_sha(memoryview(b"hello\n")), expected)

    def test_rejects_text_and_integers(self):
        with self.assertRaises(TypeError):
            git_blob_sha("hello\n")
        with self.assertRaises(TypeError):
            git_blob_sha(5)


class LocateHunkTests(unittest.TestCase):
    def test_returns_every_non_overlapping_span(self):
        self.assertEqual(locate_hunk("abcabc", "abc"), [(0, 3), (3, 6)])
        self.assertEqual(locate_hunk("x = 1\ny = 2\nx = 1\n", "x = 1\n"), [(0, 6), (12, 18)])

    def test_skips_occurrences_that_overlap_an_earlier_span(self):
        self.assertEqual(locate_hunk("aaaa", "aa"), [(0, 2), (2, 4)])
        self.assertEqual(locate_hunk("aaa", "aa"), [(0, 2)])

    def test_missing_or_empty_search_text_has_no_spans(self):
        self.assertEqual(locate_hunk("abc", "x"), [])
        self.assertEqual(locate_hunk("abc", ""), [])
        self.assertEqual(locate_hunk("", "a"), [])

    def test_offsets_are_code_points(self):
        self.assertEqual(locate_hunk("\u00e9\U0001f389\u00e9", "\u00e9"), [(0, 1), (2, 3)])


class ApplyHunksLineEndingTests(unittest.TestCase):
    def test_replaces_a_single_exact_hunk(self):
        content = "def login():\n    return True\n"
        result = apply_hunks(content, [change("    return True", "    return validate_session()")])
        self.assertEqual(result, "def login():\n    return validate_session()\n")

    def test_crlf_round_trip_preserves_crlf_and_missing_trailing_newline(self):
        content = "one\r\ntwo\r\nthree"
        result = apply_hunks(content, [change("two\nthree", "2\n3\n3.5")])
        self.assertEqual(result, "one\r\n2\r\n3\r\n3.5")
        self.assertFalse(result.endswith("\n"))
        self.assertEqual(result.count("\n"), result.count("\r\n"))

    def test_crlf_file_keeps_its_trailing_newline(self):
        self.assertEqual(apply_hunks("a\r\nb\r\n", [change("b", "c")]), "a\r\nc\r\n")

    def test_crlf_hunk_text_matches_either_line_ending_style(self):
        self.assertEqual(
            apply_hunks("one\r\ntwo\r\nthree\r\n", [change("two\r\nthree", "2\r\n3")]),
            "one\r\n2\r\n3\r\n",
        )
        # An LF file stays LF even when the model wrote CRLF in its hunk.
        self.assertEqual(apply_hunks("a\nb\n", [change("a\r\nb", "x\r\ny")]), "x\ny\n")

    def test_lf_file_without_trailing_newline_is_preserved(self):
        self.assertEqual(apply_hunks("a\nb", [change("b", "c")]), "a\nc")
        self.assertEqual(apply_hunks("a\nb\n", [change("a", "z")]), "z\nb\n")

    def test_deleting_a_whole_crlf_line_removes_both_characters(self):
        content = "a\r\nb\r\nc\r\n"
        self.assertEqual(apply_hunks(content, [change("b\n", "")]), "a\r\nc\r\n")
        self.assertEqual(apply_hunks(content, [change("\nb", "")]), "a\r\nc\r\n")

    def test_untouched_lines_keep_their_original_endings_in_a_mixed_file(self):
        content = "a\nb\r\nc\n"
        self.assertEqual(apply_hunks(content, [change("c", "C")]), "a\nb\r\nC\n")
        # Replacement text adopts CRLF because the file contains CRLF, but the
        # lone-LF lines that were not edited are not rewritten.
        self.assertEqual(apply_hunks(content, [change("a", "x\ny")]), "x\r\ny\nb\r\nc\n")


class ApplyHunksTolerantMatchTests(unittest.TestCase):
    def test_ignores_trailing_whitespace_present_only_in_the_file(self):
        content = "def f():   \n    return 1\n"
        result = apply_hunks(content, [change("def f():\n    return 1", "def f():\n    return 2")])
        self.assertEqual(result, "def f():\n    return 2\n")

    def test_ignores_trailing_whitespace_present_only_in_the_hunk(self):
        content = "x = 1\ny = 2\n"
        result = apply_hunks(content, [change("x = 1  \ny = 2", "x = 10\ny = 2")])
        self.assertEqual(result, "x = 10\ny = 2\n")

    def test_final_hunk_line_trailing_whitespace_matches_at_a_line_end(self):
        content = "a\nfoo\t \nbar\n"
        result = apply_hunks(content, [change("a \nfoo   ", "a\nFOO")])
        self.assertEqual(result, "a\nFOO\nbar\n")

    def test_final_hunk_whitespace_inside_a_line_must_match_exactly(self):
        # The tab forces the tolerant path; the trailing space after the comma
        # is real content and must not be absorbed or doubled.
        content = "x\t\ncall(a, b)\n"
        result = apply_hunks(content, [change("x\ncall(a, ", "x\ncall(z, ")])
        self.assertEqual(result, "x\ncall(z, b)\n")
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("foo bar\n", [change("foo  ", "baz")])

    def test_hunk_starting_at_a_line_break_keeps_the_previous_line_intact(self):
        content = "a  \nb  \nc\n"
        result = apply_hunks(content, [change("\nb\nc", "\nB\nc")])
        self.assertEqual(result, "a  \nB\nc\n")

    def test_hunk_starting_with_trailing_whitespace_is_one_location(self):
        content = "x   \nfoo\nbar\n"
        result = apply_hunks(content, [change("  \nfoo \nbar", "  \nFOO\nbar")])
        self.assertEqual(result, "x  \nFOO\nbar\n")

    def test_tolerant_match_maps_back_to_the_original_crlf_span(self):
        content = "keep\r\na  \r\nb\r\n"
        self.assertEqual(apply_hunks(content, [change("a\nb", "A\nB")]), "keep\r\nA\r\nB\r\n")

    def test_leading_and_interior_whitespace_stay_significant(self):
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("a  b\n", [change("a b", "c")])
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("\tx\ny\n", [change("    x\ny", "z")])

    def test_ambiguous_tolerant_match_is_rejected(self):
        content = "x  \ny\nx\t\ny\n"
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks(content, [change("x\ny", "z")])
        self.assertEqual(str(raised.exception), "Hunk 1 matches 2 locations; include more surrounding lines.")

    def test_exact_match_takes_precedence_over_tolerant_matches(self):
        content = "foo\nbar\nfoo  \nbar\n"
        self.assertEqual(apply_hunks(content, [change("foo\nbar", "X")]), "X\nfoo  \nbar\n")

    def test_unicode_trailing_whitespace_is_ignored_like_rstrip(self):
        # A non-breaking space or ideographic space at a line end is as
        # invisible in review as an ASCII space.
        content = "foo\xa0\nbar　\nbaz\n"
        self.assertEqual(apply_hunks(content, [change("foo\nbar\nbaz", "x")]), "x\n")
        self.assertEqual(apply_hunks("foo\nbar\n", [change("foo\xa0\nbar", "x")]), "x\n")
        # Interior Unicode whitespace stays significant.
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("a\xa0b\t\nc\n", [change("a b\nc", "z")])

    def test_whitespace_only_hunk_is_never_matched_tolerantly(self):
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("a\t\nb\n", [change("  \n", "\n")])


class ApplyHunksErrorTests(unittest.TestCase):
    def test_not_found_message_names_the_hunk(self):
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks("a\nb\n", [change("a", "A"), change("b", "B"), change("missing", "x")])
        self.assertEqual(str(raised.exception), "Hunk 3 was not found in the current file.")

    def test_ambiguity_message_names_the_hunk_and_location_count(self):
        content = "a = 0\nx = 1\nx = 1\nx = 1\n"
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks(content, [change("a = 0", "a = 9"), change("x = 1", "x = 2")])
        self.assertEqual(
            str(raised.exception),
            "Hunk 2 matches 3 locations; include more surrounding lines.",
        )

    def test_overlapping_occurrences_of_one_hunk_are_ambiguous(self):
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks("}\n}\n}\n", [change("}\n}\n", "}\n")])
        self.assertEqual(str(raised.exception), "Hunk 1 matches 2 locations; include more surrounding lines.")

    def test_overlapping_hunks_are_rejected(self):
        for changes in (
            [change("abcd", "X"), change("cdef", "Y")],
            [change("cdef", "Y"), change("abcd", "X")],
        ):
            with self.assertRaises(CodeEditValidationError) as raised:
                apply_hunks("abcdef", changes)
            self.assertEqual(str(raised.exception), "Hunks 1 and 2 overlap; combine them into one change.")
        with self.assertRaisesRegex(CodeEditValidationError, "Hunks 1 and 2 overlap"):
            apply_hunks("abc def", [change("abc", "x"), change("abc", "y")])
        with self.assertRaisesRegex(CodeEditValidationError, "Hunks 2 and 3 overlap"):
            apply_hunks("one two three", [change("one", "1"), change("two three", "x"), change("three", "3")])

    def test_adjacent_hunks_do_not_overlap(self):
        self.assertEqual(apply_hunks("abcdef", [change("def", "2"), change("abc", "1")]), "12")

    def test_unchanged_hunk_is_rejected(self):
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks("a\nb\n", [change("a", "A"), change("b", "b")])
        self.assertEqual(str(raised.exception), "Hunk 2 does not change the matched text.")
        # A line-ending-only difference is still a no-op after normalisation.
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 does not change"):
            apply_hunks("a\r\nb\r\n", [change("a\r\nb", "a\nb")])

    def test_malformed_changes_are_rejected_with_the_hunk_number(self):
        cases = [
            ([{"old": "", "new": "x"}], "Hunk 1 needs non-empty old text"),
            ([{"old": "a", "new": None}], "Hunk 1 needs non-empty old text"),
            ([change("a", "A"), {"old": "b"}], "Hunk 2 needs non-empty old text"),
            ([change("a", "A"), {"old": 3, "new": "x"}], "Hunk 2 needs non-empty old text"),
            ([change("a", "A"), "b"], "Hunk 2 must be an object"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(CodeEditValidationError, message):
                    apply_hunks("a\nb\n", changes)

    def test_empty_or_non_list_patch_is_rejected(self):
        for changes in ([], None, {"old": "a", "new": "b"}, "a"):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(CodeEditValidationError, "at least one change"):
                    apply_hunks("a\n", changes)

    def test_non_text_content_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            apply_hunks(b"a\n", [change("a", "b")])


class ApplyHunksMultiHunkTests(unittest.TestCase):
    CONTENT = (
        "def first():\n    return 1\n\n"
        "def second():\n    return 2\n\n"
        "def third():\n    return 3\n"
    )
    EXPECTED = (
        "def first():\n    value = compute()\n    return value\n\n"
        "def second():\n    return 2\n\n"
        "def third_renamed():\n    pass\n"
    )

    def test_hunks_apply_identically_in_any_order(self):
        changes = [
            change("    return 1\n", "    value = compute()\n    return value\n"),
            change("def third():\n    return 3", "def third_renamed():\n    pass"),
            change("return 2", "return 2  # unchanged value"),
        ]
        expected = self.EXPECTED.replace("return 2\n", "return 2  # unchanged value\n")
        for ordering in itertools.permutations(changes):
            with self.subTest(ordering=[item["old"] for item in ordering]):
                self.assertEqual(apply_hunks(self.CONTENT, list(ordering)), expected)

    def test_hunks_apply_in_any_order_on_a_crlf_file(self):
        content = self.CONTENT.replace("\n", "\r\n")
        changes = [
            change("def third():\n    return 3", "def third_renamed():\n    pass"),
            change("    return 1\n", "    value = compute()\n    return value\n"),
        ]
        expected = self.EXPECTED.replace("\n", "\r\n")
        self.assertEqual(apply_hunks(content, changes), expected)
        self.assertEqual(apply_hunks(content, list(reversed(changes))), expected)

    def test_hunks_match_the_original_file_not_earlier_replacements(self):
        self.assertEqual(
            apply_hunks("alpha beta", [change("alpha", "beta"), change("beta", "gamma")]),
            "beta gamma",
        )

    def test_unicode_content_is_patched_by_character(self):
        content = "名前 = '世界'\nemoji = '🎉'\naccent = 'cafe\u0301'\n"
        result = apply_hunks(
            content,
            [
                change("emoji = '🎉'", "emoji = '🚀✨'"),
                change("名前 = '世界'", "名前 = 'こんにちは'"),
                change("cafe\u0301", "caf\u00e9"),
            ],
        )
        self.assertEqual(result, "名前 = 'こんにちは'\nemoji = '🚀✨'\naccent = 'caf\u00e9'\n")
        crlf = content.replace("\n", "\r\n")
        self.assertEqual(
            apply_hunks(crlf, [change("emoji = '🎉'\naccent", "emoji = '🎈'\naccent")]),
            "名前 = '世界'\r\nemoji = '🎈'\r\naccent = 'cafe\u0301'\r\n",
        )


class LineChangeCountsTests(unittest.TestCase):
    def test_counts_added_removed_and_replaced_lines(self):
        self.assertEqual(line_change_counts("a\nb\nc\n", "a\nB\nc\n"), (1, 1))
        self.assertEqual(line_change_counts("a\nc\n", "a\nb\nc\n"), (1, 0))
        self.assertEqual(line_change_counts("a\nb\nc\n", "a\nc\n"), (0, 1))
        self.assertEqual(line_change_counts("a\nb\n", "x\ny\nz\n"), (3, 2))
        self.assertEqual(line_change_counts("", "x\ny\n"), (2, 0))
        self.assertEqual(line_change_counts("x\ny\n", ""), (0, 2))
        self.assertEqual(line_change_counts("same\n", "same\n"), (0, 0))

    def test_line_endings_are_normalised_before_counting(self):
        self.assertEqual(line_change_counts("a\r\nb\r\n", "a\nb\n"), (0, 0))
        self.assertEqual(line_change_counts("a\r\nb\r\n", "a\nB\n"), (1, 1))

    def test_final_newline_change_counts_like_git(self):
        self.assertEqual(line_change_counts("a", "a\n"), (1, 1))
        self.assertEqual(line_change_counts("a\nb\n", "a\nb"), (1, 1))

    def test_only_line_feeds_split_lines(self):
        # ``str.splitlines`` would count two lines here; Git counts one.
        self.assertEqual(line_change_counts("", "a\u2028b\fc\n"), (1, 0))

    def test_popular_lines_are_not_treated_as_junk(self):
        before = "first\n" + "}\n" * 300 + "last\n"
        after = "FIRST\n" + "}\n" * 300 + "LAST\n"
        self.assertEqual(line_change_counts(before, after), (2, 2))
        # Premise check: difflib's default autojunk would report a rewrite.
        junk_matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
        junk_additions = sum(j2 - j1 for tag, _, _, j1, j2 in junk_matcher.get_opcodes() if tag != "equal")
        self.assertGreater(junk_additions, 2)

    def test_small_edit_to_a_large_repetitive_file_stays_fast(self):
        before = "{\n" + "  },\n  {\n" * 20_000 + "}\n"
        after = before.replace("{\n", "{ \n", 1)
        started = time.perf_counter()
        self.assertEqual(line_change_counts(before, after), (1, 1))
        self.assertLess(time.perf_counter() - started, 2.0)


class UnifiedDiffTextTests(unittest.TestCase):
    def test_modified_file_diff(self):
        diff = unified_diff_text("a\nb\nc\n", "a\nB\nc\n", "src/app.py")
        self.assertEqual(diff, "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c")

    def test_new_file_diff(self):
        diff = unified_diff_text("", "x\ny\n", "src/new.py", is_new=True)
        self.assertEqual(diff, "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,2 @@\n+x\n+y")

    def test_new_file_ignores_stale_previous_content(self):
        self.assertEqual(
            unified_diff_text("old\n", "x\n", "n.py", is_new=True),
            "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x",
        )

    def test_deleted_content_diff(self):
        self.assertEqual(
            unified_diff_text("x\ny\n", "", "gone.py"),
            "--- a/gone.py\n+++ b/gone.py\n@@ -1,2 +0,0 @@\n-x\n-y",
        )

    def test_uses_three_lines_of_context_and_splits_distant_hunks(self):
        before = "".join(f"line{i}\n" for i in range(1, 21))
        after = before.replace("line5\n", "LINE5\n").replace("line18\n", "LINE18\n")
        self.assertEqual(
            unified_diff_text(before, after, "f.txt"),
            "--- a/f.txt\n+++ b/f.txt\n"
            "@@ -2,7 +2,7 @@\n line2\n line3\n line4\n-line5\n+LINE5\n line6\n line7\n line8\n"
            "@@ -15,6 +15,6 @@\n line15\n line16\n line17\n-line18\n+LINE18\n line19\n line20",
        )

    def test_matches_difflib_unified_diff_for_ordinary_edits(self):
        before = "".join(f"value_{i} = {i}\n" for i in range(40))
        after = (
            before.replace("value_3 = 3\n", "value_3 = 'three'\n")
            .replace("value_10 = 10\n", "")
            .replace("value_30 = 30\n", "value_30 = 30\nvalue_30b = 30.5\n")
        )
        expected = "\n".join(
            difflib.unified_diff(
                before.splitlines(), after.splitlines(), "a/m.py", "b/m.py", n=3, lineterm=""
            )
        )
        self.assertEqual(unified_diff_text(before, after, "m.py"), expected)

    def test_identical_content_returns_empty_string(self):
        self.assertEqual(unified_diff_text("a\nb\n", "a\nb\n", "f.py"), "")
        self.assertEqual(unified_diff_text("", "", "f.py", is_new=True), "")
        # Line endings are normalised, so a CRLF view of the same text is identical.
        self.assertEqual(unified_diff_text("a\r\nb\r\n", "a\nb\n", "f.py"), "")

    def test_missing_final_newline_is_marked_like_git(self):
        self.assertEqual(
            unified_diff_text("a\nb", "a\nc", "f.py"),
            "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n a\n-b\n"
            "\\ No newline at end of file\n+c\n\\ No newline at end of file",
        )
        self.assertEqual(
            unified_diff_text("a", "a\n", "f.py"),
            "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a",
        )

    def test_rendered_lines_agree_with_line_change_counts(self):
        cases = [
            ("a\nb\nc\n", "a\nB\nc\nd\n"),
            ("x\n" * 50 + "y", "x\n" * 25 + "z\n" + "x\n" * 25 + "y\n"),
            ("first\n" + "}\n" * 300 + "last\n", "FIRST\n" + "}\n" * 300 + "LAST\n"),
        ]
        for before, after in cases:
            with self.subTest(before=before[:20], after=after[:20]):
                body = unified_diff_text(before, after, "f.py").split("\n")[2:]
                additions = sum(1 for line in body if line.startswith("+"))
                deletions = sum(1 for line in body if line.startswith("-"))
                self.assertEqual((additions, deletions), line_change_counts(before, after))


class NormaliseRepositoryPathTests(unittest.TestCase):
    def test_accepts_nested_relative_paths(self):
        self.assertEqual(normalise_repository_path("src/app/main.py"), "src/app/main.py")
        self.assertEqual(normalise_repository_path("src\\app\\main.py"), "src/app/main.py")
        self.assertEqual(normalise_repository_path("  docs/guide.md  "), "docs/guide.md")
        self.assertEqual(normalise_repository_path("./src//app/./main.py"), "src/app/main.py")
        self.assertEqual(normalise_repository_path(".github/workflows/ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(normalise_repository_path("notes/a..b/c.txt"), "notes/a..b/c.txt")
        self.assertEqual(normalise_repository_path("README"), "README")

    def test_rejects_unsafe_paths(self):
        unsafe = [
            # Traversal.
            "../secrets.env", "src/../../etc/passwd", "src/..", "..", "..\\windows",
            # Absolute and UNC paths.
            "/etc/passwd", "\\etc\\passwd", "\\\\server\\share\\file.txt",
            # Windows drive paths.
            "C:/x", "c:\\Windows\\system32", "C:x", "D:",
            # NUL and other control characters.
            "src/a\x00.py", "src/a\nb.py", "src/a\tb.py",
            # Empty or naming the repository root.
            "", "   ", ".", "./",
        ]
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaises(CodeEditValidationError):
                    normalise_repository_path(value)

    def test_rejects_non_text_values(self):
        for value in (None, 5, b"src/app.py"):
            with self.subTest(value=value):
                with self.assertRaises(CodeEditValidationError):
                    normalise_repository_path(value)

    def test_rejects_drive_paths_hidden_behind_a_leading_dot_segment(self):
        # Normalisation strips "./", so a raw-input check alone would return
        # the drive path "C:/x" for these.
        for value in ("./C:/x", ".\\C:\\x", ".//D:", "././c:x"):
            with self.subTest(value=value):
                with self.assertRaises(CodeEditValidationError):
                    normalise_repository_path(value)


def _is_trailing_ws(character):
    """Whitespace the tolerant matcher ignores: what ``str.rstrip()`` strips, minus LF."""
    return character.isspace() and character != "\n"


def _reference_segment_matches(content, start, end, index, last, part, segment):
    """Brute-force rule for one line of a trailing-whitespace-tolerant match.

    ``part`` and ``segment`` never contain ``\\n``, so a bare ``rstrip()``
    strips exactly the ignorable trailing whitespace.
    """
    if index < last:
        if index == 0 and not segment:
            # A hunk that starts at a line break claims none of the previous
            # line's trailing whitespace.
            return part == ""
        if index == 0 and not segment.rstrip():
            # A hunk that starts with trailing whitespace claims the whole run.
            return not part.rstrip() and not (start and _is_trailing_ws(content[start - 1]))
        return part.rstrip() == segment.rstrip()
    if segment.rstrip() == segment:
        return part == segment
    at_line_end = end == len(content) or content[end] == "\n"
    return part == segment or (at_line_end and part.rstrip() == segment.rstrip())


def _reference_tolerant_spans(content, old):
    """Enumerate every span equal to ``old`` up to trailing whitespace per line."""
    if not old.strip():
        return []
    segments = old.split("\n")
    last = len(segments) - 1
    spans = []
    for start in range(len(content) + 1):
        ends = []
        for end in range(start, len(content) + 1):
            parts = content[start:end].split("\n")
            if len(parts) == len(segments) and all(
                _reference_segment_matches(content, start, end, index, last, part, segment)
                for index, (part, segment) in enumerate(zip(parts, segments))
            ):
                ends.append(end)
        if ends:
            # The widest span covers the whole trailing run at a line end.
            spans.append((start, max(ends)))
    return spans


def _reference_apply_one(content, old, new):
    """Spec-level oracle for one hunk on LF content: result text or error text."""
    starts = [index for index in range(len(content)) if content.startswith(old, index)]
    if len(starts) > 1:
        return f"Hunk 1 matches {len(starts)} locations; include more surrounding lines."
    if starts:
        start, end = starts[0], starts[0] + len(old)
    else:
        spans = _reference_tolerant_spans(content, old)
        if len(spans) > 1:
            return f"Hunk 1 matches {len(spans)} locations; include more surrounding lines."
        if not spans:
            return "Hunk 1 was not found in the current file."
        start, end = spans[0]
    return content[:start] + new + content[end:]


def _apply_or_error(content, changes):
    try:
        return apply_hunks(content, changes)
    except CodeEditValidationError as error:
        return f"error: {error}"


class ApplyHunksAdversarialTests(unittest.TestCase):
    def test_crlf_file_without_final_newline_keeps_it_when_editing_elsewhere(self):
        self.assertEqual(apply_hunks("a\r\nb\r\nc", [change("a", "x\ny")]), "x\r\ny\r\nb\r\nc")

    def test_tolerant_blank_line_containing_whitespace_inside_a_hunk(self):
        self.assertEqual(apply_hunks("a\n   \nb\n", [change("a\n\nb", "A\n\nB")]), "A\n\nB\n")

    def test_tolerant_hunk_ending_in_a_newline_covers_the_file_trailing_whitespace(self):
        self.assertEqual(apply_hunks("foo  \nbar\n", [change("foo\n", "FOO\n")]), "FOO\nbar\n")

    def test_tolerant_pattern_treats_regex_metacharacters_literally(self):
        self.assertEqual(apply_hunks("x = [a.*]  \ny = (b)\n", [change("x = [a.*]\ny = (b)", "z")]), "z\n")
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 was not found"):
            apply_hunks("x = [aXX]  \ny = (b)\n", [change("x = [a.*]\ny = (b)", "z")])

    def test_tolerant_hunk_trailing_whitespace_matches_at_end_of_file_without_newline(self):
        self.assertEqual(apply_hunks("a\nfoo", [change("foo  ", "bar")]), "a\nbar")

    def test_tolerant_hunk_may_start_mid_line(self):
        self.assertEqual(apply_hunks("x  foo  \nbar\n", [change("foo\nbar", "baz")]), "x  baz\n")

    def test_stray_carriage_return_in_a_hunk_never_splits_a_crlf_pair(self):
        self.assertEqual(apply_hunks("a\r\nb\r\n", [change("a\r", "z")]), "z\r\nb\r\n")

    def test_adjacent_hunks_across_a_crlf_line_break(self):
        self.assertEqual(apply_hunks("a\r\nb\r\n", [change("b", "y"), change("a\n", "x\n")]), "x\r\ny\r\n")

    def test_hunk_replacing_the_whole_crlf_file(self):
        self.assertEqual(apply_hunks("a\r\nb\r\n", [change("a\nb\n", "z\n")]), "z\r\n")

    def test_tolerant_ambiguity_reports_every_location(self):
        with self.assertRaises(CodeEditValidationError) as raised:
            apply_hunks("k  \nv\nk\t\nv\nk \nv\n", [change("k\nv", "z")])
        self.assertEqual(str(raised.exception), "Hunk 1 matches 3 locations; include more surrounding lines.")

    def test_single_hunk_agrees_with_a_brute_force_reference(self):
        rng = random.Random(20240611)
        # NBSP is Unicode whitespace that ``str.rstrip()`` strips but a naive
        # ASCII-only matcher would treat as content.
        alphabet = ["a", "b", " ", "\t", "\xa0", "\n", "\n"]
        tolerant_successes = 0
        for _ in range(2500):
            content = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            if content and rng.random() < 0.7:
                start = rng.randrange(len(content))
                old = content[start:rng.randint(start + 1, min(len(content), start + 7))]
                # Perturb trailing whitespace so the tolerant path is exercised.
                if rng.random() < 0.5:
                    old = old.replace("\n", rng.choice(["  \n", "\t\n"]), 1)
                if rng.random() < 0.3:
                    old = old.replace(" \n", "\n").replace("\t\n", "\n")
            else:
                old = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
            expected = _reference_apply_one(content, old, "#")
            if not expected.startswith("Hunk 1") and old not in content:
                tolerant_successes += 1
            if expected.startswith("Hunk 1"):
                expected = f"error: {expected}"
            with self.subTest(content=content, old=old):
                self.assertEqual(_apply_or_error(content, [change(old, "#")]), expected)
        # Guard against a generator that never reaches the tolerant path.
        self.assertGreater(tolerant_successes, 100)

    def test_crlf_and_lf_versions_of_a_file_patch_identically(self):
        rng = random.Random(7)
        alphabet = ["a", "b", "c", " ", "\t", "\n", "\n"]
        compared = 0
        while compared < 1500:
            lf = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
            if "\n" not in lf:
                continue
            changes = []
            for _ in range(rng.randint(1, 3)):
                start = rng.randrange(len(lf))
                old = lf[start:rng.randint(start + 1, min(len(lf), start + 6))]
                if rng.random() < 0.3:
                    old = old.replace("\n", " \n")
                new = "".join(rng.choice(alphabet + ["Z"]) for _ in range(rng.randint(0, 5)))
                changes.append(change(old, new))
            compared += 1
            lf_result = _apply_or_error(lf, changes)
            crlf_result = _apply_or_error(lf.replace("\n", "\r\n"), changes)
            if not lf_result.startswith("error: "):
                lf_result = lf_result.replace("\n", "\r\n")
            with self.subTest(content=lf, changes=changes):
                self.assertEqual(crlf_result, lf_result)


class DiffAdversarialTests(unittest.TestCase):
    def test_trailing_whitespace_change_counts_as_a_changed_line(self):
        self.assertEqual(line_change_counts("a \n", "a\n"), (1, 1))

    def test_crlf_input_renders_lf_diff_lines(self):
        diff = unified_diff_text("a\r\nb\r\n", "a\r\nc\r\n", "f")
        self.assertEqual(diff, "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c")
        self.assertNotIn("\r", diff)

    def test_content_added_to_an_existing_empty_file_keeps_a_headers(self):
        self.assertEqual(unified_diff_text("", "x\n", "f"), "--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+x")

    def test_matches_difflib_for_edits_with_distinct_lines(self):
        # With no repeated lines there is a single best alignment, so the
        # output must be byte-identical to difflib.unified_diff.
        rng = random.Random(42)
        for case in range(300):
            before_lines = [f"line {index}" for index in range(rng.randint(0, 60))]
            after_lines = list(before_lines)
            for edit in range(rng.randint(1, 5)):
                operation = rng.choice(("modify", "insert", "delete"))
                if operation != "insert" and not after_lines:
                    continue
                position = rng.randrange(len(after_lines) + (operation == "insert"))
                if operation == "modify":
                    after_lines[position] = f"changed {case}.{edit}"
                elif operation == "insert":
                    after_lines.insert(position, f"added {case}.{edit}")
                else:
                    del after_lines[position]
            before = "".join(f"{line}\n" for line in before_lines)
            after = "".join(f"{line}\n" for line in after_lines)
            expected = "\n".join(
                difflib.unified_diff(before_lines, after_lines, "a/f.py", "b/f.py", n=3, lineterm="")
            )
            diff = unified_diff_text(before, after, "f.py")
            with self.subTest(case=case):
                self.assertEqual(diff, expected)
                body = diff.split("\n")[2:]
                self.assertEqual(
                    line_change_counts(before, after),
                    (sum(line.startswith("+") for line in body), sum(line.startswith("-") for line in body)),
                )


def _plain_matcher_counts(before, after):
    """Spec oracle: SequenceMatcher(autojunk=False) over every LF-normalised line."""
    old_lines = before.replace("\r\n", "\n").splitlines(keepends=True)
    new_lines = after.replace("\r\n", "\n").splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return additions, deletions


def _random_repetitive_text(rng, max_lines):
    # A three-line alphabet produces many equally long matches, which is where
    # a shortcut such as trimming the shared head and tail changes the result.
    return "".join(rng.choice("abc") + "\n" for _ in range(rng.randint(0, max_lines)))


class DiffSpecConformanceTests(unittest.TestCase):
    # Trimming the shared head ("") and tail ("c\na\n") before matching
    # reports (4, 4) here; the full SequenceMatcher comparison reports (3, 3).
    TRIM_SENSITIVE = ("a\nb\na\nb\na\nb\nc\na\n", "c\nc\na\na\nb\nc\nc\na\n")

    def test_counts_equal_the_full_sequence_matcher_comparison(self):
        before, after = self.TRIM_SENSITIVE
        self.assertEqual(_plain_matcher_counts(before, after), (3, 3))
        self.assertEqual(line_change_counts(before, after), (3, 3))

    def test_counts_match_the_spec_oracle_on_repetitive_input(self):
        rng = random.Random(1)
        for _ in range(3000):
            before = _random_repetitive_text(rng, 10)
            after = _random_repetitive_text(rng, 10)
            with self.subTest(before=before, after=after):
                self.assertEqual(line_change_counts(before, after), _plain_matcher_counts(before, after))

    def test_diff_is_byte_identical_to_difflib_for_short_files(self):
        # Under 200 lines difflib's autojunk has no effect, so the specified
        # difflib.unified_diff output is the exact expectation.
        rng = random.Random(2)
        cases = [self.TRIM_SENSITIVE] + [
            (_random_repetitive_text(rng, 12), _random_repetitive_text(rng, 12)) for _ in range(1500)
        ]
        for before, after in cases:
            expected = "\n".join(
                difflib.unified_diff(
                    before.splitlines(), after.splitlines(), "a/r.txt", "b/r.txt", n=3, lineterm=""
                )
            )
            with self.subTest(before=before, after=after):
                self.assertEqual(unified_diff_text(before, after, "r.txt"), expected)

    def test_trimming_is_only_a_fallback_beyond_the_cost_budget(self):
        before, after = self.TRIM_SENSITIVE
        with mock.patch.object(code_edit, "_DIFF_PAIR_BUDGET", 0):
            self.assertEqual(line_change_counts(before, after), (4, 4))
        self.assertEqual(line_change_counts(before, after), (3, 3))

    def test_fallback_output_still_agrees_between_counts_and_diff(self):
        rng = random.Random(3)
        with mock.patch.object(code_edit, "_DIFF_PAIR_BUDGET", 0):
            for _ in range(500):
                before = _random_repetitive_text(rng, 10)
                after = _random_repetitive_text(rng, 10)
                body = unified_diff_text(before, after, "f").split("\n")[2:]
                with self.subTest(before=before, after=after):
                    self.assertEqual(
                        line_change_counts(before, after),
                        (sum(line.startswith("+") for line in body), sum(line.startswith("-") for line in body)),
                    )

    def test_edits_at_both_ends_of_a_large_lockfile_stay_fast_and_exact(self):
        lines = []
        for index in range(6000):
            lines += [
                "    {\n",
                f'      "name": "pkg{index}",\n',
                f'      "version": "1.{index}.0",\n',
                "    },\n",
            ]
        before = "".join(lines)
        lines[0] = "    { \n"
        lines[-1] = "    }, \n"
        after = "".join(lines)
        started = time.perf_counter()
        self.assertEqual(line_change_counts(before, after), (2, 2))
        diff = unified_diff_text(before, after, "package-lock.json")
        self.assertLess(time.perf_counter() - started, 2.0)
        body = diff.split("\n")[2:]
        self.assertEqual(sum(line.startswith("+") for line in body), 2)
        self.assertEqual(sum(line.startswith("-") for line in body), 2)

    def test_degenerate_repetitive_file_edited_at_both_ends_is_bounded(self):
        # Every body line is popular, so the bounded fallback may overcount;
        # what it must not do is block the event loop for many seconds.
        before = "{\n" + "  },\n  {\n" * 8000 + "}\n"
        after = "{ \n" + "  },\n  {\n" * 8000 + "} \n"
        started = time.perf_counter()
        additions, deletions = line_change_counts(before, after)
        diff = unified_diff_text(before, after, "data.json")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertGreaterEqual(additions, 2)
        self.assertGreaterEqual(deletions, 2)
        body = diff.split("\n")[2:]
        self.assertEqual(
            (additions, deletions),
            (sum(line.startswith("+") for line in body), sum(line.startswith("-") for line in body)),
        )


class HostileInputBoundTests(unittest.TestCase):
    def test_self_overlapping_hunk_in_huge_run_is_rejected_quickly(self):
        # Exhaustive overlapping counting here used to take ~14 s.
        started = time.perf_counter()
        with self.assertRaisesRegex(CodeEditValidationError, r"^Hunk 1 matches \d+ locations"):
            apply_hunks("a" * 2_000_000, [{"old": "a" * 1000, "new": "b"}])
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_small_overlapping_counts_stay_exact(self):
        with self.assertRaisesRegex(CodeEditValidationError, "matches 3 locations"):
            apply_hunks("aaaa", [{"old": "aa", "new": "b"}])

    def test_capped_count_is_a_lower_bound_above_one(self):
        with mock.patch.object(code_edit, "_OVERLAP_SCAN_LIMIT", 2):
            self.assertEqual(code_edit._occurrence_count("a" * 10, "aa"), 5)
            self.assertEqual(len(code_edit._tolerant_spans("x \n" * 10, "x\n")), 2)

    def test_many_tolerant_matches_are_rejected_quickly(self):
        started = time.perf_counter()
        with self.assertRaisesRegex(CodeEditValidationError, "Hunk 1 matches"):
            apply_hunks("a\n" * 500_000, [{"old": "a \na \na", "new": "b"}])
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
