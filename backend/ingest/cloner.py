"""Repository source acquisition and importance-ranked file selection.

Huge repositories must index their most important files instead of failing.
This module therefore owns three tightly related policies:

* which GitHub URLs identify exactly one public repository;
* which files are worth indexing (and why every other file was skipped);
* which files win when a repository is larger than the indexing budget.

Source is normally streamed from GitHub's archive endpoint so no git binary is
needed; a shallow git clone remains as a fallback for transient archive
failures.
"""

import errno
import heapq
import http.client
import logging
import math
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from config import settings

try:
    from git import GitCommandError, GitCommandNotFound, Repo
except ImportError:
    # GitPython refuses to import when no git executable is installed. The
    # archive download needs no git, so a missing binary must only disable the
    # clone fallback; it must not make the whole ingestion package unimportable.
    Repo = None

    class GitCommandError(Exception):  # type: ignore[no-redef]
        """Placeholder so ``except`` clauses stay valid without GitPython."""

    class GitCommandNotFound(Exception):  # type: ignore[no-redef]
        """Placeholder so ``except`` clauses stay valid without GitPython."""

logger = logging.getLogger(__name__)

REPOS_DIR = Path("./repos_temp")

# ---------------------------------------------------------------------------
# GitHub URL policy
# ---------------------------------------------------------------------------

GITHUB_HOSTS = {"github.com", "www.github.com"}
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}")
# Third path segments that are GitHub pages *inside* a repository. Links such
# as /owner/repo/tree/main/src are copied from the browser all the time and
# still identify exactly one repository, so the trailing route is dropped.
GITHUB_ROUTE_SEGMENTS = {
    "tree", "blob", "commits", "commit", "issues", "pulls", "pull", "releases",
    "actions", "wiki", "tags", "branches", "compare", "discussions", "security",
    "pulse", "graphs", "network", "projects", "milestones", "labels",
}
# First path segments that are GitHub product pages rather than accounts.
# /settings/profile would otherwise look like a repository called "profile".
RESERVED_OWNER_NAMES = {
    "settings", "orgs", "topics", "marketplace", "sponsors", "login", "logout",
    "features", "about", "explore", "notifications", "new", "organizations",
    "enterprise", "pricing", "search", "collections", "trending",
}
# Far above any real repository URL; bounds the work done on hostile input.
MAX_GITHUB_URL_LENGTH = 2_048
_SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")

_URL_SHAPE_MESSAGE = (
    "Provide a GitHub repository URL such as https://github.com/owner/repository or owner/repository."
)
_URL_CHARACTER_MESSAGE = "GitHub URL must not contain spaces, control characters, or backslashes."
_URL_SCHEME_MESSAGE = "Only https:// (or http://) GitHub repository URLs are supported."
_URL_HOST_MESSAGE = "Only public repositories hosted on github.com are supported."
_URL_CREDENTIALS_MESSAGE = "GitHub URL must not include a username, password, or token."
_URL_PORT_MESSAGE = "GitHub URL must not include a port number."
_URL_OWNER_MESSAGE = "GitHub owner name is not valid."
_URL_RESERVED_MESSAGE = "The URL points to a GitHub page, not to a repository owner."
_URL_REPOSITORY_MESSAGE = "GitHub repository name is not valid."
_URL_ROUTE_MESSAGE = "GitHub URL must point to a repository, not to another page inside it."

# ---------------------------------------------------------------------------
# File policy
# ---------------------------------------------------------------------------

IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "vendor", "target", "bin", "obj", ".cache", ".tox", "coverage",
    "out", ".turbo", ".yarn", ".pnpm-store", "bower_components", "jspm_packages",
    ".gradle", ".idea", "site-packages", ".mypy_cache", ".pytest_cache",
    "storybook-static", ".svelte-kit", ".nuxt", ".output", ".vercel", ".expo",
    # Exact-case on purpose: CocoaPods and Xcode use these capitalised names,
    # while a lowercase "pods" directory is usually real (e.g. Kubernetes) code.
    "Pods", "DerivedData", "elm-stuff", ".terraform", "__snapshots__",
}
# Hidden directories are tool caches or editor state, except these, which
# hold CI and development-environment definitions that explain a project.
ALLOWED_HIDDEN_DIRS = {".github", ".circleci", ".gitlab", ".devcontainer"}
# Hidden files may hold secrets (.env). Only documented templates are indexed.
ALLOWED_HIDDEN_FILES = {
    ".env.example", ".env.sample", ".env.template", ".env.local.example", ".gitlab-ci.yml",
}
SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "composer.lock",
    "cargo.lock", "bun.lockb", "i18n.lock", "bun.lock", "uv.lock", "pipfile.lock",
    "gemfile.lock", "go.sum", "npm-shrinkwrap.json", "pubspec.lock", "packages.lock.json",
    "flake.lock", "mix.lock", "podfile.lock", "deno.lock",
}
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".swift", ".kt", ".kts",
    ".scala", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".yaml", ".yml", ".json",
    ".toml", ".ini", ".cfg", ".conf", ".md", ".rst", ".txt", ".html", ".css",
    ".scss", ".sass", ".less", ".xml", ".graphql", ".gql", ".proto", ".dockerfile",
    ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro", ".prisma", ".mdx",
    ".dart", ".lua", ".ex", ".exs", ".erl", ".hrl", ".hs", ".ml", ".mli", ".r",
    ".jl", ".tf", ".tfvars", ".hcl", ".gradle", ".cmake", ".m", ".mm", ".zig",
    ".nim", ".clj", ".cljs", ".edn", ".groovy", ".pl", ".pm", ".fs", ".fsx", ".vb",
    ".sol", ".elm", ".nix", ".rake", ".gemspec", ".properties", ".hbs", ".handlebars",
    ".ejs", ".pug", ".jinja", ".j2", ".twig", ".liquid", ".njk", ".bat", ".cmd",
    ".fish", ".tex", ".graphqls", ".thrift", ".avsc",
}
TEXT_FILENAMES = {
    "dockerfile", "makefile", "readme", "license", "gitignore", "editorconfig",
    "procfile", "gemfile", "rakefile", "jenkinsfile", "vagrantfile", "brewfile",
    "justfile", "caddyfile", "containerfile", "codeowners", "contributing",
    "changelog", "gnumakefile",
}
BUILD_ARTIFACT_SUFFIXES = (".tsbuildinfo", ".map")

# Locale catalogues: a directory named like one of these, plus a language tag
# in a later directory or in the file stem (locales/fr/common.json,
# config/locales/pt_BR.yml).
LOCALE_DIRECTORY_NAMES = {
    "locales", "locale", "i18n", "l10n", "lang", "langs", "translations", "messages", "intl",
}
LOCALE_CATALOGUE_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".po", ".properties", ".xlf", ".xliff", ".arb", ".strings", ".toml",
}
# Script catalogues (locales/fr.ts) only count when the stem *is* the tag, so
# locales/fr/index.ts or i18n/index.ts (loader code) is never mistaken for one.
LOCALE_SCRIPT_EXTENSIONS = {".ts", ".js"}
ENGLISH_LANGUAGE_CODE = "en"
# ISO 639-1 two-letter codes, including the withdrawn "bh", "sh", "iw"
# (Hebrew), "in" (Indonesian), "ji" (Yiddish), "jw" (Javanese) and "mo"
# (Moldavian) that still appear in locale folders (cal.com ships locales/iw). An explicit set keeps words such as "ui",
# "db", or "js" from being read as languages.
ISO_639_1_CODES = frozenset({
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "in", "io", "is", "it", "iu", "iw",
    "ja", "ji", "jv", "jw",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mo", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "sh", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr",
    "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh",
    "yi", "yo",
    "za", "zh", "zu",
})
# Language plus up to two region/script subtags: pt_BR, zh-Hant, es-419,
# zh-Hant-TW. Longer variants are rare and not worth the false positives.
LANGUAGE_TAG_PATTERN = re.compile(
    r"(?P<language>[A-Za-z]{2})(?:[-_](?:[A-Za-z]{2}|[A-Za-z]{4}|[0-9]{3})){0,2}"
)

# ---------------------------------------------------------------------------
# Priority policy (see file_priority)
# ---------------------------------------------------------------------------

MANIFEST_FILENAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod",
    "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "gemfile", "composer.json",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "makefile",
    "tsconfig.json", "turbo.json", "nx.json", "vercel.json", "render.yaml", "netlify.toml",
}
MANIFEST_PREFIXES = ("next.config.", "vite.config.", "nuxt.config.", "svelte.config.")
SCHEMA_EXTENSIONS = {".prisma", ".graphql", ".gql", ".proto", ".graphqls"}
SCHEMA_FILENAMES = {"schema.sql"}
STYLE_TEMPLATE_EXTENSIONS = {
    ".css", ".scss", ".sass", ".less", ".html", ".hbs", ".handlebars", ".ejs", ".pug",
    ".jinja", ".j2", ".twig", ".liquid", ".njk",
}
CONFIG_DATA_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".properties",
}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".tex"}
# Everything else that is indexable by extension is source code (including
# .vue/.svelte/.astro components and infrastructure languages such as .tf).
CODE_EXTENSIONS = (
    TEXT_EXTENSIONS - SCHEMA_EXTENSIONS - STYLE_TEMPLATE_EXTENSIONS - CONFIG_DATA_EXTENSIONS - DOC_EXTENSIONS
)
# Extensionless build/runtime definitions behave like code when nested.
BUILD_SCRIPT_FILENAMES = {
    "dockerfile", "containerfile", "makefile", "gnumakefile", "rakefile", "justfile",
    "procfile", "vagrantfile", "brewfile", "caddyfile", "gemfile",
}
DOC_FILENAMES = {"license", "changelog", "contributing"}
CI_FILENAMES = {"jenkinsfile", ".gitlab-ci.yml"}
DOCS_DIRECTORIES = {"docs", "documentation"}
TEST_DIRECTORIES = {"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress", "playwright"}
TEST_FILE_PATTERNS = ("*.test.*", "*.spec.*", "test_*.py", "*_test.py", "*_test.go", "conftest.py")
FIXTURE_DIRECTORIES = {
    "fixtures", "__fixtures__", "mocks", "__mocks__", "testdata", "examples", "example",
    "samples", "sample", "demo", "demos", "stories", "storybook",
}
FIXTURE_FILE_PATTERNS = ("*.stories.*", "*.mock.*", "*.mocks.*")
GENERATED_DIRECTORIES = {"generated", "__generated__", "gen"}
GENERATED_FILE_PATTERNS = (
    "*.gen.*", "*.generated.*", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.d.ts",
)
MIGRATION_DIRECTORIES = {"migrations", "migration"}

PRIORITY_ROOT_MANIFEST = 95.0
PRIORITY_ROOT_README = 92.0
PRIORITY_PACKAGE_MANIFEST = 88.0
PRIORITY_SCHEMA = 85.0
PRIORITY_CODE = 80.0
PRIORITY_CI = 65.0
PRIORITY_STYLE_TEMPLATE = 60.0
PRIORITY_NESTED_README = 55.0
PRIORITY_CONFIG_DATA = 50.0
PRIORITY_DOCS = 45.0
PRIORITY_DOCS_DIRECTORY = 40.0
PENALTY_TEST = 20.0
PENALTY_FIXTURE = 30.0
PENALTY_MIGRATION_SQL = 25.0
# Schema-migration history in any language (Django/Alembic .py, Knex .ts) is
# rarely what a question is about; it is kept only after the live code.
PENALTY_MIGRATION = 25.0
PENALTY_GENERATED = 30.0
PENALTY_LOCALE_CATALOGUE = 20.0
PENALTY_LARGE_FILE = 15.0
LARGE_FILE_BYTES = 200 * 1024
HUGE_FILE_BYTES = 1024 * 1024
MAX_DEPTH_PENALTY = 4
# Depth only breaks ties inside a band: half a point per level below the
# third, at most MAX_DEPTH_PENALTY. A whole point per level used to decide
# everything, so a budgeted monorepo never indexed anything at depth >= 5.
DEPTH_PENALTY_PER_LEVEL = 0.5
# Priorities within the same 5-point band are interleaved across directories
# (see _selection_order) so every package gets its share of a budget.
PRIORITY_BAND_WIDTH = 5.0
FAIR_SHARE_DIRECTORY_DEPTH = 3
MIN_PRIORITY = 1.0
MAX_PRIORITY = 100.0

# Matches the chunker's measured output: small files stay a single chunk,
# empty files produce none, and larger files average about 4.2 KB of source
# per chunk (PostHog measured 4,274 B). Estimates are fractional and summed,
# not rounded up per file, which overestimated real repositories by ~12%.
SINGLE_CHUNK_MAX_BYTES = 6_000
AVERAGE_CHUNK_BYTES = 4_200
# The worker stops cleanly at its hard chunk ceiling, so only a small
# headroom is kept for estimation error.
CHUNK_BUDGET_HEADROOM = 0.98
# Most important budget-omitted files kept (with their category) so users
# can see what the budget dropped.
BUDGET_SAMPLE_LIMIT = 200

# ---------------------------------------------------------------------------
# Archive acquisition
# ---------------------------------------------------------------------------

ARCHIVE_USER_AGENT = "codebase-intel"
ARCHIVE_SOCKET_TIMEOUT_SECONDS = 120
COPY_BLOCK_BYTES = 1024 * 1024
_UNREAD_SIZE_BLOCK_BYTES = 1024 * 1024
_SAFE_COMMIT_SHA = re.compile(r"[0-9A-Fa-f]{6,64}")
# Per-entry write failures (an over-long name, a path that collides with an
# existing file) skip that one entry instead of discarding the whole archive.
_PER_ENTRY_WRITE_ERRNOS = {
    errno.ENAMETOOLONG, errno.ENOTDIR, errno.EEXIST, errno.EISDIR, errno.EILSEQ,
}
NOT_FOUND_MESSAGE = "Repository not found. Check the URL and make sure the repository is public."
_DOWNLOAD_LIMIT_MESSAGE = "Repository archive exceeds the download limit."
_DOWNLOAD_TIMEOUT_MESSAGE = "Repository download took too long. Try again later."
_DOWNLOAD_UNREACHABLE_MESSAGE = "Could not download the repository archive from GitHub."
_DOWNLOAD_INTERRUPTED_MESSAGE = "The repository archive download was interrupted."
_ARCHIVE_UNREADABLE_MESSAGE = "The repository archive could not be read."
_GIT_UNAVAILABLE_MESSAGE = "Git is not available on this server, so the repository could not be cloned."


class RepositoryValidationError(ValueError):
    pass


class RepositoryCloneError(RuntimeError):
    """Source acquisition failed.

    ``not_found`` separates a definitive "this repository does not exist or is
    private" answer from transient failures, so callers only retry (or fall
    back to git) when another attempt could succeed.
    """

    def __init__(self, message: str, *, not_found: bool = False):
        super().__init__(message)
        self.not_found = not_found


class SourceFetchCancelled(RuntimeError):
    """The ingestion job was stopped while its source was being downloaded."""


@dataclass
class RepositorySnapshot:
    """A local copy of a repository plus what was skipped while obtaining it.

    The archive path applies path- and size-based policy before writing to
    disk, so those exclusions never reach the directory walk. They travel here
    and are merged into the coverage report by ``get_file_selection_report``.
    """

    path: str
    commit_sha: str | None
    source: str
    total_seen_files: int = 0
    pre_excluded_reasons: dict[str, int] = field(default_factory=dict)
    pre_excluded_paths: list[str] = field(default_factory=list)
    pre_excluded_bytes: int = 0
    downloaded_bytes: int = 0
    pre_budget_categories: dict[str, int] = field(default_factory=dict)
    pre_budget_sample: list[dict] = field(default_factory=list)
    # The branch/tag/commit fetched when the URL named one (None = default).
    ref: str | None = None


# ---------------------------------------------------------------------------
# GitHub URL parsing
# ---------------------------------------------------------------------------


def parse_github_repository(value: str) -> tuple[str, str]:
    """Return ``(owner, repository)`` for any accepted GitHub repository form.

    Accepted: https/http URLs, scheme-less ``github.com/o/r`` and
    ``www.github.com/o/r``, ``git@github.com:o/r(.git)``, shorthand ``o/r``,
    with optional trailing slash, ``.git`` suffix, query string, fragment, or a
    trailing in-repository route such as ``/tree/main/src``. Case is preserved.
    A branch, tag or commit in the route is read by ``parse_github_ref``.
    """
    return _split_repository_path(_repository_path(value))


def _repository_path(value: str) -> str:
    if not isinstance(value, str):
        raise RepositoryValidationError(_URL_SHAPE_MESSAGE)
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_GITHUB_URL_LENGTH:
        raise RepositoryValidationError(_URL_SHAPE_MESSAGE)
    # urlsplit silently deletes tabs/newlines, which would let
    # "oc\ntocat" pass as "octocat". Reject instead of guessing.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F or char == "\\" for char in candidate):
        raise RepositoryValidationError(_URL_CHARACTER_MESSAGE)
    if candidate.startswith("git@"):
        path = _scp_repository_path(candidate)
    elif _SCHEME_PATTERN.match(candidate):
        path = _url_repository_path(candidate)
    else:
        without_query = _QUERY_OR_FRAGMENT.split(candidate, maxsplit=1)[0]
        first_segment = without_query.split("/", 1)[0]
        # Owner names never contain ".", ":" or "@", so such a first segment
        # is a host (possibly a look-alike) and must pass host validation.
        if any(char in first_segment for char in ".:@"):
            path = _url_repository_path(f"https://{candidate}")
        else:
            path = without_query
    return path


def _scp_repository_path(candidate: str) -> str:
    host, separator, path = candidate[len("git@"):].partition(":")
    if not separator:
        raise RepositoryValidationError(_URL_SHAPE_MESSAGE)
    if host.lower() not in GITHUB_HOSTS:
        raise RepositoryValidationError(_URL_HOST_MESSAGE)
    return _QUERY_OR_FRAGMENT.split(path, maxsplit=1)[0]


def _url_repository_path(candidate: str) -> str:
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
    except ValueError as error:
        raise RepositoryValidationError(_URL_SHAPE_MESSAGE) from error
    if parsed.scheme not in {"https", "http"}:
        raise RepositoryValidationError(_URL_SCHEME_MESSAGE)
    # Checked on the raw netloc: an empty username ("https://@github.com")
    # parses as falsy but is still userinfo.
    if "@" in parsed.netloc:
        raise RepositoryValidationError(_URL_CREDENTIALS_MESSAGE)
    if not hostname or hostname not in GITHUB_HOSTS:
        raise RepositoryValidationError(_URL_HOST_MESSAGE)
    if ":" in parsed.netloc:
        raise RepositoryValidationError(_URL_PORT_MESSAGE)
    return parsed.path


def _split_repository_path(path: str) -> tuple[str, str]:
    segments = path.strip("/").split("/")
    if len(segments) < 2 or not segments[0] or not segments[1]:
        raise RepositoryValidationError(_URL_SHAPE_MESSAGE)
    owner, repository = segments[0], segments[1]
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not OWNER_PATTERN.fullmatch(owner):
        raise RepositoryValidationError(_URL_OWNER_MESSAGE)
    if owner.lower() in RESERVED_OWNER_NAMES:
        raise RepositoryValidationError(_URL_RESERVED_MESSAGE)
    if repository in {".", ".."} or not REPOSITORY_PATTERN.fullmatch(repository):
        raise RepositoryValidationError(_URL_REPOSITORY_MESSAGE)
    if len(segments) > 2 and segments[2] not in GITHUB_ROUTE_SEGMENTS:
        raise RepositoryValidationError(_URL_ROUTE_MESSAGE)
    return owner, repository


# Characters git refuses in ref names (git check-ref-format), plus "%" so a
# ref can never smuggle an encoded path segment into the archive URL.
_UNSAFE_REF_CHARACTERS = set("~^:?*[\\%") | {chr(code) for code in range(0x20)} | {" ", "\x7f"}
_COMMIT_SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{7,40}")
_REF_ROUTE_KINDS = {"tree": "tree", "blob": "blob", "commit": "commit", "commits": "tree"}
MAX_REF_SEGMENTS = 12
_URL_REF_MESSAGE = "The branch, tag or commit in this GitHub URL is not valid."


@dataclass(frozen=True)
class RepositoryRef:
    """A branch, tag or commit named by a ``/tree/``, ``/blob/`` or ``/commit/`` URL.

    Branch names may contain slashes and ``/tree/<ref>/<dir>`` URLs append a
    directory, so the exact split is unknown until GitHub is asked;
    ``candidates`` lists the possible refs longest first.
    """

    kind: str
    segments: tuple[str, ...]

    @property
    def display(self) -> str:
        return "/".join(self.segments)

    def candidates(self) -> list[str]:
        if self.kind == "commit":
            return [self.segments[0]]
        longest = len(self.segments) - 1 if self.kind == "blob" and len(self.segments) > 1 else len(self.segments)
        return ["/".join(self.segments[:count]) for count in range(longest, 0, -1)]


def parse_github_ref(value: str) -> RepositoryRef | None:
    """Return the branch/tag/commit a repository URL points at, if any."""
    path = _repository_path(value)
    _split_repository_path(path)
    segments = path.strip("/").split("/")
    if len(segments) < 4:
        return None
    kind = _REF_ROUTE_KINDS.get(segments[2])
    if kind is None:
        return None
    rest = [unquote(segment) for segment in segments[3:] if segment]
    if not rest:
        return None
    if kind == "commit":
        if not _COMMIT_SHA_PATTERN.fullmatch(rest[0]):
            raise RepositoryValidationError(_URL_REF_MESSAGE)
        return RepositoryRef("commit", (rest[0],))
    rest = rest[:MAX_REF_SEGMENTS]
    for segment in rest:
        if (
            segment in {".", ".."} or segment.endswith(".lock") or segment.startswith("-")
            or len(segment) > 200 or any(character in _UNSAFE_REF_CHARACTERS for character in segment)
        ):
            raise RepositoryValidationError(_URL_REF_MESSAGE)
    return RepositoryRef(kind, tuple(rest))


def repository_source_url(github_url: str, ref: RepositoryRef | None = None) -> str:
    """Canonical repository URL that also carries the requested ref.

    Durable jobs store this form so the worker fetches the same branch or
    commit the user linked; ``normalize_github_url`` still maps it back to
    the repository itself.
    """
    owner, repository = parse_github_repository(github_url)
    if ref is None:
        ref = parse_github_ref(github_url)
    if ref is None:
        return f"https://github.com/{owner}/{repository}.git"
    route = "/".join(quote(segment, safe="") for segment in ref.segments)
    return f"https://github.com/{owner}/{repository}/{ref.kind}/{route}"


def normalize_github_url(github_url: str) -> str:
    """Return the canonical clone URL ``https://github.com/{owner}/{repo}.git``."""
    owner, repository = parse_github_repository(github_url)
    return f"https://github.com/{owner}/{repository}.git"


def repository_name(github_url: str) -> str:
    return parse_github_repository(github_url)[1]


# ---------------------------------------------------------------------------
# File policy
# ---------------------------------------------------------------------------


def language_tag_code(value: str) -> str | None:
    """Return the lowercase ISO 639-1 language of a locale tag, else None."""
    match = LANGUAGE_TAG_PATTERN.fullmatch(value)
    if not match:
        return None
    code = match.group("language").lower()
    return code if code in ISO_639_1_CODES else None


def locale_catalogue_language(rel_path: str) -> str | None:
    """Return the language of a locale catalogue file, or None for other files.

    Only tags after the first locale directory count, so an unrelated folder
    that happens to be a language code ("ts", "ml") higher up the tree cannot
    turn ordinary files into catalogues. A non-English tag wins over English
    so a mixed path is never kept by accident.
    """
    parts = rel_path.split("/")
    directories = [part.lower() for part in parts[:-1]]
    locale_index = next(
        (index for index, name in enumerate(directories) if name in LOCALE_DIRECTORY_NAMES), None
    )
    if locale_index is None:
        return None
    stem, extension = posixpath.splitext(parts[-1])
    extension = extension.lower()
    stem_language = language_tag_code(stem)
    if extension in LOCALE_SCRIPT_EXTENSIONS:
        languages = [stem_language] if stem_language else []
    elif extension in LOCALE_CATALOGUE_EXTENSIONS:
        languages = [language_tag_code(name) for name in directories[locale_index + 1:]]
        languages.append(stem_language)
        languages = [language for language in languages if language]
    else:
        return None
    if not languages:
        return None
    non_english = [language for language in languages if language != ENGLISH_LANGUAGE_CODE]
    return non_english[0] if non_english else ENGLISH_LANGUAGE_CODE


def is_translation_catalogue(rel_path: str) -> bool:
    """True for non-English locale catalogues (English ones are always indexed)."""
    language = locale_catalogue_language(rel_path)
    return language is not None and language != ENGLISH_LANGUAGE_CODE


def is_hidden_excluded_file(file_name: str) -> bool:
    return file_name.startswith(".") and file_name.lower() not in ALLOWED_HIDDEN_FILES


def is_pruned_directory(directory_name: str) -> bool:
    """Directories whose contents are never walked, extracted, or counted."""
    if directory_name in IGNORE_DIRS:
        return True
    return directory_name.startswith(".") and directory_name.lower() not in ALLOWED_HIDDEN_DIRS


def is_supported_text_name(file_name: str) -> bool:
    lower_name = file_name.lower()
    if lower_name in ALLOWED_HIDDEN_FILES or lower_name in TEXT_FILENAMES:
        return True
    if lower_name.startswith("dockerfile."):
        return True
    return posixpath.splitext(lower_name)[1] in TEXT_EXTENSIONS


def path_exclusion_reason(rel_path: str, size: int | None, *, is_symlink: bool = False) -> str | None:
    """Return why a file is not indexed using only its path, type, and size.

    The archive extractor and the directory walk share this so a file is
    judged identically whichever way the source arrived. Reasons are stable
    identifiers persisted in the coverage report and shown to users; the
    order decides which reason a file reports when several apply. ``size`` is
    None when the file could not be inspected as a regular file.
    """
    file_name = posixpath.basename(rel_path)
    lower_name = file_name.lower()
    if is_hidden_excluded_file(file_name):
        return "hidden"
    if is_symlink:
        return "symlink"
    if lower_name in SKIP_FILENAMES:
        return "lockfile"
    if ".min." in lower_name:
        return "minified"
    if lower_name.endswith(BUILD_ARTIFACT_SUFFIXES):
        return "build_artifact"
    if not settings.index_translations and is_translation_catalogue(rel_path):
        return "translations"
    if size is None:
        return "unreadable"
    if size > settings.max_file_size_bytes:
        return "file_size_limit"
    if not is_supported_text_name(file_name):
        return "unsupported_format"
    return None


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _is_manifest(lower_name: str) -> bool:
    return lower_name in MANIFEST_FILENAMES or lower_name.startswith(MANIFEST_PREFIXES)


def _is_ci_file(parts: list[str], lower_name: str) -> bool:
    lowered = [part.lower() for part in parts]
    if lowered[:2] == [".github", "workflows"] and len(lowered) > 2:
        return True
    if lowered[0] == ".circleci" and len(lowered) > 1:
        return True
    return lower_name in CI_FILENAMES


def _base_priority(parts: list[str], lower_name: str, directories: set[str]) -> float:
    """Pick the first matching band; earlier bands are more specific."""
    depth = len(parts)
    extension = posixpath.splitext(lower_name)[1]
    stem = posixpath.splitext(lower_name)[0]
    if _is_manifest(lower_name) and depth <= 3:
        return PRIORITY_ROOT_MANIFEST if depth <= 1 else PRIORITY_PACKAGE_MANIFEST
    # Only README documents: a script that happens to be called readme.py is
    # ranked as the code it is.
    if stem == "readme" and (not extension or extension in DOC_EXTENSIONS):
        return PRIORITY_ROOT_README if depth <= 1 else PRIORITY_NESTED_README
    if _is_ci_file(parts, lower_name):
        return PRIORITY_CI
    if extension in SCHEMA_EXTENSIONS or lower_name in SCHEMA_FILENAMES:
        return PRIORITY_SCHEMA
    if extension in CODE_EXTENSIONS or lower_name in BUILD_SCRIPT_FILENAMES or lower_name.startswith("dockerfile."):
        return PRIORITY_CODE
    if extension in STYLE_TEMPLATE_EXTENSIONS:
        return PRIORITY_STYLE_TEMPLATE
    if extension in DOC_EXTENSIONS or lower_name in DOC_FILENAMES:
        return PRIORITY_DOCS_DIRECTORY if directories & DOCS_DIRECTORIES else PRIORITY_DOCS
    # Config data, and the remaining small supported files (CODEOWNERS,
    # .env.example templates, editorconfig) that describe configuration.
    return PRIORITY_CONFIG_DATA


def file_priority(rel_path: str, size: int) -> float:
    """Rank a file's value to an index on a deterministic 1-100 scale.

    Bands (first match wins; depth = number of path components):

    * 95 manifest/config at the root (package.json, pyproject.toml, go.mod,
      Dockerfile, docker-compose.yml, tsconfig.json, next.config.*, ...);
      88 for the same files at depth 2-3 (monorepo packages)
    * 92 root README; 55 nested README (README with no extension or a docs
      extension; readme.py is code)
    * 65 CI (.github/workflows/*, .circleci/*, .gitlab-ci.yml, Jenkinsfile)
    * 85 schemas (.prisma, .graphql, .gql, .proto, .graphqls, schema.sql)
    * 80 source code (including .vue/.svelte/.astro and nested Dockerfiles)
    * 60 styles and templates (.css, .scss, .html, .hbs, .jinja, ...)
    * 50 config data (.json, .yaml, .toml, .xml, .properties, ...)
    * 45 docs (.md, .mdx, .rst, .txt, .tex, LICENSE); 40 under docs/

    Cumulative adjustments: tests (including conftest.py) -20,
    fixtures/mocks/examples/stories -30, anything under migrations/ -25,
    generated code -30, locale catalogues -20, larger than 200 KB -15 and
    larger than 1 MB another -15, and -0.5 per level below depth 3 (at most
    -4) for deep paths. The result is clamped to [1, 100].
    Locale catalogues are penalised whatever their language so that, with
    translations enabled, foreign catalogues never outrank English ones.
    """
    parts = rel_path.split("/")
    lower_name = parts[-1].lower()
    directories = {part.lower() for part in parts[:-1]}
    priority = _base_priority(parts, lower_name, directories)
    if directories & TEST_DIRECTORIES or _matches_any(lower_name, TEST_FILE_PATTERNS):
        priority -= PENALTY_TEST
    if directories & FIXTURE_DIRECTORIES or _matches_any(lower_name, FIXTURE_FILE_PATTERNS):
        priority -= PENALTY_FIXTURE
    if directories & MIGRATION_DIRECTORIES:
        priority -= PENALTY_MIGRATION
    if directories & GENERATED_DIRECTORIES or _matches_any(lower_name, GENERATED_FILE_PATTERNS):
        priority -= PENALTY_GENERATED
    if locale_catalogue_language(rel_path) is not None:
        priority -= PENALTY_LOCALE_CATALOGUE
    if size > LARGE_FILE_BYTES:
        priority -= PENALTY_LARGE_FILE
    if size > HUGE_FILE_BYTES:
        priority -= PENALTY_LARGE_FILE
    priority -= min(MAX_DEPTH_PENALTY, DEPTH_PENALTY_PER_LEVEL * max(0, len(parts) - 3))
    return float(min(MAX_PRIORITY, max(MIN_PRIORITY, priority)))


def file_category(rel_path: str) -> str:
    """Plain-language kind of file, used to explain what a budget skipped."""
    parts = rel_path.split("/")
    lower_name = parts[-1].lower()
    directories = {part.lower() for part in parts[:-1]}
    extension = posixpath.splitext(lower_name)[1]
    if directories & TEST_DIRECTORIES or _matches_any(lower_name, TEST_FILE_PATTERNS):
        return "tests"
    if directories & FIXTURE_DIRECTORIES or _matches_any(lower_name, FIXTURE_FILE_PATTERNS):
        return "fixtures"
    if directories & MIGRATION_DIRECTORIES:
        return "migrations"
    if directories & GENERATED_DIRECTORIES or _matches_any(lower_name, GENERATED_FILE_PATTERNS):
        return "generated"
    if locale_catalogue_language(rel_path) is not None:
        return "translations"
    if extension in DOC_EXTENSIONS or lower_name in DOC_FILENAMES or (
        posixpath.splitext(lower_name)[0] == "readme" and not extension
    ):
        return "docs"
    if extension in STYLE_TEMPLATE_EXTENSIONS:
        return "styles"
    if (
        extension in CODE_EXTENSIONS or extension in SCHEMA_EXTENSIONS or lower_name in BUILD_SCRIPT_FILENAMES
        or lower_name.startswith("dockerfile.")
    ):
        return "source"
    return "config"


BUDGET_CATEGORY_LABELS = {
    "source": "source", "tests": "tests", "fixtures": "fixtures and examples", "migrations": "migrations",
    "generated": "generated code", "translations": "translations", "docs": "docs", "styles": "styles",
    "config": "config and data",
}


def estimate_chunks(size: int) -> float:
    """Expected chunk count of one file (fractional; sum before rounding)."""
    if size <= 0:
        return 0.0
    if size <= SINGLE_CHUNK_MAX_BYTES:
        return 1.0
    return max(1.0, size / AVERAGE_CHUNK_BYTES)


# ---------------------------------------------------------------------------
# Selection report
# ---------------------------------------------------------------------------


class _ExclusionLedger:
    """Exact exclusion counts plus a bounded, sorted sample of paths.

    Huge repositories can skip hundreds of thousands of files; keeping every
    path would cost memory on a 512 MB worker, so only the lexicographically
    first ``excluded_paths_limit`` survive (counts always stay exact).
    """

    def __init__(self, path_limit: int):
        self.reasons: Counter[str] = Counter()
        self.bytes = 0
        self._path_limit = max(0, int(path_limit))
        self._paths: list[str] = []
        # What the importance budget dropped: exact counts per category plus
        # the most important omissions (a min-heap whose root is the least
        # important sampled path).
        self.budget_categories: Counter[str] = Counter()
        self._budget_heap: list[_KeptFile] = []
        self._budget_category_of: dict[str, str] = {}

    def record(self, reason: str, path: str, size: int = 0) -> None:
        self.reasons[reason] += 1
        self.bytes += max(0, int(size))
        self._add_path(path)
        if reason == "repository_budget":
            category = file_category(path)
            self.budget_categories[category] += 1
            self._sample_budget(path, file_priority(path, size), category)

    def _sample_budget(self, path: str, priority: float, category: str) -> None:
        if path in self._budget_category_of:
            return
        entry = _KeptFile(priority, path, 0)
        if len(self._budget_heap) < BUDGET_SAMPLE_LIMIT:
            heapq.heappush(self._budget_heap, entry)
        elif self._budget_heap[0] < entry:
            removed = heapq.heapreplace(self._budget_heap, entry)
            self._budget_category_of.pop(removed.rel_path, None)
        else:
            return
        self._budget_category_of[path] = category

    def merge_snapshot(self, snapshot: RepositorySnapshot) -> None:
        for reason, count in snapshot.pre_excluded_reasons.items():
            self.reasons[reason] += int(count)
        self.bytes += max(0, int(snapshot.pre_excluded_bytes))
        for path in snapshot.pre_excluded_paths:
            self._add_path(path)
        for category, count in snapshot.pre_budget_categories.items():
            self.budget_categories[category] += int(count)
        for item in snapshot.pre_budget_sample:
            path = str(item.get("path") or "")
            if path:
                self._sample_budget(path, float(item.get("priority") or 0), str(item.get("category") or "config"))

    def budget_sample(self) -> list[dict]:
        """Most important budget omissions first."""
        ordered = sorted(self._budget_heap, reverse=True)
        return [
            {"path": entry.rel_path, "priority": entry.priority, "category": self._budget_category_of.get(entry.rel_path, "config")}
            for entry in ordered
        ]

    def budget_counts(self) -> dict[str, int]:
        return {category: count for category, count in sorted(self.budget_categories.items()) if count > 0}

    def _add_path(self, path: str) -> None:
        self._paths.append(path)
        # Compact lazily so the cost stays O(n log limit) overall.
        if len(self._paths) > 2 * self._path_limit + 1_024:
            self._paths.sort()
            del self._paths[self._path_limit:]

    def counts(self) -> dict[str, int]:
        return {reason: count for reason, count in sorted(self.reasons.items()) if count > 0}

    def sorted_paths(self) -> list[str]:
        return sorted(self._paths)[: self._path_limit]


@dataclass(frozen=True)
class _Candidate:
    priority: float
    rel_path: str
    abs_path: str
    size: int
    chunks: int


def _local_file_reason(abs_path: str, rel_path: str) -> tuple[str | None, int]:
    """Classify a file on disk; returns ``(reason, size)``."""
    try:
        info = os.lstat(abs_path)
    except OSError:
        info = None
    is_symlink = info is not None and stat.S_ISLNK(info.st_mode)
    # FIFOs and devices report no usable size and must never be opened: a
    # read from a FIFO blocks forever.
    size = info.st_size if info is not None and stat.S_ISREG(info.st_mode) else None
    reason = path_exclusion_reason(rel_path, size, is_symlink=is_symlink)
    if reason is None and not is_probably_text_file(abs_path):
        reason = "binary_or_invalid_text"
    return reason, size or 0


def _priority_band(priority: float) -> int:
    return math.ceil(priority / PRIORITY_BAND_WIDTH - 1e-9)


def _fair_share_group(rel_path: str) -> str:
    return "/".join(rel_path.split("/")[:-1][:FAIR_SHARE_DIRECTORY_DEPTH])


def _selection_order(candidates: list[_Candidate]) -> list[_Candidate]:
    """Most important first, sharing each priority band fairly across directories.

    A strict ``(-priority, path)`` sort let one large directory (or simply the
    alphabetically first ones) take a whole budget. Within one 5-point band,
    files are instead taken round-robin across their directories (up to three
    levels deep), each directory contributing its own best file first, so a
    budgeted monorepo covers every package's core code.
    """
    ordered = sorted(candidates, key=lambda item: (-item.priority, item.rel_path))
    rank_in_group: dict[tuple[int, str], int] = {}
    keyed = []
    for candidate in ordered:
        band = _priority_band(candidate.priority)
        group_key = (band, _fair_share_group(candidate.rel_path))
        rank = rank_in_group.get(group_key, 0)
        rank_in_group[group_key] = rank + 1
        keyed.append(((-band, rank, -candidate.priority, candidate.rel_path), candidate))
    keyed.sort(key=lambda pair: pair[0])
    return [candidate for _key, candidate in keyed]


def _select_within_budget(
    candidates: list[_Candidate],
    ledger: _ExclusionLedger,
    max_files: int,
    max_bytes: int,
    max_chunks: int,
) -> tuple[list[_Candidate], int, int]:
    """Greedily keep the most important files that fit; never raises.

    A file that does not fit the byte or chunk budget is skipped but the scan
    continues, because a smaller, lower-priority file may still fit. Once the
    file-count budget is full nothing else can fit, so the rest are omitted.
    """
    selected: list[_Candidate] = []
    selected_bytes = 0
    selected_chunks = 0.0
    for candidate in _selection_order(candidates):
        if (
            len(selected) >= max_files
            or selected_bytes + candidate.size > max_bytes
            or selected_chunks + candidate.chunks > max_chunks
        ):
            ledger.record("repository_budget", candidate.rel_path, candidate.size)
            continue
        selected.append(candidate)
        selected_bytes += candidate.size
        selected_chunks += candidate.chunks
    return selected, selected_bytes, selected_chunks


def get_file_selection_report(
    repo_path: str,
    *,
    pre_excluded: RepositorySnapshot | None = None,
    byte_budget: int | None = None,
    chunk_budget: int | None = None,
) -> dict:
    """Return the importance-ranked file selection plus an auditable report.

    ``total_seen_files`` counts files outside pruned directories; every one
    is either selected or excluded with a reason. Repository limits are
    budgets: when a repository is too large the highest-priority files are
    kept and the rest are reported as ``repository_budget`` instead of failing
    the ingestion. ``files`` is in selection (priority) order.

    A missing or zero ``byte_budget``/``chunk_budget`` means "no extra cap":
    both come from optional quota headroom, and a zero must not silently
    produce an empty index (the quota check reports exhaustion clearly).
    """
    ledger = _ExclusionLedger(settings.excluded_paths_limit)
    candidates: list[_Candidate] = []
    total_seen = 0
    # ``files`` is documented as absolute; callers chunk and read these paths
    # later, after the working directory may no longer be the one they assumed.
    repo_path = os.path.abspath(repo_path)
    for root, dirs, files in os.walk(repo_path, followlinks=False):
        # Pruned directories are not counted as seen: they are dependency,
        # build, or tool output, not repository content.
        dirs[:] = sorted(
            directory for directory in dirs
            if not is_pruned_directory(directory) and not os.path.islink(os.path.join(root, directory))
        )
        for file_name in sorted(files):
            total_seen += 1
            abs_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(abs_path, repo_path).replace(os.sep, "/")
            reason, size = _local_file_reason(abs_path, rel_path)
            if reason:
                ledger.record(reason, rel_path, size)
                continue
            candidates.append(_Candidate(
                priority=file_priority(rel_path, size),
                rel_path=rel_path,
                abs_path=abs_path,
                size=size,
                chunks=estimate_chunks(size),
            ))

    max_files = settings.max_repository_files
    max_bytes = min(settings.max_repository_bytes, byte_budget or math.inf)
    max_chunks = chunk_budget or int(settings.max_repository_chunks * CHUNK_BUDGET_HEADROOM)
    selected, selected_bytes, selected_chunks = _select_within_budget(
        candidates, ledger, max_files, max_bytes, max_chunks
    )

    if pre_excluded is not None:
        ledger.merge_snapshot(pre_excluded)
        # The archive already counted every entry it saw, including the ones
        # it never extracted, so its count replaces the walk's. A git snapshot
        # counts nothing (0) because the walk is its only scan.
        if int(pre_excluded.total_seen_files) > 0:
            total_seen = int(pre_excluded.total_seen_files)

    reasons = ledger.counts()
    excluded_files = sum(reasons.values())
    excluded_paths = ledger.sorted_paths()
    budget_omitted = reasons.get("repository_budget", 0)
    return {
        "files": [candidate.abs_path for candidate in selected],
        "total_seen_files": total_seen,
        "eligible_files": len(selected),
        "candidate_files": len(selected) + budget_omitted,
        "excluded_files": excluded_files,
        "excluded_bytes": ledger.bytes,
        "excluded_reasons": reasons,
        "excluded_paths": excluded_paths,
        "excluded_paths_truncated": len(excluded_paths) < excluded_files,
        "eligible_bytes": selected_bytes,
        "estimated_chunks": math.ceil(selected_chunks - 1e-9),
        "budget_omitted_files": budget_omitted,
        "budget_omitted_by_category": ledger.budget_counts(),
        "budget_omitted_sample": ledger.budget_sample(),
        "selection_budget": {
            "max_files": int(max_files),
            "max_bytes": int(max_bytes),
            "max_chunks": int(max_chunks),
        },
    }


def get_files_to_process(repo_path: str) -> list[str]:
    """Backward-compatible file list API used by callers and integrations."""
    return get_file_selection_report(repo_path)["files"]


# ---------------------------------------------------------------------------
# Archive acquisition
# ---------------------------------------------------------------------------


class _ArchiveStreamReader:
    """File-like wrapper that bounds an archive download in bytes and time.

    The response is consumed as a stream by tarfile, so these checks run
    while extraction progresses and stop a runaway download early.
    """

    def __init__(self, stream, max_bytes: int, deadline: float, should_stop=None):
        self._stream = stream
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._should_stop = should_stop
        self.bytes_read = 0

    def _check_deadline(self) -> None:
        if self._should_stop is not None and self._should_stop():
            raise SourceFetchCancelled("Indexing was stopped while the repository was downloading.")
        if time.monotonic() >= self._deadline:
            raise RepositoryCloneError(_DOWNLOAD_TIMEOUT_MESSAGE)

    def read(self, size: int = -1) -> bytes:
        self._check_deadline()
        # Never let an unbounded read pull the whole response into memory
        # before the download limit can be enforced.
        if size is None or size < 0:
            size = _UNREAD_SIZE_BLOCK_BYTES
        try:
            data = self._stream.read(size)
        except (OSError, http.client.HTTPException) as error:
            # Socket timeouts, resets, and truncated chunked bodies
            # (IncompleteRead) are translated here, at the network boundary,
            # so a local disk error during extraction (such as ENOSPC) is
            # never mistaken for a transient download failure.
            raise RepositoryCloneError(_DOWNLOAD_INTERRUPTED_MESSAGE) from error
        self.bytes_read += len(data)
        if self.bytes_read > self._max_bytes:
            raise RepositoryValidationError(_DOWNLOAD_LIMIT_MESSAGE)
        self._check_deadline()
        return data


class _KeptFile:
    """Heap entry for an extracted file; the heap root is the least valuable.

    Ordered by priority, then by *reverse* path so that among equal
    priorities the file that sorts last is evicted first. That mirrors the
    selection order ``(-priority, rel_path)`` used by the report.
    """

    __slots__ = ("priority", "rel_path", "size")

    def __init__(self, priority: float, rel_path: str, size: int):
        self.priority = priority
        self.rel_path = rel_path
        self.size = size

    def __lt__(self, other: "_KeptFile") -> bool:
        return (self.priority, other.rel_path) < (other.priority, self.rel_path)


class _ExtractionBudget:
    """Keep the highest-priority archive files within the repository budget.

    Archive order is arbitrary, so a min-heap of kept files lets a later,
    more important file displace less important ones that were extracted
    earlier. Equal priorities never displace each other.
    """

    def __init__(self, destination: str, ledger: _ExclusionLedger):
        self.destination = destination
        self.ledger = ledger
        self.max_files = settings.max_repository_files
        self.max_bytes = settings.max_repository_bytes
        self.kept_bytes = 0
        self._heap: list[_KeptFile] = []
        self._kept_paths: set[str] = set()

    def offer(self, archive: tarfile.TarFile, member: tarfile.TarInfo, rel_path: str) -> None:
        size = int(member.size)
        if rel_path in self._kept_paths:
            # Real archives never repeat a path; a repeat is an attempt to
            # overwrite an already accepted file.
            self.ledger.record("unsafe_path", rel_path, size)
            return
        entry = _KeptFile(file_priority(rel_path, size), rel_path, size)
        evicted = self._make_room(entry)
        if evicted is None:
            self.ledger.record("repository_budget", rel_path, size)
            return
        try:
            self._write(archive, member, rel_path)
        except OSError as error:
            if error.errno not in _PER_ENTRY_WRITE_ERRNOS:
                raise
            for kept in evicted:
                heapq.heappush(self._heap, kept)
            self.ledger.record("unsafe_path", rel_path, size)
            return
        # Evict only after the new file is safely on disk, so a failed write
        # never costs the files it would have displaced.
        for kept in evicted:
            self._remove(kept.rel_path)
            self._kept_paths.discard(kept.rel_path)
            self.kept_bytes -= kept.size
            self.ledger.record("repository_budget", kept.rel_path, kept.size)
        heapq.heappush(self._heap, entry)
        self._kept_paths.add(rel_path)
        self.kept_bytes += size

    def _make_room(self, entry: _KeptFile) -> list[_KeptFile] | None:
        """Pop strictly lower-priority files until ``entry`` fits.

        Returns the files to evict, or None (with the heap restored) when the
        entry cannot fit without displacing equal or higher priorities.
        """
        if entry.size > self.max_bytes or self.max_files <= 0:
            return None
        evicted: list[_KeptFile] = []
        count = len(self._heap)
        total = self.kept_bytes
        while count + 1 > self.max_files or total + entry.size > self.max_bytes:
            if not self._heap or not self._heap[0].priority < entry.priority:
                for kept in evicted:
                    heapq.heappush(self._heap, kept)
                return None
            kept = heapq.heappop(self._heap)
            evicted.append(kept)
            count -= 1
            total -= kept.size
        return evicted

    def _target(self, rel_path: str) -> str:
        return os.path.join(self.destination, *rel_path.split("/"))

    def _write(self, archive: tarfile.TarFile, member: tarfile.TarInfo, rel_path: str) -> None:
        target = self._target(rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise tarfile.ExtractError(f"Archive entry has no data: {rel_path}")
        opened = False
        try:
            # Exclusive create: on a case-insensitive or normalising file
            # system "README.md" and "readme.md" share one file, and silently
            # overwriting (and later evicting) it would corrupt another kept
            # entry. A collision raises EEXIST and skips only this entry.
            with source, open(target, "xb") as output:
                opened = True
                # Copy in bounded blocks: never tar.extract/extractall, which
                # would honour archive-supplied paths, links, and modes.
                shutil.copyfileobj(source, output, COPY_BLOCK_BYTES)
        except BaseException:
            if opened:
                self._remove(rel_path)
            raise

    def _remove(self, rel_path: str) -> None:
        try:
            os.remove(self._target(rel_path))
        except FileNotFoundError:
            pass


def _archive_relative_path(name: str) -> tuple[str | None, bool]:
    """Return ``(path without the archive's top directory, is_unsafe)``.

    The path is None for the top directory itself. Unsafe names are
    rejected outright rather than normalised, because a legitimate GitHub
    archive never contains them.
    """
    if not name or name.startswith("/") or "\x00" in name or "\\" in name:
        return None, True
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # Undecodable names arrive as surrogates and cannot be written.
        return None, True
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None, True
    if len(parts) == 1:
        return None, False
    return "/".join(parts[1:]), False


def _display_path(name: str) -> str:
    """Make an attacker-controlled entry name safe to persist and display."""
    return "".join(char if char.isprintable() else "?" for char in name)[:512]


def _archive_commit_sha(archive: tarfile.TarFile) -> str | None:
    # git archive stores the commit id in the pax global header "comment".
    value = str(archive.pax_headers.get("comment") or "").strip()
    return value if _SAFE_COMMIT_SHA.fullmatch(value) else None


def _extract_archive(archive: tarfile.TarFile, destination: str) -> RepositorySnapshot:
    ledger = _ExclusionLedger(settings.excluded_paths_limit)
    budget = _ExtractionBudget(destination, ledger)
    total_seen = 0
    commit_sha: str | None = None
    first_member = True
    while True:
        member = archive.next()
        if first_member:
            # The global header precedes the first member, so it has been
            # parsed by now (also when the archive has no members).
            commit_sha = _archive_commit_sha(archive)
            first_member = False
        if member is None:
            break
        # Stream mode appends every header to archive.members; dropping them
        # keeps memory flat for archives with hundreds of thousands of files.
        del archive.members[:]
        if member.size < 0:
            # tarfile accepts negative base-256 sizes. Such an entry desyncs
            # the stream (later headers are read from the wrong offset) and
            # would drive the byte budget negative, so the archive is
            # rejected as unreadable rather than partially trusted.
            raise tarfile.ReadError("Archive entry has a negative size.")
        is_link = member.issym() or member.islnk()
        countable = member.isfile() or is_link
        rel_path, unsafe = _archive_relative_path(member.name)
        if unsafe:
            if countable:
                total_seen += 1
                ledger.record("unsafe_path", _display_path(member.name), member.size if member.isfile() else 0)
            continue
        if rel_path is None or not countable:
            continue
        if any(is_pruned_directory(part) for part in rel_path.split("/")[:-1]):
            continue
        total_seen += 1
        if is_link:
            ledger.record(path_exclusion_reason(rel_path, 0, is_symlink=True) or "symlink", rel_path, 0)
            continue
        reason = path_exclusion_reason(rel_path, int(member.size))
        if reason:
            ledger.record(reason, rel_path, int(member.size))
            continue
        budget.offer(archive, member, rel_path)
    return RepositorySnapshot(
        path=destination,
        commit_sha=commit_sha,
        source="archive",
        total_seen_files=total_seen,
        pre_excluded_reasons=ledger.counts(),
        pre_excluded_paths=ledger.sorted_paths(),
        pre_excluded_bytes=ledger.bytes,
        pre_budget_categories=ledger.budget_counts(),
        pre_budget_sample=ledger.budget_sample(),
    )


# Extended headers (pax records, GNU long names) are read whole into memory
# by tarfile, and each one recursively parses the next header. A few hundred
# bytes of gzip can declare a gigabyte pax header or a chain of thousands, so
# both the size and the chain length are capped well above what git emits.
MAX_EXTENDED_HEADER_BYTES = 1_048_576
MAX_EXTENDED_HEADER_CHAIN = 8
_EXTENDED_HEADER_TYPES = frozenset(
    {tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE}
)


class _BoundedTarInfo(tarfile.TarInfo):
    """TarInfo that refuses oversized or deeply chained extended headers."""

    def _proc_member(self, tarfile_obj):
        if self.type not in _EXTENDED_HEADER_TYPES:
            tarfile_obj._extended_header_chain = 0
            return super()._proc_member(tarfile_obj)
        depth = getattr(tarfile_obj, "_extended_header_chain", 0) + 1
        if self.size > MAX_EXTENDED_HEADER_BYTES or depth > MAX_EXTENDED_HEADER_CHAIN:
            raise tarfile.ReadError("Archive extended header exceeds the safety limit.")
        tarfile_obj._extended_header_chain = depth
        try:
            return super()._proc_member(tarfile_obj)
        finally:
            tarfile_obj._extended_header_chain = 0


def _open_archive_stream(reader: _ArchiveStreamReader) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=reader, mode="r|gz", tarinfo=_BoundedTarInfo)
    except TypeError as error:
        # tarfile parses the gzip header with ord() on single-byte reads, so a
        # response cut off inside that header raises TypeError, not ReadError.
        raise tarfile.ReadError("The gzip header is truncated.") from error


def _require_complete_stream(archive: tarfile.TarFile) -> None:
    """Reject an archive whose gzip stream stopped before its end.

    tarfile treats a header cut short by a dropped connection as a normal end
    of archive, and http.client returns a short body without error when a
    Content-Length response closes early. Without this check a truncated
    download would be indexed as if it were the whole repository.
    """
    stream = archive.fileobj
    # The tar end marker is followed only by zero padding and the gzip
    # trailer, so draining is cheap and lets the decompressor reach its end.
    while stream.read(COPY_BLOCK_BYTES):
        pass
    decompressor = getattr(stream, "cmp", None)
    if decompressor is not None and not getattr(decompressor, "eof", True):
        raise RepositoryCloneError(_DOWNLOAD_INTERRUPTED_MESSAGE)


def _download_and_extract(request: urllib.request.Request, destination: str, deadline: float, should_stop=None) -> RepositorySnapshot:
    timeout = max(1, min(ARCHIVE_SOCKET_TIMEOUT_SECONDS, settings.source_fetch_timeout_seconds))
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            raise RepositoryCloneError(NOT_FOUND_MESSAGE, not_found=True) from None
        raise RepositoryCloneError(f"GitHub archive download failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        raise RepositoryCloneError(_DOWNLOAD_UNREACHABLE_MESSAGE) from error
    try:
        with response:
            reader = _ArchiveStreamReader(response, settings.archive_max_download_bytes, deadline, should_stop)
            with _open_archive_stream(reader) as archive:
                snapshot = _extract_archive(archive, destination)
                _require_complete_stream(archive)
            snapshot.downloaded_bytes = reader.bytes_read
            return snapshot
    except (RepositoryValidationError, RepositoryCloneError, SourceFetchCancelled):
        raise
    except (tarfile.TarError, zlib.error, EOFError, ValueError) as error:
        # ValueError here is a malformed header; RepositoryValidationError
        # (also a ValueError) was re-raised above and is never swallowed.
        # Network failures were already translated by the reader; any other
        # OSError is local (disk full, permissions) and propagates unchanged
        # so it is neither mislabelled nor retried through git.
        raise RepositoryCloneError(_ARCHIVE_UNREADABLE_MESSAGE) from error


def _ref_not_found_message(ref: RepositoryRef) -> str:
    noun = "commit" if ref.kind == "commit" else "branch, tag or commit"
    return f"Could not find the {noun} '{ref.display}' in this repository. Check the URL and that the repository is public."


def fetch_repository_archive(github_url: str, ref: RepositoryRef | None = None, should_stop=None) -> RepositorySnapshot:
    """Stream GitHub's tarball for HEAD (or the linked ref) and extract only indexable files.

    Policy is applied per entry while streaming, so dependency folders,
    binaries, translations, and over-budget files are never written to disk.
    The temporary directory is removed on any failure. A ``/tree/<ref>``
    link is ambiguous when the branch contains slashes, so each possible
    split is tried longest first until GitHub finds one.
    """
    owner, repository = parse_github_repository(github_url)
    base_url = settings.github_codeload_base_url.rstrip("/")
    deadline = time.monotonic() + settings.source_fetch_timeout_seconds
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    candidates: list[str | None] = ref.candidates() if ref else [None]
    for candidate in candidates:
        if should_stop is not None and should_stop():
            raise SourceFetchCancelled("Indexing was stopped while the repository was downloading.")
        archive_ref = "HEAD" if candidate is None else quote(candidate, safe="/")
        request = urllib.request.Request(
            f"{base_url}/{owner}/{repository}/tar.gz/{archive_ref}",
            headers={"User-Agent": ARCHIVE_USER_AGENT},
        )
        local_path = tempfile.mkdtemp(prefix="repo-", dir=REPOS_DIR)
        try:
            snapshot = _download_and_extract(request, local_path, deadline, should_stop)
        except RepositoryCloneError as error:
            shutil.rmtree(local_path, ignore_errors=True)
            if ref is not None and error.not_found:
                continue
            raise
        except BaseException:
            shutil.rmtree(local_path, ignore_errors=True)
            raise
        snapshot.ref = candidate
        return snapshot
    raise RepositoryCloneError(_ref_not_found_message(ref), not_found=True)


def acquire_repository_source(github_url: str, should_stop=None) -> RepositorySnapshot:
    """Download a repository, falling back to git only for transient failures.

    A 404 is definitive (missing or private) and validation errors (such as
    the download limit) would fail the same way through git, so neither
    falls back. Without GitPython (no git executable) there is nothing to
    fall back to, and the archive error is the clearest explanation.
    ``github_url`` may name a branch or commit (``/tree/<ref>``,
    ``/commit/<sha>``); that ref is fetched instead of the default branch.
    ``should_stop`` is polled while downloading so a stopped job ends fast.
    """
    ref = parse_github_ref(github_url)
    canonical_url = normalize_github_url(github_url)
    try:
        return fetch_repository_archive(canonical_url, ref, should_stop)
    except RepositoryCloneError as error:
        if error.not_found or Repo is None or (ref is not None and ref.kind == "commit"):
            raise
        logger.warning("Archive download failed (%s); falling back to git clone.", error)
    branch = ref.candidates()[-1] if ref is not None else None
    local_path = clone_repo_shallow(canonical_url, **({"branch": branch} if branch else {}))
    try:
        commit_sha = Repo(local_path).head.commit.hexsha
    except Exception:
        commit_sha = None
    return RepositorySnapshot(path=local_path, commit_sha=commit_sha, source="git", ref=branch)


def clone_repo_shallow(github_url: str, branch: str | None = None) -> str:
    """Clone to a unique temporary directory to avoid concurrent-ingestion collisions."""
    canonical_url = normalize_github_url(github_url)
    if Repo is None:
        raise RepositoryCloneError(_GIT_UNAVAILABLE_MESSAGE)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    local_path = tempfile.mkdtemp(prefix="repo-", dir=REPOS_DIR)
    try:
        Repo.clone_from(
            canonical_url,
            local_path,
            depth=1,
            **({"branch": branch} if branch else {}),
            # Avoid downloading historical blobs before checkout. This reduces
            # the blast radius of very large public repositories; the worker's
            # post-clone limits remain a second line of defence.
            multi_options=["--filter=blob:none"],
            kill_after_timeout=settings.clone_timeout_seconds,
        )
        return local_path
    except GitCommandNotFound as error:
        # GitPython can import (GIT_PYTHON_REFRESH=quiet) yet find no git at
        # run time; report that plainly instead of an unexplained crash.
        shutil.rmtree(local_path, ignore_errors=True)
        raise RepositoryCloneError(_GIT_UNAVAILABLE_MESSAGE) from error
    except GitCommandError as error:
        shutil.rmtree(local_path, ignore_errors=True)
        raise RepositoryCloneError(
            "Repository clone failed. Verify that it is public, accessible, and a valid GitHub repository."
        ) from error
    except Exception:
        shutil.rmtree(local_path, ignore_errors=True)
        raise


def cleanup_repo(repo_path: str):
    """Delete only the unique clone directory created for this ingestion."""
    resolved_root = REPOS_DIR.resolve()
    resolved_path = Path(repo_path).resolve()
    if resolved_root not in resolved_path.parents:
        raise ValueError("Refusing to clean up a path outside repos_temp.")
    shutil.rmtree(resolved_path, ignore_errors=True)


def should_process_file(filepath: str) -> bool:
    """Single-file form of the selection policy (budgets aside)."""
    if os.path.islink(filepath):
        return False
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return False
    if path_exclusion_reason(filepath.replace(os.sep, "/"), size) is not None:
        return False
    return is_probably_text_file(filepath)


def is_probably_text_file(filepath: str) -> bool:
    try:
        with open(filepath, "rb") as file_handle:
            sample = file_handle.read(4096)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    text_bytes = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)))
    return not bool(sample.translate(None, text_bytes))
