"""Tests for declaration-aware chunking with exact line ranges."""

import io
import random
import re
import tempfile
import time
import unittest
from pathlib import Path

from ingest.chunker import (
    BOUNDARY_REGEX,
    CHUNK_OVERLAP,
    MAX_CHARS_PER_CHUNK,
    MAX_LINES_PER_CHUNK,
    STREAMING_FILE_THRESHOLD,
    SYMBOL_REGEX,
    TARGET_CHARS,
    TARGET_LINES,
    chunk_file,
    chunk_file_content,
    extract_symbols,
    is_probably_minified,
    iter_chunk_file,
    iter_streaming_chunks,
    split_by_characters,
    split_by_lines,
)

CHUNK_KEYS = {"file_path", "start_line", "end_line", "content", "language", "symbols"}


def body(count: int, indent: str = "    ", width: int = 0) -> list[str]:
    """Distinct, non-declaration body lines (optionally padded to ``width``)."""
    lines = []
    for index in range(count):
        line = f"{indent}value_{index} = compute({index})"
        lines.append(line.ljust(width, "x") if width else line)
    return lines


def ranges(chunks: list[dict]) -> list[tuple[int, int]]:
    return [(chunk["start_line"], chunk["end_line"]) for chunk in chunks]


class ChunkAssertions(unittest.TestCase):
    def assert_exact_chunks(self, content: str, chunks: list[dict], language: str | None = None) -> None:
        """The invariant every line-based chunk must satisfy, plus full coverage."""
        lines = content.splitlines()
        self.assertTrue(chunks, "a non-blank file must produce chunks")
        previous_start = 0
        covered: set[int] = set()
        for chunk in chunks:
            self.assertEqual(set(chunk), CHUNK_KEYS)
            start, end = chunk["start_line"], chunk["end_line"]
            self.assertTrue(1 <= start <= end <= len(lines), (start, end, len(lines)))
            self.assertEqual(chunk["content"], "\n".join(lines[start - 1:end]), (start, end))
            self.assertTrue(chunk["content"].strip(), "whitespace-only chunks must be dropped")
            self.assertLessEqual(len(chunk["content"]), MAX_CHARS_PER_CHUNK)
            self.assertLessEqual(end - start + 1, 200)
            self.assertGreaterEqual(start, previous_start, "chunks must stay in file order")
            self.assertEqual(chunk["symbols"], extract_symbols(chunk["content"]))
            if language is not None:
                self.assertEqual(chunk["language"], language)
            previous_start = start
            covered.update(range(start, end + 1))
        uncovered = [number for number, line in enumerate(lines, 1) if line.strip() and number not in covered]
        self.assertEqual(uncovered, [], "every non-blank line must be covered")


# --- Sample files -------------------------------------------------------------

def typescript_sample() -> str:
    lines = ['import { memo, forwardRef } from "react";', 'import type { Store } from "./store";', ""]
    for index in range(36):
        lines += [
            f"export interface Props{index} {{",
            "  label: string;",
            "  onClick?: (event: MouseEvent) => void;",
            "}",
            "",
            f"export const Button{index} = memo(({{ label }}: Props{index}) => {{",
            *[f"  const derived{n} = label.length + {n};" for n in range(index % 9)],
            "  if (!label) {",
            "    return null;",
            "  }",
            "  return label;",
            "});",
            "",
            f"export class Store{index} {{",
            "  private items: string[] = [];",
            "  add(item: string) {",
            "    for (const existing of this.items) {",
            "      if (existing === item) return;",
            "    }",
            "    this.items.push(item);",
            "  }",
            "}",
            "",
            f"export type Mapping{index} = Record<string, Props{index}>;",
            "",
        ]
    return "\n".join(lines) + "\n"


def python_sample() -> str:
    lines = ['"""Service module."""', "", "import os", "from functools import wraps", ""]
    for index in range(30):
        lines += [
            "@dataclass",
            f"class Model{index}:",
            "    name: str",
            "",
            "    @property",
            "    def label(self):",
            *[f"        part_{n} = self.name[{n}:]" for n in range(index % 11)],
            "        return self.name",
            "",
            "",
            "@app.route(",
            f'    "/items/{index}",',
            '    methods=["GET"],',
            ")",
            f"async def handler_{index}(request):",
            "    if request is None:",
            "        return None",
            "    return request",
            "",
        ]
    # One oversized function forces the split_by_lines path.
    lines += ["def oversized():"] + body(260) + ["    return None", ""]
    return "\n".join(lines) + "\n"


def go_sample() -> str:
    lines = ["package store", "", 'import "context"', ""]
    for index in range(30):
        lines += [
            f"type Repo{index} struct {{",
            "\tdb *DB",
            "}",
            "",
            f"func (r *Repo{index}) Save(ctx context.Context, item Item) error {{",
            *[f"\tif err := r.db.Step{n}(ctx); err != nil {{ return err }}" for n in range(index % 8)],
            "\treturn nil",
            "}",
            "",
            f"func NewRepo{index}(db *DB) *Repo{index} {{",
            f"\treturn &Repo{index}{{db: db}}",
            "}",
            "",
        ]
    return "\n".join(lines) + "\n"


def rust_sample() -> str:
    lines = ["use std::fmt;", ""]
    for index in range(30):
        lines += [
            "#[derive(Debug, Clone)]",
            f"pub struct Point{index} {{",
            "    x: i64,",
            "    y: i64,",
            "}",
            "",
            f"impl fmt::Display for Point{index} {{",
            "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {",
            '        write!(f, "({}, {})", self.x, self.y)',
            "    }",
            "}",
            "",
            f"pub(crate) fn distance{index}(a: &Point{index}, b: &Point{index}) -> i64 {{",
            *[f"    let step{n} = a.x - b.x + {n};" for n in range(index % 10)],
            "    (a.x - b.x).abs() + (a.y - b.y).abs()",
            "}",
            "",
        ]
    lines += ["macro_rules! square {", "    ($x:expr) => { $x * $x };", "}", ""]
    return "\n".join(lines) + "\n"


def sql_sample() -> str:
    lines = ["-- schema", ""]
    for index in range(40):
        create = "CREATE TABLE" if index % 2 else "create table if not exists"
        lines += [
            f'{create} "public"."table_{index}" (',
            "    id BIGSERIAL PRIMARY KEY,",
            *[f"    column_{n} TEXT NOT NULL DEFAULT ''," for n in range(index % 7)],
            "    created_at TIMESTAMPTZ DEFAULT now()",
            ");",
            f"CREATE INDEX idx_table_{index}_created ON table_{index}(created_at);",
            "",
        ]
    lines += ["CREATE OR REPLACE FUNCTION app.touch() RETURNS trigger AS $$", "BEGIN", "  RETURN NEW;", "END;", "$$;", ""]
    return "\n".join(lines) + "\n"


def markdown_sample() -> str:
    lines = ["# Project", ""]
    for index in range(12):
        lines += [f"## Section {index}", ""]
        lines += [f"Paragraph line {n} of section {index} explains the behaviour." for n in range(20 + index)]
        lines += ["", "```bash", "# this is a shell comment, not a heading", "make test", "```", ""]
        lines += [f"### Details {index}", "", "#### Not a boundary", "More text.", ""]
    return "\n".join(lines) + "\n"


def generated_sample(line_count: int = 1000) -> str:
    """A deterministic but irregular Python file mixing every structural case."""
    rng = random.Random(1_000)
    lines: list[str] = ["import os", ""]
    function_index = 0
    while len(lines) < line_count:
        kind = rng.choice(["def", "def", "class", "decorated", "blank", "comment", "long"])
        function_index += 1
        if kind == "def":
            lines += [f"def function_{function_index}(arg):"] + body(rng.randint(1, 40)) + [""]
        elif kind == "class":
            lines += [f"class Klass{function_index}:"]
            for method in range(rng.randint(1, 4)):
                lines += [f"    def method_{method}(self):"] + body(rng.randint(1, 15), indent="        ")
            lines += [""]
        elif kind == "decorated":
            lines += ["@decorator", f"@other({function_index})", f"def decorated_{function_index}():"]
            lines += body(rng.randint(1, 20)) + [""]
        elif kind == "blank":
            lines += [""] * rng.randint(1, 30)
        elif kind == "comment":
            lines += [f"# note {function_index}: " + "explanation " * rng.randint(1, 20)]
        else:
            lines += [f"def long_{function_index}():"] + body(rng.randint(5, 30), width=rng.randint(80, 300)) + [""]
        if function_index == 7:
            # Guarantee at least one segment that must be split into windows.
            lines += ["def enormous():"] + body(320) + [""]
    return "\n".join(lines[:line_count]) + "\n"


# --- Boundaries ---------------------------------------------------------------

class BoundaryRegexTests(unittest.TestCase):
    def assert_boundaries(self, lines: list[str]) -> None:
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNotNone(BOUNDARY_REGEX.match(line))

    def test_javascript_and_typescript_declarations(self):
        self.assert_boundaries([
            "function foo() {", "function* gen() {", "function *gen() {", "async function load() {",
            "class Foo {", "abstract class Base {", "interface Props {", "enum Color {", "type Props = {",
            "const add = (a, b) => a + b;", "let add = (a, b) => a + b;", "var add = (a, b) => a + b;",
            "const signIn = async (user) => {", "const handler: Handler = async (req, res) => {",
            "const add = (a: number, b: number): number => a + b;", "const inc = x => x + 1",
            "const Foo = memo(", "const Foo = forwardRef(", "const Foo = React.memo(",
            "export const Input = forwardRef<HTMLInputElement, Props>((props, ref) => {",
            "export const Button = ({", "export const Button = ({ label }: Props) => {",
            "const Foo: React.FC<Props> = ({ a }) => {", "const fn: (a: number) => void = (a) => {}",
            "export function foo() {", "export default function App() {", "export default async function h() {",
            "export default class Page extends Component {", "export abstract class Base<T> {",
            "export interface Props extends Base {", "export enum Color {", "export const enum Dir {",
            "export type Props<T> = {", "export default const X = () => 1",
            "declare function foo(): void;", "declare class Foo {", "declare enum E {", "declare type X = number;",
            "declare const x = () => 1", "export declare function bar(x: number): string;",
            # Anonymous default exports are the only legal unnamed forms.
            "export default function () {", "export default function() {", "export default async function () {",
            "export default function* () {", "export default class {", "export default class extends Base {",
            # Parameters (or an inline props type) continuing on later lines.
            "export const Button = (", "export const Button = ({ label, onClick }: {",
        ])

    def test_python_go_and_rust_declarations(self):
        self.assert_boundaries([
            "def foo():", "async def foo():", "class Foo:", "class Foo(Base):",
            "func main() {", "func (r *Repo) Save(ctx context.Context) error {", "func Map[T any](xs []T) []T {",
            "type Repo struct {", "type ID string", "type (",
            "fn main() {", "pub fn new() -> Self {", "pub(crate) fn helper() {", "pub(super) async fn x() {",
            "async fn run() {", "pub async fn run() {", "const fn square(x: u32) -> u32 {",
            'pub unsafe extern "C" fn cb() {', "struct Point {", "pub struct Point<T> {", "pub(crate) enum Kind {",
            "trait Shape {", "pub trait Shape: Send {", "impl Foo {", "impl<T> Foo<T> {",
            "impl fmt::Display for Point {", "unsafe impl Send for Foo {}", "mod tests {", "pub mod api;",
            "macro_rules! my_macro {",
        ])

    def test_kotlin_and_modifier_led_jvm_dotnet_swift_php_declarations(self):
        self.assert_boundaries([
            "fun main() {", "class Repo {", "object Singleton {", "interface Repo {", "data class User(val id: Int)",
            "private fun helper() {", "suspend fun load() {", "sealed class Result<out T> {", "enum class Color {",
            "internal object Foo {",
            "public class Foo {", "public final class Foo {", "private interface Bar {", "protected enum E {",
            "internal struct S {", "public record Point(int x, int y) {", "static class Holder {",
            "abstract class X {", "open class Base {", "final class Foo {", "public func foo() {",
            "public function handle($request) {", "open fun x() {", "private fun x() {",
            "public static void main(String[] args) {", "private int compute(int a) {",
            "public async Task<int> GetAsync() {", "static int helper(void) {", "static char *dup(const char *s) {",
            "public Foo(int x) {", "public static <T> List<T> of(T... xs) {", "protected abstract void run();",
        ])

    def test_ruby_sql_prisma_graphql_and_proto_declarations(self):
        self.assert_boundaries([
            "def foo", "def self.call(x)", "class Foo < Bar", "module Foo", "module Foo::Bar",
            "CREATE TABLE users (", "create table users (", "Create Table users (",
            "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$", "ALTER TABLE users ADD COLUMN x int;",
            "alter table users drop column x;", "CREATE UNIQUE INDEX idx ON users(email);", "create index idx on t(x);",
            "create view v as", "CREATE MATERIALIZED VIEW stats AS", "CREATE TRIGGER t BEFORE INSERT",
            "CREATE TYPE mood AS ENUM ('a');", "create function f() returns int",
            "ALTER VIEW v RENAME TO w;", "alter index idx rename to idx2;", "ALTER FUNCTION f() OWNER TO app;",
            "alter trigger t on users rename to t2;", "ALTER TYPE mood ADD VALUE 'b';",
            "model User {", "enum Role {", "generator client {", "datasource db {",
            "type Query {", "message User {", "service Auth {", "input CreateUser {", "extend type Query {",
            # The opening brace may be on the next line.
            "message User", "service Auth", "model User", "message User  ",
            "union SearchResult = Photo | Person", "union Value {", "pub union Raw<T> {",
        ])

    def test_sql_set_operators_and_bare_keywords_are_not_boundaries(self):
        # ``union all`` at column 0 continues a query; splitting there would
        # cut one statement in half.
        for line in [
            "union all", "union", "union select id from archived", "union distinct", "union all select 1",
            "message", "message = build()", "service.start()", "model.fit(x)", "input = read()",
            "function_map = {}", "classify(x)", "class_ = None", "classes = {}",
        ]:
            with self.subTest(line=line):
                self.assertIsNone(BOUNDARY_REGEX.match(line))

    def test_property_delegates_are_not_method_signatures(self):
        # ``by viewModels()`` follows a property name, so it is not a method.
        for line in [
            "private val viewModel by viewModels<MainViewModel>()",
            "private val binding by lazy(LazyThreadSafetyMode.NONE) {",
            "public var state by mutableStateOf(0)",
            "private let queue = DispatchQueue(label: \"q\")",
        ]:
            with self.subTest(line=line):
                self.assertIsNone(BOUNDARY_REGEX.match(line))
                self.assertEqual(extract_symbols(line), [])
                self.assertEqual(extract_symbols("    " + line), [])

    def test_indented_and_non_declaration_lines_are_not_boundaries(self):
        for line in [
            "  function foo() {", "    def method(self):", "    async def method(self):", "\tfunc helper() {",
            "  public void run() {", "  const inner = () => {", "    class Nested:", "  fn inner() {",
            "if (x) {", "return foo(", "for (const x of xs) {", "while (true) {", "switch (x) {", "} catch (e) {",
            "new Foo()", "else {", "const x = 5;", "const total = (a + b) * c;", "export { a, b };",
            "export * from './x';", "export default {", "console.log('x')", "import foo from 'foo';",
            "module.exports = foo;", "function_map = {}", "functions = []", "class_name = 'x'", "def_value = 1",
            "type_ = 1", "type(obj)", "object = None", "record = {}", "data = load()", "final_result = 1",
            "public = 1", "private", "static const char *names[] = {",
            "private static final Map<String, X> CACHE = new HashMap<>();", "# Heading", "## Sub",
            "// function foo() {}", "/* class X */", "@decorator", "created_at = now()", "CREATE_TIMEOUT = 5",
            "", "}", "new Vue({", "await foo(bar)", "x = function() {}", "type: string", "enum: [1, 2]",
            "impl_detail = 3", "modules = 1", "model = load()", "message = 'hi'", "createTable(users)",
        ]:
            with self.subTest(line=line):
                self.assertIsNone(BOUNDARY_REGEX.match(line))


class BoundaryChunkingTests(ChunkAssertions):
    def test_only_column_zero_declarations_split_a_file(self):
        lines = ['import { api } from "./api";', ""]
        class_line = len(lines) + 1
        lines += ["export class Service {", "  async load() {"] + body(50, "    ")
        lines += ["  }", "  function helper() {"] + body(50, "    ") + ["  }", "}", ""]
        function_line = len(lines) + 1
        lines += ["export function standalone() {"] + body(100, "  ") + ["}", ""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "src/service.ts")

        self.assert_exact_chunks(content, chunks, "ts")
        # The preamble merges into the class chunk; indented methods never split it.
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, function_line])
        self.assertEqual(chunks[0]["content"].splitlines()[class_line - 1], "export class Service {")
        self.assertEqual(chunks[0]["end_line"], function_line - 1)
        self.assertEqual(chunks[1]["content"].splitlines()[0], "export function standalone() {")

    def test_decorators_move_the_boundary_to_the_first_decorator_line(self):
        lines = ["import os", "", "def first():"] + body(100) + [""]
        decorator_line = len(lines) + 1
        lines += ["@app.route(", '    "/users",', '    methods=["GET"],', ")", "@login_required", "def second():"]
        lines += body(100) + [""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "app/routes.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, decorator_line])
        self.assertTrue(chunks[1]["content"].startswith("@app.route(\n"))
        self.assertEqual(chunks[0]["end_line"], decorator_line - 1)

    def test_typescript_multiline_decorator_belongs_to_its_class(self):
        lines = ["export function setup() {"] + body(100, "  ") + ["}", ""]
        decorator_line = len(lines) + 1
        lines += ["@Component({", "  selector: 'app-root',", "  templateUrl: './app.html',", "})"]
        lines += ["export class AppComponent {"] + body(100, "  ") + ["}", ""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "src/app.component.ts")

        self.assert_exact_chunks(content, chunks, "ts")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, decorator_line])
        self.assertEqual(chunks[1]["symbols"][0], "AppComponent")

    def test_rust_outer_attributes_belong_to_the_following_item(self):
        lines = ["#![allow(dead_code)]", "", "pub fn first() {"] + body(100) + ["}", ""]
        tests_line = len(lines) + 1
        lines += ["#[cfg(test)]", "mod tests {"] + body(100) + ["}", ""]
        derive_line = len(lines) + 1
        lines += ["#[derive(", "    Debug,", "    Clone,", ")]", "pub struct Point {"] + body(100) + ["}", ""]
        inner_line = len(lines) + 1
        # ``#!`` is an inner attribute of the enclosing module, so it is not
        # pulled down into the item below it.
        lines += ["#![cfg(unix)]", "pub mod unix_only {"] + body(100) + ["}", ""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "src/lib.rs")

        self.assert_exact_chunks(content, chunks, "rs")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, tests_line, derive_line, inner_line + 1])
        self.assertTrue(chunks[1]["content"].startswith("#[cfg(test)]\nmod tests {"))
        self.assertEqual(chunks[2]["symbols"], ["Point"])
        # The inner attribute stays at the end of the preceding chunk.
        self.assertEqual(chunks[2]["content"].splitlines()[-1], "#![cfg(unix)]")
        self.assertEqual(chunks[2]["content"].splitlines()[-2], "")
        self.assertTrue(chunks[3]["content"].startswith("pub mod unix_only {"))

    def test_sql_union_all_does_not_split_a_statement(self):
        lines = ["CREATE VIEW all_events AS", "select id, name"] + [f"  , column_{n}" for n in range(70)]
        lines += ["from events", "union all", "select id, name"] + [f"  , column_{n}" for n in range(70)]
        lines += ["from archived_events;", ""]
        view_end = len(lines)
        lines += ["CREATE TABLE audit (", "  id bigint,"] + [f"  note_{n} text," for n in range(60)]
        lines += ["  type text", ");"]
        content = "\n".join(lines) + "\n"
        self.assertGreater(len(lines), 200)

        chunks = chunk_file_content(content, "db/views.sql")

        self.assert_exact_chunks(content, chunks, "sql")
        # The view is 140+ lines, so a wrong ``union all`` split could not be
        # hidden by merging: the halves would come back as two chunks.
        self.assertEqual(ranges(chunks), [(1, view_end), (view_end + 1, len(lines))])
        self.assertEqual(chunks[0]["symbols"], ["all_events"])
        self.assertEqual(chunks[1]["symbols"], ["audit"])

    def test_annotations_on_the_declaration_line_still_make_it_a_boundary(self):
        # Each declaration is long enough that merging cannot hide a missed
        # boundary: neighbours would come back as one oversized segment.
        cases = {
            "app/Models.kt": ("fun first() {", "@Serializable data class Big(", ")"),
            "App/Library.swift": ("func first() {", "@Observable final class Library {", "}"),
            "src/app.component.ts": (
                "export function first() {", "@Component({ selector: 'app-root' }) export class AppComponent {", "}",
            ),
            "src/point.rs": ("pub fn first() {", "#[derive(Debug, Clone)] pub struct Point {", "}"),
        }
        for name, (opener, annotated, closer) in cases.items():
            with self.subTest(file=name):
                lines = [opener] + body(100) + ["}", ""]
                annotated_line = len(lines) + 1
                lines += [annotated] + body(100) + [closer]
                # No blank line: the closing ``}``/``)`` must not carry the
                # annotation over to the next declaration.
                after_line = len(lines) + 1
                lines += ["fun after() {" if name.endswith(".kt") else "func after() {"] + body(100) + ["}"]
                content = "\n".join(lines) + "\n"

                chunks = chunk_file_content(content, name)

                self.assert_exact_chunks(content, chunks)
                self.assertEqual([chunk["start_line"] for chunk in chunks], [1, annotated_line, after_line])
                self.assertEqual(chunks[1]["content"].splitlines()[0], annotated)

    def test_decorator_stack_above_an_annotated_declaration_moves_the_boundary_up(self):
        lines = ["fun first() {"] + body(100) + ["}", ""]
        stack_line = len(lines) + 1
        lines += ["@Serializable", '@SerialName("user") data class User('] + body(100) + [")"]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "app/User.kt")

        self.assert_exact_chunks(content, chunks, "kt")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, stack_line])
        self.assertEqual(chunks[1]["symbols"][0], "User")

    def test_comment_between_stacked_decorators_keeps_them_together(self):
        lines = ["def first():"] + body(100) + [""]
        stack_line = len(lines) + 1
        lines += ["@pytest.mark.parametrize(", '    "x", [1, 2],', ")", "# Flaky on CI; see #12", "@pytest.mark.slow"]
        lines += ["def test_second(x):"] + body(100) + [""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "tests/test_jobs.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, stack_line])

    def test_a_comment_alone_does_not_start_a_decorator_stack(self):
        lines = ["def first():"] + body(100) + ["# helpers below"]
        def_line = len(lines) + 1
        lines += ["def second():"] + body(100) + [""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "app/jobs.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, def_line])

    def test_csharp_attribute_lines_belong_to_the_following_class(self):
        lines = ["namespace App.Controllers;", "", "public class First {"] + body(100) + ["}", ""]
        attribute_line = len(lines) + 1
        lines += ["[ApiController]", '[Route("api/[controller]")]', "public class UsersController : ControllerBase {"]
        lines += body(100) + ["}"]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "Controllers/UsersController.cs")

        self.assert_exact_chunks(content, chunks, "cs")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, attribute_line])
        self.assertEqual(chunks[1]["symbols"], ["UsersController"])

    def test_a_column_zero_list_line_is_not_an_attribute(self):
        # Only a whole-line ``[Name]``/``[Name(...)]`` is a C# attribute.
        lines = ["routes = ["] + [f"    ('/r{n}', handler_{n})," for n in range(100)] + ["[extra, routes],", "]"]
        def_line = len(lines) + 1
        lines += ["def second():"] + body(100) + [""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "app/routes.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, def_line])

    def test_sql_files_split_only_on_sql_statements(self):
        # Hand-written DDL with column-0 columns whose names are declaration
        # keywords elsewhere (record, type, interface, object, module, enum).
        columns = ["record jsonb,", "type text,", "interface varchar(20),", "object text,", "module text,",
                   "enum text,", "message text", "class text,", "def text,"]
        lines = ["CREATE TABLE events ("] + [f"col_{n} text," for n in range(70)] + columns
        lines += [f"more_{n} text," for n in range(70)] + ["id bigint", ");", ""]
        second_line = len(lines) + 1
        lines += ["create table other ("] + [f"col_{n} text," for n in range(70)] + ["id bigint", ");"]
        content = "\n".join(lines) + "\n"

        for name, language in (("db/schema.sql", "sql"), ("db/schema.pgsql", "pgsql")):
            with self.subTest(name=name):
                chunks = chunk_file_content(content, name)
                self.assert_exact_chunks(content, chunks, language)
                self.assertEqual(ranges(chunks), [(1, second_line - 1), (second_line, len(lines))])
                self.assertEqual(chunks[0]["symbols"][0], "events")
                self.assertEqual(chunks[1]["symbols"], ["other"])

    def test_a_blank_line_detaches_a_decorator_from_the_declaration(self):
        lines = ["def first():"] + body(100) + ["@orphan", ""]
        def_line = len(lines) + 1
        lines += ["def second():"] + body(100) + [""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "app/jobs.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual([chunk["start_line"] for chunk in chunks], [1, def_line])

    def test_markdown_headings_split_doc_files_but_not_code(self):
        # Each would-be split below leaves more than TARGET_LINES / 2 lines on
        # both sides, so merging cannot hide a wrongly detected heading.
        lines = ["# Title"] + [f"Intro line {n}." for n in range(65)]
        lines += ["#### Too deep to be a boundary"] + [f"Intro detail {n}." for n in range(65)]
        install_line = len(lines) + 1
        lines += ["## Install"] + [f"Install step {n}." for n in range(65)]
        lines += ["```bash", "# not a heading inside a fence", "## still not a heading", "```"]
        lines += [f"Install detail {n}." for n in range(65)]
        usage_line = len(lines) + 1
        lines += ["### Usage"] + [f"Usage line {n}." for n in range(65)]
        lines += ["~~~", "### fenced with tildes", "~~~"] + [f"Usage detail {n}." for n in range(65)]
        content = "\n".join(lines) + "\n"

        for name, language in (("README.md", "md"), ("docs/page.mdx", "mdx"), ("docs/index.rst", "rst"),
                               ("NOTES.MD", "md")):
            with self.subTest(name=name):
                chunks = chunk_file_content(content, name)
                self.assert_exact_chunks(content, chunks, language)
                self.assertEqual([chunk["start_line"] for chunk in chunks], [1, install_line, usage_line])

        # In source code a leading ``#`` is a comment, so the same text has no
        # boundaries and falls back to overlapping line windows.
        code_chunks = chunk_file_content(content, "notes.py")
        self.assert_exact_chunks(content, code_chunks, "py")
        expected = split_by_lines(content.splitlines(), 1, "notes.py", "py")
        self.assertEqual(ranges(code_chunks), ranges(expected))
        self.assertEqual(code_chunks[1]["start_line"], MAX_LINES_PER_CHUNK - CHUNK_OVERLAP + 1)


# --- Merging ------------------------------------------------------------------

class MergeTests(ChunkAssertions):
    def test_merge_targets_and_public_ceilings(self):
        self.assertEqual((TARGET_LINES, TARGET_CHARS), (120, 6000))
        self.assertEqual((MAX_LINES_PER_CHUNK, CHUNK_OVERLAP, MAX_CHARS_PER_CHUNK), (150, 40, 12000))
        self.assertEqual(STREAMING_FILE_THRESHOLD, 2_000_000)

    def test_small_segments_merge_up_to_target_lines(self):
        lines = []
        for index in range(60):
            lines += [f"function f{index}() {{", f"  const a = {index};", "  return a;", "}", "// end", ""]
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "src/many.js")

        self.assert_exact_chunks(content, chunks, "js")
        # Each segment is 6 lines, so exactly 20 fit in TARGET_LINES.
        self.assertEqual(ranges(chunks), [(1, 120), (121, 240), (241, 360)])
        self.assertTrue(all(chunk["content"].startswith("function f") for chunk in chunks))
        self.assertEqual(chunks[0]["symbols"], [f"f{index}" for index in range(20)])

    def test_merge_stops_before_target_chars(self):
        lines = []
        for index in range(30):
            lines += [f"def wide_{index}():"] + body(9, width=110) + [""]
        content = "\n".join(lines) + "\n"
        segment_lines = 11

        chunks = chunk_file_content(content, "pkg/wide.py")

        self.assert_exact_chunks(content, chunks, "py")
        all_lines = content.splitlines()
        for chunk in chunks:
            self.assertLessEqual(len(chunk["content"]), TARGET_CHARS)
            self.assertLessEqual(chunk["end_line"] - chunk["start_line"] + 1, TARGET_LINES)
            next_end = chunk["end_line"] + segment_lines
            if next_end <= len(all_lines):
                # Greedy: one more segment would have crossed a target.
                grown = "\n".join(all_lines[chunk["start_line"] - 1:next_end])
                self.assertGreater(len(grown), TARGET_CHARS)
        self.assertGreater(len(chunks), 1)

    def test_segment_between_target_and_200_lines_is_emitted_whole(self):
        lines = ["def small():"] + body(30) + [""]
        big_start = len(lines) + 1
        lines += ["def medium():"] + body(160) + [""]
        big_end = len(lines)
        tail_start = len(lines) + 1
        lines += ["def tail():"] + body(30) + [""]
        content = "\n".join(lines) + "\n"
        self.assertGreater(len(lines), 200)

        chunks = chunk_file_content(content, "pkg/medium.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertEqual(ranges(chunks), [(1, big_start - 1), (big_start, big_end), (tail_start, len(lines))])

    def test_segment_over_200_lines_is_split_with_split_by_lines(self):
        lines = ["def small():"] + body(10) + [""]
        big_start = len(lines) + 1
        segment = ["def huge():"] + body(449) + [""]
        lines += segment
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "pkg/huge.py")

        self.assert_exact_chunks(content, chunks, "py")
        expected = split_by_lines(segment, big_start, "pkg/huge.py", "py")
        self.assertEqual(ranges(chunks), [(1, big_start - 1)] + ranges(expected))
        self.assertEqual(ranges(expected)[:2], [(big_start, big_start + 149), (big_start + 110, big_start + 259)])

    def test_segment_over_character_ceiling_is_split_below_the_ceiling(self):
        lines = ["def dense():"] + body(99, width=200) + [""]
        content = "\n".join(lines) + "\n"
        self.assertGreater(len(content), MAX_CHARS_PER_CHUNK)
        self.assertFalse(is_probably_minified(content, content.splitlines()))

        chunks = chunk_file_content(content, "pkg/dense.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertGreater(len(chunks), 1)

    def test_long_but_not_minified_lines_never_exceed_the_ceiling(self):
        lines = []
        for index in range(40):
            lines += [f"def mixed_{index}():", "    " + "y" * 1_900] + body(6) + [""]
        content = "\n".join(lines) + "\n"
        self.assertFalse(is_probably_minified(content, content.splitlines()))

        chunks = chunk_file_content(content, "pkg/mixed.py")

        self.assert_exact_chunks(content, chunks, "py")

    def test_whitespace_only_regions_never_become_chunks(self):
        lines = ["def first():"] + body(5) + [""] * 400 + ["def second():"] + body(5) + [" \t "] * 300
        content = "\n".join(lines) + "\n"

        chunks = chunk_file_content(content, "pkg/sparse.py")

        self.assert_exact_chunks(content, chunks, "py")
        self.assertTrue(all(chunk["content"].strip() for chunk in chunks))


# --- Small files and line splitting --------------------------------------------

class SmallFileTests(ChunkAssertions):
    def test_small_file_is_one_chunk_with_exact_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "app.py"
            file_path.write_text("class Login:\n    pass\n\ndef handle_login():\n    return True\n", encoding="utf-8")
            chunks = chunk_file(str(file_path), str(root))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertEqual(chunks[0]["end_line"], 5)
        self.assertEqual(chunks[0]["symbols"], ["Login", "handle_login"])
        self.assertEqual(chunks[0]["content"], "class Login:\n    pass\n\ndef handle_login():\n    return True")
        self.assertEqual(chunks[0]["file_path"], "app.py")
        self.assertEqual(chunks[0]["language"], "py")

    def test_small_file_limits_are_200_lines_and_the_character_ceiling(self):
        def file_of(line_count: int) -> str:
            lines = []
            while len(lines) < line_count:
                lines += [f"def f{len(lines)}():", "    return 1"]
            return "\n".join(lines[:line_count]) + "\n"

        at_limit = chunk_file_content(file_of(200), "a.py")
        self.assertEqual(ranges(at_limit), [(1, 200)])
        over_limit = chunk_file_content(file_of(201), "a.py")
        self.assertGreater(len(over_limit), 1)
        self.assert_exact_chunks(file_of(201), over_limit, "py")

        wide = "\n".join(body(100, width=130)) + "\n"
        self.assertGreater(len(wide), MAX_CHARS_PER_CHUNK)
        self.assertGreater(len(chunk_file_content(wide, "wide.py")), 1)

    def test_blank_files_produce_no_chunks(self):
        self.assertEqual(chunk_file_content("", "a.py"), [])
        self.assertEqual(chunk_file_content(" \n\t\n\n", "a.py"), [])

    def test_line_numbers_follow_real_line_breaks_only(self):
        # CRLF is one break; a form feed or U+2028 inside a line is not a break
        # for editors or GitHub, so it must not shift later line numbers.
        crlf = "def a():\r\n    return 1\r\n"
        self.assertEqual(chunk_file_content(crlf, "a.py")[0]["content"], "def a():\n    return 1")
        self.assertEqual(chunk_file_content(crlf, "a.py")[0]["end_line"], 2)

        lines = ["def first():"] + body(150) + ["    page = '\x0c'", "    text = '\u2028'"] + [""]
        target = len(lines) + 1
        lines += ["def second():"] + body(150)
        content = "\n".join(lines) + "\n"
        chunks = chunk_file_content(content, "pkg/ff.py")
        real_lines = content.split("\n")
        for chunk in chunks:
            self.assertEqual(chunk["content"], "\n".join(real_lines[chunk["start_line"] - 1:chunk["end_line"]]))
        self.assertIn(target, [chunk["start_line"] for chunk in chunks])


# --- The exact-range invariant ------------------------------------------------

class ExactRangeInvariantTests(ChunkAssertions):
    def test_exact_ranges_and_full_coverage_over_varied_files(self):
        samples = {
            "web/src/components.tsx": (typescript_sample(), "tsx"),
            "service/handlers.py": (python_sample(), "py"),
            "store/repo.go": (go_sample(), "go"),
            "geo/src/point.rs": (rust_sample(), "rs"),
            "db/schema.sql": (sql_sample(), "sql"),
            "docs/guide.md": (markdown_sample(), "md"),
            "gen/generated.py": (generated_sample(1000), "py"),
            "web/src/windows.ts": (typescript_sample().replace("\n", "\r\n"), "ts"),
            "small/tiny.ts": ("export const a = () => 1;\n\nexport function b() {}\n", "ts"),
            "small/no_trailing_newline.go": ("package x\n\nfunc A() {}", "go"),
        }
        for name, (content, language) in samples.items():
            with self.subTest(file=name):
                chunks = chunk_file_content(content, name)
                self.assert_exact_chunks(content, chunks, language)
                self.assertTrue(all(chunk["file_path"] == name for chunk in chunks))
                if len(content.splitlines()) > 200:
                    self.assertGreater(len(chunks), 1)

    def test_generated_file_is_large_and_exercises_every_path(self):
        content = generated_sample(1000)
        lines = content.splitlines()
        self.assertEqual(len(lines), 1000)
        chunks = chunk_file_content(content, "gen/generated.py")
        self.assert_exact_chunks(content, chunks, "py")
        sizes = [chunk["end_line"] - chunk["start_line"] + 1 for chunk in chunks]
        # Overlapping windows (split path) and merged segments both occur.
        self.assertTrue(any(left["end_line"] >= right["start_line"] for left, right in zip(chunks, chunks[1:])))
        self.assertTrue(any(size <= TARGET_LINES for size in sizes))
        self.assertIn("decorated", " ".join(" ".join(chunk["symbols"]) for chunk in chunks))

    def test_reading_from_disk_matches_in_memory_chunking(self):
        content = generated_sample(1000)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "generated.py").write_text(content, encoding="utf-8")
            from_disk = chunk_file(str(root / "pkg" / "generated.py"), str(root))
        self.assertEqual(from_disk, chunk_file_content(content, "pkg/generated.py"))

    def test_symbols_of_sample_chunks_name_their_declarations(self):
        go_symbols = {symbol for chunk in chunk_file_content(go_sample(), "repo.go") for symbol in chunk["symbols"]}
        self.assertTrue({"Repo0", "Save", "NewRepo0"} <= go_symbols)
        rust_symbols = {symbol for chunk in chunk_file_content(rust_sample(), "p.rs") for symbol in chunk["symbols"]}
        self.assertTrue({"Point0", "distance0", "fmt", "square"} <= rust_symbols)
        sql_symbols = {symbol for chunk in chunk_file_content(sql_sample(), "s.sql") for symbol in chunk["symbols"]}
        self.assertTrue({"table_0", "table_1", "touch"} <= sql_symbols)
        self.assertNotIn("idx_table_0_created", sql_symbols)


# --- Symbols ------------------------------------------------------------------

class SymbolTests(unittest.TestCase):
    def assert_symbols(self, cases: dict[str, list[str]]) -> None:
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(extract_symbols(source), expected)

    def test_typescript_and_javascript_symbols(self):
        self.assert_symbols({
            "export function foo() {": ["foo"],
            "export default function App() {": ["App"],
            "export default async function handler(req, res) {": ["handler"],
            "declare function ambient(): void;": ["ambient"],
            "function* gen() {}": ["gen"],
            "export default class Page extends Component {": ["Page"],
            "export abstract class Base<T> {": ["Base"],
            "declare class Ambient {": ["Ambient"],
            "export interface Props extends Base {": ["Props"],
            "export enum Color {": ["Color"],
            "export const enum Dir {": ["Dir"],
            "export type Props<T> = {": ["Props"],
            "declare type Alias = number;": ["Alias"],
            "declare interface Window {": ["Window"],
            "declare enum Flags {": ["Flags"],
            "declare const ready = () => true;": ["ready"],
            "export default interface Settings {": ["Settings"],
            "export class Exported {": ["Exported"],
            "async function* stream() {": ["stream"],
            "const signIn = async () => {}": ["signIn"],
            "export const add = (a: number, b: number): number => a + b;": ["add"],
            "const handler: Handler<Req, Res> = async (req) => {": ["handler"],
            "const fn: (a: number) => void = (a) => {}": ["fn"],
            "const inc = x => x + 1": ["inc"],
            "export const Foo = memo(function Foo() {": ["Foo"],
            "export const Bar = React.memo(() => null)": ["Bar"],
            "export const Input = forwardRef<HTMLInputElement, Props>((props, ref) => {": ["Input"],
            "export const Button = ({": ["Button"],
            "export default memo(Foo)": [],
            "export const API_URL = 'x';": [],
            "const total = (a + b) * c;": [],
        })

    def test_python_symbols_include_indented_methods(self):
        source = "class Service(Base):\n    def load(self):\n        pass\n\n    async def save(self):\n        pass\n"
        self.assertEqual(extract_symbols(source), ["Service", "load", "save"])
        self.assertEqual(extract_symbols("async def fetch(url):\n    pass"), ["fetch"])

    def test_go_symbols(self):
        self.assert_symbols({
            "func main() {": ["main"],
            "func (r *Repo) Save(ctx context.Context) error {": ["Save"],
            "func (Repo) Load() {": ["Load"],
            "func Map[T any](xs []T) []T {": ["Map"],
            "type Repo struct {": ["Repo"],
            "type ID string": ["ID"],
            "type Handler func(w http.ResponseWriter)": ["Handler"],
            "type Pair[K comparable, V any] struct {": ["Pair"],
            "type Duration time.Duration // wrapped": ["Duration"],
        })

    def test_rust_symbols(self):
        self.assert_symbols({
            "fn main() {": ["main"],
            "pub fn new() -> Self {": ["new"],
            "pub(crate) async fn helper<'a>(x: &'a str) {": ["helper"],
            "pub struct Point<T> {": ["Point"],
            "pub enum Kind {": ["Kind"],
            "pub trait Shape: Send {": ["Shape"],
            "mod tests {": ["tests"],
            "pub mod api;": ["api"],
            "impl Foo {": ["Foo"],
            "impl<T> Foo<T> {": ["Foo"],
            "impl fmt::Display for Point {": ["Point"],
            "impl<T: Clone> From<T> for Wrapper<T> {": ["Wrapper"],
            "impl<'a> From<&'a str> for Name {": ["Name"],
            "impl<T: Into<String>> Named for Holder<T> {": ["Holder"],
            "unsafe impl Send for Ptr {}": ["Ptr"],
            "macro_rules! my_macro {": ["my_macro"],
        })

    def test_kotlin_and_ruby_symbols(self):
        self.assert_symbols({
            "fun main() {": ["main"],
            "private suspend fun load(): Data {": ["load"],
            "fun <T> List<T>.second(): T = this[1]": ["second"],
            "data class User(val id: Int)": ["User"],
            "object Singleton {": ["Singleton"],
            "companion object Factory {": ["Factory"],
            "sealed class Result<out T> {": ["Result"],
            "def save!": ["save!"],
            "def valid?": ["valid?"],
            "def self.call(x)": ["call"],
            "class Foo < Bar": ["Foo"],
            "module Admin::Reports": ["Reports"],
            "module Billing\n  class Invoice\n  end\nend": ["Billing", "Invoice"],
        })

    def test_sql_prisma_graphql_and_proto_symbols(self):
        self.assert_symbols({
            "CREATE TABLE users (": ["users"],
            'create table if not exists "public"."orders" (': ["orders"],
            "CREATE OR REPLACE FUNCTION app.get_user(uid int) RETURNS": ["get_user"],
            "CREATE MATERIALIZED VIEW stats AS": ["stats"],
            "CREATE VIEW `db`.`active_users` AS": ["active_users"],
            "CREATE TABLE [dbo].[Users] (": ["Users"],
            "model User {": ["User"],
            "enum Role {": ["Role"],
            "type Query {": ["Query"],
            "type User implements Node {": ["User"],
            "extend type Mutation {": ["Mutation"],
            "message GetUserRequest {": ["GetUserRequest"],
            "service UserService {\n  rpc GetUser(GetUserRequest) returns (User);\n}": ["UserService", "GetUser"],
        })

    def test_modifier_led_members(self):
        self.assert_symbols({
            "public class Foo {": ["Foo"],
            "public static void main(String[] args) {": ["main"],
            "    public async Task<IActionResult> Get(int id) {": ["Get"],
            "    public Foo(int x) {": ["Foo"],
            "public function handle($request) {": ["handle"],
            "    private final Map<String, X> cache = new HashMap<>();": [],
            "    public string Name { get; set; }": [],
        })

    def test_control_keywords_and_prose_are_not_symbols(self):
        self.assert_symbols({
            "if (x) {": [],
            "return foo(": [],
            "  return foo(bar);": [],
            "} else if (y) {": [],
            "for (const x of xs) {": [],
            "while (true) {": [],
            "switch (x) {": [],
            "} catch (e) {": [],
            "new Foo()": [],
            "  return new Foo(x);": [],
            "else {": [],
            "class names are compared": [],
            "type something in the box": [],
            "export default class extends Component {": [],
            "// function commented() {}": [],
            "# def commented():": [],
        })

    def test_same_line_annotations_do_not_hide_declarations(self):
        self.assert_symbols({
            "@Observable class Book: Identifiable {": ["Book"],
            "@MainActor final class ViewModel: ObservableObject {": ["ViewModel"],
            "    @Test fun addsNumbers() {": ["addsNumbers"],
            "    @Override public String toString() {": ["toString"],
            "@Serializable data class User(val id: Int)": ["User"],
            "@available(iOS 13.0, *) public struct Card {": ["Card"],
            "#[derive(Debug)] pub struct Point;": ["Point"],
            "@Component({ selector: 'app-root' }) export class AppComponent {": ["AppComponent"],
            # Annotations and at-rules alone name nothing.
            "@property": [],
            '@app.route("/users")': [],
            "@media screen and (max-width: 600px) {": [],
            "@property (nonatomic) NSString *name;": [],
            "@interface Foo : NSObject": [],
        })

    def test_byte_order_mark_does_not_hide_the_first_declaration(self):
        content = "﻿class Login:\n    pass\n\ndef handle_login():\n    return True\n"
        self.assertEqual(extract_symbols(content), ["Login", "handle_login"])
        chunk = chunk_file_content(content, "app.py")[0]
        # The mark stays in the stored text so the range is still exact.
        self.assertEqual(chunk["content"], content.rstrip("\n"))
        self.assertEqual(chunk["symbols"], ["Login", "handle_login"])

    def test_sql_if_not_exists_without_a_name_on_the_line(self):
        self.assert_symbols({
            "CREATE TABLE IF NOT EXISTS": [],
            "create table if not exists\r": [],
            "CREATE TABLE IF NOT EXISTS users (": ["users"],
            "CREATE TABLE if_flags (": ["if_flags"],
        })

    def test_carriage_returns_do_not_hide_line_end_declarations(self):
        self.assertEqual(extract_symbols("module Billing\r\nclass Invoice\r\ntype ID string\r\n"),
                         ["Billing", "Invoice", "ID"])
        self.assertEqual(extract_symbols("export const Card = (\r\n  props\r\n) => null\r\n"), ["Card"])

    def test_order_dedupe_and_cap(self):
        self.assertEqual(extract_symbols("def b():\n    pass\ndef a():\n    pass\ndef b():\n    pass\n"), ["b", "a"])
        source = "\n".join(f"def f{index}():\n    pass" for index in range(60))
        self.assertEqual(extract_symbols(source), [f"f{index}" for index in range(50)])

    def test_symbol_regex_is_a_compiled_multiline_pattern(self):
        self.assertTrue(SYMBOL_REGEX.flags & re.MULTILINE)
        self.assertEqual([match.group(0).strip() for match in SYMBOL_REGEX.finditer("x = 1\ndef a():\n")], ["def a"])


# --- Streaming ----------------------------------------------------------------

class StreamingTests(unittest.TestCase):
    def test_streaming_chunks_use_improved_symbols(self):
        source = (
            "export const Card = memo(({ title }: Props) => {\n  return title;\n});\n"
            "func (r *Repo) Save(ctx context.Context) error {\n\treturn nil\n}\n"
            "impl fmt::Display for Point {\n}\n"
            "if (x) {\n  return foo(x);\n}\n"
        )
        chunks = list(iter_streaming_chunks(io.StringIO(source), "src/mixed.ts"))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["symbols"], ["Card", "Save", "Point"])
        self.assertEqual(chunks[0]["language"], "ts")

    def test_streaming_keeps_150_line_windows_with_40_line_overlap(self):
        lines = [f"value = {index}" for index in range(400)]
        chunks = list(iter_streaming_chunks(io.StringIO("\n".join(lines) + "\n"), "big.py"))
        self.assertEqual(ranges(chunks), [(1, 150), (111, 260), (221, 370), (331, 400)])
        for chunk in chunks:
            self.assertEqual(chunk["content"], "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]]))

    def test_streaming_long_lines_stay_bounded_and_keep_advancing(self):
        lines = [f"{index:05d}" + "z" * 695 for index in range(2_000)]
        chunks = list(iter_streaming_chunks(io.StringIO("\n".join(lines) + "\n"), "data/rows.py"))
        covered: set[int] = set()
        for chunk in chunks:
            self.assertLessEqual(len(chunk["content"]), MAX_CHARS_PER_CHUNK)
            self.assertEqual(chunk["content"], "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]]))
            covered.update(range(chunk["start_line"], chunk["end_line"] + 1))
        self.assertEqual(covered, set(range(1, 2_001)))
        # Each 17-line window keeps at most half as overlap, so the stream
        # advances several lines per chunk instead of one.
        self.assertLess(len(chunks), 2_000 // 8)

    def test_streaming_overlap_never_pushes_a_window_past_the_ceiling(self):
        # 40 short lines then 40 wide ones fill a window to 11,959 chars; the
        # 40-line overlap (all wide) plus the next 600-char line would exceed
        # the ceiling, so the overlap must shed lines first.
        lines = [f"s{index:06d}" for index in range(40)] + ["m" * 290] * 40 + ["L" * 600]
        lines += [f"tail {index}" for index in range(50)]
        chunks = list(iter_streaming_chunks(io.StringIO("\n".join(lines) + "\n"), "data/mixed.py"))
        self.assertEqual((chunks[0]["start_line"], chunks[0]["end_line"]), (1, 80))
        self.assertEqual(chunks[1]["start_line"], 42)
        covered: set[int] = set()
        for chunk in chunks:
            self.assertLessEqual(len(chunk["content"]), MAX_CHARS_PER_CHUNK)
            self.assertEqual(chunk["content"], "\n".join(lines[chunk["start_line"] - 1:chunk["end_line"]]))
            covered.update(range(chunk["start_line"], chunk["end_line"] + 1))
        self.assertEqual(covered, set(range(1, len(lines) + 1)))

    def test_streaming_splits_a_line_longer_than_the_ceiling(self):
        long_line = "q" * 30_000
        source = "first\n" + long_line + "\nlast\n"
        chunks = list(iter_streaming_chunks(io.StringIO(source), "min.js"))
        self.assertEqual(chunks[0]["content"], "first")
        fragments = [chunk for chunk in chunks if chunk["start_line"] == 2]
        self.assertTrue(all(chunk["end_line"] == 2 for chunk in fragments))
        self.assertTrue(all(len(chunk["content"]) <= MAX_CHARS_PER_CHUNK for chunk in fragments))
        self.assertEqual(fragments[-1]["content"][-1:], "q")
        self.assertEqual((chunks[-1]["start_line"], chunks[-1]["content"]), (3, "last"))

    def test_iter_chunk_file_streams_files_above_the_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Dockerfile.prod"
            line = "RUN echo build-step-with-some-padding\n"
            path.write_text(line * (STREAMING_FILE_THRESHOLD // len(line) + 10), encoding="utf-8")
            first = next(iter_chunk_file(str(path), str(root)))
        self.assertEqual(first["language"], "dockerfile")
        self.assertEqual((first["start_line"], first["end_line"]), (1, MAX_LINES_PER_CHUNK))


# --- Language detection --------------------------------------------------------

class LanguageDetectionTests(unittest.TestCase):
    def language_of(self, path: str) -> str:
        return chunk_file_content("x = 1\n", path)[0]["language"]

    def test_extension_without_dot_lowercased(self):
        self.assertEqual(self.language_of("src/App.TSX"), "tsx")
        self.assertEqual(self.language_of("pkg/main.go"), "go")
        self.assertEqual(self.language_of("dir.with.dots/script.PY"), "py")

    def test_extensionless_known_names(self):
        expected = {
            "Dockerfile": "dockerfile", "docker/dockerfile": "dockerfile", "Makefile": "makefile",
            "Gemfile": "ruby", "Rakefile": "ruby", "Procfile": "text", "Jenkinsfile": "groovy",
        }
        for path, language in expected.items():
            with self.subTest(path=path):
                self.assertEqual(self.language_of(path), language)

    def test_dockerfile_variants_and_unknown_names(self):
        self.assertEqual(self.language_of("deploy/Dockerfile.dev"), "dockerfile")
        self.assertEqual(self.language_of("dockerfile.prod"), "dockerfile")
        self.assertEqual(self.language_of("LICENSE"), "text")
        self.assertEqual(self.language_of("bin/run"), "text")
        self.assertEqual(self.language_of(".gitignore"), "text")
        self.assertEqual(self.language_of("weird."), "text")

    def test_streaming_and_disk_paths_use_the_same_detection(self):
        streamed = list(iter_streaming_chunks(io.StringIO("all:\n\tmake\n"), "Makefile"))
        self.assertEqual(streamed[0]["language"], "makefile")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Jenkinsfile").write_text("pipeline {}\n", encoding="utf-8")
            self.assertEqual(chunk_file(str(root / "Jenkinsfile"), str(root))[0]["language"], "groovy")


# --- Split helpers ------------------------------------------------------------

class SplitHelperTests(unittest.TestCase):
    def test_split_by_lines_keeps_150_line_windows_and_40_line_overlap(self):
        lines = [f"line {index}" for index in range(400)]
        chunks = split_by_lines(lines, 11, "a.py", "py")
        self.assertEqual(ranges(chunks), [(11, 160), (121, 270), (231, 380), (341, 410)])
        for chunk in chunks:
            self.assertEqual(chunk["content"], "\n".join(lines[chunk["start_line"] - 11:chunk["end_line"] - 10]))

    def test_split_by_lines_covers_the_tail_of_character_limited_windows(self):
        lines = [f"{index:04d}" + "w" * 96 for index in range(280)]
        chunks = split_by_lines(lines, 1, "a.py", "py")
        covered = set()
        for chunk in chunks:
            self.assertLessEqual(len(chunk["content"]), MAX_CHARS_PER_CHUNK)
            covered.update(range(chunk["start_line"], chunk["end_line"] + 1))
        self.assertEqual(covered, set(range(1, 281)))

    def test_split_by_lines_long_lines_respect_ceiling_without_duplication(self):
        lines = ["v" * 700 for _ in range(300)]
        chunks = split_by_lines(lines, 1, "a.py", "py")
        self.assertTrue(all(len(chunk["content"]) <= MAX_CHARS_PER_CHUNK for chunk in chunks))
        self.assertEqual(chunks[-1]["end_line"], 300)
        self.assertLessEqual(len(chunks), 40)

    def test_split_by_lines_isolates_a_single_line_over_the_ceiling(self):
        lines = ["short"] * 5 + ["L" * 15_000] + ["short"] * 5
        chunks = split_by_lines(lines, 1, "a.py", "py")
        # The over-long line is cut into overlapping single-line fragments that
        # stay under the ceiling; every other chunk is whole lines.
        self.assertTrue(all(len(chunk["content"]) <= MAX_CHARS_PER_CHUNK for chunk in chunks))
        fragments = [chunk for chunk in chunks if (chunk["start_line"], chunk["end_line"]) == (6, 6)]
        self.assertEqual([chunk["content"] for chunk in fragments], ["L" * 12_000, "L" * 4_500])
        self.assertEqual(chunks[-1]["end_line"], 11)
        self.assertEqual(ranges(chunks), [(1, 5), (6, 6), (6, 6), (7, 11)])

    def test_minified_content_stays_line_aligned(self):
        content = "a" * 5_000 + "\n" + "b" * 20_000 + "\n" + "c" * 100 + "\n"
        self.assertTrue(is_probably_minified(content, content.splitlines()))
        chunks = chunk_file_content(content, "dist/app.min.js")
        # Only the over-long line 2 is split by characters; lines 1 and 3 are
        # whole, so every range still names exactly the text it holds.
        self.assertEqual(ranges(chunks), [(1, 1), (2, 2), (2, 2), (3, 3)])
        self.assertTrue(all(len(chunk["content"]) <= MAX_CHARS_PER_CHUNK for chunk in chunks))
        self.assertEqual(chunks[0]["content"], "a" * 5_000)
        self.assertEqual([chunks[1]["content"], chunks[2]["content"]], ["b" * 12_000, "b" * 9_500])
        self.assertEqual(chunks[3]["content"], "c" * 100)

    def test_character_windows_count_only_newlines(self):
        content = ("x" * 3_000 + "\x0c") * 5 + "\n" + "y" * 3_000
        chunks = split_by_characters(content, "a.js", "js")
        self.assertEqual(ranges(chunks), [(1, 1), (1, 2)])

    def test_minified_detection_thresholds(self):
        self.assertFalse(is_probably_minified("", []))
        def text(longest: int) -> str:
            return "\n".join(["x" * longest] + ["y"] * 10)

        self.assertTrue(is_probably_minified(text(2_001), text(2_001).splitlines()))
        self.assertFalse(is_probably_minified(text(2_000), text(2_000).splitlines()))
        wide = "\n".join(["y" * 401] * 10)
        self.assertTrue(is_probably_minified(wide, wide.splitlines()))


# --- Performance --------------------------------------------------------------

class PerformanceTests(unittest.TestCase):
    def test_one_megabyte_typescript_file_chunks_quickly(self):
        block = typescript_sample()
        content = block * (1_000_000 // len(block) + 1)
        self.assertGreaterEqual(len(content), 1_000_000)
        started = time.perf_counter()
        chunks = chunk_file_content(content, "src/huge.ts")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        self.assertGreater(len(chunks), 100)
        self.assertTrue(all(len(chunk["content"]) <= MAX_CHARS_PER_CHUNK for chunk in chunks))

    def test_long_lines_with_many_parentheses_do_not_backtrack(self):
        lines = [
            "(" * 10_000 + ")" * 10_000,
            "const x = " + "(" * 20_000,
            "const y = (" + "(a)" * 6_700,
            "const z: " + "a = (" * 4_000,
            "public " + "a " * 10_000 + "(",
            "impl " + "<" * 20_000,
            "impl<" + "<a>" * 6_700,
            "fun " + "a." * 10_000,
            "CREATE TABLE " + "a." * 10_000,
            "export const X = ({" + "a," * 10_000,
            " " * 20_000 + "x",
            # Same-line annotations and attribute lines.
            "@a(" + "x" * 20_000,
            "@a " * 6_700,
            "@" + "a." * 10_000,
            "#[a]" + " " * 20_000,
            "[a(" + ")" * 20_000,
        ]
        started = time.perf_counter()
        for line in lines:
            self.assertGreaterEqual(len(line), 20_000)
            BOUNDARY_REGEX.match(line)
            extract_symbols(line)
            extract_symbols(("    " + line + "\n") * 3)
        chunk_file_content("\n".join(lines) + "\n" + "def ok():\n    pass\n" * 300, "gen/patho.py")
        self.assertLess(time.perf_counter() - started, 1.0)


class HostileInputTests(unittest.TestCase):
    def _reference_fragments(self, line):
        step = MAX_CHARS_PER_CHUNK - 1500
        return [line[o:o + MAX_CHARS_PER_CHUNK] for o in range(0, len(line), step)]

    def test_streaming_never_reads_more_than_a_bounded_piece(self):
        class Recorder(io.StringIO):
            largest = 0

            def readline(self, size=-1):
                self.assertion = size
                piece = super().readline(size)
                Recorder.largest = max(Recorder.largest, len(piece))
                return piece

            def __iter__(self):
                raise AssertionError("unbounded line iteration")

        line = "x" * 3_000_000
        chunks = list(iter_streaming_chunks(Recorder("a\n" + line + "\nb\n"), "big.js"))
        self.assertLessEqual(Recorder.largest, MAX_CHARS_PER_CHUNK + 2)
        middle = [c for c in chunks if c["start_line"] == 2]
        self.assertEqual([c["content"] for c in middle], self._reference_fragments(line))
        self.assertEqual(chunks[0]["content"], "a")
        self.assertEqual((chunks[-1]["start_line"], chunks[-1]["content"]), (3, "b"))

    def test_streamed_long_line_fragments_match_full_slicing_at_edges(self):
        for length in (MAX_CHARS_PER_CHUNK + 1, MAX_CHARS_PER_CHUNK + 2, 2 * MAX_CHARS_PER_CHUNK,
                       MAX_CHARS_PER_CHUNK - 1500 + MAX_CHARS_PER_CHUNK, 50_001):
            for ending in ("", "\n", "\r\n"):
                with self.subTest(length=length, ending=repr(ending)):
                    line = "".join(chr(97 + i % 26) for i in range(length))
                    chunks = list(iter_streaming_chunks(io.StringIO(line + ending), "f.js"))
                    self.assertEqual([c["content"] for c in chunks], self._reference_fragments(line))
                    self.assertTrue(all(c["start_line"] == c["end_line"] == 1 for c in chunks))

    def test_line_exactly_at_ceiling_with_crlf_stays_normal(self):
        line = "y" * MAX_CHARS_PER_CHUNK
        chunks = list(iter_streaming_chunks(io.StringIO(line + "\r\nz\r\n"), "f.js"))
        self.assertEqual([c["content"] for c in chunks], [line, "z"])
        self.assertEqual([c["start_line"] for c in chunks], [1, 2])

    def test_symlink_escaping_the_repository_is_not_read(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as repo:
            secret = Path(outside) / "secret.py"
            secret.write_text("SECRET_KEY = 'hunter2'\n")
            link = Path(repo) / "config.py"
            link.symlink_to(secret)
            self.assertEqual(chunk_file(str(link), repo), [])
            (Path(repo) / "dirlink").symlink_to(outside)
            self.assertEqual(chunk_file(str(Path(repo) / "dirlink" / "secret.py"), repo), [])
            self.assertEqual(chunk_file(str(Path(repo) / ".." / Path(outside).name / "secret.py"), repo), [])

    def test_symlink_inside_the_repository_is_still_read(self):
        with tempfile.TemporaryDirectory() as repo:
            (Path(repo) / "real.py").write_text("def ok():\n    pass\n")
            (Path(repo) / "alias.py").symlink_to(Path(repo) / "real.py")
            chunks = chunk_file(str(Path(repo) / "alias.py"), repo)
            self.assertEqual(chunks[0]["file_path"], "alias.py")
            self.assertEqual(chunks[0]["symbols"], ["ok"])

    def test_fifo_and_directories_do_not_block_or_raise(self):
        import os
        import threading
        with tempfile.TemporaryDirectory() as repo:
            fifo = Path(repo) / "pipe.py"
            os.mkfifo(fifo)
            result = []
            worker = threading.Thread(target=lambda: result.append(chunk_file(str(fifo), repo)), daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "reading a FIFO blocked the worker")
            self.assertEqual(result, [[]])
            (Path(repo) / "sub").mkdir()
            self.assertEqual(chunk_file(str(Path(repo) / "sub"), repo), [])
            self.assertEqual(chunk_file(str(Path(repo) / "missing.py"), repo), [])
            self.assertEqual(chunk_file(repo, repo), [])

    def test_file_handles_are_closed_even_when_iteration_stops_early(self):
        import gc
        import warnings
        with tempfile.TemporaryDirectory() as repo:
            path = Path(repo) / "big.py"
            path.write_text("x = 1\n" * (STREAMING_FILE_THRESHOLD // 6 + 10))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                iterator = iter_chunk_file(str(path), repo)
                next(iterator)
                iterator.close()
                del iterator
                gc.collect()
            self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])


if __name__ == "__main__":
    unittest.main()
