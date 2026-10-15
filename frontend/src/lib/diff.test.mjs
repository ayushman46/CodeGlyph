// Run with: node --test src/lib/diff.test.mjs
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { devNull, tmpdir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { performance } from 'node:perf_hooks';
import { getHeapStatistics } from 'node:v8';

import {
  createUnifiedPatch,
  diffLines,
  diffStats,
  hasTrailingNewline,
  splitLines,
  toHunks,
  toRows,
  toSplitRows,
  wordDiff,
} from './diff.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Seeded PRNG (mulberry32) so randomized checks are reproducible on failure.
const createRandom = (seed) => {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
};

const pick = (random, values) => values[Math.floor(random() * values.length)];

// Independent quadratic LCS used as ground truth for minimality.
const lcsLength = (a, b) => {
  let next = new Array(b.length + 1).fill(0);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    const current = new Array(b.length + 1).fill(0);
    for (let j = b.length - 1; j >= 0; j -= 1) {
      current[j] = a[i] === b[j] ? next[j + 1] + 1 : Math.max(next[j], current[j + 1]);
    }
    next = current;
  }
  return next[0];
};

const oldSide = (ops) => ops.flatMap((op) => (op.type === 'insert' ? [] : op.lines));
const newSide = (ops) => ops.flatMap((op) => (op.type === 'delete' ? [] : op.lines));
const editCount = (ops) => ops.reduce((sum, op) => sum + (op.type === 'equal' ? 0 : op.lines.length), 0);

const assertCanonicalOps = (ops) => {
  ops.forEach((op, index) => {
    assert.ok(op.lines.length > 0, 'ops are never empty');
    const next = ops[index + 1];
    if (!next) return;
    assert.notEqual(op.type, next.type, 'adjacent ops of the same type are merged');
    if (op.type === 'insert') assert.equal(next.type, 'equal', 'an insert is never followed by a delete');
  });
};

const lines = (count, make = (index) => `line ${index + 1}`) => Array.from({ length: count }, (_, index) => make(index));
const text = (values, trailingNewline = true) => (values.length === 0 ? '' : `${values.join('\n')}${trailingNewline ? '\n' : ''}`);

// The ops the maxCost fallback must produce: shared ends kept as equal ops
// and everything between them as one delete+insert block.
const blockFallback = (a, b) => {
  let prefix = 0;
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < a.length - prefix &&
    suffix < b.length - prefix &&
    a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return [
    { type: 'equal', lines: a.slice(0, prefix) },
    { type: 'delete', lines: a.slice(prefix, a.length - suffix) },
    { type: 'insert', lines: b.slice(prefix, b.length - suffix) },
    { type: 'equal', lines: a.slice(a.length - suffix) },
  ].filter((op) => op.lines.length > 0);
};

// Applies hunks using only their header numbers (no searching), so a wrong
// start or count makes the reconstruction fail loudly.
const applyHunksStrictly = (oldLines, items) => {
  const result = [];
  let consumed = 0;
  for (const item of items) {
    if (item.kind !== 'hunk') continue;
    const copyUntil = item.oldLines === 0 ? item.oldStart : item.oldStart - 1;
    assert.ok(copyUntil >= consumed, 'hunks are ordered and do not overlap');
    result.push(...oldLines.slice(consumed, copyUntil));
    consumed = copyUntil;
    // The new side follows the same convention: first line, or the line
    // before when the hunk has no new-side lines.
    assert.equal(item.newStart, item.newLines === 0 ? result.length : result.length + 1, 'newStart');
    for (const row of item.rows) {
      if (row.kind !== 'add') {
        assert.equal(oldLines[consumed], row.text, 'hunk preimage matches the old file at oldStart');
        consumed += 1;
      }
      if (row.kind !== 'del') result.push(row.text);
    }
  }
  result.push(...oldLines.slice(consumed));
  return result;
};

const gitAvailable = (() => {
  const probe = spawnSync('git', ['--version'], { encoding: 'utf8' });
  return !probe.error && probe.status === 0;
})();

// Isolate from the developer's git config (e.g. apply.whitespace=error or
// autocrlf) so the result depends only on the patch text. Inherited GIT_*
// variables are dropped too: inside a git hook GIT_DIR/GIT_INDEX_FILE would
// point `git init`/`git apply` at the caller's real repository, and
// GIT_DIFF_OPTS would silently override the -U context of the git diff oracle.
const GIT_ENV = {
  ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('GIT_'))),
  GIT_CONFIG_GLOBAL: devNull,
  GIT_CONFIG_NOSYSTEM: '1',
};

// A hung git (a lock wait or an editor or credential prompt) must fail its
// test rather than stall the whole suite, and with stdin closed nothing can
// block waiting for input.
const GIT_TIMEOUT_MS = 60000;
const runGit = (cwd, args) => spawnSync('git', args, {
  cwd,
  env: GIT_ENV,
  encoding: 'utf8',
  stdio: ['ignore', 'pipe', 'pipe'],
  timeout: GIT_TIMEOUT_MS,
});

// Resolves a test file inside the temp repo and refuses any path that would
// write outside it, so a careless test case can never touch the real disk.
const insideRepo = (repo, path) => {
  const target = resolve(repo, path);
  const fromRepo = relative(repo, target);
  const escapes = fromRepo === '' || fromRepo === '..' || fromRepo.startsWith(`..${sep}`) || isAbsolute(fromRepo);
  assert.ok(!escapes, `${JSON.stringify(path)} escapes the test repository`);
  return target;
};

// Writes each file's "before" into a fresh repo, runs git apply --check and
// then git apply, and asserts every file now matches "after" byte for byte.
const assertGitApplies = (files, options = {}, applyArgs = []) => {
  // Built before anything is written, so a path createUnifiedPatch refuses
  // never reaches the disk; insideRepo guards the paths it accepts.
  const patch = createUnifiedPatch(files, options);
  const root = mkdtempSync(join(tmpdir(), 'codebase-diff-'));
  try {
    const repo = join(root, 'repo');
    mkdirSync(repo);
    const init = runGit(repo, ['init', '-q']);
    assert.equal(init.status, 0, init.stderr);
    for (const file of files) {
      if (file.isNew) continue;
      const target = insideRepo(repo, file.path);
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, file.before ?? '');
    }
    const patchPath = join(root, 'change.patch');
    writeFileSync(patchPath, patch);

    const check = runGit(repo, ['apply', '--check', ...applyArgs, patchPath]);
    assert.equal(check.status, 0, `git apply --check rejected the patch:\n${check.stderr}\n${patch}`);
    const apply = runGit(repo, ['apply', ...applyArgs, patchPath]);
    assert.equal(apply.status, 0, `git apply failed:\n${apply.stderr}\n${patch}`);

    for (const file of files) {
      const target = insideRepo(repo, file.path);
      if (file.isDeleted) {
        assert.equal(existsSync(target), false, `${file.path} should be deleted`);
      } else {
        assert.equal(readFileSync(target, 'utf8'), file.after ?? '', `${file.path} content after apply`);
      }
    }
    return patch;
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
};

// ---------------------------------------------------------------------------
// splitLines / hasTrailingNewline
// ---------------------------------------------------------------------------

describe('splitLines', () => {
  it('returns no lines for empty or missing text', () => {
    assert.deepEqual(splitLines(''), []);
    assert.deepEqual(splitLines(null), []);
    assert.deepEqual(splitLines(undefined), []);
  });

  it('does not create an extra empty line for a trailing newline', () => {
    assert.deepEqual(splitLines('a'), ['a']);
    assert.deepEqual(splitLines('a\n'), ['a']);
    assert.deepEqual(splitLines('a\nb'), ['a', 'b']);
    assert.deepEqual(splitLines('a\nb\n'), ['a', 'b']);
  });

  it('keeps genuinely empty lines', () => {
    assert.deepEqual(splitLines('\n'), ['']);
    assert.deepEqual(splitLines('a\n\n'), ['a', '']);
    assert.deepEqual(splitLines('\n\na'), ['', '', 'a']);
  });

  it('normalizes CRLF and lone CR to LF', () => {
    assert.deepEqual(splitLines('a\r\nb\rc\r\n'), ['a', 'b', 'c']);
    assert.deepEqual(splitLines('\r\n'), ['']);
    assert.deepEqual(splitLines('\r'), ['']);
    // A lone CR followed by CRLF is two terminators, not one.
    assert.deepEqual(splitLines('a\r\r\nb'), ['a', '', 'b']);
    // Only the final terminator is dropped; the blank line before it stays.
    assert.deepEqual(splitLines('a\r\n\r\n'), ['a', '']);
    assert.deepEqual(splitLines('\r\r'), ['', '']);
    assert.deepEqual(splitLines('\n\n'), ['', '']);
  });

  it('preserves whitespace inside lines', () => {
    assert.deepEqual(splitLines('  \n\tx \n'), ['  ', '\tx ']);
  });
});

describe('hasTrailingNewline', () => {
  it('detects LF, CRLF and lone CR terminators', () => {
    assert.equal(hasTrailingNewline('a\n'), true);
    assert.equal(hasTrailingNewline('a\r\n'), true);
    assert.equal(hasTrailingNewline('a\r'), true);
    assert.equal(hasTrailingNewline('\n'), true);
  });

  it('is false for empty text and unterminated last lines', () => {
    assert.equal(hasTrailingNewline(''), false);
    assert.equal(hasTrailingNewline(null), false);
    assert.equal(hasTrailingNewline('a'), false);
    assert.equal(hasTrailingNewline('a\nb'), false);
  });
});

// ---------------------------------------------------------------------------
// diffLines
// ---------------------------------------------------------------------------

describe('diffLines', () => {
  it('returns no ops for two empty files', () => {
    assert.deepEqual(diffLines('', ''), []);
    assert.deepEqual(diffLines(null, undefined), []);
  });

  it('returns a single equal op for identical files', () => {
    assert.deepEqual(diffLines('a\nb\nc\n', 'a\nb\nc\n'), [{ type: 'equal', lines: ['a', 'b', 'c'] }]);
  });

  it('treats CRLF, lone CR and LF files with the same lines as identical', () => {
    assert.deepEqual(diffLines('a\r\nb\r\n', 'a\nb\n'), [{ type: 'equal', lines: ['a', 'b'] }]);
    assert.deepEqual(diffLines('a\rb', 'a\nb'), [{ type: 'equal', lines: ['a', 'b'] }]);
  });

  it('compares CRLF and LF files by line content when they really differ', () => {
    assert.deepEqual(diffLines('a\r\nb\r\nc\r\n', 'a\nB\nc\n'), [
      { type: 'equal', lines: ['a'] },
      { type: 'delete', lines: ['b'] },
      { type: 'insert', lines: ['B'] },
      { type: 'equal', lines: ['c'] },
    ]);
  });

  it('reports blank-line insertions and deletions', () => {
    assert.deepEqual(diffLines('a\nb', 'a\n\nb'), [
      { type: 'equal', lines: ['a'] },
      { type: 'insert', lines: [''] },
      { type: 'equal', lines: ['b'] },
    ]);
    assert.deepEqual(diffLines('a\n\n\nb\n', 'a\nb\n'), [
      { type: 'equal', lines: ['a'] },
      { type: 'delete', lines: ['', ''] },
      { type: 'equal', lines: ['b'] },
    ]);
  });

  it('ignores a trailing-newline-only difference (reported by hasTrailingNewline instead)', () => {
    assert.deepEqual(diffLines('a\nb', 'a\nb\n'), [{ type: 'equal', lines: ['a', 'b'] }]);
  });

  it('reports an all-insert diff when the old file is empty', () => {
    assert.deepEqual(diffLines('', 'x\ny\n'), [{ type: 'insert', lines: ['x', 'y'] }]);
  });

  it('reports an all-delete diff when the new file is empty', () => {
    assert.deepEqual(diffLines('x\ny\n', ''), [{ type: 'delete', lines: ['x', 'y'] }]);
  });

  it('handles single-line files', () => {
    assert.deepEqual(diffLines('a', 'b'), [
      { type: 'delete', lines: ['a'] },
      { type: 'insert', lines: ['b'] },
    ]);
    assert.deepEqual(diffLines('a', 'a'), [{ type: 'equal', lines: ['a'] }]);
    assert.deepEqual(diffLines('a', ''), [{ type: 'delete', lines: ['a'] }]);
  });

  it('detects whitespace-only changes', () => {
    assert.deepEqual(diffLines('a\n  b\nc\n', 'a\n\tb\nc\n'), [
      { type: 'equal', lines: ['a'] },
      { type: 'delete', lines: ['  b'] },
      { type: 'insert', lines: ['\tb'] },
      { type: 'equal', lines: ['c'] },
    ]);
    assert.deepEqual(diffLines('x', 'x '), [
      { type: 'delete', lines: ['x'] },
      { type: 'insert', lines: ['x '] },
    ]);
  });

  it('keeps the common prefix and suffix as equal ops around the change', () => {
    assert.deepEqual(diffLines('h1\nh2\nold\nt1\nt2', 'h1\nh2\nnew1\nnew2\nt1\nt2'), [
      { type: 'equal', lines: ['h1', 'h2'] },
      { type: 'delete', lines: ['old'] },
      { type: 'insert', lines: ['new1', 'new2'] },
      { type: 'equal', lines: ['t1', 't2'] },
    ]);
  });

  it('finds interior matches with Myers instead of replacing the whole middle', () => {
    assert.deepEqual(diffLines('x\ns\ny', 'p\ns\nq'), [
      { type: 'delete', lines: ['x'] },
      { type: 'insert', lines: ['p'] },
      { type: 'equal', lines: ['s'] },
      { type: 'delete', lines: ['y'] },
      { type: 'insert', lines: ['q'] },
    ]);
  });

  it('handles lines that collide with Object.prototype names', () => {
    const ops = diffLines('__proto__\nconstructor\nx', 'constructor\n__proto__\nhasOwnProperty\nx');
    assert.deepEqual(oldSide(ops), ['__proto__', 'constructor', 'x']);
    assert.deepEqual(newSide(ops), ['constructor', '__proto__', 'hasOwnProperty', 'x']);
    assert.equal(editCount(ops), 3);
  });

  it('produces minimal, canonical, exact diffs (randomized against a brute-force LCS)', () => {
    const random = createRandom(0xc0ffee);
    for (let iteration = 0; iteration < 3000; iteration += 1) {
      const oldAlphabet = ['a', 'b', 'c', 'd', 'e', '', '}'].slice(0, 1 + Math.floor(random() * 7));
      const newAlphabet = ['b', 'c', 'd', 'f', 'g', '', '}'].slice(0, 1 + Math.floor(random() * 7));
      const a = lines(Math.floor(random() * 14), () => pick(random, oldAlphabet));
      const b = lines(Math.floor(random() * 14), () => pick(random, newAlphabet));
      const ops = diffLines(text(a), text(b), { maxCost: Infinity });
      const label = JSON.stringify({ a, b });
      assert.deepEqual(oldSide(ops), a, label);
      assert.deepEqual(newSide(ops), b, label);
      assert.equal(editCount(ops), a.length + b.length - 2 * lcsLength(a, b), label);
      assertCanonicalOps(ops);
    }
  });

  it('stays minimal when one side is far shorter than the other (randomized)', () => {
    // Lopsided inputs over one to three symbols drive the search to its
    // edges: diagonals run off the grid and get pruned, and splits land next
    // to an empty side. A stale or off-by-one frontier there would show up as
    // a non-minimal script or as lines lost from one side.
    const random = createRandom(0x5eed);
    for (let iteration = 0; iteration < 1500; iteration += 1) {
      const symbols = 1 + Math.floor(random() * 3);
      const longSide = lines(Math.floor(random() * 70), () => `s${Math.floor(random() * symbols)}`);
      const shortSide = lines(Math.floor(random() * 6), () => `s${Math.floor(random() * (symbols + 1))}`);
      const [a, b] = random() < 0.5 ? [longSide, shortSide] : [shortSide, longSide];
      const distance = a.length + b.length - 2 * lcsLength(a, b);
      const label = JSON.stringify({ a, b });
      for (const maxCost of [Infinity, 20000, distance, Math.max(0, distance - 1)]) {
        const ops = diffLines(text(a), text(b), { maxCost });
        assert.deepEqual(oldSide(ops), a, label);
        assert.deepEqual(newSide(ops), b, label);
        assertCanonicalOps(ops);
        if (distance <= maxCost) assert.equal(editCount(ops), distance, `${label} maxCost ${maxCost}`);
        else assert.deepEqual(ops, blockFallback(a, b), `${label} maxCost ${maxCost}`);
      }
    }
  });

  it('is deterministic and does not mutate its inputs', () => {
    const before = 'a\nb\nc\nd\ne\nb\nc\n';
    const after = 'b\nx\nc\nd\nb\ny\nc\n';
    const first = diffLines(before, after);
    const second = diffLines(before, after);
    assert.deepEqual(first, second);
    first[0].lines.push('mutated');
    assert.notDeepEqual(diffLines(before, after), first, 'each call returns fresh arrays');
  });

  describe('maxCost', () => {
    const before = 'top\nx\ns\ny\nbottom';
    const after = 'top\np\ns\nq\nbottom';

    it('computes the exact diff when the edit distance equals maxCost', () => {
      assert.deepEqual(diffLines(before, after, { maxCost: 4 }), [
        { type: 'equal', lines: ['top'] },
        { type: 'delete', lines: ['x'] },
        { type: 'insert', lines: ['p'] },
        { type: 'equal', lines: ['s'] },
        { type: 'delete', lines: ['y'] },
        { type: 'insert', lines: ['q'] },
        { type: 'equal', lines: ['bottom'] },
      ]);
    });

    it('falls back to one delete+insert block for the middle when the distance exceeds maxCost', () => {
      assert.deepEqual(diffLines(before, after, { maxCost: 3 }), [
        { type: 'equal', lines: ['top'] },
        { type: 'delete', lines: ['x', 's', 'y'] },
        { type: 'insert', lines: ['p', 's', 'q'] },
        { type: 'equal', lines: ['bottom'] },
      ]);
    });

    it('falls back immediately with maxCost 0 but still reports identical files as equal', () => {
      assert.deepEqual(diffLines('a\nb', 'a\nc', { maxCost: 0 }), [
        { type: 'equal', lines: ['a'] },
        { type: 'delete', lines: ['b'] },
        { type: 'insert', lines: ['c'] },
      ]);
      assert.deepEqual(diffLines('a\nb', 'a\nb', { maxCost: 0 }), [{ type: 'equal', lines: ['a', 'b'] }]);
    });

    it('applies the bound when the extra lines are all one-sided', () => {
      assert.deepEqual(diffLines('a\nz', 'a\nu\nv\nz', { maxCost: 1 }), [
        { type: 'equal', lines: ['a'] },
        { type: 'insert', lines: ['u', 'v'] },
        { type: 'equal', lines: ['z'] },
      ]);
      assert.deepEqual(diffLines('k\nu\nk\nv', 'v\nk\nw\nk', { maxCost: 2 }), [
        { type: 'delete', lines: ['k', 'u', 'k', 'v'] },
        { type: 'insert', lines: ['v', 'k', 'w', 'k'] },
      ]);
    });

    it('is exact when within the bound and a single middle block otherwise (randomized)', () => {
      const random = createRandom(42);
      let fallbacks = 0;
      for (let iteration = 0; iteration < 3000; iteration += 1) {
        const a = lines(Math.floor(random() * 12), () => pick(random, ['a', 'b', 'c', 'd']));
        const b = lines(Math.floor(random() * 12), () => pick(random, ['b', 'c', 'd', 'e']));
        const maxCost = Math.floor(random() * 16);
        const ops = diffLines(text(a), text(b), { maxCost });
        const distance = a.length + b.length - 2 * lcsLength(a, b);
        const label = JSON.stringify({ a, b, maxCost });
        assert.deepEqual(oldSide(ops), a, label);
        assert.deepEqual(newSide(ops), b, label);
        assertCanonicalOps(ops);
        if (distance <= maxCost) {
          assert.equal(editCount(ops), distance, label);
          continue;
        }
        fallbacks += 1;
        assert.deepEqual(ops, blockFallback(a, b), label);
      }
      assert.ok(fallbacks > 100, 'the randomized inputs exercise the fallback path');
    });

    it('stays minimal on larger inputs and switches exactly at the threshold (randomized)', () => {
      // The 14-line checks above barely recurse; these sizes force several
      // levels of middle-snake splits and frontiers sized by maxCost rather
      // than by the input, where an undersized frontier would silently drop
      // values (typed arrays ignore out-of-range writes) instead of throwing.
      const random = createRandom(2024);
      for (let iteration = 0; iteration < 120; iteration += 1) {
        const alphabet = 2 + Math.floor(random() * 6);
        const a = lines(Math.floor(random() * 160), () => `v${Math.floor(random() * alphabet)}`);
        const b = random() < 0.5
          ? lines(Math.floor(random() * 160), () => `v${Math.floor(random() * (alphabet + 2))}`)
          : a.filter(() => random() > 0.15).map((line) => (random() < 0.15 ? `w${line}` : line));
        const distance = a.length + b.length - 2 * lcsLength(a, b);
        for (const maxCost of [distance - 1, distance - 0.5, distance, distance + 0.5]) {
          if (maxCost < 0) continue;
          const ops = diffLines(text(a), text(b), { maxCost });
          const label = JSON.stringify({ a, b, maxCost });
          assert.deepEqual(oldSide(ops), a, label);
          assert.deepEqual(newSide(ops), b, label);
          assertCanonicalOps(ops);
          if (distance <= maxCost) assert.equal(editCount(ops), distance, label);
          else assert.deepEqual(ops, blockFallback(a, b), label);
        }
      }
    });

    it('charges one-sided lines against the budget left for shared lines', () => {
      // u and w occur on one side only (cost 2), and reordering S and T costs
      // 2 more, so D = 4: exact at maxCost 4, a single block at maxCost 3.
      const exact = diffLines('S\nu\nT', 'T\nw\nS', { maxCost: 4 });
      assert.equal(editCount(exact), 4);
      assert.deepEqual(oldSide(exact), ['S', 'u', 'T']);
      assert.deepEqual(newSide(exact), ['T', 'w', 'S']);
      assert.ok(exact.some((op) => op.type === 'equal'), 'a shared line is kept');
      assert.deepEqual(diffLines('S\nu\nT', 'T\nw\nS', { maxCost: 3 }), [
        { type: 'delete', lines: ['S', 'u', 'T'] },
        { type: 'insert', lines: ['T', 'w', 'S'] },
      ]);
    });

    it('applies the bound when the shared lines left after filtering have nothing in common', () => {
      // Dropping the one-sided u and w leaves a,a,b vs a,b,b; after trimming
      // their shared ends the middles [a] and [b] share nothing, so the search
      // ends without an overlap and must still charge D = 2 + 2 = 4.
      assert.deepEqual(diffLines('u\na\na\nb', 'a\nb\nb\nw', { maxCost: 3 }), [
        { type: 'delete', lines: ['u', 'a', 'a', 'b'] },
        { type: 'insert', lines: ['a', 'b', 'b', 'w'] },
      ]);
      const exact = diffLines('u\na\na\nb', 'a\nb\nb\nw', { maxCost: 4 });
      assert.equal(editCount(exact), 4);
      assert.deepEqual(oldSide(exact), ['u', 'a', 'a', 'b']);
      assert.deepEqual(newSide(exact), ['a', 'b', 'b', 'w']);
    });

    it('accepts Infinity and rejects invalid values', () => {
      assert.equal(editCount(diffLines('a\nb', 'b\na', { maxCost: Infinity })), 2);
      for (const bad of [-1, Number.NaN, '5', null]) {
        assert.throws(() => diffLines('a', 'b', { maxCost: bad }), RangeError);
      }
    });

    it('uses the default bound when options are omitted or null', () => {
      assert.deepEqual(diffLines('a', 'b', null), diffLines('a', 'b'));
      assert.deepEqual(diffLines('a', 'b', {}), diffLines('a', 'b', { maxCost: 20000 }));
    });
  });

  describe('performance', () => {
    const base = lines(20000, (index) => `    const value${index} = compute(${index}); // line ${index + 1}`);

    it('diffs a 20,000-line file with a 3-line change in under 150 ms', () => {
      const changed = base.slice();
      changed[9000] = '    const replaced = 1;';
      changed[9001] = '    const replaced = 2;';
      changed[9002] = '    const replaced = 3;';
      const start = performance.now();
      const ops = diffLines(text(base), text(changed));
      const elapsed = performance.now() - start;
      assert.ok(elapsed < 150, `took ${elapsed.toFixed(1)} ms`);
      assert.deepEqual(ops, [
        { type: 'equal', lines: base.slice(0, 9000) },
        { type: 'delete', lines: base.slice(9000, 9003) },
        { type: 'insert', lines: changed.slice(9000, 9003) },
        { type: 'equal', lines: base.slice(9003) },
      ]);
    });

    it('diffs a 20,000-line file with 3 scattered changed lines in under 150 ms', () => {
      const changed = base.slice();
      changed[100] = 'first edit';
      changed[10000] = 'second edit';
      changed[19900] = 'third edit';
      const start = performance.now();
      const ops = diffLines(text(base), text(changed));
      const elapsed = performance.now() - start;
      assert.ok(elapsed < 150, `took ${elapsed.toFixed(1)} ms`);
      assert.deepEqual(diffStats(ops), { additions: 3, deletions: 3 });
      assert.equal(ops.length, 10);
      assert.deepEqual(newSide(ops), changed);
    });

    it('diffs a highly repetitive 20,000-line file with 3 scattered changes in under 150 ms', () => {
      // Every line repeats, so neither the one-sided filter nor unique-line
      // anchors help; only the O(ND) search with D = 6 keeps this fast.
      const random = createRandom(5);
      const vocabulary = ['', '}', '{', '  return x;', '  }', 'const a = 1;'];
      const repetitive = lines(20000, () => pick(random, vocabulary));
      const changed = repetitive.slice();
      changed[0] = 'first edit';
      changed[10000] = 'second edit';
      changed[19999] = 'third edit';
      const start = performance.now();
      const ops = diffLines(text(repetitive), text(changed));
      const elapsed = performance.now() - start;
      assert.ok(elapsed < 150, `took ${elapsed.toFixed(1)} ms`);
      assert.deepEqual(diffStats(ops), { additions: 3, deletions: 3 });
      assert.deepEqual(oldSide(ops), repetitive);
      assert.deepEqual(newSide(ops), changed);
    });

    it('aborts the bounded search quickly when shared lines are reordered past maxCost', () => {
      // All lines are shared (nothing is filtered out), and swapping the two
      // halves costs D = 20,000, so the search itself must hit the bound.
      const half = 10000;
      const before = [...Array(half).fill('x'), ...Array(half).fill('y')];
      const after = [...Array(half).fill('y'), ...Array(half).fill('x')];
      const start = performance.now();
      const ops = diffLines(text(before), text(after), { maxCost: 1000 });
      const elapsed = performance.now() - start;
      assert.ok(elapsed < 150, `took ${elapsed.toFixed(1)} ms`);
      assert.deepEqual(ops, [
        { type: 'delete', lines: before },
        { type: 'insert', lines: after },
      ]);
    });

    it('falls back quickly when a large file is rewritten entirely', () => {
      const rewritten = base.map((line) => `${line} // rewritten`);
      const start = performance.now();
      const ops = diffLines(text(base), text(rewritten));
      const elapsed = performance.now() - start;
      assert.ok(elapsed < 500, `took ${elapsed.toFixed(1)} ms`);
      assert.deepEqual(ops, [
        { type: 'delete', lines: base },
        { type: 'insert', lines: rewritten },
      ]);
    });
  });

  describe('search effort cap', () => {
    // Two alternating lines with every gap-th line removed. Every line is
    // shared, so the one-sided filter removes nothing, and snakes run along
    // half of all diagonals. Bounding D alone left this O(file length * D):
    // a 2M-line version with D = 10,000 took 25 s at the default maxCost.
    const alternatingWithDeletions = (count, gap) => {
      const before = lines(count, (index) => (index % 2 ? 'odd' : 'even'));
      return [before, before.filter((_, index) => index % gap !== gap - 1)];
    };
    const [before, after] = alternatingWithDeletions(30000, 30);

    it('falls back to one block once the search outgrows its effort limit', () => {
      // The unbounded search (never capped) finds D = 1,000, within maxCost
      // 1,200, so only the effort limit can explain the fallback below;
      // before the cap this input returned the exact diff.
      const exact = diffLines(text(before), text(after), { maxCost: Infinity });
      assert.equal(editCount(exact), 1000);
      assert.deepEqual(newSide(exact), after);
      const start = performance.now();
      const ops = diffLines(text(before), text(after), { maxCost: 1200 });
      const elapsed = performance.now() - start;
      assert.deepEqual(ops, blockFallback(before, after));
      assert.ok(elapsed < 500, `took ${elapsed.toFixed(1)} ms`);
    });

    it('never caps a search whose shorter side has at most maxCost lines', () => {
      // The same input stays exact once maxCost covers the 29,000-line side.
      const ops = diffLines(text(before), text(after), { maxCost: after.length });
      assert.deepEqual(diffStats(ops), { additions: 0, deletions: 1000 });
      assert.deepEqual(newSide(ops), after);
      // At the spec's reference scale the default maxCost covers the file.
      const [smallBefore, smallAfter] = alternatingWithDeletions(20000, 40);
      assert.equal(editCount(diffLines(text(smallBefore), text(smallAfter))), 500);
    });

    it('sizes the effort allowance by the caller\'s maxCost, not what one-sided lines leave of it', () => {
      // 27,800 lines found only in the old file use up all but 1,200 of
      // maxCost 29,000. The shared-line search left over is the capped case
      // above, yet the shorter side (29,000 lines) is within maxCost, so the
      // guarantee still applies and the diff stays exact (D = 28,800).
      const oneSided = lines(27800, (index) => `removed ${index}`);
      const withRemovals = [...oneSided, ...before];
      const ops = diffLines(text(withRemovals), text(after), { maxCost: after.length });
      assert.deepEqual(diffStats(ops), { additions: 0, deletions: 28800 });
      assert.deepEqual(oldSide(ops), withRemovals);
      assert.deepEqual(newSide(ops), after);
    });

    it('never caps a search with at most 62 differing shared lines, however long the file', () => {
      const [longBefore, longAfter] = alternatingWithDeletions(100000, 1612);
      assert.equal(longBefore.length - longAfter.length, 62);
      // Both sides far exceed maxCost, so only the small-D allowance applies.
      const ops = diffLines(text(longBefore), text(longAfter), { maxCost: 62 });
      assert.deepEqual(diffStats(ops), { additions: 0, deletions: 62 });
      assert.deepEqual(newSide(ops), longAfter);
    });
  });
});

// ---------------------------------------------------------------------------
// toRows
// ---------------------------------------------------------------------------

describe('toRows', () => {
  it('returns no rows for no ops', () => {
    assert.deepEqual(toRows([]), []);
  });

  it('numbers each side from 1 and leaves the missing side null', () => {
    assert.deepEqual(toRows(diffLines('a\nb\nc\n', 'a\nx\nc\nd\n')), [
      { kind: 'context', oldNo: 1, newNo: 1, text: 'a' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'b' },
      { kind: 'add', oldNo: null, newNo: 2, text: 'x' },
      { kind: 'context', oldNo: 3, newNo: 3, text: 'c' },
      { kind: 'add', oldNo: null, newNo: 4, text: 'd' },
    ]);
  });

  it('numbers an all-insert and an all-delete diff', () => {
    assert.deepEqual(toRows(diffLines('', 'x\ny')), [
      { kind: 'add', oldNo: null, newNo: 1, text: 'x' },
      { kind: 'add', oldNo: null, newNo: 2, text: 'y' },
    ]);
    assert.deepEqual(toRows(diffLines('x\ny', '')), [
      { kind: 'del', oldNo: 1, newNo: null, text: 'x' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'y' },
    ]);
  });

  it('numbers identical, CRLF and single-line inputs on both sides', () => {
    assert.deepEqual(toRows(diffLines('a\r\nb\r\n', 'a\nb')), [
      { kind: 'context', oldNo: 1, newNo: 1, text: 'a' },
      { kind: 'context', oldNo: 2, newNo: 2, text: 'b' },
    ]);
    assert.deepEqual(toRows(diffLines('only', 'changed')), [
      { kind: 'del', oldNo: 1, newNo: null, text: 'only' },
      { kind: 'add', oldNo: null, newNo: 1, text: 'changed' },
    ]);
  });

  it('keeps the two sides\' numbering independent after uneven blocks', () => {
    assert.deepEqual(toRows([
      { type: 'delete', lines: ['a', 'b', 'c'] },
      { type: 'insert', lines: ['x'] },
      { type: 'equal', lines: ['same'] },
    ]), [
      { kind: 'del', oldNo: 1, newNo: null, text: 'a' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'b' },
      { kind: 'del', oldNo: 3, newNo: null, text: 'c' },
      { kind: 'add', oldNo: null, newNo: 1, text: 'x' },
      { kind: 'context', oldNo: 4, newNo: 2, text: 'same' },
    ]);
  });

  it('rejects malformed ops', () => {
    assert.throws(() => toRows([{ type: 'replace', lines: ['a'] }]), TypeError);
    assert.throws(() => toRows([{ type: 'equal' }]), TypeError);
    assert.throws(() => toRows([null]), TypeError);
    assert.throws(() => toRows('nope'), TypeError);
  });
});

// ---------------------------------------------------------------------------
// toHunks
// ---------------------------------------------------------------------------

const rowsFor = (before, after) => toRows(diffLines(before, after));
const summarize = (items) => items.map((item) => (item.kind === 'hunk' ? item.header : `collapsed ${item.count}`));

describe('toHunks', () => {
  it('returns [] for empty input', () => {
    assert.deepEqual(toHunks([]), []);
  });

  it('returns a single collapsed item covering all rows when nothing changed', () => {
    const rows = rowsFor('a\nb\nc', 'a\nb\nc');
    assert.deepEqual(toHunks(rows), [{ kind: 'collapsed', count: 3, rows }]);
  });

  it('builds one hunk with 3 context lines and collapses the unchanged ends', () => {
    const before = lines(20);
    const after = before.slice();
    after[9] = 'changed';
    const rows = rowsFor(text(before), text(after));
    const items = toHunks(rows);
    assert.deepEqual(summarize(items), ['collapsed 6', '@@ -7,7 +7,7 @@', 'collapsed 7']);
    const [leading, hunk, trailing] = items;
    assert.deepEqual(leading.rows.map((row) => row.oldNo), [1, 2, 3, 4, 5, 6]);
    assert.equal(hunk.oldStart, 7);
    assert.equal(hunk.oldLines, 7);
    assert.equal(hunk.newStart, 7);
    assert.equal(hunk.newLines, 7);
    assert.deepEqual(hunk.rows.map((row) => row.kind), ['context', 'context', 'context', 'del', 'add', 'context', 'context', 'context']);
    assert.deepEqual(trailing.rows.map((row) => row.oldNo), [14, 15, 16, 17, 18, 19, 20]);
  });

  it('starts at line 1 without a leading collapse when the first line changes', () => {
    const before = lines(10);
    const after = ['changed', ...before.slice(1)];
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(after)))), ['@@ -1,4 +1,4 @@', 'collapsed 6']);
  });

  it('ends without a trailing collapse when the last line changes', () => {
    const before = lines(10);
    const after = [...before.slice(0, 9), 'changed'];
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(after)))), ['collapsed 6', '@@ -7,4 +7,4 @@']);
  });

  it('collapses a leading region only when it is longer than the context', () => {
    const before = lines(12);
    const changeAt = (index) => before.map((line, position) => (position === index ? 'changed' : line));
    // Exactly 3 unchanged lines before the change: all of them are context.
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(changeAt(3))))), ['@@ -1,7 +1,7 @@', 'collapsed 5']);
    // 4 unchanged lines: one of them collapses.
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(changeAt(4))))), ['collapsed 1', '@@ -2,7 +2,7 @@', 'collapsed 4']);
  });

  it('merges changes separated by at most 2*context lines and splits beyond that', () => {
    const before = lines(30);
    const twoChanges = (gap) => before.map((line, index) => (index === 5 || index === 6 + gap ? `changed ${index}` : line));
    // Gap of exactly 6 unchanged lines: one hunk.
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(twoChanges(6))))), [
      'collapsed 2',
      '@@ -3,14 +3,14 @@',
      'collapsed 14',
    ]);
    // Gap of 7: two hunks with one collapsed line between them.
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(twoChanges(7))))), [
      'collapsed 2',
      '@@ -3,7 +3,7 @@',
      'collapsed 1',
      '@@ -11,7 +11,7 @@',
      'collapsed 13',
    ]);
  });

  it('honours a custom context size', () => {
    const before = lines(10);
    const after = before.slice();
    after[4] = 'changed';
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(after)), 1)), ['collapsed 3', '@@ -4,3 +4,3 @@', 'collapsed 4']);
    assert.deepEqual(summarize(toHunks(rowsFor(text(before), text(after)), 0)), ['collapsed 4', '@@ -5,1 +5,1 @@', 'collapsed 5']);
  });

  it('uses 0 as the start of a side with no lines (new and deleted files)', () => {
    assert.deepEqual(summarize(toHunks(rowsFor('', 'a\nb\nc'))), ['@@ -0,0 +1,3 @@']);
    assert.deepEqual(summarize(toHunks(rowsFor('a\nb\nc', ''))), ['@@ -1,3 +0,0 @@']);
  });

  it('anchors a zero-line side to the preceding line with context 0', () => {
    const before = lines(10);
    const inserted = [...before.slice(0, 5), 'new', ...before.slice(5)];
    const [, insertion] = toHunks(rowsFor(text(before), text(inserted)), 0);
    assert.equal(insertion.header, '@@ -5,0 +6,1 @@');
    assert.equal(insertion.oldStart, 5);
    assert.equal(insertion.oldLines, 0);

    const removed = [...before.slice(0, 4), ...before.slice(5)];
    const [, deletion] = toHunks(rowsFor(text(before), text(removed)), 0);
    assert.equal(deletion.header, '@@ -5,1 +4,0 @@');

    const atTop = toHunks(rowsFor(text(before), text(['new', ...before])), 0);
    assert.equal(atTop[0].header, '@@ -0,0 +1,1 @@');
  });

  it('handles single-line files', () => {
    const changed = rowsFor('a', 'b');
    assert.deepEqual(toHunks(changed), [{
      kind: 'hunk',
      header: '@@ -1,1 +1,1 @@',
      oldStart: 1,
      oldLines: 1,
      newStart: 1,
      newLines: 1,
      rows: changed,
    }]);
    const same = rowsFor('a', 'a');
    assert.deepEqual(toHunks(same), [{ kind: 'collapsed', count: 1, rows: same }]);
  });

  it('makes two hunks without leading or trailing collapses when both ends change', () => {
    const before = lines(20);
    const after = ['first', ...before.slice(1, 19), 'last'];
    const items = toHunks(rowsFor(text(before), text(after)));
    assert.deepEqual(summarize(items), ['@@ -1,4 +1,4 @@', 'collapsed 12', '@@ -17,4 +17,4 @@']);
    assert.deepEqual(items[1].rows.map((row) => row.oldNo), lines(12, (index) => index + 5));
  });

  it('treats a whitespace-only change as a real hunk', () => {
    const items = toHunks(rowsFor('if (x) {\n  return 1;\n}\n', 'if (x) {\n\treturn 1;\n}\n'));
    assert.deepEqual(summarize(items), ['@@ -1,3 +1,3 @@']);
    assert.deepEqual(items[0].rows.map((row) => [row.kind, row.text]), [
      ['context', 'if (x) {'],
      ['del', '  return 1;'],
      ['add', '\treturn 1;'],
      ['context', '}'],
    ]);
  });

  it('puts the whole file in one hunk with an infinite context', () => {
    const before = lines(50);
    const after = before.slice();
    after[2] = 'x';
    after[45] = 'y';
    const items = toHunks(rowsFor(text(before), text(after)), Infinity);
    assert.deepEqual(summarize(items), ['@@ -1,50 +1,50 @@']);
  });

  it('keeps every row exactly once with consistent headers (randomized)', () => {
    const random = createRandom(7);
    for (let iteration = 0; iteration < 1500; iteration += 1) {
      const a = lines(Math.floor(random() * 40), () => pick(random, ['a', 'b', 'c', 'd', 'e', 'f']));
      const b = a.filter(() => random() > 0.08).map((line) => (random() < 0.08 ? 'z' : line));
      if (random() < 0.3) b.splice(Math.floor(random() * (b.length + 1)), 0, 'ins');
      const context = Math.floor(random() * 5);
      const rows = toRows(diffLines(text(a), text(b)));
      const items = toHunks(rows, context);
      const label = JSON.stringify({ a, b, context });

      assert.deepEqual(items.flatMap((item) => item.rows), rows, label);
      assert.deepEqual(applyHunksStrictly(a, items), b, label);
      items.forEach((item, index) => {
        if (item.kind === 'collapsed') {
          assert.equal(item.count, item.rows.length, label);
          assert.ok(item.count > 0, label);
          assert.ok(item.rows.every((row) => row.kind === 'context'), label);
          assert.notEqual(items[index + 1]?.kind, 'collapsed', label);
          return;
        }
        assert.equal(item.header, `@@ -${item.oldStart},${item.oldLines} +${item.newStart},${item.newLines} @@`, label);
        assert.equal(item.oldLines, item.rows.filter((row) => row.kind !== 'add').length, label);
        assert.equal(item.newLines, item.rows.filter((row) => row.kind !== 'del').length, label);
        assert.notEqual(items[index + 1]?.kind, 'hunk', `${label} adjacent hunks would have been merged`);
        const changeIndexes = item.rows.flatMap((row, position) => (row.kind === 'context' ? [] : [position]));
        assert.ok(changeIndexes.length > 0, label);
        const leading = changeIndexes[0];
        const trailing = item.rows.length - 1 - changeIndexes[changeIndexes.length - 1];
        // A hunk next to a collapsed region carries exactly `context` lines
        // on that side; at a file edge it carries whatever exists (<= context).
        if (items[index - 1]?.kind === 'collapsed') assert.equal(leading, context, label);
        else assert.ok(leading <= context, label);
        if (items[index + 1]?.kind === 'collapsed') assert.equal(trailing, context, label);
        else assert.ok(trailing <= context, label);
        // Unchanged runs inside one hunk are never long enough to split it.
        for (let position = 1; position < changeIndexes.length; position += 1) {
          assert.ok(changeIndexes[position] - changeIndexes[position - 1] - 1 <= 2 * context, label);
        }
      });
      if (b.join() === a.join()) assert.equal(items.length, a.length === 0 ? 0 : 1, label);
    }
  });

  it('matches an independent window-based reference exactly (randomized)', () => {
    // Reference: a row is shown when it lies within `context` rows of any
    // change; maximal runs of shown rows are hunks, the rest collapse. An
    // empty side is anchored to the number of that side's rows before the
    // hunk, the unified-diff convention git and GNU diff both use.
    const referenceHunks = (rows, context) => {
      const shown = rows.map(() => false);
      rows.forEach((row, index) => {
        if (row.kind === 'context') return;
        const from = Math.max(0, index - context);
        const to = Math.min(rows.length - 1, index + context);
        for (let position = from; position <= to; position += 1) shown[position] = true;
      });
      const items = [];
      for (let start = 0; start < rows.length;) {
        let end = start;
        while (end < rows.length && shown[end] === shown[start]) end += 1;
        const slice = rows.slice(start, end);
        if (!shown[start]) {
          items.push({ kind: 'collapsed', count: slice.length, rows: slice });
        } else {
          const before = rows.slice(0, start);
          const oldRows = slice.filter((row) => row.kind !== 'add');
          const newRows = slice.filter((row) => row.kind !== 'del');
          const oldStart = oldRows.length > 0 ? oldRows[0].oldNo : before.filter((row) => row.kind !== 'add').length;
          const newStart = newRows.length > 0 ? newRows[0].newNo : before.filter((row) => row.kind !== 'del').length;
          items.push({
            kind: 'hunk',
            header: `@@ -${oldStart},${oldRows.length} +${newStart},${newRows.length} @@`,
            oldStart,
            oldLines: oldRows.length,
            newStart,
            newLines: newRows.length,
            rows: slice,
          });
        }
        start = end;
      }
      return items;
    };
    const random = createRandom(0xbadc0de);
    for (let iteration = 0; iteration < 1200; iteration += 1) {
      const original = lines(Math.floor(random() * 45), (index) => `L${index}`);
      const edited = random() < 0.2 ? ['top'] : [];
      for (const line of original) {
        const roll = random();
        if (roll < 0.07) continue;
        if (roll < 0.12) edited.push(`X${edited.length}`);
        else edited.push(line);
        if (random() < 0.05) edited.push(`I${edited.length}`);
      }
      const rows = rowsFor(text(original), text(edited));
      for (const context of [0, 1, 2, 3, 5, Infinity]) {
        const label = JSON.stringify({ original, edited, context });
        assert.deepEqual(toHunks(rows, context), referenceHunks(rows, context === Infinity ? rows.length : context), label);
      }
    }
  });

  it('rejects invalid context values and rows', () => {
    const rows = rowsFor('a', 'b');
    for (const bad of [-1, 1.5, Number.NaN, '3', null]) {
      assert.throws(() => toHunks(rows, bad), RangeError);
    }
    assert.throws(() => toHunks([{ kind: 'moved', oldNo: 1, newNo: 1, text: 'a' }]), TypeError);
    assert.throws(() => toHunks('rows'), TypeError);
  });
});

// ---------------------------------------------------------------------------
// toSplitRows
// ---------------------------------------------------------------------------

const ctx = (oldNo, newNo, text) => ({ kind: 'context', oldNo, newNo, text });
const del = (oldNo, text) => ({ kind: 'del', oldNo, newNo: null, text });
const add = (newNo, text) => ({ kind: 'add', oldNo: null, newNo, text });

describe('toSplitRows', () => {
  it('returns [] for no rows', () => {
    assert.deepEqual(toSplitRows([]), []);
  });

  it('shows context rows on both sides with each side\'s line number', () => {
    assert.deepEqual(toSplitRows([ctx(3, 5, 'same')]), [
      { left: { no: 3, text: 'same', kind: 'context' }, right: { no: 5, text: 'same', kind: 'context' } },
    ]);
  });

  it('pairs a deletion run with the following addition run line by line', () => {
    assert.deepEqual(toSplitRows([ctx(1, 1, 'a'), del(2, 'b'), del(3, 'c'), add(2, 'B'), add(3, 'C'), ctx(4, 4, 'd')]), [
      { left: { no: 1, text: 'a', kind: 'context' }, right: { no: 1, text: 'a', kind: 'context' } },
      { left: { no: 2, text: 'b', kind: 'del' }, right: { no: 2, text: 'B', kind: 'add' } },
      { left: { no: 3, text: 'c', kind: 'del' }, right: { no: 3, text: 'C', kind: 'add' } },
      { left: { no: 4, text: 'd', kind: 'context' }, right: { no: 4, text: 'd', kind: 'context' } },
    ]);
  });

  it('fills the shorter side with null when runs have different lengths', () => {
    assert.deepEqual(toSplitRows([del(1, 'a'), del(2, 'b'), add(1, 'A')]), [
      { left: { no: 1, text: 'a', kind: 'del' }, right: { no: 1, text: 'A', kind: 'add' } },
      { left: { no: 2, text: 'b', kind: 'del' }, right: null },
    ]);
    assert.deepEqual(toSplitRows([del(1, 'a'), add(1, 'A'), add(2, 'B')]), [
      { left: { no: 1, text: 'a', kind: 'del' }, right: { no: 1, text: 'A', kind: 'add' } },
      { left: null, right: { no: 2, text: 'B', kind: 'add' } },
    ]);
  });

  it('leaves the other side empty for pure additions and pure deletions', () => {
    assert.deepEqual(toSplitRows(rowsFor('', 'x')), [{ left: null, right: { no: 1, text: 'x', kind: 'add' } }]);
    assert.deepEqual(toSplitRows(rowsFor('x', '')), [{ left: { no: 1, text: 'x', kind: 'del' }, right: null }]);
  });

  it('does not pair an addition run with a deletion run that follows it', () => {
    assert.deepEqual(toSplitRows([add(1, 'new'), del(1, 'old')]), [
      { left: null, right: { no: 1, text: 'new', kind: 'add' } },
      { left: { no: 1, text: 'old', kind: 'del' }, right: null },
    ]);
  });

  it('shows an identical file as context on both sides', () => {
    assert.deepEqual(toSplitRows(rowsFor('a\r\nb\r\n', 'a\nb\n')), [
      { left: { no: 1, text: 'a', kind: 'context' }, right: { no: 1, text: 'a', kind: 'context' } },
      { left: { no: 2, text: 'b', kind: 'context' }, right: { no: 2, text: 'b', kind: 'context' } },
    ]);
  });

  it('never pairs a deletion with an addition across a context row', () => {
    assert.deepEqual(toSplitRows([del(1, 'a'), ctx(2, 1, 'm'), add(2, 'b')]), [
      { left: { no: 1, text: 'a', kind: 'del' }, right: null },
      { left: { no: 2, text: 'm', kind: 'context' }, right: { no: 1, text: 'm', kind: 'context' } },
      { left: null, right: { no: 2, text: 'b', kind: 'add' } },
    ]);
  });

  it('pairs each change block independently', () => {
    assert.deepEqual(toSplitRows([del(1, 'a'), add(1, 'A'), ctx(2, 2, 'm'), del(3, 'b'), del(4, 'c'), add(3, 'B')]), [
      { left: { no: 1, text: 'a', kind: 'del' }, right: { no: 1, text: 'A', kind: 'add' } },
      { left: { no: 2, text: 'm', kind: 'context' }, right: { no: 2, text: 'm', kind: 'context' } },
      { left: { no: 3, text: 'b', kind: 'del' }, right: { no: 3, text: 'B', kind: 'add' } },
      { left: { no: 4, text: 'c', kind: 'del' }, right: null },
    ]);
  });

  it('keeps every row from a real diff exactly once', () => {
    const rows = rowsFor('a\nb\nc\nd\ne\n', 'a\nB\nC\nX\nd\n');
    const split = toSplitRows(rows);
    assert.equal(split.filter((pair) => pair.left).length, rows.filter((row) => row.kind !== 'add').length);
    assert.equal(split.filter((pair) => pair.right).length, rows.filter((row) => row.kind !== 'del').length);
  });

  it('pairs rows exactly like a straightforward reference on random diffs', () => {
    const cell = (row, no) => ({ no, text: row.text, kind: row.kind });
    const referenceSplit = (rows) => {
      const result = [];
      for (let index = 0; index < rows.length;) {
        if (rows[index].kind === 'context') {
          result.push({ left: cell(rows[index], rows[index].oldNo), right: cell(rows[index], rows[index].newNo) });
          index += 1;
          continue;
        }
        const deleted = [];
        while (index < rows.length && rows[index].kind === 'del') deleted.push(rows[index++]);
        const added = [];
        while (index < rows.length && rows[index].kind === 'add') added.push(rows[index++]);
        for (let offset = 0; offset < Math.max(deleted.length, added.length); offset += 1) {
          result.push({
            left: deleted[offset] ? cell(deleted[offset], deleted[offset].oldNo) : null,
            right: added[offset] ? cell(added[offset], added[offset].newNo) : null,
          });
        }
      }
      return result;
    };
    const random = createRandom(77);
    for (let iteration = 0; iteration < 800; iteration += 1) {
      const a = lines(Math.floor(random() * 25), () => pick(random, ['a', 'b', 'c', '', '}']));
      const b = lines(Math.floor(random() * 25), () => pick(random, ['b', 'c', 'd', '', '}']));
      const rows = rowsFor(text(a), text(b));
      assert.deepEqual(toSplitRows(rows), referenceSplit(rows), JSON.stringify({ a, b }));
    }
  });

  it('rejects unknown row kinds instead of looping forever', () => {
    assert.throws(() => toSplitRows([{ kind: 'moved', text: 'a' }]), TypeError);
  });
});

// ---------------------------------------------------------------------------
// wordDiff
// ---------------------------------------------------------------------------

const joinSegments = (segments) => segments.map((segment) => segment.text).join('');

describe('wordDiff', () => {
  it('marks nothing as changed for identical lines', () => {
    assert.deepEqual(wordDiff('const a = 1;', 'const a = 1;'), {
      old: [{ text: 'const a = 1;', changed: false }],
      new: [{ text: 'const a = 1;', changed: false }],
    });
    assert.deepEqual(wordDiff('', ''), { old: [], new: [] });
    assert.deepEqual(wordDiff(null, undefined), { old: [], new: [] });
  });

  it('highlights only the changed word', () => {
    assert.deepEqual(wordDiff('const foo = 1;', 'const bar = 1;'), {
      old: [
        { text: 'const ', changed: false },
        { text: 'foo', changed: true },
        { text: ' = 1;', changed: false },
      ],
      new: [
        { text: 'const ', changed: false },
        { text: 'bar', changed: true },
        { text: ' = 1;', changed: false },
      ],
    });
  });

  it('treats punctuation as individual tokens', () => {
    assert.deepEqual(wordDiff('foo(bar)', 'foo[bar]'), {
      old: [
        { text: 'foo', changed: false },
        { text: '(', changed: true },
        { text: 'bar', changed: false },
        { text: ')', changed: true },
      ],
      new: [
        { text: 'foo', changed: false },
        { text: '[', changed: true },
        { text: 'bar', changed: false },
        { text: ']', changed: true },
      ],
    });
  });

  it('highlights whitespace-only changes', () => {
    assert.deepEqual(wordDiff('a  b', 'a\tb'), {
      old: [
        { text: 'a', changed: false },
        { text: '  ', changed: true },
        { text: 'b', changed: false },
      ],
      new: [
        { text: 'a', changed: false },
        { text: '\t', changed: true },
        { text: 'b', changed: false },
      ],
    });
  });

  it('marks the whole other side when one side is empty', () => {
    assert.deepEqual(wordDiff('', 'abc def'), { old: [], new: [{ text: 'abc def', changed: true }] });
    assert.deepEqual(wordDiff('abc', ''), { old: [{ text: 'abc', changed: true }], new: [] });
  });

  it('keeps astral characters such as emoji in one segment', () => {
    assert.deepEqual(wordDiff('😀 ok', '😁 ok'), {
      old: [{ text: '😀', changed: true }, { text: ' ok', changed: false }],
      new: [{ text: '😁', changed: true }, { text: ' ok', changed: false }],
    });
  });

  it('runs the LCS at exactly 40,000 token pairs and marks whole lines above that', () => {
    const old200 = 'x,'.repeat(100);
    const new200 = 'y,'.repeat(100);
    const atLimit = wordDiff(old200, new200);
    assert.equal(atLimit.old.length, 200, 'commas still match token by token');
    assert.ok(atLimit.old.some((segment) => !segment.changed));

    const new201 = `${new200}z`;
    assert.deepEqual(wordDiff(old200, new201), {
      old: [{ text: old200, changed: true }],
      new: [{ text: new201, changed: true }],
    });
  });

  it('reproduces each input exactly and finds a longest common token subsequence (randomized)', () => {
    const random = createRandom(99);
    const pieces = ['foo', 'bar', ' ', '  ', '\t', '(', ')', '.', '=', '😀', 'é', ' ', '_x1', '"', '\uD800'];
    const tokenize = (value) => value.match(/\s+|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/gu) ?? [];
    for (let iteration = 0; iteration < 2000; iteration += 1) {
      const oldLine = lines(Math.floor(random() * 10), () => pick(random, pieces)).join('');
      const newLine = lines(Math.floor(random() * 10), () => pick(random, pieces)).join('');
      const result = wordDiff(oldLine, newLine);
      const label = JSON.stringify({ oldLine, newLine });
      assert.equal(joinSegments(result.old), oldLine, label);
      assert.equal(joinSegments(result.new), newLine, label);
      for (const side of [result.old, result.new]) {
        side.forEach((segment, index) => {
          assert.ok(segment.text.length > 0, label);
          if (index > 0) assert.notEqual(segment.changed, side[index - 1].changed, `${label} segments are merged`);
        });
      }
      const unchangedTokens = (segments) => segments.filter((segment) => !segment.changed).flatMap((segment) => tokenize(segment.text));
      const common = lcsLength(tokenize(oldLine), tokenize(newLine));
      assert.equal(unchangedTokens(result.old).length, common, label);
      assert.deepEqual(unchangedTokens(result.old), unchangedTokens(result.new), label);
    }
  });

  it('never marks identical lines as changed, even above the token bound', () => {
    const long = 'token '.repeat(1000);
    assert.deepEqual(wordDiff(long, long), {
      old: [{ text: long, changed: false }],
      new: [{ text: long, changed: false }],
    });
  });

  it('marks the whole other side when one side is empty however long it is', () => {
    // Nothing can match an empty side, so the result needs no tokenizing;
    // it must still be exact, never truncated.
    const long = 'word '.repeat(50000);
    assert.deepEqual(wordDiff('', long), { old: [], new: [{ text: long, changed: true }] });
    assert.deepEqual(wordDiff(long, ''), { old: [{ text: long, changed: true }], new: [] });
  });

  it('stops tokenizing a huge line as soon as the token bound is exceeded', () => {
    // An 8 MB line of punctuation is 8M tokens. Collecting them all before
    // checking the bound took ~190 ms and ~350 MB of heap per call; the
    // bounded tokenizer reads at most 40,001 of them.
    const huge = ','.repeat(8_000_000);
    const changedWhole = (line) => (line === '' ? [] : [{ text: line, changed: true }]);
    // Warm the regex and tokenizer up first so a cold JIT on a busy machine
    // does not eat the time budget; the heap check below guards regressions.
    wordDiff('x', ','.repeat(50_000));
    for (const [oldLine, newLine] of [['x', huge], [huge, 'x'], [huge, `${huge};`], ['', huge], [huge, '']]) {
      const heapBefore = getHeapStatistics().total_heap_size;
      const start = performance.now();
      const result = wordDiff(oldLine, newLine);
      const elapsed = performance.now() - start;
      const heapGrowth = getHeapStatistics().total_heap_size - heapBefore;
      assert.deepEqual(result, { old: changedWhole(oldLine), new: changedWhole(newLine) });
      assert.ok(elapsed < 50, `took ${elapsed.toFixed(1)} ms`);
      assert.ok(heapGrowth < 100 * 1024 * 1024, `heap grew by ${(heapGrowth / 1048576).toFixed(0)} MB`);
    }
  });

  it('applies the token bound exactly when the two sides are lopsided', () => {
    // 3 tokens against 13,333 is 39,999 pairs: the LCS runs and the shared
    // comma stays unhighlighted. One more token is 40,002 pairs: whole lines.
    const within = wordDiff('a,b', ','.repeat(13333));
    assert.deepEqual(within.old, [
      { text: 'a', changed: true },
      { text: ',', changed: false },
      { text: 'b', changed: true },
    ]);
    assert.deepEqual(joinSegments(within.new), ','.repeat(13333));
    assert.deepEqual(wordDiff('a,b', ','.repeat(13334)), {
      old: [{ text: 'a,b', changed: true }],
      new: [{ text: ','.repeat(13334), changed: true }],
    });
    // 40,000 tokens against 1 is exactly the bound; 40,001 is past it.
    const atBound = wordDiff(','.repeat(40000), ',');
    assert.deepEqual(atBound.new, [{ text: ',', changed: false }]);
    assert.deepEqual(atBound.old, [{ text: ',', changed: false }, { text: ','.repeat(39999), changed: true }]);
    assert.deepEqual(wordDiff(','.repeat(40001), ','), {
      old: [{ text: ','.repeat(40001), changed: true }],
      new: [{ text: ',', changed: true }],
    });
  });

  it('treats a leftover CR as a whitespace change', () => {
    assert.deepEqual(wordDiff('a\r', 'a'), {
      old: [{ text: 'a', changed: false }, { text: '\r', changed: true }],
      new: [{ text: 'a', changed: false }],
    });
  });

  it('splits on identifier boundaries, not on letters inside an identifier', () => {
    assert.deepEqual(wordDiff('user_id1.name', 'user_id2.name'), {
      old: [{ text: 'user_id1', changed: true }, { text: '.name', changed: false }],
      new: [{ text: 'user_id2', changed: true }, { text: '.name', changed: false }],
    });
  });

  it('is deterministic', () => {
    assert.deepEqual(wordDiff('a b a b', 'b a b a'), wordDiff('a b a b', 'b a b a'));
  });
});

// ---------------------------------------------------------------------------
// diffStats
// ---------------------------------------------------------------------------

describe('diffStats', () => {
  it('counts added and deleted lines', () => {
    assert.deepEqual(diffStats(diffLines('a\nb\nc\n', 'a\nx\ny\nc\nd\n')), { additions: 3, deletions: 1 });
    assert.deepEqual(diffStats(diffLines('', 'a\nb')), { additions: 2, deletions: 0 });
    assert.deepEqual(diffStats(diffLines('a\nb', '')), { additions: 0, deletions: 2 });
  });

  it('reports zeros for empty and identical inputs', () => {
    assert.deepEqual(diffStats([]), { additions: 0, deletions: 0 });
    assert.deepEqual(diffStats(diffLines('a\nb', 'a\nb')), { additions: 0, deletions: 0 });
    assert.deepEqual(diffStats(diffLines('a\r\nb\r\n', 'a\nb\n')), { additions: 0, deletions: 0 });
  });

  it('counts whitespace-only and single-line changes', () => {
    assert.deepEqual(diffStats(diffLines('a\n  b\n', 'a\n\tb\n')), { additions: 1, deletions: 1 });
    assert.deepEqual(diffStats(diffLines('x', 'y')), { additions: 1, deletions: 1 });
  });

  it('rejects malformed ops', () => {
    assert.throws(() => diffStats([{ type: 'moved', lines: [] }]), TypeError);
    assert.throws(() => diffStats(null), TypeError);
  });
});

// ---------------------------------------------------------------------------
// createUnifiedPatch (patch text)
// ---------------------------------------------------------------------------

describe('createUnifiedPatch', () => {
  it('produces a git-style patch for a modification', () => {
    const patch = createUnifiedPatch([{ path: 'src/app.js', before: 'a\nb\nc\n', after: 'a\nB\nc\n' }]);
    assert.equal(patch, [
      'diff --git a/src/app.js b/src/app.js',
      '--- a/src/app.js',
      '+++ b/src/app.js',
      '@@ -1,3 +1,3 @@',
      ' a',
      '-b',
      '+B',
      ' c',
      '',
    ].join('\n'));
  });

  it('marks new files with /dev/null and a file mode', () => {
    assert.equal(createUnifiedPatch([{ path: 'new.txt', before: '', after: 'x\ny\n', isNew: true }]), [
      'diff --git a/new.txt b/new.txt',
      'new file mode 100644',
      '--- /dev/null',
      '+++ b/new.txt',
      '@@ -0,0 +1,2 @@',
      '+x',
      '+y',
      '',
    ].join('\n'));
  });

  it('marks deleted files with /dev/null and a file mode', () => {
    assert.equal(createUnifiedPatch([{ path: 'old.txt', before: 'x\ny\n', after: '', isDeleted: true }]), [
      'diff --git a/old.txt b/old.txt',
      'deleted file mode 100644',
      '--- a/old.txt',
      '+++ /dev/null',
      '@@ -1,2 +0,0 @@',
      '-x',
      '-y',
      '',
    ].join('\n'));
  });

  it('writes only the header for a new or deleted empty file', () => {
    assert.equal(createUnifiedPatch([{ path: 'e.txt', isNew: true }]), 'diff --git a/e.txt b/e.txt\nnew file mode 100644\n');
    assert.equal(createUnifiedPatch([{ path: 'e.txt', isDeleted: true }]), 'diff --git a/e.txt b/e.txt\ndeleted file mode 100644\n');
  });

  it('adds the no-newline marker after the last line of the side that lacks one', () => {
    assert.equal(createUnifiedPatch([{ path: 'f', before: 'a\nb', after: 'a\nb\nc\n' }]), [
      'diff --git a/f b/f',
      '--- a/f',
      '+++ b/f',
      '@@ -1,2 +1,3 @@',
      ' a',
      '-b',
      '\\ No newline at end of file',
      '+b',
      '+c',
      '',
    ].join('\n'));
    assert.equal(createUnifiedPatch([{ path: 'f', before: 'a\nb\n', after: 'a\nc' }]), [
      'diff --git a/f b/f',
      '--- a/f',
      '+++ b/f',
      '@@ -1,2 +1,2 @@',
      ' a',
      '-b',
      '+c',
      '\\ No newline at end of file',
      '',
    ].join('\n'));
  });

  it('adds the marker once after an unchanged last line when both sides lack a newline', () => {
    assert.equal(createUnifiedPatch([{ path: 'f', before: 'a\nz', after: 'b\nz' }]), [
      'diff --git a/f b/f',
      '--- a/f',
      '+++ b/f',
      '@@ -1,2 +1,2 @@',
      '-a',
      '+b',
      ' z',
      '\\ No newline at end of file',
      '',
    ].join('\n'));
  });

  it('treats adding or removing only the final newline as a change', () => {
    assert.equal(createUnifiedPatch([{ path: 'f', before: 'a', after: 'a\n' }]), [
      'diff --git a/f b/f',
      '--- a/f',
      '+++ b/f',
      '@@ -1,1 +1,1 @@',
      '-a',
      '\\ No newline at end of file',
      '+a',
      '',
    ].join('\n'));
    assert.match(createUnifiedPatch([{ path: 'f', before: 'a\n', after: 'a' }]), /\n-a\n\+a\n\\ No newline at end of file\n$/);
  });

  it('keeps whitespace-only changes verbatim after the line prefix', () => {
    assert.equal(createUnifiedPatch([{ path: 'w', before: 'if (x) {\n  return 1;\n}\ntail \n', after: 'if (x) {\n\treturn 1;\n}\ntail\n' }]), [
      'diff --git a/w b/w',
      '--- a/w',
      '+++ b/w',
      '@@ -1,4 +1,4 @@',
      ' if (x) {',
      '-  return 1;',
      '+\treturn 1;',
      ' }',
      '-tail ',
      '+tail',
      '',
    ].join('\n'));
  });

  it('marks both sides of a single-line file that has no trailing newline', () => {
    assert.equal(createUnifiedPatch([{ path: 's', before: 'x', after: 'y' }]), [
      'diff --git a/s b/s',
      '--- a/s',
      '+++ b/s',
      '@@ -1,1 +1,1 @@',
      '-x',
      '\\ No newline at end of file',
      '+y',
      '\\ No newline at end of file',
      '',
    ].join('\n'));
  });

  it('skips unchanged files and returns an empty string when nothing changed', () => {
    assert.equal(createUnifiedPatch([]), '');
    assert.equal(createUnifiedPatch([{ path: 'same.txt', before: 'a\n', after: 'a\n' }]), '');
    const patch = createUnifiedPatch([
      { path: 'same.txt', before: 'a\n', after: 'a\n' },
      { path: 'changed.txt', before: 'a\n', after: 'b\n' },
    ]);
    assert.ok(!patch.includes('same.txt'));
    assert.ok(patch.startsWith('diff --git a/changed.txt b/changed.txt\n'));
  });

  it('concatenates files in order and always ends with a newline', () => {
    const patch = createUnifiedPatch([
      { path: 'one', before: '1\n', after: '2\n' },
      { path: 'two', before: '', after: 'x', isNew: true },
    ]);
    assert.ok(patch.indexOf('diff --git a/one b/one') < patch.indexOf('diff --git a/two b/two'));
    assert.ok(patch.endsWith('\n'));
    assert.ok(patch.endsWith('+x\n\\ No newline at end of file\n'));
  });

  it('emits separate hunks for changes far apart and honours the context option', () => {
    const before = lines(100);
    const after = before.slice();
    after[4] = 'early';
    after[89] = 'late';
    const patch = createUnifiedPatch([{ path: 'f', before: text(before), after: text(after) }]);
    assert.deepEqual(patch.match(/^@@ .* @@$/gm), ['@@ -2,7 +2,7 @@', '@@ -87,7 +87,7 @@']);
    const tight = createUnifiedPatch([{ path: 'f', before: text(before), after: text(after) }], { context: 1 });
    assert.deepEqual(tight.match(/^@@ .* @@$/gm), ['@@ -4,3 +4,3 @@', '@@ -89,3 +89,3 @@']);
  });

  it('keeps CRLF line endings byte-exact so the patch applies to the real file', () => {
    const patch = createUnifiedPatch([{ path: 'w.txt', before: 'a\r\nb\r\n', after: 'a\r\nB\r\n' }]);
    assert.ok(patch.includes('\n a\r\n-b\r\n+B\r\n'));
    // A pure line-ending conversion is a real change for git.
    assert.match(createUnifiedPatch([{ path: 'w.txt', before: 'a\r\n', after: 'a\n' }]), /\n-a\r\n\+a\n$/);
  });

  it('treats null before/after as empty content', () => {
    assert.equal(
      createUnifiedPatch([{ path: 'n.txt', before: null, after: 'x\n', isNew: true }]),
      createUnifiedPatch([{ path: 'n.txt', before: '', after: 'x\n', isNew: true }]),
    );
  });

  it('quotes paths like git: tab after names with spaces, C-style escapes for special characters', () => {
    const spaced = createUnifiedPatch([{ path: 'my dir/a b.txt', before: '1\n', after: '2\n' }]);
    assert.ok(spaced.startsWith('diff --git a/my dir/a b.txt b/my dir/a b.txt\n--- a/my dir/a b.txt\t\n+++ b/my dir/a b.txt\t\n'));

    const special = createUnifiedPatch([{ path: 'q"t\\b\tn\nx\x01\x7f.txt', before: '1\n', after: '2\n' }]);
    const label = 'q\\"t\\\\b\\tn\\nx\\001\\177.txt';
    assert.ok(special.startsWith(`diff --git "a/${label}" "b/${label}"\n--- "a/${label}"\n+++ "b/${label}"\n`));

    const unicode = createUnifiedPatch([{ path: 'café/日本.txt', before: '1\n', after: '2\n' }]);
    assert.ok(unicode.startsWith('diff --git a/café/日本.txt b/café/日本.txt\n'));
  });

  it('rejects invalid input', () => {
    assert.throws(() => createUnifiedPatch('nope'), TypeError);
    assert.throws(() => createUnifiedPatch([null]), TypeError);
    assert.throws(() => createUnifiedPatch([{ before: 'a', after: 'b' }]), TypeError);
    assert.throws(() => createUnifiedPatch([{ path: '', before: 'a', after: 'b' }]), TypeError);
    assert.throws(() => createUnifiedPatch([{ path: 'f', before: 'a', after: 'b', isNew: true }]), /new file/);
    assert.throws(() => createUnifiedPatch([{ path: 'f', before: 'a', after: 'b', isDeleted: true }]), /deleted file/);
    assert.throws(() => createUnifiedPatch([{ path: 'f', isNew: true, isDeleted: true }]), /both new and deleted/);
    assert.throws(() => createUnifiedPatch([{ path: 'f', before: 'a', after: 'b' }], { context: -1 }), RangeError);
  });

  it('is deterministic', () => {
    const files = [{ path: 'f', before: 'a\nb\nc\nb\n', after: 'b\nc\nx\nb\n' }];
    assert.equal(createUnifiedPatch(files), createUnifiedPatch(files));
  });

  // git apply refuses every one of these, but patch(1) follows "../" and
  // absolute names out of the checkout and writes into .git (a hook there
  // runs on the next commit), and git apply itself truncates a name at NUL
  // and patches a different file. Backslash is a separator on Windows.
  const UNSAFE_PATHS = [
    '../escape.txt',
    'src/../../escape.txt',
    '/etc/passwd',
    '.git/hooks/pre-commit',
    '.GIT/config',
    '.Git/hooks/post-checkout',
    'vendor/lib/.git/config',
    '.git',
    '.git./config',
    '.git /config',
    '.git. . /config',
    'git~1/config',
    'GIT~1/hooks/pre-commit',
    '.git:stream/config',
    'a//b.txt',
    './a.txt',
    'a/./b.txt',
    'dir/',
    'readme.md\0.txt',
    'readme.md\0../../escape.txt',
    '..\\escape.txt',
    'src\\..\\..\\escape.txt',
    '.git\\hooks\\pre-commit',
    '\\absolute.txt',
    // HFS+ skips these code points when comparing names, so each is .git on
    // such a disk (git apply refuses them on macOS via core.protectHFS).
    '.g‌it/hooks/pre-commit',
    '﻿.git/config',
    '.GI‍T/config',
    'sub/.gi⁯t/config',
    '.‮git/hooks/post-merge',
  ];

  it('refuses paths that are absolute, climb out, or point into .git', () => {
    for (const path of UNSAFE_PATHS) {
      assert.throws(
        () => createUnifiedPatch([{ path, before: 'a\n', after: 'b\n' }]),
        /^Error: Unsafe patch path /,
        JSON.stringify(path),
      );
      // A new file is refused too, as is an unchanged file: the path is
      // checked before anything else.
      assert.throws(() => createUnifiedPatch([{ path, after: 'x\n', isNew: true }]), /Unsafe patch path/, JSON.stringify(path));
      assert.throws(() => createUnifiedPatch([{ path, before: 'same', after: 'same' }]), /Unsafe patch path/, JSON.stringify(path));
    }
  });

  it('accepts ordinary names that merely resemble unsafe ones', () => {
    const lookalikes = [
      '.gitignore',
      '.github/workflows/ci.yml',
      '.gitattributes',
      'docs/x.git',
      '.gitx/notes',
      'git~10/file',
      'git~1.txt/file',
      '...',
      'a..b/c..',
      '..hidden',
      'name.git.bak',
      'back\\slash.txt',
      'zero‌width.txt',
      '.gi‌t.d/x',
    ];
    for (const path of lookalikes) {
      assert.match(createUnifiedPatch([{ path, before: 'a\n', after: 'b\n' }]), /^diff --git /, JSON.stringify(path));
    }
  });

  it('keeps error messages short and on one line, without running caller code', () => {
    const hostile = { toString() { throw new Error('caller code ran'); } };
    assert.throws(() => diffLines('a', 'b', { maxCost: hostile }), (error) => error instanceof RangeError && /an object/.test(error.message));
    assert.throws(() => diffLines('a', 'b', { maxCost: Object.create(null) }), RangeError);
    assert.throws(() => toHunks([], hostile), (error) => error instanceof RangeError && /an object/.test(error.message));
    assert.throws(() => toRows([{ type: hostile, lines: [] }]), (error) => error instanceof TypeError && /an object/.test(error.message));
    assert.throws(() => toSplitRows([{ kind: Object.create(null) }]), (error) => error instanceof TypeError && /an object/.test(error.message));
    assert.throws(() => diffStats([{ type: () => {}, lines: [] }]), /a function/);

    // A path that is huge and full of newlines must not flood or forge log lines.
    const forged = `${'x/'.repeat(50000)}../\nERROR forged log line`;
    assert.throws(() => createUnifiedPatch([{ path: forged, before: 'a', after: 'b' }]), (error) => {
      assert.ok(!error.message.includes('\n'), 'message stays on one line');
      assert.ok(error.message.length < 200, `message is ${error.message.length} characters`);
      return true;
    });
    assert.throws(() => createUnifiedPatch([{ path: 'new\nline', before: 'a', after: 'b', isNew: true }]), (error) => {
      assert.equal(error.message, '"new\\nline": a new file must have empty "before" content.');
      return true;
    });
  });
});

// ---------------------------------------------------------------------------
// Purity
// ---------------------------------------------------------------------------

const deepFreeze = (value) => {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
};

describe('purity', () => {
  it('never mutates ops, rows or files passed in (frozen inputs would throw)', () => {
    const ops = deepFreeze(diffLines('a\nb\nc\nd\n', 'a\nB\nc\nD\ne\n'));
    const rows = deepFreeze(toRows(ops));
    const expectedRows = toRows(diffLines('a\nb\nc\nd\n', 'a\nB\nc\nD\ne\n'));
    assert.deepEqual(rows, expectedRows);
    assert.deepEqual(diffStats(ops), { additions: 3, deletions: 2 });
    assert.deepEqual(toHunks(rows, 0).flatMap((item) => item.rows), expectedRows);
    assert.equal(toSplitRows(rows).length, 5);
    const files = deepFreeze([{ path: 'f', before: 'a\n', after: 'b\n' }]);
    assert.ok(createUnifiedPatch(files, deepFreeze({ context: 1 })).includes('-a\n+b\n'));
  });
});

// ---------------------------------------------------------------------------
// createUnifiedPatch verified by real `git apply`
// ---------------------------------------------------------------------------

describe('createUnifiedPatch with git apply', { skip: gitAvailable ? false : 'git is not installed' }, () => {
  it('applies a modification in the middle of a file', () => {
    const before = lines(30);
    const after = [...before.slice(0, 14), 'replaced 15', 'inserted', ...before.slice(15)];
    assertGitApplies([{ path: 'middle.txt', before: text(before), after: text(after) }]);
  });

  it('applies a new file, including one in a new directory', () => {
    assertGitApplies([
      { path: 'brand-new.txt', before: '', after: 'hello\nworld\n', isNew: true },
      { path: 'nested/deeper/file.js', after: 'export const x = 1;\n', isNew: true },
    ]);
  });

  it('applies changes where one side has no trailing newline', () => {
    assertGitApplies([
      { path: 'loses-newline.txt', before: 'a\nb\nc\n', after: 'a\nb\nc' },
      { path: 'gains-newline.txt', before: 'a\nb\nc', after: 'a\nb\nc\n' },
      { path: 'edit-no-newline.txt', before: 'a\nb\nc', after: 'a\nB\nc\nd' },
      { path: 'both-without.txt', before: 'x\ny\nz', after: 'X\ny\nz' },
    ]);
  });

  it('applies multiple hunks far apart', () => {
    const before = lines(200);
    const after = before.slice();
    after[3] = 'edit near the top';
    after.splice(100, 2, 'middle replacement');
    after[after.length - 2] = 'edit near the bottom';
    const patch = assertGitApplies([{ path: 'far.txt', before: text(before), after: text(after) }]);
    assert.equal(patch.match(/^@@ /gm).length, 3);
  });

  it('applies a deleted file and a deleted file without a trailing newline', () => {
    assertGitApplies([
      { path: 'gone.txt', before: 'x\ny\n', after: '', isDeleted: true },
      { path: 'gone-no-newline.txt', before: 'x\ny', isDeleted: true },
    ]);
  });

  it('applies new and deleted empty files', () => {
    assertGitApplies([
      { path: 'empty-new.txt', isNew: true },
      { path: 'empty-old.txt', before: '', isDeleted: true },
    ]);
  });

  it('applies changes to CRLF files byte-exactly', () => {
    assertGitApplies([
      { path: 'windows.txt', before: 'a\r\nb\r\nc\r\n', after: 'a\r\nB\r\nc\r\nd\r\n' },
      { path: 'converted.txt', before: 'a\r\nb\r\n', after: 'a\nb\n' },
    ]);
  });

  it('applies changes to files whose paths need quoting', { skip: process.platform === 'win32' ? 'path characters not allowed' : false }, () => {
    assertGitApplies([
      { path: 'dir with space/a b.txt', before: '1\n2\n', after: '1\n3\n' },
      { path: 'quote"and\\slash.txt', before: 'x\n', after: 'y\n' },
      { path: 'tab\there.txt', before: '', after: 'z\n', isNew: true },
    ]);
  });

  it('applies names with control characters, edge spaces, or a space next to a quote', { skip: process.platform === 'win32' ? 'path characters not allowed' : false }, () => {
    // Quoted names get C-style escapes, and git still appends a tab to a
    // quoted ---/+++ label that contains a space; leading and trailing
    // spaces are only unambiguous because the two names must be identical.
    for (const path of ['new\nline.txt', 'ctrl\x01\x7f.txt', 'cr\r.txt', 'a "b.txt', ' lead.txt', 'trail.txt ', 'b/a b/c']) {
      assertGitApplies([{ path, before: '1\n2\n', after: '1\n3\n' }]);
      assertGitApplies([{ path, after: 'z\n', isNew: true }]);
      assertGitApplies([{ path, before: 'z\n', isDeleted: true }]);
    }
  });

  it('applies content lines that look like patch syntax', () => {
    // The hunk body is parsed by counting lines from the header, so file
    // content that resembles headers, markers or prefixes must stay data.
    const tricky = [
      'diff --git a/x b/x',
      '--- a/x',
      '+++ b/x',
      '@@ -1,2 +1,2 @@',
      '\\ No newline at end of file',
      '-',
      '+',
      ' ',
      '',
      'plain',
    ];
    const before = text([...tricky, ...tricky], false);
    const after = text([...tricky.slice(1), 'changed', ...tricky.slice(0, 5), '+++ b/y', ...tricky.slice(5)], false);
    for (const context of [1, 3]) {
      assertGitApplies([
        { path: 'tricky.txt', before, after },
        { path: 'marker-last.txt', before: 'a\n\\ No newline at end of file', after: 'b\n\\ No newline at end of file\n' },
        { path: 'tricky-new.txt', after: `${tricky.join('\n')}\n`, isNew: true },
      ], { context });
    }
  });

  it('applies names that resemble refused ones (the path guard is not over-broad)', { skip: process.platform === 'win32' ? 'backslash is a separator there' : false }, () => {
    assertGitApplies([
      { path: '.gitignore', before: 'node_modules\n', after: 'node_modules\ndist\n' },
      { path: '.github/workflows/ci.yml', after: 'on: push\n', isNew: true },
      { path: 'docs/x.git', before: 'a\n', after: 'b\n' },
      { path: '.gitx/notes', before: 'a\n', after: 'b\n' },
      { path: 'git~10/file', before: 'a\n', after: 'b\n' },
      { path: 'git~1.txt/file', before: 'a\n', after: 'b\n' },
      { path: '...', before: 'a\n', after: 'b\n' },
      { path: 'a..b/c..', before: 'a\n', after: 'b\n' },
      { path: '..hidden', before: 'a\n', after: 'b\n' },
      { path: 'back\\slash.txt', before: 'a\n', after: 'b\n' },
      { path: 'zero‌width.txt', before: 'a\n', after: 'b\n' },
    ]);
  });

  it('applies zero-context patches with --unidiff-zero (anchors use the preceding line)', () => {
    const before = lines(12);
    const after = [...before.slice(0, 5), 'inserted', ...before.slice(5, 8), ...before.slice(9)];
    assertGitApplies([
      { path: 'zero.txt', before: text(before), after: text(after) },
      { path: 'top.txt', before: 'b\nc\n', after: 'a\nb\nc\n' },
      { path: 'end.txt', before: 'a\nb', after: 'a\nb\nc\n' },
    ], { context: 0 }, ['--unidiff-zero']);
  });

  it('rejects a patch whose preimage does not match the file (the harness can fail)', () => {
    const root = mkdtempSync(join(tmpdir(), 'codebase-diff-'));
    try {
      const repo = join(root, 'repo');
      mkdirSync(repo);
      assert.equal(runGit(repo, ['init', '-q']).status, 0);
      writeFileSync(join(repo, 'drift.txt'), 'a\nDRIFTED\nc\n');
      const patchPath = join(root, 'change.patch');
      writeFileSync(patchPath, createUnifiedPatch([{ path: 'drift.txt', before: 'a\nb\nc\n', after: 'a\nB\nc\n' }]));
      assert.notEqual(runGit(repo, ['apply', '--check', patchPath]).status, 0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('applies whitespace-only changes and single-line files', () => {
    assertGitApplies([
      { path: 'indent.txt', before: 'if (x) {\n  return 1;\n}\n', after: 'if (x) {\n\treturn 1;\n}\n' },
      { path: 'trailing.txt', before: 'a \nb\n', after: 'a\nb  \n' },
      { path: 'blank-lines.txt', before: 'a\nb\n', after: 'a\n\n\nb\n' },
      { path: 'one-line.txt', before: 'x', after: 'y' },
      { path: 'one-line-eol.txt', before: 'x\n', after: 'y\n' },
    ]);
  });

  it('applies edits that fill an empty file or empty a file without new/deleted flags', () => {
    assertGitApplies([
      { path: 'was-empty.txt', before: '', after: 'now\nfilled\n' },
      { path: 'now-empty.txt', before: 'gone\n', after: '' },
      { path: 'now-empty-no-eol.txt', before: 'gone', after: '' },
    ]);
  });

  it('applies the single block emitted when the edit distance exceeds the default maxCost', () => {
    // 20,000 one-sided lines use up the whole default budget, and swapping S
    // and T costs 2 more, so D = 20,002. An exact diff would keep S or T as
    // context; the fallback must be one block with no context lines at all.
    const before = ['S', ...lines(10000, (index) => `old ${index}`), 'T'];
    const after = ['T', ...lines(10000, (index) => `new ${index}`), 'S'];
    const patch = assertGitApplies([{ path: 'rewrite.txt', before: text(before), after: text(after) }]);
    assert.deepEqual(patch.match(/^@@ .* @@$/gm), ['@@ -1,10002 +1,10002 @@']);
    assert.equal(patch.split('\n').filter((line) => line.startsWith(' ')).length, 0);
  });

  it('matches git diff hunk for hunk on unambiguous edits at contexts 0 to 4', () => {
    // With unique lines kept in order there is exactly one longest common
    // subsequence, so git's own output is a byte-exact oracle for hunk
    // grouping, header numbers (including the anchor of a zero-line side at
    // context 0) and the placement of the no-newline marker.
    const hunkSection = (patch) => patch.slice(patch.search(/^@@ /m));
    // git omits a count of 1 and appends the enclosing function after @@.
    const normalizeGitHeaders = (section) => section.replace(
      /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$/gm,
      (_, oldStart, oldLines = '1', newStart, newLines = '1') => `@@ -${oldStart},${oldLines} +${newStart},${newLines} @@`,
    );
    const random = createRandom(31337);
    const root = mkdtempSync(join(tmpdir(), 'codebase-diff-oracle-'));
    let compared = 0;
    try {
      for (let iteration = 0; iteration < 50; iteration += 1) {
        let counter = 0;
        const original = lines(Math.floor(random() * 50), () => `L${counter++}`);
        const edited = [];
        if (random() < 0.15) edited.push(`N${counter++}`);
        for (const line of original) {
          const roll = random();
          if (roll < 0.08) continue;
          if (roll < 0.14) {
            edited.push(`N${counter++}`);
            continue;
          }
          edited.push(line);
          if (random() < 0.06) edited.push(`N${counter++}`);
        }
        const context = iteration % 5;
        const before = text(original, random() < 0.8);
        const after = text(edited, random() < 0.8);
        if (before === after) continue;
        writeFileSync(join(root, 'old'), before);
        writeFileSync(join(root, 'new'), after);
        const result = runGit(root, ['diff', '--no-index', '--no-color', '--no-ext-diff', '--diff-algorithm=myers', `-U${context}`, 'old', 'new']);
        assert.equal(result.status, 1, `git diff failed:\n${result.stderr}`);
        const label = JSON.stringify({ before, after, context });
        assert.equal(hunkSection(createUnifiedPatch([{ path: 'f', before, after }], { context })), normalizeGitHeaders(hunkSection(result.stdout)), label);
        compared += 1;
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
    assert.ok(compared >= 40, `compared ${compared} cases`);
  });

  it('applies randomized multi-file patches at several context sizes', () => {
    const vocabulary = ['', '}', '{', '  return x;', 'const a = 1;', 'b', 'c', '\tindented', 'trailing  ', 'é'];
    for (const [seed, context] of [[1, 0], [2, 1], [3, 3], [4, 5]]) {
      const random = createRandom(seed);
      const files = [];
      for (let index = 0; index < 30; index += 1) {
        const crlf = random() < 0.15;
        const render = (values, trailing) => {
          const joined = values.join(crlf ? '\r\n' : '\n');
          return values.length === 0 ? '' : `${joined}${trailing ? (crlf ? '\r\n' : '\n') : ''}`;
        };
        const original = lines(Math.floor(random() * 25), () => pick(random, vocabulary));
        const edited = original.slice();
        const edits = Math.floor(random() * 5);
        for (let edit = 0; edit < edits; edit += 1) {
          const position = Math.floor(random() * (edited.length + 1));
          const action = random();
          if (action < 0.35) edited.splice(position, 0, pick(random, vocabulary));
          else if (action < 0.7) edited.splice(position, 1);
          else edited.splice(position, 1, `edited ${edit}`);
        }
        const before = render(original, random() < 0.8);
        const after = render(edited, random() < 0.8);
        const kind = random();
        const path = `fuzz/${index % 3}/file-${index}.txt`;
        if (kind < 0.1) files.push({ path, before: '', after, isNew: true });
        else if (kind < 0.2) files.push({ path, before, after: '', isDeleted: true });
        else if (kind < 0.25) files.push({ path, before, after: before });
        else files.push({ path, before, after });
      }
      assertGitApplies(files, { context }, context === 0 ? ['--unidiff-zero'] : []);
    }
  });
});
