"""Tests for repository URL parsing, file policy, ranking, budgets, and archive acquisition."""

import errno
import gzip
import http.client
import io
import os
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import settings
from ingest import cloner
from ingest.cloner import (
    RepositoryCloneError,
    RepositorySnapshot,
    RepositoryValidationError,
    acquire_repository_source,
    estimate_chunks,
    fetch_repository_archive,
    file_priority,
    get_file_selection_report,
    language_tag_code,
    locale_catalogue_language,
    normalize_github_url,
    parse_github_repository,
    path_exclusion_reason,
    repository_name,
    should_process_file,
)

CANONICAL = "https://github.com/octocat/Hello-World.git"
URLOPEN_TARGET = "ingest.cloner.urllib.request.urlopen"


def write_tree(root: Path, files: dict[str, bytes | str]) -> None:
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content)


def report_for(files: dict[str, bytes | str], **kwargs) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_tree(root, files)
        report = get_file_selection_report(str(root), **kwargs)
        # Return paths relative to the tree so assertions do not depend on tmp.
        report["relative_files"] = [os.path.relpath(path, tmp).replace(os.sep, "/") for path in report["files"]]
        return report


class GithubUrlNormalizationTests(unittest.TestCase):
    def test_accepts_common_repository_url_forms(self):
        accepted = [
            ("https://github.com/octocat/Hello-World", CANONICAL),
            (" https://github.com/octocat/Hello-World/ ", CANONICAL),
            ("https://github.com/octocat/Hello-World.git", CANONICAL),
            ("https://github.com/octocat/Hello-World.git/", CANONICAL),
            ("http://github.com/octocat/Hello-World", CANONICAL),
            ("HTTPS://GitHub.COM/octocat/Hello-World", CANONICAL),
            ("https://www.github.com/octocat/Hello-World", CANONICAL),
            ("github.com/octocat/Hello-World", CANONICAL),
            ("www.github.com/octocat/Hello-World", CANONICAL),
            ("github.com/octocat/Hello-World.git", CANONICAL),
            ("git@github.com:octocat/Hello-World.git", CANONICAL),
            ("git@github.com:octocat/Hello-World", CANONICAL),
            ("octocat/Hello-World", CANONICAL),
            ("octocat/Hello-World.git", CANONICAL),
            ("https://github.com/octocat/Hello-World?tab=readme-ov-file", CANONICAL),
            ("https://github.com/octocat/Hello-World#readme", CANONICAL),
            ("https://github.com/octocat/Hello-World/?tab=readme#install", CANONICAL),
            ("https://github.com/octocat/Hello-World/tree/main/src", CANONICAL),
            ("https://github.com/octocat/Hello-World/blob/main/README.md", CANONICAL),
            ("https://github.com/octocat/Hello-World/issues/12", CANONICAL),
            ("https://github.com/octocat/Hello-World/pull/7/files", CANONICAL),
            ("github.com/octocat/Hello-World/tree/main?x=1", CANONICAL),
            ("https://github.com/calcom/cal.com", "https://github.com/calcom/cal.com.git"),
            ("https://github.com/calcom/cal.com/pulls", "https://github.com/calcom/cal.com.git"),
            ("https://github.com/a-b/c_d.e-f", "https://github.com/a-b/c_d.e-f.git"),
            ("https://github.com/A1/Repo.GIT", "https://github.com/A1/Repo.git"),
            ("https://github.com/a/.github", "https://github.com/a/.github.git"),
        ]
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertEqual(normalize_github_url(value), expected)

    def test_accepts_every_documented_github_route(self):
        for route in sorted(cloner.GITHUB_ROUTE_SEGMENTS):
            with self.subTest(route=route):
                self.assertEqual(normalize_github_url(f"https://github.com/octocat/Hello-World/{route}/x"), CANONICAL)
                self.assertEqual(normalize_github_url(f"https://github.com/octocat/Hello-World/{route}"), CANONICAL)

    def test_rejects_other_hosts_credentials_ports_and_malformed_urls(self):
        rejected = [
            "",
            "   ",
            "https://gitlab.com/octocat/Hello-World",
            "https://github.com.evil.test/octocat/Hello-World",
            "github.com.evil.test/octocat/Hello-World",
            "https://evilgithub.com/octocat/Hello-World",
            "https://gist.github.com/octocat/Hello-World",
            "https://api.github.com/repos/octocat/Hello-World",
            "https://github.com./octocat/Hello-World",
            "https://evil.test/a/b",
            "https://user:token@github.com/octocat/Hello-World",
            "https://token@github.com/octocat/Hello-World",
            "https://@github.com/octocat/Hello-World",
            "user:pass@github.com/octocat/Hello-World",
            "https://github.com\\@evil.test/octocat/Hello-World",
            "https://github.com:443/octocat/Hello-World",
            "https://github.com:/octocat/Hello-World",
            "github.com:8080/octocat/Hello-World",
            "ftp://github.com/octocat/Hello-World",
            "ssh://git@github.com/octocat/Hello-World.git",
            "git@gitlab.com:octocat/Hello-World.git",
            "git@github.com/octocat/Hello-World",
            "https://github.com/octocat",
            "https://github.com/",
            "github.com",
            "octocat",
            "octocat//Hello-World",
            "https://github.com/octocat/Hello-World/unknown",
            "https://github.com/octocat/Hello-World/tree-like/main",
            "https://github.com/-octocat/Hello-World",
            "https://github.com/octo_cat/Hello-World",
            f"https://github.com/{'a' * 40}/Hello-World",
            f"https://github.com/octocat/{'r' * 101}",
            "https://github.com/octocat/..",
            "https://github.com/octocat/.",
            "https://github.com/octocat/.git",
            "https://github.com/octocat/..git",
            "https://github.com/octocat/Hello%20World",
            "https://github.com/octo%2Fcat/Hello-World",
            "https://github.com/octo cat/Hello-World",
            "https://github.com/octo\ncat/Hello-World",
            "https://github.com/octocat/Hello-World\t/x",
            "https://[::1]/octocat/Hello-World",
            "https://[broken/octocat/Hello-World",
            "https://github.com/settings/profile",
            "https://github.com/" + "a/b?" + "x" * cloner.MAX_GITHUB_URL_LENGTH,
        ]
        self.assertGreaterEqual(len(rejected), 25)
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(RepositoryValidationError):
                    normalize_github_url(value)

    def test_rejects_every_reserved_owner_name(self):
        for owner in sorted(cloner.RESERVED_OWNER_NAMES):
            with self.subTest(owner=owner):
                with self.assertRaises(RepositoryValidationError):
                    normalize_github_url(f"https://github.com/{owner}/anything")
                with self.assertRaises(RepositoryValidationError):
                    normalize_github_url(f"https://github.com/{owner.upper()}/anything")

    def test_rejects_non_string_input(self):
        for value in (None, 42, b"octocat/Hello-World"):
            with self.subTest(value=value):
                with self.assertRaises(RepositoryValidationError):
                    parse_github_repository(value)  # type: ignore[arg-type]

    def test_length_limits_are_inclusive(self):
        owner = "a" * 39
        repository = "r" * 100
        self.assertEqual(parse_github_repository(f"{owner}/{repository}"), (owner, repository))

    def test_parse_and_repository_name_preserve_case(self):
        self.assertEqual(parse_github_repository("git@github.com:CalCom/Cal.com.git"), ("CalCom", "Cal.com"))
        self.assertEqual(repository_name("https://github.com/calcom/cal.com/tree/main"), "cal.com")
        self.assertEqual(repository_name("https://github.com/octocat/Hello-World.git"), "Hello-World")

    def test_errors_explain_the_problem(self):
        cases = {
            "https://gitlab.com/a/b": "github.com",
            "https://u:p@github.com/a/b": "password",
            "https://github.com:443/a/b": "port",
            "https://github.com/a/b/unknown": "repository",
        }
        for value, fragment in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(RepositoryValidationError) as raised:
                    normalize_github_url(value)
                self.assertIn(fragment, str(raised.exception))


class FilePolicyTests(unittest.TestCase):
    def test_language_tags(self):
        self.assertEqual(language_tag_code("fr"), "fr")
        self.assertEqual(language_tag_code("zh-CN"), "zh")
        self.assertEqual(language_tag_code("pt_BR"), "pt")
        self.assertEqual(language_tag_code("zh-Hant"), "zh")
        self.assertEqual(language_tag_code("es-419"), "es")
        self.assertEqual(language_tag_code("zh-Hant-TW"), "zh")
        self.assertEqual(language_tag_code("EN-us"), "en")
        for value in ("index", "api", "config", "ui", "db", "js", "xx", "fr-", "fr-CNN", "f"):
            with self.subTest(value=value):
                self.assertIsNone(language_tag_code(value))
        official = set(
            "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv "
            "dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig "
            "ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh "
            "mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru "
            "rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug "
            "uk ur uz ve vi vo wa wo xh yi yo za zh zu".split()
        )
        self.assertEqual(len(official), 184)
        self.assertLessEqual(official, cloner.ISO_639_1_CODES)
        for code in sorted(official):
            with self.subTest(code=code):
                self.assertEqual(language_tag_code(code.upper() + "-XY"), code)

    def test_translation_catalogues_are_excluded_but_english_and_code_are_kept(self):
        excluded = [
            "packages/i18n/locales/fr/common.json",
            "public/static/locales/zh-CN/common.json",
            "locales/pt_BR.json",
            "config/locales/de.yml",
            "locale/fr/LC_MESSAGES/django.po",
            "src/locales/es-419.ts",
            "lib/l10n/ja.arb",
            "src/lang/ko.js",
            "apps/web/translations/it/messages.xliff",
        ]
        kept = [
            "packages/i18n/locales/en/common.json",
            "locales/en_GB.json",
            "locales/en-US/common.json",
            "src/i18n/index.ts",
            "src/i18n/fr/index.ts",
            "src/messages/api.json",
            "locales/config.json",
            "src/fr/app.json",
            "src/ts/i18n/en/common.json",
            "locales/fr/README.md",
        ]
        for rel_path in excluded:
            with self.subTest(rel_path=rel_path):
                self.assertEqual(path_exclusion_reason(rel_path, 10), "translations")
        for rel_path in kept:
            with self.subTest(rel_path=rel_path):
                self.assertNotEqual(path_exclusion_reason(rel_path, 10), "translations")
        self.assertEqual(locale_catalogue_language("packages/i18n/locales/en/common.json"), "en")
        self.assertIsNone(locale_catalogue_language("src/i18n/index.ts"))

    def test_translations_are_reported_through_the_selection_report(self):
        report = report_for({
            "packages/i18n/locales/fr/common.json": '{"a": "b"}',
            "packages/i18n/locales/en/common.json": '{"a": "b"}',
            "src/i18n/index.ts": "export {}\n",
            "public/static/locales/zh-CN/common.json": "{}",
            "locales/pt_BR.json": "{}",
            "src/messages/api.json": "{}",
        })
        self.assertEqual(report["excluded_reasons"], {"translations": 3})
        self.assertEqual(set(report["excluded_paths"]), {
            "packages/i18n/locales/fr/common.json",
            "public/static/locales/zh-CN/common.json",
            "locales/pt_BR.json",
        })
        self.assertEqual(set(report["relative_files"]), {
            "packages/i18n/locales/en/common.json", "src/i18n/index.ts", "src/messages/api.json",
        })

    def test_index_translations_setting_keeps_catalogues(self):
        with patch.object(settings, "index_translations", True):
            self.assertIsNone(path_exclusion_reason("packages/i18n/locales/fr/common.json", 10))
            report = report_for({"locales/fr/common.json": "{}", "src/app.ts": "x\n"})
        self.assertEqual(report["eligible_files"], 2)
        self.assertNotIn("translations", report["excluded_reasons"])

    def test_lockfiles_are_excluded_case_insensitively(self):
        for name in sorted(cloner.SKIP_FILENAMES):
            with self.subTest(name=name):
                self.assertEqual(path_exclusion_reason(f"packages/web/{name}", 10), "lockfile")
        for name in ("Gemfile.lock", "Pipfile.lock", "Podfile.lock", "go.sum", "i18n.lock", "bun.lock", "uv.lock", "deno.lock"):
            with self.subTest(name=name):
                self.assertEqual(path_exclusion_reason(name, 10), "lockfile")

    def test_build_artifacts_and_minified_files_are_excluded(self):
        self.assertEqual(path_exclusion_reason("tsconfig.tsbuildinfo", 4_000_000), "build_artifact")
        self.assertEqual(path_exclusion_reason("packages/ui/tsconfig.build.tsbuildinfo", 10), "build_artifact")
        self.assertEqual(path_exclusion_reason("public/app.js.map", 10), "build_artifact")
        self.assertEqual(path_exclusion_reason("styles/site.css.map", 10), "build_artifact")
        self.assertEqual(path_exclusion_reason("public/app.min.js", 10), "minified")
        report = report_for({"tsconfig.tsbuildinfo": "{}", "dist2/app.js.map": "{}", "src/app.ts": "x\n"})
        self.assertEqual(report["excluded_reasons"], {"build_artifact": 2})
        self.assertEqual(report["relative_files"], ["src/app.ts"])

    def test_hidden_policy_allows_ci_and_env_templates_only(self):
        report = report_for({
            ".github/workflows/ci.yml": "on: push\n",
            ".github/CODEOWNERS": "* @team\n",
            ".circleci/config.yml": "version: 2\n",
            ".gitlab/issue_templates/bug.md": "# Bug\n",
            ".devcontainer/devcontainer.json": "{}",
            ".gitlab-ci.yml": "stages: []\n",
            ".env": "SECRET=x\n",
            ".env.example": "SECRET=\n",
            ".env.sample": "SECRET=\n",
            ".env.template": "SECRET=\n",
            ".env.local.example": "SECRET=\n",
            ".eslintrc.json": "{}",
            ".github/workflows/.hidden.yml": "x: 1\n",
            ".vscode/settings.json": "{}",
            ".husky/pre-commit": "npm test\n",
            "src/.cache/x.ts": "x\n",
        })
        self.assertEqual(set(report["relative_files"]), {
            ".github/workflows/ci.yml", ".github/CODEOWNERS", ".circleci/config.yml",
            ".gitlab/issue_templates/bug.md", ".devcontainer/devcontainer.json", ".gitlab-ci.yml",
            ".env.example", ".env.sample", ".env.template", ".env.local.example",
        })
        self.assertEqual(report["excluded_reasons"], {"hidden": 3})
        self.assertEqual(set(report["excluded_paths"]), {".env", ".eslintrc.json", ".github/workflows/.hidden.yml"})
        # Pruned hidden directories (.vscode, .husky, .cache) are not counted as seen.
        self.assertEqual(report["total_seen_files"], 13)

    def test_ignored_directories_are_pruned_and_not_counted(self):
        files = {f"{directory}/x.js": "x\n" for directory in sorted(cloner.IGNORE_DIRS)}
        files["src/app.ts"] = "x\n"
        files["packages/web/node_modules/dep/index.js"] = "x\n"
        report = report_for(files)
        self.assertEqual(report["relative_files"], ["src/app.ts"])
        self.assertEqual(report["total_seen_files"], 1)
        self.assertEqual(report["excluded_files"], 0)

    def test_ignored_directory_names_are_case_sensitive(self):
        # Built in its own tree: on a case-insensitive filesystem "Pods" and
        # "pods" would be the same directory.
        report = report_for({"k8s/pods/handler.ts": "x\n"})
        self.assertEqual(report["relative_files"], ["k8s/pods/handler.ts"])
        self.assertTrue(cloner.is_pruned_directory("Pods"))
        self.assertFalse(cloner.is_pruned_directory("pods"))
        self.assertTrue(cloner.is_pruned_directory(".vscode"))
        self.assertFalse(cloner.is_pruned_directory(".GitHub"))

    def test_new_text_formats_and_filenames_are_supported(self):
        for extension in (".mjs", ".vue", ".svelte", ".astro", ".prisma", ".mdx", ".tf", ".sol", ".hbs", ".bat", ".avsc", ".R"):
            with self.subTest(extension=extension):
                self.assertIsNone(path_exclusion_reason(f"src/file{extension}", 10))
        for name in ("Procfile", "Gemfile", "Jenkinsfile", "Justfile", "CODEOWNERS", "CHANGELOG", "GNUmakefile",
                     "Containerfile", "Dockerfile", "Dockerfile.prod", "dockerfile.dev", "api.dockerfile", "LICENSE"):
            with self.subTest(name=name):
                self.assertIsNone(path_exclusion_reason(name, 10))
        for name in ("image.png", "font.woff2", "archive.zip", "Dockerfiles", "notes"):
            with self.subTest(name=name):
                self.assertEqual(path_exclusion_reason(name, 10), "unsupported_format")

    def test_reason_order_and_size_limit(self):
        with patch.object(settings, "max_file_size_bytes", 100):
            self.assertEqual(path_exclusion_reason("src/big.py", 101), "file_size_limit")
            self.assertIsNone(path_exclusion_reason("src/ok.py", 100))
            # Path-only reasons win over the size limit.
            self.assertEqual(path_exclusion_reason("package-lock.json", 10_000), "lockfile")
            self.assertEqual(path_exclusion_reason("locales/fr.json", 10_000), "translations")
            # Size wins over format.
            self.assertEqual(path_exclusion_reason("image.png", 10_000), "file_size_limit")
        self.assertEqual(path_exclusion_reason(".env", 10, is_symlink=True), "hidden")
        self.assertEqual(path_exclusion_reason("src/link.py", 10, is_symlink=True), "symlink")
        self.assertEqual(path_exclusion_reason("src/unknown.py", None), "unreadable")

    def test_binary_content_and_symlinks_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {"src/app.py": "print(1)\n", "src/data.json": b"{\x00}"})
            (root / "src" / "link.py").symlink_to(root / "src" / "app.py")
            (root / "linked_dir").symlink_to(root / "src", target_is_directory=True)
            report = get_file_selection_report(str(root))
        self.assertEqual(report["excluded_reasons"], {"binary_or_invalid_text": 1, "symlink": 1})
        self.assertEqual(report["eligible_files"], 1)
        # A symlinked directory is pruned (not followed and not counted).
        self.assertEqual(report["total_seen_files"], 3)

    def test_should_process_file_agrees_with_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root, {
                "src/app.py": "print(1)\n",
                ".env": "SECRET=x\n",
                ".env.example": "SECRET=\n",
                "yarn.lock": "x\n",
                "tsconfig.tsbuildinfo": "{}",
                "locales/fr/common.json": "{}",
                "locales/en/common.json": "{}",
                "image.png": b"\x89PNG",
                "src/blob.py": b"\x00\x01",
            })
            (root / "src" / "link.py").symlink_to(root / "src" / "app.py")
            results = {name: should_process_file(str(root / name)) for name in (
                "src/app.py", ".env", ".env.example", "yarn.lock", "tsconfig.tsbuildinfo",
                "locales/fr/common.json", "locales/en/common.json", "image.png", "src/blob.py",
                "src/link.py", "missing.py",
            )}
        self.assertEqual({name for name, keep in results.items() if keep}, {
            "src/app.py", ".env.example", "locales/en/common.json",
        })

    def test_policy_constants_include_existing_and_new_entries(self):
        existing_extensions = {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".c", ".cc", ".cpp",
            ".cxx", ".h", ".hpp", ".cs", ".swift", ".kt", ".kts", ".scala", ".sql", ".sh", ".bash", ".zsh",
            ".ps1", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".md", ".rst", ".txt",
            ".html", ".css", ".scss", ".sass", ".less", ".xml", ".graphql", ".gql", ".proto", ".dockerfile",
        }
        new_extensions = set(
            ".mjs .cjs .mts .cts .vue .svelte .astro .prisma .mdx .dart .lua .ex .exs .erl .hrl .hs .ml .mli .r "
            ".jl .tf .tfvars .hcl .gradle .cmake .m .mm .zig .nim .clj .cljs .edn .groovy .pl .pm .fs .fsx .vb "
            ".sol .elm .nix .rake .gemspec .properties .hbs .handlebars .ejs .pug .jinja .j2 .twig .liquid .njk "
            ".bat .cmd .fish .tex .graphqls .thrift .avsc".split()
        )
        self.assertLessEqual(existing_extensions | new_extensions, cloner.TEXT_EXTENSIONS)
        self.assertLessEqual(set(
            "dockerfile makefile readme license gitignore editorconfig procfile gemfile rakefile jenkinsfile "
            "vagrantfile brewfile justfile caddyfile containerfile codeowners contributing changelog gnumakefile".split()
        ), cloner.TEXT_FILENAMES)
        self.assertLessEqual(set(
            "package-lock.json yarn.lock pnpm-lock.yaml poetry.lock composer.lock cargo.lock bun.lockb i18n.lock "
            "bun.lock uv.lock pipfile.lock gemfile.lock go.sum npm-shrinkwrap.json pubspec.lock packages.lock.json "
            "flake.lock mix.lock podfile.lock deno.lock".split()
        ), cloner.SKIP_FILENAMES)
        self.assertTrue(all(name == name.lower() for name in cloner.SKIP_FILENAMES))
        self.assertLessEqual(set(
            "node_modules .git __pycache__ .venv venv dist build .next vendor target bin obj .cache .tox coverage "
            "out .turbo .yarn .pnpm-store bower_components jspm_packages .gradle .idea site-packages .mypy_cache "
            ".pytest_cache storybook-static .svelte-kit .nuxt .output .vercel .expo Pods DerivedData elm-stuff "
            ".terraform __snapshots__".split()
        ), cloner.IGNORE_DIRS)
        self.assertEqual(cloner.ALLOWED_HIDDEN_DIRS, {".github", ".circleci", ".gitlab", ".devcontainer"})
        self.assertEqual(cloner.ALLOWED_HIDDEN_FILES, {
            ".env.example", ".env.sample", ".env.template", ".env.local.example", ".gitlab-ci.yml",
        })
        self.assertEqual(cloner.LOCALE_DIRECTORY_NAMES, {
            "locales", "locale", "i18n", "l10n", "lang", "langs", "translations", "messages", "intl",
        })


class PriorityTests(unittest.TestCase):
    def test_documented_ordering(self):
        ordered = [
            "package.json", "src/app.ts", "src/app.test.ts", "docs/guide.md", "test/fixtures/data.json",
        ]
        priorities = [file_priority(path, 1_000) for path in ordered]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        self.assertEqual(len(set(priorities)), len(priorities))

    def test_band_values(self):
        cases = {
            "package.json": 95, "pyproject.toml": 95, "Dockerfile": 95, "docker-compose.yaml": 95,
            "next.config.mjs": 95, "vite.config.ts": 95, "Makefile": 95, "go.mod": 95,
            "README.md": 92, "README": 92,
            "packages/ui/package.json": 88, "apps/web/next.config.js": 88, "services/package.json": 88,
            "src/Dockerfile": 88,
            "prisma/schema.prisma": 85, "src/schema.graphql": 85, "api/service.proto": 85,
            "db/schema.sql": 85, "src/api.graphqls": 85,
            "src/app.ts": 80, "src/App.vue": 80, "infra/main.tf": 80, "src/Dockerfile.dev": 80,
            "deploy/images/api/Dockerfile": 79.5,
            ".github/workflows/ci.yml": 65, ".circleci/config.yml": 65, ".gitlab-ci.yml": 65, "Jenkinsfile": 65,
            "src/styles/app.css": 60, "templates/page.hbs": 60,
            "src/README.md": 55, "docs/README.md": 55,
            "config/settings.yaml": 50, "src/data.json": 50, "CODEOWNERS": 50, ".env.example": 50,
            "CONTRIBUTING.md": 45, "LICENSE": 45, "notes.txt": 45,
            "docs/guide.md": 40, "documentation/setup.rst": 40,
        }
        for rel_path, expected in cases.items():
            with self.subTest(rel_path=rel_path):
                self.assertEqual(file_priority(rel_path, 1_000), float(expected))

    def test_readme_band_is_only_for_readme_documents(self):
        cases = {
            "README.md": 92, "readme.rst": 92, "README.txt": 92, "README": 92,
            "packages/ui/README.mdx": 55,
            # A script named readme is code, not the project README.
            "readme.py": 80, "src/readme.ts": 80,
            "README.zh-CN.md": 45,
        }
        for rel_path, expected in cases.items():
            with self.subTest(rel_path=rel_path):
                self.assertEqual(file_priority(rel_path, 1_000), float(expected))

    def test_adjustments_are_cumulative(self):
        cases = {
            "src/app.test.ts": 60, "src/app.spec.ts": 60, "tests/test_app.py": 60, "pkg/app_test.go": 60,
            "src/__tests__/app.ts": 60, "e2e/login.ts": 60,
            "src/__mocks__/api.ts": 50, "examples/demo.py": 50, "src/Button.stories.tsx": 50,
            "db/migrations/001_init.sql": 55,
            "src/gen/client.ts": 50, "src/types.d.ts": 50, "proto/api_pb2.py": 50, "api/api.pb.go": 50,
            "src/client.generated.ts": 50,
            "locales/en/common.json": 30,
            "test/fixtures/data.json": 1,
            "tests/fixtures/generated/sample.test.ts": 1,
        }
        for rel_path, expected in cases.items():
            with self.subTest(rel_path=rel_path):
                self.assertEqual(file_priority(rel_path, 1_000), float(expected))
        with patch.object(settings, "index_translations", True):
            # Foreign catalogues never outrank English ones when translations are indexed.
            self.assertEqual(file_priority("locales/fr/common.json", 1_000), 30.0)

    def test_size_depth_and_clamping(self):
        self.assertEqual(file_priority("src/app.py", cloner.LARGE_FILE_BYTES), 80.0)
        self.assertEqual(file_priority("src/app.py", cloner.LARGE_FILE_BYTES + 1), 65.0)
        self.assertEqual(file_priority("src/app.py", cloner.HUGE_FILE_BYTES), 65.0)
        self.assertEqual(file_priority("src/app.py", cloner.HUGE_FILE_BYTES + 1), 50.0)
        self.assertEqual(file_priority("a/b/app.py", 1), 80.0)
        # Depth is a tie-break (half a point per level, at most 4), not a band.
        self.assertEqual(file_priority("a/b/c/app.py", 1), 79.5)
        self.assertEqual(file_priority("a/b/c/d/e/app.py", 1), 78.5)
        self.assertEqual(file_priority("/".join("abcdefghijklmnop") + "/app.py", 1), 76.0)
        self.assertEqual(file_priority("a/b/c/package.json", 1), 49.5)
        for rel_path, size in (("package.json", 0), ("test/fixtures/x/y/z/w/v/u/data.json", 5_000_000)):
            with self.subTest(rel_path=rel_path):
                value = file_priority(rel_path, size)
                self.assertGreaterEqual(value, 1.0)
                self.assertLessEqual(value, 100.0)
                self.assertIsInstance(value, float)
        self.assertEqual(file_priority("src/app.py", 10), file_priority("src/app.py", 10))

    def test_estimate_chunks(self):
        # Calibrated against real chunker output: empty files make no chunk,
        # and large files are estimated fractionally at ~4.2 KB per chunk.
        cases = {0: 0, 1: 1, 6_000: 1, 8_400: 2, 42_000: 10}
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertAlmostEqual(estimate_chunks(size), expected)
        self.assertAlmostEqual(estimate_chunks(6_001), 6_001 / 4_200)


class BudgetSelectionTests(unittest.TestCase):
    def test_file_count_budget_never_raises_and_keeps_highest_priority_files(self):
        files = {
            "docs/guide.md": "# Guide\n",
            "src/app.ts": "x\n",
            "package.json": "{}",
            "src/app.test.ts": "x\n",
            "test/fixtures/data.json": "{}",
        }
        with patch.object(settings, "max_repository_files", 2):
            report = report_for(files)
        self.assertEqual(report["relative_files"], ["package.json", "src/app.ts"])
        self.assertEqual(report["eligible_files"], 2)
        self.assertEqual(report["budget_omitted_files"], 3)
        self.assertEqual(report["excluded_reasons"], {"repository_budget": 3})
        self.assertEqual(report["candidate_files"], 5)
        self.assertEqual(report["total_seen_files"], 5)
        self.assertEqual(set(report["excluded_paths"]), {"docs/guide.md", "src/app.test.ts", "test/fixtures/data.json"})
        self.assertEqual(report["selection_budget"]["max_files"], 2)

    def test_files_are_returned_in_priority_order(self):
        report = report_for({
            "docs/guide.md": "x\n", "src/b.ts": "x\n", "src/a.ts": "x\n", "README.md": "x\n",
            "package.json": "{}", "src/app.test.ts": "x\n", "schema.prisma": "x\n",
        })
        self.assertEqual(report["relative_files"], [
            "package.json", "README.md", "schema.prisma", "src/a.ts", "src/b.ts", "src/app.test.ts", "docs/guide.md",
        ])

    def test_byte_budget_skips_a_large_file_and_keeps_scanning(self):
        files = {"src/app.ts": "x" * 500, "src/small.test.ts": "x" * 50, "docs/guide.md": "x" * 40}
        report = report_for(files, byte_budget=100)
        self.assertEqual(report["relative_files"], ["src/small.test.ts", "docs/guide.md"])
        self.assertEqual(report["eligible_bytes"], 90)
        self.assertEqual(report["excluded_reasons"], {"repository_budget": 1})
        self.assertEqual(report["excluded_paths"], ["src/app.ts"])
        self.assertEqual(report["excluded_bytes"], 500)
        self.assertEqual(report["selection_budget"]["max_bytes"], 100)

    def test_repository_byte_setting_caps_a_larger_byte_budget(self):
        with patch.object(settings, "max_repository_bytes", 60):
            report = report_for({"src/a.ts": "x" * 50, "src/b.ts": "x" * 50}, byte_budget=10_000)
        self.assertEqual(report["relative_files"], ["src/a.ts"])
        self.assertEqual(report["selection_budget"]["max_bytes"], 60)

    def test_zero_or_missing_byte_budget_means_no_extra_cap(self):
        for budget in (None, 0):
            with self.subTest(budget=budget):
                report = report_for({"src/a.ts": "x" * 50}, byte_budget=budget)
                self.assertEqual(report["eligible_files"], 1)
                self.assertEqual(report["selection_budget"]["max_bytes"], settings.max_repository_bytes)

    def test_chunk_budget_is_honored(self):
        files = {"src/a.ts": "x" * 7_000, "src/b.ts": "x\n", "src/c.ts": "x\n", "docs/d.md": "x\n"}
        report = report_for(files, chunk_budget=3)
        # a.ts needs two chunks; b.ts fills the budget; c.ts and d.md no longer fit.
        self.assertEqual(report["relative_files"], ["src/a.ts", "src/b.ts"])
        self.assertEqual(report["estimated_chunks"], 3)
        self.assertEqual(report["budget_omitted_files"], 2)
        self.assertEqual(report["selection_budget"]["max_chunks"], 3)

    def test_default_chunk_budget_keeps_headroom(self):
        with patch.object(settings, "max_repository_chunks", 3):
            report = report_for({"src/a.ts": "x\n", "src/b.ts": "x\n", "src/c.ts": "x\n"})
        self.assertEqual(report["selection_budget"]["max_chunks"], 2)
        self.assertEqual(report["relative_files"], ["src/a.ts", "src/b.ts"])
        self.assertEqual(report["excluded_reasons"], {"repository_budget": 1})

    def test_full_file_budget_omits_everything_that_remains(self):
        with patch.object(settings, "max_repository_files", 1):
            report = report_for({"src/big.ts": "x" * 100, "docs/tiny.md": "x"}, byte_budget=150)
        self.assertEqual(report["relative_files"], ["src/big.ts"])
        self.assertEqual(report["excluded_paths"], ["docs/tiny.md"])

    def test_report_shape_and_excluded_path_cap(self):
        files = {f"assets/image{index:02d}.png": b"\x89PNG" for index in range(5)}
        files["src/app.ts"] = "x\n"
        with patch.object(settings, "excluded_paths_limit", 3):
            report = report_for(files)
        self.assertEqual(report["excluded_files"], 5)
        self.assertEqual(report["excluded_paths"], ["assets/image00.png", "assets/image01.png", "assets/image02.png"])
        self.assertTrue(report["excluded_paths_truncated"])
        self.assertEqual(set(report), {
            "files", "total_seen_files", "eligible_files", "candidate_files", "excluded_files", "excluded_bytes",
            "excluded_reasons", "excluded_paths", "excluded_paths_truncated", "eligible_bytes",
            "estimated_chunks", "budget_omitted_files", "selection_budget", "relative_files",
            "budget_omitted_by_category", "budget_omitted_sample",
        })
        self.assertEqual(set(report["selection_budget"]), {"max_files", "max_bytes", "max_chunks"})
        untruncated = report_for({"a.png": b"x", "src/app.ts": "x\n"})
        self.assertFalse(untruncated["excluded_paths_truncated"])

    def test_pre_excluded_snapshot_is_merged(self):
        snapshot = RepositorySnapshot(
            path="unused", commit_sha="abc123", source="archive", total_seen_files=10,
            pre_excluded_reasons={"translations": 2, "repository_budget": 1, "unsupported_format": 4},
            pre_excluded_paths=["a/fr.json", "b/de.json", "c/big.py", "img/1.png", "img/2.png", "img/3.png", "img/4.png"],
            pre_excluded_bytes=1_000,
        )
        report = report_for({"src/app.ts": "x\n", "src/blob.py": b"\x00"}, pre_excluded=snapshot)
        self.assertEqual(report["total_seen_files"], 10)
        self.assertEqual(report["excluded_reasons"], {
            "binary_or_invalid_text": 1, "repository_budget": 1, "translations": 2, "unsupported_format": 4,
        })
        self.assertEqual(report["excluded_files"], 8)
        self.assertEqual(report["excluded_bytes"], 1_001)
        self.assertIn("a/fr.json", report["excluded_paths"])
        self.assertIn("src/blob.py", report["excluded_paths"])
        self.assertEqual(report["excluded_paths"], sorted(report["excluded_paths"]))
        self.assertEqual(report["candidate_files"], 2)
        self.assertEqual(report["budget_omitted_files"], 1)

    def test_git_snapshot_keeps_the_walk_count(self):
        snapshot = RepositorySnapshot(path="unused", commit_sha=None, source="git")
        report = report_for({"src/app.ts": "x\n", "a.png": b"x"}, pre_excluded=snapshot)
        self.assertEqual(report["total_seen_files"], 2)

    def test_files_are_absolute_even_for_a_relative_repository_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(Path(tmp), {"repo/src/app.ts": "x\n", "repo/README.md": "# r\n"})
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                report = get_file_selection_report("repo")
                files = report["files"]
                self.assertTrue(all(os.path.isabs(path) for path in files))
                # Relative to the working directory as the OS reports it
                # (macOS resolves /var to /private/var after chdir).
                base = os.path.join(os.getcwd(), "repo")
                self.assertEqual(
                    [os.path.relpath(path, base).replace(os.sep, "/") for path in files],
                    ["README.md", "src/app.ts"],
                )
            finally:
                os.chdir(previous)


def build_archive(
    entries: list[tuple[str, bytes | None, str]],
    *,
    comment: str | None = "abc123",
    top: str = "octocat-Hello-World-abc123",
) -> bytes:
    """Build an in-memory GitHub-style tar.gz.

    Each entry is ``(name, data, kind)`` where kind is file, dir, symlink,
    hardlink, fifo, or raw (a file whose name is used verbatim, without the
    top directory, to model hostile archives).
    """
    buffer = io.BytesIO()
    pax_headers = {"comment": comment} if comment is not None else {}
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax_headers) as tar:
        top_info = tarfile.TarInfo(top)
        top_info.type = tarfile.DIRTYPE
        tar.addfile(top_info)
        for name, data, kind in entries:
            info = tarfile.TarInfo(name if kind == "raw" else f"{top}/{name}")
            if kind in {"file", "raw"}:
                payload = data or b""
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            if kind in {"symlink", "hardlink"}:
                info.linkname = (data or b"target").decode()
            tar.addfile(info)
    return buffer.getvalue()


class ArchiveAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repos_dir = Path(self._tmp.name) / "repos_temp"
        patcher = patch.object(cloner, "REPOS_DIR", self.repos_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def serve(self, archive: bytes) -> MagicMock:
        return MagicMock(side_effect=lambda *args, **kwargs: io.BytesIO(archive))

    def leftover_dirs(self) -> list[str]:
        return sorted(os.listdir(self.repos_dir)) if self.repos_dir.exists() else []

    def test_archive_extraction_applies_policy_and_captures_commit(self):
        archive = build_archive([
            ("src", None, "dir"),
            ("src/app.py", b"print('hello')\n", "file"),
            ("README.md", b"# Hello\n", "file"),
            ("image.png", b"\x89PNG\r\n", "file"),
            ("node_modules/x.js", b"x\n", "file"),
            (".vscode/settings.json", b"{}", "file"),
            (".env", b"SECRET=x\n", "file"),
            (".github/workflows/ci.yml", b"on: push\n", "file"),
            ("locales/fr/common.json", b"{}", "file"),
            ("yarn.lock", b"x\n", "file"),
            ("link.py", b"src/app.py", "symlink"),
            ("hard.py", b"octocat-Hello-World-abc123/src/app.py", "hardlink"),
            ("pipe", None, "fifo"),
            ("../evil.py", b"import os\n", "raw"),
            ("/abs/evil.py", b"x\n", "raw"),
            ("octocat-Hello-World-abc123/dir\\evil.py", b"x\n", "raw"),
            ("octocat-Hello-World-abc123/./dot.py", b"x\n", "raw"),
        ])
        opener = self.serve(archive)
        with patch(URLOPEN_TARGET, opener):
            snapshot = fetch_repository_archive("https://github.com/octocat/Hello-World")
        root = Path(snapshot.path)
        self.assertEqual(snapshot.commit_sha, "abc123")
        self.assertEqual(snapshot.source, "archive")
        self.assertEqual(snapshot.downloaded_bytes, len(archive))
        self.assertTrue(root.is_relative_to(self.repos_dir))
        # The archive's top directory is stripped.
        self.assertTrue((root / "src" / "app.py").is_file())
        self.assertTrue((root / "README.md").is_file())
        self.assertTrue((root / ".github" / "workflows" / "ci.yml").is_file())
        extracted = sorted(
            os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
            for directory, _, names in os.walk(root) for name in names
        )
        self.assertEqual(extracted, [".github/workflows/ci.yml", "README.md", "src/app.py"])
        self.assertFalse(Path(self._tmp.name, "evil.py").exists())
        self.assertEqual(snapshot.pre_excluded_reasons, {
            "hidden": 1, "lockfile": 1, "symlink": 2, "translations": 1, "unsafe_path": 4,
            "unsupported_format": 1,
        })
        self.assertIn("../evil.py", snapshot.pre_excluded_paths)
        self.assertIn("image.png", snapshot.pre_excluded_paths)
        self.assertNotIn("node_modules/x.js", snapshot.pre_excluded_paths)
        self.assertEqual(snapshot.pre_excluded_paths, sorted(snapshot.pre_excluded_paths))
        # 3 extracted + 6 policy exclusions + 4 unsafe; pruned dirs, dirs, and the fifo are not counted.
        self.assertEqual(snapshot.total_seen_files, 13)
        self.assertEqual(snapshot.pre_excluded_bytes, len(b"\x89PNG\r\n") + len(b"SECRET=x\n") + 2 + 2 + 10 + 2 + 2 + 2)

        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://codeload.github.com/octocat/Hello-World/tar.gz/HEAD")
        self.assertEqual(request.get_header("User-agent"), "codebase-intel")
        self.assertEqual(opener.call_args.kwargs["timeout"], min(120, settings.source_fetch_timeout_seconds))

        report = get_file_selection_report(snapshot.path, pre_excluded=snapshot)
        self.assertEqual(report["total_seen_files"], 13)
        self.assertEqual(report["eligible_files"], 3)
        self.assertEqual(report["excluded_files"], 10)
        self.assertEqual(report["excluded_reasons"]["unsafe_path"], 4)
        self.assertEqual(report["excluded_reasons"]["unsupported_format"], 1)
        self.assertEqual([Path(path).name for path in report["files"]], ["README.md", "app.py", "ci.yml"])

    def test_archive_applies_every_path_and_size_reason_without_extracting(self):
        archive = build_archive([
            ("public/app.min.js", b"x\n", "file"),
            ("tsconfig.tsbuildinfo", b"{}", "file"),
            ("dist2/app.js.map", b"{}", "file"),
            ("src/big.py", b"b" * 101, "file"),
            ("go.sum", b"x\n", "file"),
            (".env.example", b"KEY=\n", "file"),
            (".gitlab-ci.yml", b"stages: []\n", "file"),
            (".eslintrc.json", b"{}", "file"),
            ("src/ok.py", b"o" * 100, "file"),
        ])
        with patch.object(settings, "max_file_size_bytes", 100), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        extracted = sorted(
            os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
            for directory, _, names in os.walk(root) for name in names
        )
        self.assertEqual(extracted, [".env.example", ".gitlab-ci.yml", "src/ok.py"])
        self.assertEqual(snapshot.pre_excluded_reasons, {
            "build_artifact": 2, "file_size_limit": 1, "hidden": 1, "lockfile": 1, "minified": 1,
        })
        self.assertEqual(snapshot.total_seen_files, 9)
        with patch.object(settings, "max_file_size_bytes", 100):
            report = get_file_selection_report(snapshot.path, pre_excluded=snapshot)
        self.assertEqual(report["eligible_files"], 3)
        self.assertEqual(report["total_seen_files"], report["eligible_files"] + report["excluded_files"])

    def test_extraction_never_uses_tar_extract_and_copies_in_one_megabyte_blocks(self):
        archive = build_archive([("src/app.py", b"x\n", "file")])
        copy = MagicMock(wraps=cloner.shutil.copyfileobj)
        refuse = MagicMock(side_effect=AssertionError("tar extraction must never be used"))
        with patch(URLOPEN_TARGET, self.serve(archive)), \
                patch.object(cloner.shutil, "copyfileobj", copy), \
                patch.object(tarfile.TarFile, "extract", refuse), \
                patch.object(tarfile.TarFile, "extractall", refuse):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual((Path(snapshot.path) / "src" / "app.py").read_bytes(), b"x\n")
        refuse.assert_not_called()
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(copy.call_args.args[2], 1024 * 1024)

    def test_extraction_directory_is_created_inside_repos_dir(self):
        archive = build_archive([("src/app.py", b"x\n", "file")])
        self.assertFalse(self.repos_dir.exists())
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(Path(snapshot.path).resolve().parent, self.repos_dir.resolve())
        self.assertTrue(Path(snapshot.path).name.startswith("repo-"))

    def test_equal_priority_eviction_drops_the_file_that_sorts_last(self):
        # docs/a.md and docs/b.md share a priority; the selection report would
        # keep docs/a.md first, so the archive evicts docs/b.md.
        archive = build_archive([
            ("docs/a.md", b"a", "file"),
            ("docs/b.md", b"b", "file"),
            ("src/app.py", b"x", "file"),
        ])
        with patch.object(settings, "max_repository_files", 2), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        self.assertTrue((root / "docs" / "a.md").is_file())
        self.assertTrue((root / "src" / "app.py").is_file())
        self.assertFalse((root / "docs" / "b.md").exists())
        self.assertEqual(snapshot.pre_excluded_paths, ["docs/b.md"])

    def test_missing_commit_comment_and_unsafe_comment_are_ignored(self):
        for comment in (None, "not a sha; rm -rf"):
            with self.subTest(comment=comment):
                archive = build_archive([("a.py", b"x\n", "file")], comment=comment)
                with patch(URLOPEN_TARGET, self.serve(archive)):
                    snapshot = fetch_repository_archive("octocat/Hello-World")
                self.assertIsNone(snapshot.commit_sha)

    def test_codeload_base_url_setting_is_used(self):
        opener = self.serve(build_archive([("a.py", b"x\n", "file")]))
        with patch.object(settings, "github_codeload_base_url", "https://mirror.test/"), patch(URLOPEN_TARGET, opener):
            fetch_repository_archive("git@github.com:CalCom/Cal.com.git")
        self.assertEqual(opener.call_args.args[0].full_url, "https://mirror.test/CalCom/Cal.com/tar.gz/HEAD")

    def test_heap_eviction_keeps_the_higher_priority_file(self):
        archive = build_archive([
            ("docs/guide.md", b"d" * 50, "file"),
            ("src/app.py", b"a" * 50, "file"),
        ])
        with patch.object(settings, "max_repository_bytes", 60), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        self.assertTrue((root / "src" / "app.py").is_file())
        self.assertFalse((root / "docs" / "guide.md").exists())
        self.assertEqual(snapshot.pre_excluded_reasons, {"repository_budget": 1})
        self.assertEqual(snapshot.pre_excluded_paths, ["docs/guide.md"])
        self.assertEqual(snapshot.pre_excluded_bytes, 50)
        report = get_file_selection_report(snapshot.path, pre_excluded=snapshot)
        self.assertEqual(report["candidate_files"], 2)
        self.assertEqual(report["budget_omitted_files"], 1)
        self.assertEqual(report["eligible_files"], 1)

    def test_lower_priority_file_is_not_extracted_when_the_budget_is_full(self):
        archive = build_archive([
            ("src/app.py", b"a" * 50, "file"),
            ("docs/guide.md", b"d" * 50, "file"),
            ("src/other.py", b"o" * 50, "file"),
        ])
        with patch.object(settings, "max_repository_bytes", 60), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        self.assertTrue((root / "src" / "app.py").is_file())
        # Lower priority: never written. Equal priority: never displaces a kept file.
        self.assertFalse((root / "docs").exists())
        self.assertFalse((root / "src" / "other.py").exists())
        self.assertEqual(snapshot.pre_excluded_reasons, {"repository_budget": 2})

    def test_file_count_budget_evicts_several_lower_priority_files(self):
        archive = build_archive([
            ("docs/a.md", b"a", "file"),
            ("docs/b.md", b"b", "file"),
            ("package.json", b"{}", "file"),
            ("src/app.py", b"x", "file"),
        ])
        with patch.object(settings, "max_repository_files", 2), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        self.assertTrue((root / "package.json").is_file())
        self.assertTrue((root / "src" / "app.py").is_file())
        self.assertEqual(sorted(snapshot.pre_excluded_paths), ["docs/a.md", "docs/b.md"])

    def test_file_larger_than_the_whole_budget_is_skipped(self):
        archive = build_archive([("docs/a.md", b"a" * 10, "file"), ("src/huge.py", b"h" * 100, "file")])
        with patch.object(settings, "max_repository_bytes", 50), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertTrue((Path(snapshot.path) / "docs" / "a.md").is_file())
        self.assertEqual(snapshot.pre_excluded_paths, ["src/huge.py"])

    def test_duplicate_entries_do_not_overwrite_accepted_files(self):
        archive = build_archive([("src/app.py", b"first\n", "file"), ("src/app.py", b"second\n", "file")])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual((Path(snapshot.path) / "src" / "app.py").read_bytes(), b"first\n")
        self.assertEqual(snapshot.pre_excluded_reasons, {"unsafe_path": 1})

    def test_excluded_paths_are_capped(self):
        archive = build_archive([(f"img/{index}.png", b"x", "file") for index in range(5)])
        with patch.object(settings, "excluded_paths_limit", 2), patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(snapshot.pre_excluded_paths, ["img/0.png", "img/1.png"])
        self.assertEqual(snapshot.pre_excluded_reasons, {"unsupported_format": 5})
        with patch.object(settings, "excluded_paths_limit", 2):
            report = get_file_selection_report(snapshot.path, pre_excluded=snapshot)
        self.assertTrue(report["excluded_paths_truncated"])
        self.assertEqual(report["excluded_files"], 5)

    def test_not_found_raises_clone_error_with_not_found(self):
        error = urllib.error.HTTPError("https://codeload.github.com/x", 404, "Not Found", {}, None)
        with patch(URLOPEN_TARGET, side_effect=error):
            with self.assertRaises(RepositoryCloneError) as raised:
                fetch_repository_archive("octocat/Missing")
        self.assertTrue(raised.exception.not_found)
        self.assertIn("Repository not found", str(raised.exception))
        self.assertEqual(self.leftover_dirs(), [])

    def test_transient_failures_raise_clone_error_without_not_found(self):
        failures = {
            "http_500": dict(side_effect=urllib.error.HTTPError("u", 500, "Server Error", {}, None)),
            "network": dict(side_effect=urllib.error.URLError("connection refused")),
            "timeout": dict(side_effect=TimeoutError("timed out")),
            "corrupt": dict(side_effect=lambda *args, **kwargs: io.BytesIO(b"not a gzip stream")),
            "truncated": dict(side_effect=lambda *args, **kwargs: io.BytesIO(
                build_archive([("a.py", b"x" * 5_000, "file")])[:60]
            )),
        }
        for name, behaviour in failures.items():
            with self.subTest(name=name):
                with patch(URLOPEN_TARGET, **behaviour):
                    with self.assertRaises(RepositoryCloneError) as raised:
                        fetch_repository_archive("octocat/Hello-World")
                self.assertFalse(raised.exception.not_found)
                self.assertNotIn("connection refused", str(raised.exception))
                self.assertEqual(self.leftover_dirs(), [])

    def test_download_limit_raises_validation_error_and_cleans_up(self):
        archive = build_archive([("src/app.py", os.urandom(4_096), "file")])
        with patch.object(settings, "archive_max_download_bytes", 100), patch(URLOPEN_TARGET, self.serve(archive)):
            with self.assertRaises(RepositoryValidationError) as raised:
                fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(str(raised.exception), "Repository archive exceeds the download limit.")
        self.assertEqual(self.leftover_dirs(), [])

    def _raw_extended_header_archive(self, headers: list[tuple[bytes, int]]) -> bytes:
        """gzip a tar stream of extended headers (type, payload size) then one file."""
        stream = io.BytesIO()
        for header_type, size in headers:
            info = tarfile.TarInfo("repo/././@PaxHeader")
            info.type = header_type
            info.size = size
            stream.write(info.tobuf(format=tarfile.USTAR_FORMAT))
            payload = b"8 a=bcd\n" if size == 8 else b"\0" * size
            stream.write(payload + b"\0" * (-size % 512))
        member = tarfile.TarInfo("repo/src/app.py")
        member.size = 2
        stream.write(member.tobuf(format=tarfile.USTAR_FORMAT) + b"x\n" + b"\0" * 510 + b"\0" * 1024)
        return gzip.compress(stream.getvalue())

    def test_oversized_pax_header_is_rejected_before_it_is_buffered(self):
        # 8 MB of zeros gzip to a few KB; tarfile would read the header whole.
        archive = self._raw_extended_header_archive([(tarfile.XHDTYPE, 8 * 1024 * 1024)])
        self.assertLess(len(archive), 100_000)
        with patch(URLOPEN_TARGET, self.serve(archive)):
            with self.assertRaises(RepositoryCloneError) as raised:
                fetch_repository_archive("octocat/Hello-World")
        self.assertFalse(raised.exception.not_found)
        self.assertEqual(self.leftover_dirs(), [])

    def test_oversized_gnu_long_name_is_rejected(self):
        archive = self._raw_extended_header_archive([(tarfile.GNUTYPE_LONGNAME, 4 * 1024 * 1024)])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            with self.assertRaises(RepositoryCloneError):
                fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(self.leftover_dirs(), [])

    def test_long_chain_of_extended_headers_is_rejected_without_recursion_error(self):
        archive = self._raw_extended_header_archive([(tarfile.XHDTYPE, 8)] * 5_000)
        with patch(URLOPEN_TARGET, self.serve(archive)):
            with self.assertRaises(RepositoryCloneError):
                fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(self.leftover_dirs(), [])

    def test_short_chain_of_extended_headers_is_still_accepted(self):
        archive = self._raw_extended_header_archive([(tarfile.XHDTYPE, 8)] * 3)
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        try:
            self.assertTrue(os.path.isfile(os.path.join(snapshot.path, "src", "app.py")))
        finally:
            cloner.cleanup_repo(snapshot.path)

    def test_overall_deadline_raises_clone_error(self):
        archive = build_archive([("src/app.py", b"x\n", "file")])
        with patch.object(settings, "source_fetch_timeout_seconds", 0), patch(URLOPEN_TARGET, self.serve(archive)) as opener:
            with self.assertRaises(RepositoryCloneError) as raised:
                fetch_repository_archive("octocat/Hello-World")
        self.assertFalse(raised.exception.not_found)
        self.assertIn("too long", str(raised.exception))
        # A zero socket timeout would make the socket non-blocking.
        self.assertEqual(opener.call_args.kwargs["timeout"], 1)
        self.assertEqual(self.leftover_dirs(), [])

    def test_invalid_url_is_rejected_before_any_download(self):
        with patch(URLOPEN_TARGET) as opener:
            with self.assertRaises(RepositoryValidationError):
                fetch_repository_archive("https://evil.test/a/b")
        opener.assert_not_called()

    def test_unsafe_archive_names_are_detected(self):
        top = "octocat-Hello-World-abc123"
        self.assertEqual(cloner._archive_relative_path(f"{top}/src/app.py"), ("src/app.py", False))
        self.assertEqual(cloner._archive_relative_path(top), (None, False))
        for name in (
            "", "/etc/passwd", "../evil.py", f"{top}/../../evil.py", f"{top}/a/../b.py", f"{top}/./a.py",
            f"{top}//a.py", f"{top}/a/", f"{top}/a\\b.py", f"{top}/a\x00.py", f"{top}/bad\udc80.py",
        ):
            with self.subTest(name=name):
                self.assertEqual(cloner._archive_relative_path(name), (None, True))
        self.assertEqual(cloner._display_path("a\x00b\udc80\n.py"), "a?b??.py")

    def test_empty_path_component_entry_is_counted_as_unsafe(self):
        archive = build_archive([
            ("octocat-Hello-World-abc123/src//double.py", b"x\n", "raw"),
            ("src/ok.py", b"x\n", "file"),
        ])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(snapshot.pre_excluded_reasons, {"unsafe_path": 1})
        self.assertEqual(snapshot.total_seen_files, 2)
        self.assertTrue((Path(snapshot.path) / "src" / "ok.py").is_file())

    def test_every_truncated_download_fails_instead_of_indexing_a_partial_tree(self):
        # tarfile reports a header cut off mid-stream as a clean end of
        # archive, and a Content-Length response that closes early returns a
        # short body without error; both must fail rather than index a
        # partial (or empty) repository. Only the 8-byte gzip trailer
        # (CRC and length) may be missing, because all data precedes it.
        archive = build_archive([
            ("src/app.py", b"print('hello')\n" * 20, "file"),
            ("README.md", b"# Hello\n" * 10, "file"),
        ])
        for length in range(len(archive) - 8):
            truncated = archive[:length]
            with self.subTest(length=length):
                with patch(URLOPEN_TARGET, side_effect=lambda *a, _data=truncated, **k: io.BytesIO(_data)):
                    with self.assertRaises(RepositoryCloneError) as raised:
                        fetch_repository_archive("octocat/Hello-World")
                self.assertFalse(raised.exception.not_found)
                self.assertEqual(self.leftover_dirs(), [])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual(snapshot.total_seen_files, 2)
        self.assertEqual(snapshot.downloaded_bytes, len(archive))

    def test_gzip_header_cut_off_is_a_clone_error_not_a_type_error(self):
        archive = build_archive([("src/app.py", b"x\n", "file")])
        # Three bytes leave tarfile's gzip parser calling ord() on b"".
        for length in (1, 3, 4, 9):
            with self.subTest(length=length):
                with patch(URLOPEN_TARGET, side_effect=lambda *a, _n=length, **k: io.BytesIO(archive[:_n])):
                    with self.assertRaises(RepositoryCloneError) as raised:
                        fetch_repository_archive("octocat/Hello-World")
                self.assertEqual(str(raised.exception), "The repository archive could not be read.")

    def test_negative_member_size_rejects_the_archive(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
            top = tarfile.TarInfo("top")
            top.type = tarfile.DIRTYPE
            tar.addfile(top)
            for name, payload in (("top/a.py", b"abcd"), ("top/b.py", b"xyz")):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        raw = bytearray(buffer.getvalue())
        header = 512  # the a.py header follows the top directory header
        raw[header + 124:header + 136] = b"\xff" * 11 + b"\x00"  # base-256 negative size
        raw[header + 148:header + 156] = b" " * 8
        raw[header + 148:header + 156] = ("%06o\0 " % sum(raw[header:header + 512])).encode()
        archive = gzip.compress(bytes(raw))
        with patch(URLOPEN_TARGET, self.serve(archive)):
            with self.assertRaises(RepositoryCloneError) as raised:
                fetch_repository_archive("octocat/Hello-World")
        self.assertFalse(raised.exception.not_found)
        self.assertEqual(self.leftover_dirs(), [])

    def test_network_failure_mid_stream_is_an_interrupted_download(self):
        archive = build_archive([("src/app.py", os.urandom(20_000), "file")])
        failures = (ConnectionResetError("reset by peer"), TimeoutError("timed out"), http.client.IncompleteRead(b""))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                stream = MagicMock()
                stream.__enter__.return_value = stream
                stream.read.side_effect = [archive[:2_000], failure]
                with patch(URLOPEN_TARGET, return_value=stream):
                    with self.assertRaises(RepositoryCloneError) as raised:
                        fetch_repository_archive("octocat/Hello-World")
                self.assertFalse(raised.exception.not_found)
                self.assertEqual(str(raised.exception), "The repository archive download was interrupted.")
                self.assertEqual(self.leftover_dirs(), [])

    def test_local_disk_errors_propagate_unchanged(self):
        # A full disk is not a download failure: it must not be relabelled
        # as one, and retrying through git would only fill the disk again.
        archive = build_archive([("src/app.py", b"x\n", "file")])
        disk_full = OSError(errno.ENOSPC, "No space left on device")
        with patch(URLOPEN_TARGET, self.serve(archive)), patch.object(cloner.shutil, "copyfileobj", side_effect=disk_full):
            with self.assertRaises(OSError) as raised:
                fetch_repository_archive("octocat/Hello-World")
        self.assertNotIsInstance(raised.exception, RepositoryCloneError)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(self.leftover_dirs(), [])
        with patch(URLOPEN_TARGET, self.serve(archive)), \
                patch.object(cloner.shutil, "copyfileobj", side_effect=disk_full), \
                patch("ingest.cloner.clone_repo_shallow") as clone:
            with self.assertRaises(OSError):
                acquire_repository_source("octocat/Hello-World")
        clone.assert_not_called()

    def test_entry_colliding_with_an_existing_path_never_overwrites_it(self):
        archive = build_archive([("README.md", b"second\n", "file")])
        with tempfile.TemporaryDirectory() as destination:
            # Models a case-insensitive file system, where "readme.md" was
            # already written and "README.md" names the same file.
            Path(destination, "README.md").write_bytes(b"first\n")
            ledger = cloner._ExclusionLedger(10)
            budget = cloner._ExtractionBudget(destination, ledger)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as tar:
                tar.next()  # the archive's top directory
                budget.offer(tar, tar.next(), "README.md")
            self.assertEqual(Path(destination, "README.md").read_bytes(), b"first\n")
        self.assertEqual(ledger.counts(), {"unsafe_path": 1})
        self.assertEqual(budget.kept_bytes, 0)

    def test_case_colliding_entries_follow_the_file_system(self):
        archive = build_archive([("readme.md", b"lower\n", "file"), ("README.md", b"upper\n", "file")])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        root = Path(snapshot.path)
        names = sorted(os.listdir(root))
        if (root / "README.md").exists() and names == ["readme.md"]:
            # Case-insensitive file system: the first entry is kept intact.
            self.assertEqual((root / "readme.md").read_bytes(), b"lower\n")
            self.assertEqual(snapshot.pre_excluded_reasons, {"unsafe_path": 1})
        else:
            self.assertEqual(names, ["README.md", "readme.md"])
            self.assertEqual(snapshot.pre_excluded_reasons, {})
        self.assertEqual(snapshot.total_seen_files, 2)

    def test_file_where_a_directory_was_extracted_is_skipped(self):
        archive = build_archive([("src/x.py/y.py", b"y\n", "file"), ("src/x.py", b"clash\n", "file")])
        with patch(URLOPEN_TARGET, self.serve(archive)):
            snapshot = fetch_repository_archive("octocat/Hello-World")
        self.assertEqual((Path(snapshot.path) / "src" / "x.py" / "y.py").read_bytes(), b"y\n")
        self.assertEqual(snapshot.pre_excluded_reasons, {"unsafe_path": 1})
        self.assertEqual(snapshot.pre_excluded_paths, ["src/x.py"])
        self.assertEqual(snapshot.total_seen_files, 2)

    def test_snapshot_defaults_are_independent(self):
        first = RepositorySnapshot(path="a", commit_sha=None, source="git")
        second = RepositorySnapshot(path="b", commit_sha=None, source="git")
        first.pre_excluded_reasons["hidden"] = 1
        first.pre_excluded_paths.append(".env")
        self.assertEqual((second.pre_excluded_reasons, second.pre_excluded_paths), ({}, []))
        self.assertEqual((second.total_seen_files, second.pre_excluded_bytes, second.downloaded_bytes), (0, 0, 0))

    def test_clone_error_defaults_to_not_found_false(self):
        self.assertFalse(RepositoryCloneError("boom").not_found)
        self.assertTrue(RepositoryCloneError("missing", not_found=True).not_found)


class CloneRepoShallowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repos_dir = Path(self._tmp.name) / "repos_temp"
        patcher = patch.object(cloner, "REPOS_DIR", self.repos_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clones_the_canonical_url_into_a_unique_directory(self):
        with patch("ingest.cloner.Repo.clone_from") as clone_from:
            path = cloner.clone_repo_shallow("https://github.com/octocat/Hello-World/tree/main")
        self.assertTrue(Path(path).is_relative_to(self.repos_dir))
        self.assertEqual(clone_from.call_args.args[:2], (CANONICAL, path))
        self.assertEqual(clone_from.call_args.kwargs["depth"], 1)

    def test_git_failure_raises_clone_error_and_removes_the_directory(self):
        from git import GitCommandError
        with patch("ingest.cloner.Repo.clone_from", side_effect=GitCommandError("clone", 128)):
            with self.assertRaises(RepositoryCloneError) as raised:
                cloner.clone_repo_shallow("octocat/Hello-World")
        self.assertFalse(raised.exception.not_found)
        self.assertEqual(os.listdir(self.repos_dir), [])

    def test_missing_git_executable_is_a_clone_error(self):
        from git import GitCommandNotFound
        with patch("ingest.cloner.Repo.clone_from", side_effect=GitCommandNotFound("git", "not found")):
            with self.assertRaises(RepositoryCloneError) as raised:
                cloner.clone_repo_shallow("octocat/Hello-World")
        self.assertIn("Git is not available", str(raised.exception))
        self.assertEqual(os.listdir(self.repos_dir), [])
        with patch.object(cloner, "Repo", None):
            with self.assertRaises(RepositoryCloneError):
                cloner.clone_repo_shallow("octocat/Hello-World")

    def test_module_imports_and_selects_files_without_gitpython(self):
        # GitPython raises ImportError at import time when no git executable
        # is installed; the archive path must still be importable and usable.
        import importlib.util
        import sys
        alias = "ingest._cloner_without_git"
        spec = importlib.util.spec_from_file_location(alias, cloner.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"git": None, alias: module}):
            spec.loader.exec_module(module)
        self.assertIsNone(module.Repo)
        with patch.object(module, "REPOS_DIR", self.repos_dir):
            with self.assertRaises(module.RepositoryCloneError) as raised:
                module.clone_repo_shallow("octocat/Hello-World")
            self.assertIn("Git is not available", str(raised.exception))
            with patch(URLOPEN_TARGET, side_effect=urllib.error.URLError("down")):
                with self.assertRaises(module.RepositoryCloneError) as raised:
                    module.acquire_repository_source("octocat/Hello-World")
            # The archive failure is reported, not a pointless clone attempt.
            self.assertEqual(str(raised.exception), "Could not download the repository archive from GitHub.")
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(Path(tmp), {"src/app.ts": "x\n", "image.png": b"\x89PNG"})
            report = module.get_file_selection_report(tmp)
        self.assertEqual(report["eligible_files"], 1)
        self.assertEqual(report["excluded_reasons"], {"unsupported_format": 1})


class AcquireRepositorySourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(cloner, "REPOS_DIR", Path(self._tmp.name) / "repos_temp")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_archive_snapshot_when_download_succeeds(self):
        archive = build_archive([("src/app.py", b"x\n", "file")])
        with patch(URLOPEN_TARGET, side_effect=lambda *a, **k: io.BytesIO(archive)), \
                patch("ingest.cloner.clone_repo_shallow") as clone:
            snapshot = acquire_repository_source("https://github.com/octocat/Hello-World/tree/main")
        clone.assert_not_called()
        self.assertEqual(snapshot.source, "archive")
        self.assertEqual(snapshot.commit_sha, "abc123")

    def test_falls_back_to_git_clone_on_a_network_error(self):
        clone_path = os.path.join(self._tmp.name, "clone")
        os.makedirs(clone_path)
        repo = MagicMock()
        repo.head.commit.hexsha = "def456"
        with patch(URLOPEN_TARGET, side_effect=urllib.error.URLError("down")), \
                patch("ingest.cloner.clone_repo_shallow", return_value=clone_path) as clone, \
                patch("ingest.cloner.Repo", return_value=repo), \
                self.assertLogs("ingest.cloner", level="WARNING") as logs:
            snapshot = acquire_repository_source("octocat/Hello-World")
        self.assertIn("falling back to git clone", logs.output[0])
        clone.assert_called_once_with(CANONICAL)
        self.assertEqual((snapshot.path, snapshot.commit_sha, snapshot.source), (clone_path, "def456", "git"))
        self.assertEqual(snapshot.total_seen_files, 0)

    def test_git_fallback_without_readable_head_has_no_commit(self):
        clone_path = os.path.join(self._tmp.name, "clone")
        os.makedirs(clone_path)
        with patch(URLOPEN_TARGET, side_effect=TimeoutError()), \
                patch("ingest.cloner.clone_repo_shallow", return_value=clone_path), \
                self.assertLogs("ingest.cloner", level="WARNING"):
            snapshot = acquire_repository_source("octocat/Hello-World")
        self.assertIsNone(snapshot.commit_sha)
        self.assertEqual(snapshot.source, "git")

    def test_does_not_fall_back_when_git_is_unavailable(self):
        with patch(URLOPEN_TARGET, side_effect=urllib.error.URLError("down")), \
                patch.object(cloner, "Repo", None), \
                patch("ingest.cloner.clone_repo_shallow") as clone:
            with self.assertRaises(RepositoryCloneError) as raised:
                acquire_repository_source("octocat/Hello-World")
        clone.assert_not_called()
        self.assertFalse(raised.exception.not_found)
        self.assertEqual(str(raised.exception), "Could not download the repository archive from GitHub.")

    def test_does_not_fall_back_on_not_found(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with patch(URLOPEN_TARGET, side_effect=error), patch("ingest.cloner.clone_repo_shallow") as clone:
            with self.assertRaises(RepositoryCloneError) as raised:
                acquire_repository_source("octocat/Missing")
        self.assertTrue(raised.exception.not_found)
        clone.assert_not_called()

    def test_does_not_fall_back_on_validation_errors(self):
        archive = build_archive([("src/app.py", os.urandom(4_096), "file")])
        with patch.object(settings, "archive_max_download_bytes", 100), \
                patch(URLOPEN_TARGET, side_effect=lambda *a, **k: io.BytesIO(archive)), \
                patch("ingest.cloner.clone_repo_shallow") as clone:
            with self.assertRaises(RepositoryValidationError):
                acquire_repository_source("octocat/Hello-World")
        clone.assert_not_called()
        with patch("ingest.cloner.clone_repo_shallow") as clone, patch(URLOPEN_TARGET) as opener:
            with self.assertRaises(RepositoryValidationError):
                acquire_repository_source("https://gitlab.com/a/b")
        clone.assert_not_called()
        opener.assert_not_called()

    def test_cleanup_repo_refuses_paths_outside_repos_dir(self):
        with self.assertRaises(ValueError):
            cloner.cleanup_repo(self._tmp.name)
        inside = cloner.REPOS_DIR / "repo-x"
        inside.mkdir(parents=True)
        cloner.cleanup_repo(str(inside))
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
