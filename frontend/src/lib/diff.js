/**
 * Line and word diffing for the code review surface (green additions, red
 * deletions, unified and split views, downloadable git patches).
 *
 * The module is dependency-free standard JavaScript so the exact same code
 * runs in the browser bundle and under `node --test`. Every export is pure:
 * inputs are never mutated and identical inputs always produce identical
 * output, which keeps React memoisation and test expectations stable.
 */

const DEFAULT_MAX_COST = 20000;
const DEFAULT_CONTEXT = 3;
const WORD_DIFF_MAX_CELLS = 40000;
const NO_NEWLINE_MARKER = '\\ No newline at end of file';
// Sentinel from the bounded Myers search: the edit distance exceeds maxCost,
// or proving it does not would take more effort than maxCost allows.
const ABORTED = Symbol('aborted');
// Effort the bounded search may spend per line of input on top of its
// maxCost-sized allowance (see searchEffortLimit). 64 per line is what a
// search with D <= 62 can need, so small edits in huge files stay exact.
const SEARCH_EFFORT_PER_LINE = 64;
// Longest excerpt of a rejected string quoted in an error message.
const MAX_QUOTED_LENGTH = 80;

// git's own directory under every name git apply refuses for it: ".git" in
// any case, plus what Windows resolves to it (trailing dots or spaces, the
// 8.3 short name "git~1", an NTFS stream suffix after a colon).
const GIT_DIR_SEGMENT = /^(?:\.git|git~1)[. ]*(?::|$)/i;
// Code points HFS+ skips when comparing names (git's next_hfs_char list), so
// ".g‌it" is the .git directory on such a disk. git apply refuses these
// names on macOS (core.protectHFS), but patch(1) would write straight into
// .git/hooks, so they are stripped before the segment test above.
const HFS_IGNORABLE = /[‌-‏‪-‮⁪-⁯﻿]/g;

// Whitespace runs, identifier-like runs, or one other code point. The `u`
// flag keeps astral characters (emoji, CJK extensions) as single tokens so a
// highlight never splits a surrogate pair into two unrenderable halves.
const TOKEN_PATTERN = /\s+|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/gu;

// git's C-style quoting table for paths (quote_c_style). Characters outside
// this map that still need quoting are emitted as three-digit octal escapes.
const C_STYLE_ESCAPES = new Map([
  ['\x07', 'a'],
  ['\b', 'b'],
  ['\t', 't'],
  ['\n', 'n'],
  ['\v', 'v'],
  ['\f', 'f'],
  ['\r', 'r'],
  ['"', '"'],
  ['\\', '\\'],
]);

/**
 * @typedef {{ type: 'equal' | 'insert' | 'delete', lines: string[] }} DiffOp
 * @typedef {{ kind: 'context' | 'add' | 'del', oldNo: number | null, newNo: number | null, text: string }} DiffRow
 * @typedef {{ kind: 'hunk', header: string, oldStart: number, oldLines: number, newStart: number, newLines: number, rows: DiffRow[] }} DiffHunk
 * @typedef {{ kind: 'collapsed', count: number, rows: DiffRow[] }} CollapsedRegion
 * @typedef {{ no: number | null, text: string, kind: 'context' | 'add' | 'del' }} SplitCell
 * @typedef {{ text: string, changed: boolean }} WordSegment
 */

// API payloads can carry null for a missing file side; treating it as empty
// text lets callers pass raw responses without defensive conversions.
const toText = (value) => (value == null ? '' : String(value));

// Names a rejected value in an error message. String() is avoided for
// objects because it runs caller code (a hostile toString can throw a
// different error or return megabytes), and strings are quoted and cut short
// so a path with newlines or a huge value cannot forge or flood log lines.
const describeValue = (value) => {
  if (typeof value === 'string') {
    const excerpt = value.length > MAX_QUOTED_LENGTH ? `${value.slice(0, MAX_QUOTED_LENGTH)}...` : value;
    return JSON.stringify(excerpt);
  }
  if (typeof value === 'function') return 'a function';
  if (value !== null && typeof value === 'object') return Array.isArray(value) ? 'an array' : 'an object';
  return String(value);
};

const assertMaxCost = (maxCost) => {
  if (typeof maxCost !== 'number' || Number.isNaN(maxCost) || maxCost < 0) {
    throw new RangeError(`maxCost must be a non-negative number, received ${describeValue(maxCost)}`);
  }
};

const assertContext = (context) => {
  // Infinity is allowed on purpose: it yields one hunk spanning the whole
  // file, which is how a "show full file" toggle can reuse the same code.
  const valid = context === Infinity || (Number.isInteger(context) && context >= 0);
  if (!valid) {
    throw new RangeError(`context must be a non-negative integer, received ${describeValue(context)}`);
  }
};

const assertOp = (op) => {
  if (!op || !Array.isArray(op.lines)) {
    throw new TypeError('Each diff op must be an object with a lines array.');
  }
  if (op.type !== 'equal' && op.type !== 'insert' && op.type !== 'delete') {
    throw new TypeError(`Unknown diff op type: ${describeValue(op.type)}`);
  }
};

const assertRows = (rows) => {
  if (!Array.isArray(rows)) throw new TypeError('rows must be an array.');
  // Validating up front matters for toSplitRows: an unknown kind would never
  // be consumed by its run-collecting loops and would spin forever.
  for (const row of rows) {
    if (!row || (row.kind !== 'context' && row.kind !== 'add' && row.kind !== 'del')) {
      throw new TypeError(`Unknown diff row kind: ${describeValue(row?.kind)}`);
    }
  }
};

/**
 * Split text into lines the way editors and git count them.
 *
 * @param {string} text
 * @returns {string[]}
 */
export const splitLines = (text) => {
  const normalized = toText(text).replace(/\r\n?/g, '\n');
  if (normalized === '') return [];
  const lines = normalized.split('\n');
  // A final newline terminates the last line rather than opening a new empty
  // one, so "a\n" is one line and "a\n\n" is two.
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
};

/**
 * Whether text ends with a line terminator (LF, CRLF or a lone CR).
 * splitLines deliberately hides this, so the UI asks separately when it needs
 * to warn about a missing final newline.
 *
 * @param {string} text
 * @returns {boolean}
 */
export const hasTrailingNewline = (text) => {
  const value = toText(text);
  return value.endsWith('\n') || value.endsWith('\r');
};

/**
 * Collects edits in order and normalises them so every change block between
 * two equal runs is exactly one delete followed by one insert. Any order of
 * deletes and inserts between the same equal runs is an equivalent edit
 * script, and the canonical order is what review UIs and git expect.
 */
const createOpCollector = (oldLines, newLines) => {
  const ops = [];
  let deleted = [];
  let inserted = [];

  const flushChanges = () => {
    if (deleted.length > 0) {
      ops.push({ type: 'delete', lines: deleted });
      deleted = [];
    }
    if (inserted.length > 0) {
      ops.push({ type: 'insert', lines: inserted });
      inserted = [];
    }
  };

  return {
    equal(start, end) {
      if (start >= end) return;
      flushChanges();
      let last = ops[ops.length - 1];
      if (!last || last.type !== 'equal') {
        last = { type: 'equal', lines: [] };
        ops.push(last);
      }
      // Push one at a time: spreading a 100k-line range into push() would
      // exceed the engine's argument limit and throw a RangeError.
      for (let index = start; index < end; index += 1) last.lines.push(oldLines[index]);
    },
    remove(start, end) {
      for (let index = start; index < end; index += 1) deleted.push(oldLines[index]);
    },
    insert(start, end) {
      for (let index = start; index < end; index += 1) inserted.push(newLines[index]);
    },
    finish() {
      flushChanges();
      return ops;
    },
  };
};

/**
 * The most work (frontier visits plus snake steps) a bounded search over n
 * and m lines may do, given the maxCost the caller passed to diffLines.
 *
 * Bounding D alone does not bound time. A search visits O(D^2) diagonals,
 * but each snake can run the length of the file. A 2M-line file of two
 * alternating lines with ~10k scattered edits has D under the default
 * maxCost and took 25 s, growing linearly with the file. A search that ends
 * at distance D does at most (D + 6)^2 / 4 visits and 2 * (D + 2) * min(n, m)
 * snake steps, because a diagonal's frontier only moves forward. Since D never
 * exceeds callerMaxCost, this limit never stops a search whose shorter side
 * has at most callerMaxCost lines, nor one with D <= 62. Anything larger falls
 * back after O(callerMaxCost^2) work plus a linear term, instead of
 * O(file length * maxCost).
 */
const searchEffortLimit = (n, m, callerMaxCost) => (
  callerMaxCost === Infinity
    ? Infinity
    : (9 / 4) * (callerMaxCost + 6) ** 2 + SEARCH_EFFORT_PER_LINE * (n + m)
);

/**
 * Myers' bidirectional greedy search for a point on an optimal edit path.
 *
 * This is the linear-space refinement of the O(ND) algorithm: storing every
 * furthest-reaching frontier for a later backtrack costs O(D^2) memory, which
 * is gigabytes for D near maxCost. Searching from both ends and recursing on
 * the halves keeps memory O(N + M) with the same O((N + M) * D) time.
 *
 * Returns the split point, null when the ranges share nothing (a block
 * replacement is then already optimal), or ABORTED when the minimal edit
 * distance is proven to exceed maxCost or the search outgrows
 * searchEffortLimit for callerMaxCost.
 */
const findMiddleSplit = (a, aLo, aHi, b, bLo, bHi, maxCost, callerMaxCost) => {
  const n = aHi - aLo;
  const m = bHi - bLo;
  const effortLimit = searchEffortLimit(n, m, callerMaxCost);
  let effort = 0;
  const maxD = Math.ceil((n + m) / 2);
  // Frontiers only need diagonals the bounded search can reach, so a huge
  // file with a small maxCost does not allocate arrays sized to the file.
  const dLimit = Math.min(maxD, Math.floor(maxCost / 2) + 1);
  const offset = dLimit + 1;
  const size = 2 * offset + 1;
  const forward = new Int32Array(size).fill(-1);
  const reverse = new Int32Array(size).fill(-1);
  forward[offset + 1] = 0;
  reverse[offset + 1] = 0;
  const delta = n - m;
  // D always has the parity of delta, so overlaps can only complete in the
  // forward pass when delta is odd and in the reverse pass when it is even.
  const deltaIsOdd = delta % 2 !== 0;
  let forwardStart = 0;
  let forwardEnd = 0;
  let reverseStart = 0;
  let reverseEnd = 0;

  for (let d = 0; d < maxD; d += 1) {
    // The cheapest path this round could still discover costs 2d-1 (odd
    // delta) or 2d (even delta); past maxCost the caller wants the fallback.
    if ((deltaIsOdd ? 2 * d - 1 : 2 * d) > maxCost) return ABORTED;

    for (let k = -d + forwardStart; k <= d - forwardEnd; k += 2) {
      const index = offset + k;
      let x = k === -d || (k !== d && forward[index - 1] < forward[index + 1])
        ? forward[index + 1]
        : forward[index - 1] + 1;
      let y = x - k;
      const snakeStart = x;
      while (x < n && y < m && a[aLo + x] === b[bLo + y]) {
        x += 1;
        y += 1;
      }
      forward[index] = x;
      effort += 1 + x - snakeStart;
      if (x > n) {
        // Ran off the right edge; this diagonal can never help again.
        forwardEnd += 2;
      } else if (y > m) {
        // Ran off the bottom edge.
        forwardStart += 2;
      } else if (deltaIsOdd) {
        const reverseIndex = offset + delta - k;
        if (reverseIndex >= 0 && reverseIndex < size && reverse[reverseIndex] !== -1) {
          if (x >= n - reverse[reverseIndex]) return { x: aLo + x, y: bLo + y };
        }
      }
      if (effort > effortLimit) return ABORTED;
    }

    for (let k = -d + reverseStart; k <= d - reverseEnd; k += 2) {
      const index = offset + k;
      let x = k === -d || (k !== d && reverse[index - 1] < reverse[index + 1])
        ? reverse[index + 1]
        : reverse[index - 1] + 1;
      let y = x - k;
      const snakeStart = x;
      while (x < n && y < m && a[aHi - 1 - x] === b[bHi - 1 - y]) {
        x += 1;
        y += 1;
      }
      reverse[index] = x;
      effort += 1 + x - snakeStart;
      if (x > n) {
        reverseEnd += 2;
      } else if (y > m) {
        reverseStart += 2;
      } else if (!deltaIsOdd) {
        const forwardIndex = offset + delta - k;
        if (forwardIndex >= 0 && forwardIndex < size && forward[forwardIndex] !== -1) {
          const forwardX = forward[forwardIndex];
          if (forwardX >= n - x) {
            return { x: aLo + forwardX, y: bLo + forwardX - (delta - k) };
          }
        }
      }
      if (effort > effortLimit) return ABORTED;
    }
  }
  // No overlap at all means nothing is shared and D is n + m, which the
  // per-round bound above cannot see, so it is checked here.
  return n + m > maxCost ? ABORTED : null;
};

/**
 * Diff a[aLo, aHi) against b[bLo, bHi) (arrays of interned line ids) and
 * stream the edit script into emit.
 *
 * Returns false, having emitted nothing, when the minimal edit distance
 * exceeds maxCost or the search outgrows its effort limit; checking before
 * emitting lets the caller replace the whole region with one block instead of
 * a half-diffed mix. Only the outermost call carries a finite maxCost: halves
 * of an optimal split never cost more than the whole, so recursion can never
 * abort, and its total work is at most about that of the outermost search
 * (each level halves D).
 */
const diffIdRange = (a, aLo, aHi, b, bLo, bHi, emit, maxCost = Infinity, callerMaxCost = maxCost) => {
  let prefix = 0;
  while (aLo + prefix < aHi && bLo + prefix < bHi && a[aLo + prefix] === b[bLo + prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    aHi - suffix > aLo + prefix &&
    bHi - suffix > bLo + prefix &&
    a[aHi - suffix - 1] === b[bHi - suffix - 1]
  ) {
    suffix += 1;
  }
  const midALo = aLo + prefix;
  const midAHi = aHi - suffix;
  const midBLo = bLo + prefix;
  const midBHi = bHi - suffix;
  const oneSideEmpty = midALo === midAHi || midBLo === midBHi;

  let split = null;
  if (oneSideEmpty) {
    if ((midAHi - midALo) + (midBHi - midBLo) > maxCost) return false;
  } else {
    split = findMiddleSplit(a, midALo, midAHi, b, midBLo, midBHi, maxCost, callerMaxCost);
    if (split === ABORTED) return false;
  }

  emit.equal(aLo, midALo);
  // A split at either corner would recurse on the same problem forever; the
  // theory rules it out, and the guard makes termination unconditional.
  const usable = split !== null
    && !(split.x === midALo && split.y === midBLo)
    && !(split.x === midAHi && split.y === midBHi);
  if (usable) {
    diffIdRange(a, midALo, split.x, b, midBLo, split.y, emit);
    diffIdRange(a, split.x, midAHi, b, split.y, midBHi, emit);
  } else {
    emit.remove(midALo, midAHi);
    emit.insert(midBLo, midBHi);
  }
  emit.equal(midAHi, aHi);
  return true;
};

// Interning lines to integers turns every comparison in the hot loop into an
// Int32 compare instead of a string compare. A Map (not a plain object) keeps
// lines such as "__proto__" from colliding with built-in properties.
const internLines = (lines, start, end, ids) => {
  const result = new Int32Array(end - start);
  for (let index = start; index < end; index += 1) {
    const line = lines[index];
    let id = ids.get(line);
    if (id === undefined) {
      id = ids.size;
      ids.set(line, id);
    }
    result[index - start] = id;
  }
  return result;
};

// Positions (into ids) of the lines whose id also occurs on the other side.
const matchablePositions = (ids, otherSideCounts) => {
  const positions = [];
  for (let index = 0; index < ids.length; index += 1) {
    if (otherSideCounts[ids[index]] > 0) positions.push(index);
  }
  return Int32Array.from(positions);
};

/**
 * Diff the differing middle oldLines[oldStart, oldEnd) vs newLines[newStart,
 * newEnd) and stream it into the collector.
 *
 * Lines that occur on only one side can never be part of a longest common
 * subsequence, so Myers runs on the matchable lines alone. That reduction is
 * exact (same LCS, so the result stays minimal) and it is what keeps rewrites
 * fast: a reformatted or replaced file is mostly one-sided lines, and their
 * count is also an exact lower bound on D for the maxCost check.
 */
const diffMiddle = (oldLines, oldStart, oldEnd, newLines, newStart, newEnd, collector, maxCost) => {
  const ids = new Map();
  const a = internLines(oldLines, oldStart, oldEnd, ids);
  const b = internLines(newLines, newStart, newEnd, ids);
  const oldCounts = new Int32Array(ids.size);
  const newCounts = new Int32Array(ids.size);
  for (let index = 0; index < a.length; index += 1) oldCounts[a[index]] += 1;
  for (let index = 0; index < b.length; index += 1) newCounts[b[index]] += 1;
  const oldMap = matchablePositions(a, newCounts);
  const newMap = matchablePositions(b, oldCounts);
  const oneSided = (a.length - oldMap.length) + (b.length - newMap.length);

  if (oneSided > maxCost) {
    collector.remove(oldStart, oldEnd);
    collector.insert(newStart, newEnd);
    return;
  }

  const filteredA = oldMap.map((position) => a[position]);
  const filteredB = newMap.map((position) => b[position]);
  // Next original (middle-relative) line on each side not yet emitted, plus
  // the next filtered new-side position, which equal runs consume implicitly.
  let oldCursor = 0;
  let newCursor = 0;
  let filteredNew = 0;
  const emit = {
    equal(start, end) {
      for (let offset = 0; offset < end - start; offset += 1) {
        const oldIndex = oldMap[start + offset];
        const newIndex = newMap[filteredNew + offset];
        // One-sided lines sitting between two matches are edits by
        // definition; flush them so each equal line lands in order.
        collector.remove(oldStart + oldCursor, oldStart + oldIndex);
        collector.insert(newStart + newCursor, newStart + newIndex);
        collector.equal(oldStart + oldIndex, oldStart + oldIndex + 1);
        oldCursor = oldIndex + 1;
        newCursor = newIndex + 1;
      }
      filteredNew += end - start;
    },
    remove(start, end) {
      if (start >= end) return;
      const through = oldMap[end - 1] + 1;
      collector.remove(oldStart + oldCursor, oldStart + through);
      oldCursor = through;
    },
    insert(start, end) {
      if (start >= end) return;
      const through = newMap[end - 1] + 1;
      collector.insert(newStart + newCursor, newStart + through);
      newCursor = through;
      filteredNew = end;
    },
  };
  // One-sided lines use up edit budget but not search effort, so the effort
  // allowance stays tied to the caller's maxCost.
  diffIdRange(filteredA, 0, filteredA.length, filteredB, 0, filteredB.length, emit, maxCost - oneSided, maxCost);
  // Flush one-sided lines after the last match. If the bounded search
  // aborted it emitted nothing, so both cursors are still 0 and these two
  // calls produce the documented single delete+insert block for the middle.
  collector.remove(oldStart + oldCursor, oldEnd);
  collector.insert(newStart + newCursor, newEnd);
};

/**
 * Diff two line arrays exactly. Shared by diffLines (LF-normalised lines)
 * and createUnifiedPatch (byte-exact lines), which differ only in splitting.
 */
const diffLineArrays = (oldLines, newLines, maxCost) => {
  const collector = createOpCollector(oldLines, newLines);
  const oldLength = oldLines.length;
  const newLength = newLines.length;

  // Real review diffs are a few edits inside a large file. Trimming the
  // shared ends first makes that case linear and keeps Myers on the middle.
  let prefix = 0;
  while (prefix < oldLength && prefix < newLength && oldLines[prefix] === newLines[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < oldLength - prefix &&
    suffix < newLength - prefix &&
    oldLines[oldLength - 1 - suffix] === newLines[newLength - 1 - suffix]
  ) {
    suffix += 1;
  }
  const oldEnd = oldLength - suffix;
  const newEnd = newLength - suffix;

  collector.equal(0, prefix);
  if (prefix < oldEnd || prefix < newEnd) {
    diffMiddle(oldLines, prefix, oldEnd, newLines, prefix, newEnd, collector, maxCost);
  }
  collector.equal(oldEnd, oldLength);
  return collector.finish();
};

/**
 * Line diff of two texts (CRLF and lone CR are treated as LF).
 *
 * Ops alternate equal runs with change blocks; each change block is at most
 * one delete op followed by one insert op. When the minimal edit distance
 * exceeds maxCost the differing middle becomes one delete+insert block: still
 * an exact transformation, just coarser than the optimum. The same fallback
 * caps search time on pathological inputs (see searchEffortLimit). It can
 * only apply when both sides of the middle exceed maxCost lines and more than
 * 62 shared lines differ; maxCost Infinity always gives the minimal diff.
 *
 * @param {string} oldText
 * @param {string} newText
 * @param {{ maxCost?: number }} [options]
 * @returns {DiffOp[]}
 */
export const diffLines = (oldText, newText, options = {}) => {
  const { maxCost = DEFAULT_MAX_COST } = options ?? {};
  assertMaxCost(maxCost);
  return diffLineArrays(splitLines(oldText), splitLines(newText), maxCost);
};

/**
 * Expand ops into unified rows with 1-based line numbers per side.
 *
 * @param {DiffOp[]} ops
 * @returns {DiffRow[]}
 */
export const toRows = (ops) => {
  if (!Array.isArray(ops)) throw new TypeError('ops must be an array.');
  const rows = [];
  let oldNo = 1;
  let newNo = 1;
  for (const op of ops) {
    assertOp(op);
    for (const text of op.lines) {
      if (op.type === 'equal') {
        rows.push({ kind: 'context', oldNo, newNo, text });
        oldNo += 1;
        newNo += 1;
      } else if (op.type === 'delete') {
        rows.push({ kind: 'del', oldNo, newNo: null, text });
        oldNo += 1;
      } else {
        rows.push({ kind: 'add', oldNo: null, newNo, text });
        newNo += 1;
      }
    }
  }
  return rows;
};

/**
 * Row ranges [start, end) of each hunk, merged the way git and GNU diff do:
 * two change runs share a hunk when at most 2 * context unchanged rows
 * separate them, because their context windows would touch or overlap.
 */
const findHunkRanges = (rows, context) => {
  const ranges = [];
  const total = rows.length;
  let index = 0;
  while (index < total) {
    if (rows[index].kind === 'context') {
      index += 1;
      continue;
    }
    const start = Math.max(0, index - context);
    let cursor = index;
    let changeEnd = index;
    for (;;) {
      while (cursor < total && rows[cursor].kind !== 'context') cursor += 1;
      changeEnd = cursor;
      let gapEnd = cursor;
      while (gapEnd < total && rows[gapEnd].kind === 'context') gapEnd += 1;
      if (gapEnd < total && gapEnd - changeEnd <= 2 * context) {
        cursor = gapEnd;
        continue;
      }
      break;
    }
    ranges.push([start, Math.min(total, changeEnd + context)]);
    index = changeEnd;
  }
  return ranges;
};

// The line number of the closest numbered row before index on one side, or 0
// at the top of the file. Unified diffs anchor a side with no lines in a
// hunk to the line it follows (e.g. "-5,0" inserts after old line 5); git
// apply --unidiff-zero relies on this to place zero-context insertions.
const lineBefore = (rows, index, key) => {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const value = rows[cursor][key];
    if (value != null) return value;
  }
  return 0;
};

const buildHunk = (rows, start, end) => {
  const hunkRows = rows.slice(start, end);
  let oldStart = null;
  let newStart = null;
  let oldLines = 0;
  let newLines = 0;
  for (const row of hunkRows) {
    if (row.kind !== 'add') {
      if (oldStart === null) oldStart = row.oldNo;
      oldLines += 1;
    }
    if (row.kind !== 'del') {
      if (newStart === null) newStart = row.newNo;
      newLines += 1;
    }
  }
  if (oldStart === null) oldStart = lineBefore(rows, start, 'oldNo');
  if (newStart === null) newStart = lineBefore(rows, start, 'newNo');
  return {
    kind: 'hunk',
    header: `@@ -${oldStart},${oldLines} +${newStart},${newLines} @@`,
    oldStart,
    oldLines,
    newStart,
    newLines,
    rows: hunkRows,
  };
};

const buildCollapsed = (rows, start, end) => ({
  kind: 'collapsed',
  count: end - start,
  rows: rows.slice(start, end),
});

/**
 * Group rows into unified-diff hunks with `context` unchanged rows around
 * each change, and collapsed items for every unchanged row left outside a
 * hunk. Concatenating the items' rows always reproduces the input rows.
 *
 * @param {DiffRow[]} rows
 * @param {number} [context=3]
 * @returns {Array<DiffHunk | CollapsedRegion>}
 */
export const toHunks = (rows, context = DEFAULT_CONTEXT) => {
  assertContext(context);
  assertRows(rows);
  if (rows.length === 0) return [];
  const ranges = findHunkRanges(rows, context);
  if (ranges.length === 0) return [buildCollapsed(rows, 0, rows.length)];

  const items = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) items.push(buildCollapsed(rows, cursor, start));
    items.push(buildHunk(rows, start, end));
    cursor = end;
  }
  if (cursor < rows.length) items.push(buildCollapsed(rows, cursor, rows.length));
  return items;
};

/**
 * Pair unified rows into side-by-side rows. Each run of deletions is matched
 * line-by-line with the run of additions that immediately follows it, so a
 * modified line sits opposite its replacement; surplus lines face null.
 *
 * @param {DiffRow[]} rows
 * @returns {Array<{ left: SplitCell | null, right: SplitCell | null }>}
 */
export const toSplitRows = (rows) => {
  assertRows(rows);
  const result = [];
  let index = 0;
  while (index < rows.length) {
    const row = rows[index];
    if (row.kind === 'context') {
      result.push({
        left: { no: row.oldNo, text: row.text, kind: 'context' },
        right: { no: row.newNo, text: row.text, kind: 'context' },
      });
      index += 1;
      continue;
    }
    const deleted = [];
    while (index < rows.length && rows[index].kind === 'del') {
      deleted.push(rows[index]);
      index += 1;
    }
    const added = [];
    while (index < rows.length && rows[index].kind === 'add') {
      added.push(rows[index]);
      index += 1;
    }
    const pairs = Math.max(deleted.length, added.length);
    for (let offset = 0; offset < pairs; offset += 1) {
      const left = deleted[offset];
      const right = added[offset];
      result.push({
        left: left ? { no: left.oldNo, text: left.text, kind: 'del' } : null,
        right: right ? { no: right.newNo, text: right.text, kind: 'add' } : null,
      });
    }
  }
  return result;
};

// Tokens of text, or null as soon as there are more than limit of them.
// Stopping early matters: a minified line has millions of tokens, and
// collecting them all only to learn that the LCS bound is exceeded cost
// ~40 bytes of heap per input character (+250 MB for a 6 MB line).
// matchAll iterates a private copy of the pattern, so the shared global
// regex's lastIndex is never involved.
const tokenizeAtMost = (text, limit) => {
  const tokens = [];
  for (const match of text.matchAll(TOKEN_PATTERN)) {
    if (tokens.length === limit) return null;
    tokens.push(match[0]);
  }
  return tokens;
};

const wholeLine = (text, changed) => (text === '' ? [] : [{ text, changed }]);

// Marks which tokens fall outside one longest common subsequence. Shared
// ends are matched first so edits anchor where a reader expects; that never
// changes the LCS length, only which equal-length alignment is reported.
const markChangedTokens = (oldTokens, newTokens) => {
  const oldChanged = new Array(oldTokens.length).fill(true);
  const newChanged = new Array(newTokens.length).fill(true);
  let prefix = 0;
  while (
    prefix < oldTokens.length &&
    prefix < newTokens.length &&
    oldTokens[prefix] === newTokens[prefix]
  ) {
    oldChanged[prefix] = false;
    newChanged[prefix] = false;
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < oldTokens.length - prefix &&
    suffix < newTokens.length - prefix &&
    oldTokens[oldTokens.length - 1 - suffix] === newTokens[newTokens.length - 1 - suffix]
  ) {
    oldChanged[oldTokens.length - 1 - suffix] = false;
    newChanged[newTokens.length - 1 - suffix] = false;
    suffix += 1;
  }

  const n = oldTokens.length - prefix - suffix;
  const m = newTokens.length - prefix - suffix;
  if (n === 0 || m === 0) return { oldChanged, newChanged };

  // table[i][j] = LCS length of the middle suffixes starting at i and j.
  const width = m + 1;
  const table = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] = oldTokens[prefix + i] === newTokens[prefix + j]
        ? table[(i + 1) * width + j + 1] + 1
        : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (oldTokens[prefix + i] === newTokens[prefix + j]) {
      oldChanged[prefix + i] = false;
      newChanged[prefix + j] = false;
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }
  return { oldChanged, newChanged };
};

const toSegments = (tokens, changed) => {
  const segments = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const last = segments[segments.length - 1];
    if (last && last.changed === changed[index]) {
      last.text += tokens[index];
    } else {
      segments.push({ text: tokens[index], changed: changed[index] });
    }
  }
  return segments;
};

/**
 * Intra-line highlight for a modified line pair. Adjacent segments with the
 * same flag are merged, and joining each side's segment texts reproduces
 * that input exactly.
 *
 * @param {string} oldLine
 * @param {string} newLine
 * @returns {{ old: WordSegment[], new: WordSegment[] }}
 */
export const wordDiff = (oldLine, newLine) => {
  const oldText = toText(oldLine);
  const newText = toText(newLine);
  if (oldText === newText) {
    return { old: wholeLine(oldText, false), new: wholeLine(newText, false) };
  }
  // Against an empty side nothing can match, so the other side is one
  // changed segment; answering directly never tokenizes a huge line.
  if (oldText === '' || newText === '') {
    return { old: wholeLine(oldText, true), new: wholeLine(newText, true) };
  }
  // The LCS table is quadratic; on very long lines (minified bundles) a
  // whole-line highlight is more useful than freezing the review UI. Every
  // non-empty string has at least one token, so the product exceeds the
  // bound exactly when either side has more tokens than the bound divided
  // by the other side's count, and neither side is tokenized past that.
  const oldTokens = tokenizeAtMost(oldText, WORD_DIFF_MAX_CELLS);
  const newTokens = oldTokens && tokenizeAtMost(newText, Math.floor(WORD_DIFF_MAX_CELLS / oldTokens.length));
  if (!newTokens) {
    return { old: wholeLine(oldText, true), new: wholeLine(newText, true) };
  }
  const { oldChanged, newChanged } = markChangedTokens(oldTokens, newTokens);
  return { old: toSegments(oldTokens, oldChanged), new: toSegments(newTokens, newChanged) };
};

/**
 * Count added and deleted lines.
 *
 * @param {DiffOp[]} ops
 * @returns {{ additions: number, deletions: number }}
 */
export const diffStats = (ops) => {
  if (!Array.isArray(ops)) throw new TypeError('ops must be an array.');
  let additions = 0;
  let deletions = 0;
  for (const op of ops) {
    assertOp(op);
    if (op.type === 'insert') additions += op.lines.length;
    if (op.type === 'delete') deletions += op.lines.length;
  }
  return { additions, deletions };
};

/**
 * Split text for a patch byte-exactly: only LF ends a line (as in git), so
 * CRLF files keep their CR inside line content and the patch applies to the
 * real blob. A final line without a newline gets "\n" appended as a marker;
 * no split line can contain "\n", so that key never equals a terminated line,
 * which is exactly how git distinguishes "b" from "b\n" at end of file.
 */
const toPatchLines = (text) => {
  if (text === '') return [];
  const lines = text.split('\n');
  if (lines[lines.length - 1] === '') {
    lines.pop();
  } else {
    lines[lines.length - 1] += '\n';
  }
  return lines;
};

// Mirrors git's quote_c_style so paths with quotes, backslashes or control
// characters survive the round trip through `git apply`. Non-ASCII stays
// readable UTF-8, which git apply accepts unquoted.
const quotePath = (path) => {
  if (!/[\x00-\x1f"\\\x7f]/.test(path)) return path;
  let quoted = '"';
  for (const char of path) {
    const escape = C_STYLE_ESCAPES.get(char);
    if (escape !== undefined) {
      quoted += `\\${escape}`;
    } else if (char < ' ' || char === '\x7f') {
      quoted += `\\${char.charCodeAt(0).toString(8).padStart(3, '0')}`;
    } else {
      quoted += char;
    }
  }
  return `${quoted}"`;
};

/**
 * Why path must not appear in a patch, or null when it is a plain
 * repository-relative path. git apply refuses all of these, but patch(1)
 * does not: it follows "../" or an absolute name out of the checkout and
 * writes into .git, where a hook such as .git/hooks/pre-commit runs on the
 * next commit (on a case-insensitive disk ".GIT/config" is .git/config too).
 * A NUL is worse: git apply truncates the name there and silently patches a
 * different file. Backslash counts as a separator because Windows tools
 * treat it as one.
 */
const unsafePathReason = (path) => {
  if (path.includes('\0')) return 'it contains a NUL character';
  for (const segment of path.split(/[\\/]/)) {
    if (segment === '') return 'it is absolute or has an empty segment';
    if (segment === '.' || segment === '..') return 'it has a "." or ".." segment';
    if (GIT_DIR_SEGMENT.test(segment.replace(HFS_IGNORABLE, ''))) return 'it points into the .git directory';
  }
  return null;
};

// git appends a tab after a ---/+++ name containing a space so the name's
// end is unambiguous to patch parsers; matching that keeps output identical.
const fileLabelLine = (marker, label) => `${marker} ${label}${label.includes(' ') ? '\t' : ''}`;

const pushPatchRow = (lines, row) => {
  const prefix = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' ';
  if (row.text.endsWith('\n')) {
    lines.push(`${prefix}${row.text.slice(0, -1)}`, NO_NEWLINE_MARKER);
  } else {
    lines.push(`${prefix}${row.text}`);
  }
};

const createFilePatch = (file, context) => {
  if (!file || typeof file !== 'object') throw new TypeError('Each file must be an object.');
  const { path, isNew = false, isDeleted = false } = file;
  if (typeof path !== 'string' || path === '') {
    throw new TypeError('Each file needs a non-empty path.');
  }
  const label = describeValue(path);
  // Checked before the unchanged-file shortcut so a bad path is reported
  // whether or not this particular file changed.
  const unsafeReason = unsafePathReason(path);
  if (unsafeReason) throw new Error(`Unsafe patch path ${label}: ${unsafeReason}.`);
  const before = toText(file.before);
  const after = toText(file.after);
  // Contradictory flags would yield a patch git rejects or, worse, one that
  // silently discards content, so they are reported instead of guessed at.
  if (isNew && isDeleted) throw new Error(`${label}: a file cannot be both new and deleted.`);
  if (isNew && before !== '') throw new Error(`${label}: a new file must have empty "before" content.`);
  if (isDeleted && after !== '') throw new Error(`${label}: a deleted file must have empty "after" content.`);
  if (!isNew && !isDeleted && before === after) return '';

  const oldLabel = quotePath(`a/${path}`);
  const newLabel = quotePath(`b/${path}`);
  const lines = [`diff --git ${oldLabel} ${newLabel}`];
  if (isNew) lines.push('new file mode 100644');
  if (isDeleted) lines.push('deleted file mode 100644');

  const oldLines = toPatchLines(before);
  const newLines = toPatchLines(after);
  // Creating or deleting an empty file has no hunk; git writes only the
  // header lines for it and git apply expects exactly that shape.
  if (oldLines.length > 0 || newLines.length > 0) {
    lines.push(fileLabelLine('---', isNew ? '/dev/null' : oldLabel));
    lines.push(fileLabelLine('+++', isDeleted ? '/dev/null' : newLabel));
    const rows = toRows(diffLineArrays(oldLines, newLines, DEFAULT_MAX_COST));
    for (const item of toHunks(rows, context)) {
      if (item.kind !== 'hunk') continue;
      lines.push(item.header);
      for (const row of item.rows) pushPatchRow(lines, row);
    }
  }
  return `${lines.join('\n')}\n`;
};

/**
 * Build a git-style patch that `git apply` accepts. Files whose content is
 * unchanged are skipped (a new or deleted file is always a change), and an
 * input with no changes yields an empty string. Throws for a path that is
 * not a plain repository-relative path (absolute, "..", ".git", NUL; see
 * unsafePathReason), since such a patch is either refused or dangerous.
 *
 * @param {Array<{ path: string, before?: string, after?: string, isNew?: boolean, isDeleted?: boolean }>} files
 * @param {{ context?: number }} [options]
 * @returns {string}
 */
export const createUnifiedPatch = (files, options = {}) => {
  const { context = DEFAULT_CONTEXT } = options ?? {};
  assertContext(context);
  if (!Array.isArray(files)) throw new TypeError('files must be an array.');
  return files.map((file) => createFilePatch(file, context)).join('');
};
