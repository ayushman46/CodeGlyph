import os
import re
import stat
from collections.abc import Iterator
from bisect import bisect_left
from typing import Dict, List, Optional

MAX_LINES_PER_CHUNK = 150
CHUNK_OVERLAP = 40
MAX_CHARS_PER_CHUNK = 12000
# Character fragments of one over-long line overlap by this many characters.
LONG_LINE_OVERLAP = 1500
# ``chunk_file_content`` intentionally remains the compatibility path for
# callers that already have a string.  The ingestion worker uses the streaming
# path for larger files so a 50 MB source file never becomes a second 50 MB
# string plus a list of all of its chunks in the worker.
STREAMING_FILE_THRESHOLD = 2_000_000
# Longest piece read per ``readline`` on the streaming path (one over the
# ceiling, plus room for a CRLF), so a newline-free file never loads whole.
_LINE_READ_LIMIT = MAX_CHARS_PER_CHUNK + 2
# Adjacent declaration segments are merged up to these targets.  Emitting one
# chunk per three-line helper floods retrieval and embedding with fragments
# that carry no surrounding context; ~120 lines keeps a chunk focused while
# staying comfortably below the hard payload ceiling.
TARGET_LINES = 120
TARGET_CHARS = 6000
# A whole file, or one declaration segment, of at most this many lines (and at
# most MAX_CHARS_PER_CHUNK characters) is kept intact; anything larger is cut
# into overlapping windows by ``split_by_lines``.
MAX_SEGMENT_LINES = 200
_MAX_SYMBOLS = 50

# --- Declaration boundaries ---------------------------------------------------
# Boundaries are only recognised at column 0: an indented ``def``/``fun`` is a
# method of the enclosing declaration, and splitting a class between its
# methods would separate them from the class header they depend on.  Every
# quantifier that can scan a line is bounded or unambiguous so a 20k-character
# generated line cannot trigger catastrophic backtracking.
_IDENT = r"[A-Za-z_$][\w$]*"
_WORD = r"[A-Za-z_]\w*"

# Modifier words that may precede a declaration keyword in the supported
# languages (TS ``export default``/``declare``, Rust ``pub(crate)``/``unsafe``,
# Kotlin ``data``/``suspend``, Java/C# access modifiers, Lua ``local``, ...).
_MODIFIER = (
    r"(?:export|default|declare|async|abstract|public|private|protected|internal|static|final|open"
    r"|sealed|data|enum|value|annotation|inner|companion|override|operator|infix|tailrec|external"
    r"|suspend|inline|partial|readonly|virtual|unsafe|const|fileprivate|mutating|indirect|case"
    r"|implicit|extend|local|expect|actual|typedef"
    r"|pub(?:\([^)\n]{0,100}\))?"
    r'|extern(?:[ \t]+"[^"\n]{0,20}")?)'
)
_MODIFIERS = r"(?:" + _MODIFIER + r"[ \t]+){0,8}"

_BOUNDARY_DECLARATION = (
    # An unnamed ``function () {`` or ``class {`` is only legal at column 0
    # after ``export default``, which is exactly the declaration to keep.
    r"(?:function(?:[ \t]*[*(]|[ \t]+[A-Za-z_$])"
    r"|(?:def|fn|fun|func)[ \t]+[A-Za-z_(<]"
    r"|class(?:[ \t]+[A-Za-z_$]|[ \t]*\{)"
    r"|(?:interface|enum|struct|trait|object|record|protocol|extension|module|mod)"
    r"[ \t]+[A-Za-z_$]"
    # A C/Rust/GraphQL union always opens a body or an ``=`` member list;
    # requiring one keeps SQL's column-0 ``union all``/``union select`` from
    # splitting a query in half.
    r"|union[ \t]+[A-Za-z_$][\w$]*[ \t]*[{=<]"
    r"|impl(?:[ \t]*<|[ \t]+[A-Za-z_&!])"
    r"|(?:type|typealias)[ \t]+(?:[A-Za-z_$]|\()"
    # The opening brace may sit on the next line (``message Foo`` / ``{``).
    r"|(?:model|message|service|input|generator|datasource)[ \t]+[A-Za-z_]\w*[ \t]*(?:\{|\r?$)"
    r"|macro_rules![ \t]*[A-Za-z_])"
)

# Java/C#/Swift/PHP members are recognised by a leading access or storage
# modifier followed by a method signature, e.g. ``public static int add(``.
_SIGNATURE_LEAD = r"(?:public|private|protected|internal|static|final|abstract|open)"
# ``val``/``var``/``let`` only ever introduce a property, never a method's
# return type; without this, a Kotlin delegate such as
# ``private val model by viewModels()`` reads as a method named "viewModels".
_TYPE_TOKEN = r"(?!(?:val|var|let)[ \t])[\w$.<>\[\]?,*&]+"
_SIGNATURE = (
    _SIGNATURE_LEAD + r"[ \t]+(?:" + _TYPE_TOKEN + r"[ \t]+){0,6}[*&]*(" + _IDENT + r")"
    r"(?:<[^>\n]{0,100}>)?(?=[ \t]*\()"
)

# ``const X = (...) =>`` and React component factories.  The optional type
# annotation may itself contain ``=>`` (``const f: (a: A) => B = ...``).
_TYPE_ANNOTATION = r"(?:[ \t]*:(?:[^=\n]|=>){1,200}?)?"
_ARROW_START = (
    r"(?:(?:async[ \t]*)?(?:<[^>\n]{0,200}>[ \t]*)?\((?:[^()\n]|\([^()\n]{0,200}\)){0,500}\)"
    r"(?:[ \t]*:[^=\n]{1,200}?)?[ \t]*=>"
    r"|(?:async[ \t]+)?[A-Za-z_$][\w$]*[ \t]*=>"
    # Parameters that continue on the following lines: ``= (``, ``= ({ a,``
    # or an inline props type ``= ({ label }: {``.
    r"|(?:async[ \t]*)?\([ \t]*(?:\{[^()\n]{0,500})?\r?$"
    r"|(?:async[ \t]+)?function\b"
    r"|(?:React\.)?(?:memo|forwardRef)[ \t]*[(<])"
)
_JS_PREFIX = r"(?:export[ \t]+(?:default[ \t]+)?)?(?:declare[ \t]+)?"
_ARROW_CONST = (
    _JS_PREFIX + r"(?:const|let|var)[ \t]+(" + _IDENT + r")" + _TYPE_ANNOTATION
    + r"[ \t]*=[ \t]*" + _ARROW_START
)

# SQL keywords are case-insensitive; the scoped flag keeps every other
# language's keywords case-sensitive (``Class`` is not ``class``).
_SQL_BOUNDARY = (
    r"(?i:(?:create|alter)[ \t]+(?:or[ \t]+replace[ \t]+)?"
    r"(?:(?:temp|temporary|unlogged|materialized|unique|fulltext|spatial|clustered|nonclustered"
    r"|global|local|recursive|virtual|constraint)[ \t]+){0,3}"
    r"(?:table|view|index|function|procedure|trigger|type|sequence|schema|extension|policy)\b)"
)

# Every alternative starts with a keyword letter; the lookahead rejects
# braces, comments and blank lines before any alternative is attempted.
BOUNDARY_REGEX = re.compile(
    r"^(?=[A-Za-z])(?:" + _MODIFIERS + _BOUNDARY_DECLARATION
    + r"|" + _SIGNATURE
    + r"|" + _ARROW_CONST
    + r"|" + _SQL_BOUNDARY + r")",
    re.MULTILINE,
)

# SQL files only split on SQL statements.  Hand-written DDL often lists columns
# at column 0, and names such as ``record``, ``type`` or ``interface`` would
# otherwise read as declarations and cut one CREATE TABLE in half.
_SQL_LANGUAGES = frozenset({"sql", "psql", "pgsql", "plsql", "ddl"})
_SQL_BOUNDARY_REGEX = re.compile(r"^" + _SQL_BOUNDARY)

# A column-0 ``@name`` decorator/annotation, a Rust ``#[attr]`` outer
# attribute (``#[cfg(test)]``, ``#[derive(...)]``) or a C# ``[Attribute]`` line
# belongs to the declaration that follows it rather than to the preceding
# chunk.  ``#![attr]`` applies to the enclosing module, so it is deliberately
# not matched.  A C# attribute must fill the whole line so a column-0 row of a
# list literal (``[extra, routes],``) is not mistaken for one.
_DECORATOR_LINE = re.compile(r"@[A-Za-z_$]|#\[|\[[A-Za-z_][\w.]*(?:\([^\n]*\))?\][ \t]*$")
# Annotations written on the declaration's own line (``@Observable final
# class Model {``, ``@Serializable data class User(``, ``#[derive(Debug)]
# struct P;``).  Argument lists are limited to one level of parentheses and
# 200 characters, which keeps the pattern linear.
_ANNOTATION = r"(?:@[A-Za-z_$][\w$.]*(?:\([^()\n]{0,200}\))?|#\[[^\n]{0,200}?\])"
_INLINE_ANNOTATIONS = re.compile(r"(?:" + _ANNOTATION + r"[ \t]+){1,4}")
_MARKDOWN_HEADING = re.compile(r"#{1,3}[ \t]")
_MARKDOWN_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_DOC_LANGUAGES = frozenset({"md", "mdx", "markdown", "rst"})

# --- Symbols ------------------------------------------------------------------
# Declaration keywords double as English words ("class names are ..."), so a
# type-like name must be followed by something only a declaration has.
_TYPE_FOLLOW = (
    r"(?=[ \t]*(?:[{(<\[:;=@]|\r?$)"
    r"|[ \t]+(?:extends|implements|inherits|with|where|derives|private|public|internal|protected"
    r"|constructor|final|sealed|open)\b)"
)
# ``type`` additionally covers Go (``type ID string``, ``type Repo struct``)
# and GraphQL (``type Query {``) while rejecting prose ("type something in").
# A bare ``type X`` ending the line is rejected: no supported language
# idiomatically declares a type that way, but a SQL column named ``type`` does
# (``  type text`` as the last column would otherwise yield a "text" symbol).
_TYPE_ALIAS_FOLLOW = (
    r"(?=[ \t]*[={<\[@]"
    r"|[ \t]+(?:implements\b|(?:struct|interface|func|map|chan)\b|[*\[]|[\w.]+[ \t]*(?:\r?$|//)))"
)
_GENERIC = r"<(?:[^<>\n]|<(?:[^<>\n]|<[^<>\n]{0,100}>){0,200}>){0,300}>"
_RUST_PATH = r"(?:" + _WORD + r"::){0,8}" + _WORD
# ``impl Foo`` and ``impl<T> Trait<T> for Foo<T>`` both record ``Foo``: the
# implementing type is what a reader searches for.
_RUST_IMPL = (
    r"(?:[ \t]*" + _GENERIC + r")?[ \t]+"
    r"(?:!?" + _RUST_PATH + r"(?:" + _GENERIC + r")?[ \t]+for[ \t]+)?"
    r"(?:&(?:'" + _WORD + r"[ \t]+)?(?:mut[ \t]+)?)?(?:dyn[ \t]+)?"
    r"(?:" + _WORD + r"::){0,8}(" + _WORD + r")"
)
_SYMBOL_DECLARATION = (
    r"function(?:[ \t]*\*[ \t]*|[ \t]+)(?:[A-Za-z_$][\w$]*[.:]){0,4}(" + _IDENT + r")(?=[ \t]*[(<])"
    r"|def[ \t]+(?:self\.)?([A-Za-z_]\w*[?!]?)"
    r"|fn[ \t]+(" + _WORD + r")(?=[ \t]*[(<])"
    r"|fun[ \t]+(?:<[^>\n]{0,100}>[ \t]*)?(?:[A-Za-z_][\w<>?, ]{0,100}\.)?(" + _WORD + r")(?=[ \t]*[(<])"
    r"|func[ \t]+(?:\([^)\n]{0,200}\)[ \t]*)?(" + _WORD + r")(?=[ \t]*[(<\[])"
    r"|(?:class|interface|trait|struct|object|record|protocol|module|mod"
    r"|enum(?:[ \t]+(?:class|struct))?)[ \t]+(?:" + _WORD + r"::){0,8}(" + _IDENT + r")" + _TYPE_FOLLOW
    # Same rule as the boundary: SQL's ``union all`` is not a declaration.
    + r"|union[ \t]+(" + _IDENT + r")(?=[ \t]*[{=<])"
    + r"|(?:type|typealias)[ \t]+(" + _IDENT + r")" + _TYPE_ALIAS_FOLLOW
    + r"|(?:model|message|service|input)[ \t]+(" + _WORD + r")(?=[ \t]*\{)"
    r"|impl" + _RUST_IMPL
)
_SQL_NAME = r"(?:[`\"\[]?[\w$]+[`\"\]]?\.){0,2}[`\"\[]?[\w$]+[`\"\]]?"
_SQL_SYMBOL = (
    r"(?i:create[ \t]+(?:or[ \t]+replace[ \t]+)?"
    r"(?:(?:temp|temporary|unlogged|materialized|global|local|recursive|virtual|secure|constraint)"
    r"[ \t]+){0,3}"
    r"(?:table|view|function|procedure|trigger|type)[ \t]+(?:if[ \t]+not[ \t]+exists[ \t]+)?)"
    # When the name moves to the next line (``CREATE TABLE IF NOT EXISTS``
    # alone), the optional clause above cannot match and ``IF`` would be
    # captured as the name; an unquoted ``if`` is never a legal SQL name.
    r"(?!(?i:if)(?:[ \t]|\r?$))"
    r"(" + _SQL_NAME + r")"
)

# The ``(?=[A-Za-z])`` after the indentation matters for speed, not meaning:
# when no declaration matches, the engine backtracks through the indentation
# one character at a time, and the lookahead rejects each of those positions
# in a single step instead of retrying every alternative (a long run of
# spaces would otherwise cost alternatives x indentation width).  Leading
# same-line annotations (``@Test fun adds()``, ``@Observable class Book``)
# are skipped so the declaration after them is still named.  A UTF-8 byte
# order mark survives ``open(encoding="utf-8")`` (Visual Studio writes one by
# default) and would otherwise hide a declaration on a file's first line.
SYMBOL_REGEX = re.compile(
    r"^﻿?[ \t]*(?:" + _ANNOTATION + r"[ \t]+){0,4}(?=[A-Za-z])(?:"
    + _MODIFIERS + r"(?:" + _SYMBOL_DECLARATION + r")"
    + r"|" + _ARROW_CONST
    + r"|" + _SIGNATURE
    + r"|" + _SQL_SYMBOL
    + r"|rpc[ \t]+(" + _WORD + r")(?=[ \t]*\()"
    + r"|macro_rules![ \t]*(" + _WORD + r"))",
    re.MULTILINE,
)

# Last line of defence against control flow masquerading as a declaration.
# Deliberately excludes names that are legitimate declarations elsewhere
# (Rust ``fn new``, Python ``def delete``, ``impl From<T>``'s ``fn from``).
_NOT_SYMBOLS = frozenset({
    "if", "else", "elif", "for", "while", "do", "switch", "case", "catch", "try", "finally",
    "return", "throw", "await", "yield", "extends", "implements", "import", "export",
    "typeof", "instanceof",
})

# Extension-less files that are still source code.  Anything else without an
# extension is reported as plain text.
_KNOWN_FILENAMES = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "vagrantfile": "ruby",
    "podfile": "ruby",
    "brewfile": "ruby",
    "procfile": "text",
    "jenkinsfile": "groovy",
}


def _detect_language(file_path: str) -> str:
    """Return the chunk language label for a repository-relative path."""
    name = file_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    # ``Dockerfile.dev`` has an extension of "dev" but is still a Dockerfile.
    if name.startswith("dockerfile."):
        return "dockerfile"
    suffix = os.path.splitext(name)[1][1:]
    if suffix:
        return suffix
    return _KNOWN_FILENAMES.get(name, "text")


def extract_symbols(content: str) -> List[str]:
    """Return declaration names in source order, without pretending to parse an AST."""
    symbols: List[str] = []
    seen: set[str] = set()
    for match in SYMBOL_REGEX.finditer(content):
        raw = next((group for group in match.groups() if group), None)
        if not raw:
            continue
        symbol = _clean_symbol(raw)
        if not symbol or symbol in _NOT_SYMBOLS or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
        if len(symbols) >= _MAX_SYMBOLS:
            break
    return symbols


def _clean_symbol(raw: str) -> str:
    # Only SQL names can be schema-qualified or quoted ("public"."users",
    # `db`.`orders`, [dbo].[Users]); other captures never contain these.
    return raw.rsplit(".", 1)[-1].strip("`\"[] ")


def _split_lines(content: str) -> List[str]:
    """Split on real line breaks only, matching editors, GitHub and file iteration.

    ``str.splitlines`` also breaks on form feeds, ``\\x85`` and ``\\u2028``,
    which shifts every later line number away from what a reader sees and
    from the streaming path (which iterates the file on ``\\n``).
    """
    lines = _normalize_newlines(content).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _normalize_newlines(content: str) -> str:
    if "\r" not in content:
        return content
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _make_chunk(file_path: str, start_line: int, end_line: int, content: str, language: str) -> Dict:
    return {
        "file_path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
        "language": language,
        "symbols": extract_symbols(content),
    }


def chunk_file_content(content: str, rel_path: str) -> List[Dict]:
    """Split already-loaded source text into deterministic retrieval chunks.

    Keeping the pure content operation separate lets the ingestion worker run
    several *bounded* files concurrently without changing chunk boundaries or
    reading a file more than once inside a worker.

    Every chunk on the line-based paths satisfies
    ``content == "\\n".join(lines[start_line - 1:end_line])`` so citations and
    edits can rely on the reported range being exactly the stored text.
    """
    if not content.strip():
        return []

    language = _detect_language(rel_path)
    text = _normalize_newlines(content)
    lines = _split_lines(text)

    # 1. If file is small, treat it as a single chunk.  The content is the
    # joined lines (no trailing newline) so the exact-range invariant holds for
    # small files exactly as it does for boundary chunks.
    if len(lines) <= MAX_SEGMENT_LINES and len(text) <= MAX_CHARS_PER_CHUNK:
        return [_make_chunk(rel_path, 1, len(lines), "\n".join(lines), language)]

    # Long lines (even a whole minified bundle) stay on the line-aligned path:
    # only a single line longer than MAX_CHARS_PER_CHUNK is itself cut into
    # overlapping character fragments, exactly like the streaming path.  A
    # whole-file character split used to cut ordinary lines in half and made
    # citation ranges disagree with their text.
    return _boundary_chunks(lines, rel_path, language)


def _boundary_chunks(lines: List[str], file_path: str, language: str) -> List[Dict]:
    """Group declaration segments into chunks near the target size."""
    starts = _find_boundaries(lines, language)
    # Prefix sums give the exact joined length of any line range in O(1), so
    # merging never has to materialise candidate strings.
    prefix = [0]
    running = 0
    for line in lines:
        running += len(line)
        prefix.append(running)

    def span_chars(first: int, last: int) -> int:
        return prefix[last] - prefix[first] + (last - first - 1)

    chunks: List[Dict] = []
    pending_first = -1
    pending_last = -1
    edges = starts + [len(lines)]
    for first, last in zip(edges, edges[1:]):
        if last <= first:
            continue
        if last - first > MAX_SEGMENT_LINES or span_chars(first, last) > MAX_CHARS_PER_CHUNK:
            if pending_first >= 0:
                chunks.append(_span_chunk(lines, pending_first, pending_last, file_path, language))
                pending_first = -1
            chunks.extend(split_by_lines(lines[first:last], first + 1, file_path, language))
            continue
        if (
            pending_first >= 0
            and last - pending_first <= TARGET_LINES
            and span_chars(pending_first, last) <= TARGET_CHARS
        ):
            pending_last = last
            continue
        if pending_first >= 0:
            chunks.append(_span_chunk(lines, pending_first, pending_last, file_path, language))
        pending_first, pending_last = first, last
    if pending_first >= 0:
        chunks.append(_span_chunk(lines, pending_first, pending_last, file_path, language))

    # Filter empty chunks
    return [chunk for chunk in chunks if chunk["content"].strip()]


def _span_chunk(lines: List[str], first: int, last: int, file_path: str, language: str) -> Dict:
    return _make_chunk(file_path, first + 1, last, "\n".join(lines[first:last]), language)


def _find_boundaries(lines: List[str], language: str) -> List[int]:
    """Return sorted 0-based segment start lines, always beginning with 0."""
    if language in _DOC_LANGUAGES:
        return _heading_boundaries(lines)
    if language in _SQL_LANGUAGES:
        return [0] + [
            index for index, line in enumerate(lines)
            if index > 0 and line[:1].isalpha() and _SQL_BOUNDARY_REGEX.match(line)
        ]

    starts = [0]
    decorator_start: Optional[int] = None
    for index, line in enumerate(lines):
        if not line:
            # "Immediately above" means contiguous: a blank line ends a stack.
            decorator_start = None
            continue
        first = line[0]
        if first == " " or first == "\t":
            if line.isspace():
                decorator_start = None
            # Indented lines (bodies, multi-line decorator arguments) never
            # start a declaration at column 0.
            continue
        if first in ")]}":
            # Closes a multi-line decorator such as ``@Component({ ... })``.
            continue
        if first in "@#[" and _DECORATOR_LINE.match(line):
            if not _is_inline_annotated_declaration(line):
                if decorator_start is None:
                    decorator_start = index
                continue
        elif decorator_start is not None and (first == "#" or line.startswith("//")):
            # A comment between stacked decorators does not detach them.
            continue
        elif not (first.isalpha() and BOUNDARY_REGEX.match(line)):
            decorator_start = None
            continue
        start = index if decorator_start is None else decorator_start
        if start > starts[-1]:
            starts.append(start)
        decorator_start = None
    return starts


def _is_inline_annotated_declaration(line: str) -> bool:
    """True for ``@Observable final class Model {`` style one-line declarations.

    Such a line is the declaration itself, not a decorator of the next one:
    treating it as a stack line would hide the declaration and, through the
    column-0 ``}`` that closes its body, glue it to whatever follows.
    """
    annotations = _INLINE_ANNOTATIONS.match(line)
    return annotations is not None and BOUNDARY_REGEX.match(line[annotations.end():]) is not None


def _heading_boundaries(lines: List[str]) -> List[int]:
    """Markdown sections start at ``#``/``##``/``###`` headings outside code fences."""
    starts = [0]
    fence: Optional[str] = None
    for index, line in enumerate(lines):
        fence_match = _MARKDOWN_FENCE.match(line)
        if fence_match:
            marker, rest = fence_match.groups()
            if fence is None:
                # A backtick info string cannot contain backticks; such a line
                # is inline code (```x```), not an opening fence.
                if not (marker[0] == "`" and "`" in rest):
                    fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = None
            continue
        if fence is None and index > 0 and _MARKDOWN_HEADING.match(line):
            starts.append(index)
    return starts


def chunk_file(filepath: str, repo_path: str) -> List[Dict]:
    """Read a file and split it using the same deterministic chunking rules."""
    return list(iter_chunk_file(filepath, repo_path))


def iter_chunk_file(filepath: str, repo_path: str) -> Iterator[Dict]:
    """Yield source chunks without retaining an entire large file in memory.

    Small files use the established boundary-aware implementation.  Large
    files use a bounded line window with overlap; this keeps line ranges and
    exact source text deterministic while limiting live memory to one chunk.
    The public ``chunk_file`` function still returns a list for compatibility,
    whereas the ingestion pipeline consumes this iterator incrementally.
    """
    handle = _open_repository_file(filepath, repo_path)
    if handle is None:
        return
    rel_path = os.path.relpath(filepath, repo_path).replace(os.sep, "/")
    try:
        with handle:
            file_size = os.fstat(handle.fileno()).st_size
            if file_size <= STREAMING_FILE_THRESHOLD:
                content = handle.read()
                yield from chunk_file_content(content, rel_path)
                return
            yield from iter_streaming_chunks(handle, rel_path)
    except (OSError, UnicodeError):
        return


def _open_repository_file(filepath: str, repo_path: str):
    """Open a regular file inside the repository, or return ``None``.

    Cloned repositories are untrusted: a committed symlink can point at
    ``/etc/passwd`` or server secrets, which would otherwise be indexed and
    served back in answers, and a FIFO or device node would block the reader
    thread forever on ``open``/``read``.  Opening non-blocking and checking the
    *opened* descriptor avoids both a hang and a swap-after-check race.
    """
    try:
        real_repo = os.path.realpath(repo_path)
        real_file = os.path.realpath(filepath)
        if os.path.commonpath([real_repo, real_file]) != real_repo or real_file == real_repo:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(real_file, flags)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return None
        return os.fdopen(descriptor, "r", encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        os.close(descriptor)
        return None


def iter_streaming_chunks(handle, file_path: str) -> Iterator[Dict]:
    """Yield fixed, overlapping line windows from an open text file.

    Boundary detection is deliberately skipped for this large-file path: a
    boundary list and ``splitlines`` would scale with the whole file.  The
    same 150-line/12k-character ceilings and 40-line overlap retain enough
    context for retrieval while keeping the producer's allocation bounded.
    The overlap shrinks (to at most half a window, and further if needed) when
    long lines make a window character-limited, so every window advances and
    stays under the ceiling.  An unusually long line is split by characters
    rather than allowed to bypass the payload ceiling.
    """
    language = _detect_language(file_path)
    window: list[str] = []
    start_line = 1
    line_number = 0
    # Always the exact length of "\n".join(window).
    window_chars = 0

    def emit(lines: list[str], first_line: int) -> Dict | None:
        if not lines:
            return None
        content = "\n".join(lines)
        if not content.strip():
            return None
        return _make_chunk(file_path, first_line, first_line + len(lines) - 1, content, language)

    while True:
        # A bounded read: iterating the handle would materialise a hostile
        # newline-free file (hundreds of MB) as one string.
        raw_line = handle.readline(_LINE_READ_LIMIT)
        if not raw_line:
            break
        line_number += 1
        complete = raw_line.endswith("\n") or len(raw_line) < _LINE_READ_LIMIT
        line = raw_line.rstrip("\r\n") if complete else raw_line
        # A generated/minified source file can contain a single very large
        # line. Flush ordinary content first, then split that line safely.
        if not complete or len(line) > MAX_CHARS_PER_CHUNK:
            item = emit(window, start_line)
            if item:
                yield item
            yield from _stream_long_line(handle, line, complete, emit, line_number)
            # The long line has been emitted in full. Do not emit the prior
            # overlap again at EOF or prepend it to the next normal window.
            window = []
            start_line = line_number + 1
            window_chars = 0
            continue

        candidate_chars = window_chars + len(line) + (1 if window else 0)
        if window and (len(window) >= MAX_LINES_PER_CHUNK or candidate_chars > MAX_CHARS_PER_CHUNK):
            item = emit(window, start_line)
            if item:
                yield item
            window = _overlap_tail(window)
            window_chars = _joined_length(window)
            # The overlap is context only.  With long lines a full 40-line
            # overlap could already exceed the ceiling, which used to make
            # every following line re-emit an ever larger window.
            while window and window_chars + 1 + len(line) > MAX_CHARS_PER_CHUNK:
                window_chars -= len(window[0]) + (1 if len(window) > 1 else 0)
                window.pop(0)
            start_line = line_number - len(window)
        window.append(line)
        window_chars += len(line) + (1 if len(window) > 1 else 0)

    item = emit(window, start_line)
    if item:
        yield item


def _stream_long_line(handle, buffer: str, complete: bool, emit, line_number: int) -> Iterator[Dict]:
    """Emit ``line[k*step : k*step + MAX]`` fragments while holding O(MAX) text.

    Produces exactly the fragments of slicing the whole line at a
    ``MAX_CHARS_PER_CHUNK - 1500`` stride, without ever holding the line.
    """
    step = MAX_CHARS_PER_CHUNK - LONG_LINE_OVERLAP
    # A CRLF can straddle two bounded reads on an untranslated handle; hold a
    # trailing "\r" back so it is never emitted as line content.
    carry = ""
    if not complete and buffer.endswith("\r"):
        buffer, carry = buffer[:-1], "\r"
    while True:
        while len(buffer) >= MAX_CHARS_PER_CHUNK:
            item = emit([buffer[:MAX_CHARS_PER_CHUNK]], line_number)
            if item:
                yield item
            buffer = buffer[step:]
        if complete:
            break
        piece = handle.readline(_LINE_READ_LIMIT)
        if not piece:
            break
        complete = piece.endswith("\n") or len(piece) < _LINE_READ_LIMIT
        piece, carry = carry + piece, ""
        if complete:
            buffer += piece.rstrip("\r\n")
        elif piece.endswith("\r"):
            buffer, carry = buffer + piece[:-1], "\r"
        else:
            buffer += piece
    # A carried "\r" at EOF was the line terminator, which the normal path
    # strips too, so it is dropped rather than appended.
    item = emit([buffer], line_number)
    if item:
        yield item


def _overlap_tail(window: List[str]) -> List[str]:
    # A character-limited window can hold far fewer than 150 lines; capping
    # the overlap at half the window guarantees each window advances.
    keep = min(CHUNK_OVERLAP, len(window) // 2)
    return window[len(window) - keep:] if keep else []


def _joined_length(lines: List[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def split_by_lines(lines: List[str], offset_line: int, file_path: str, language: str) -> List[Dict]:
    """Splits lines into manageable chunks with overlap and a char ceiling.

    Windows hold up to ``MAX_LINES_PER_CHUNK`` lines and never exceed
    ``MAX_CHARS_PER_CHUNK`` characters unless a single line does.  Every line
    is covered: the loop ends only once a window reaches the last line.
    Consecutive windows share ``CHUNK_OVERLAP`` lines, capped at half of a
    character-limited window; a fixed 40-line overlap on a 17-line window
    would advance one line at a time and duplicate the segment many times.
    """
    chunks: List[Dict] = []
    total = len(lines)
    start = 0
    previous_end = 0
    while start < total:
        end = _window_end(lines, start)
        if end <= previous_end:
            # The next line is too long to join any window, so this overlap
            # window would only repeat lines the previous chunk already holds.
            start = previous_end
            continue
        if end - start == 1 and len(lines[start]) > MAX_CHARS_PER_CHUNK:
            line_number = offset_line + start
            for fragment in long_line_fragments(lines[start]):
                if fragment.strip():
                    chunks.append(_make_chunk(file_path, line_number, line_number, fragment, language))
            previous_end = start = end
            continue
        chunks.append(_make_chunk(
            file_path, offset_line + start, offset_line + end - 1, "\n".join(lines[start:end]), language,
        ))
        previous_end = end
        if end >= total:
            break
        start = end - min(CHUNK_OVERLAP, (end - start) // 2)
    return chunks


def long_line_fragments(line: str) -> List[str]:
    """Cut one line into ``line[k*step : k*step + MAX]`` fragments.

    Identical to what ``_stream_long_line`` emits, so the in-memory and the
    streaming paths store the same rows.  Each fragment is a chunk whose
    ``start_line == end_line``; consecutive fragments share
    ``LONG_LINE_OVERLAP`` characters, which lets a reader stitch the line back.
    """
    step = MAX_CHARS_PER_CHUNK - LONG_LINE_OVERLAP
    fragments: List[str] = []
    buffer = line
    while len(buffer) >= MAX_CHARS_PER_CHUNK:
        fragments.append(buffer[:MAX_CHARS_PER_CHUNK])
        buffer = buffer[step:]
    fragments.append(buffer)
    return fragments


def _window_end(lines: List[str], start: int) -> int:
    """Exclusive end of the largest window at ``start`` within both ceilings."""
    limit = min(len(lines), start + MAX_LINES_PER_CHUNK)
    end = start + 1
    size = len(lines[start])
    while end < limit:
        size += 1 + len(lines[end])
        if size > MAX_CHARS_PER_CHUNK:
            break
        end += 1
    return end


def split_by_characters(content: str, file_path: str, language: str) -> List[Dict]:
    """Legacy whole-file character windows (no longer used for ingestion).

    Windows start and end mid-line, so ``content`` does not equal the lines of
    the reported range.  Kept for compatibility with older callers only.
    """
    chunks = []
    window = MAX_CHARS_PER_CHUNK
    overlap = 1500
    start_idx = 0
    newline_offsets = _newline_offsets(content)

    while start_idx < len(content):
        end_idx = min(len(content), start_idx + window)
        chunk_text = content[start_idx:end_idx]
        # A line number is one plus the newlines strictly before a position;
        # counting "\n" (not ``splitlines``) keeps ranges consistent with the
        # line-based paths.
        start_line = bisect_left(newline_offsets, start_idx) + 1
        end_line = bisect_left(newline_offsets, end_idx - 1) + 1

        # A window of pure whitespace (padding inside generated output) has
        # nothing to retrieve; the line-based paths drop such chunks too.
        if chunk_text.strip():
            chunks.append(_make_chunk(file_path, start_line, end_line, chunk_text, language))

        if end_idx >= len(content):
            break
        start_idx = max(0, end_idx - overlap)

    return chunks


def _newline_offsets(content: str) -> List[int]:
    offsets: List[int] = []
    position = content.find("\n")
    while position != -1:
        offsets.append(position)
        position = content.find("\n", position + 1)
    return offsets


def is_probably_minified(content: str, lines: List[str]) -> bool:
    """Heuristic kept for callers and diagnostics; chunking no longer uses it."""
    if not lines:
        return False

    longest_line = max(len(line) for line in lines)
    average_line = len(content) / max(1, len(lines))
    return longest_line > 2000 or average_line > 400
