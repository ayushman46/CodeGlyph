import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DIFF_GAP, buildToolChips, countToolLines, defaultToolChipsLabels, failureToToolCall, fileToToolChange,
  humanizeToolName, hunksToToolLines, proposalToToolChanges, stageToToolCall, summarizeToolRun, toolCallsToCalls,
} from './toolChips.js';

// Recorded from a real edit run on sindresorhus/ky ("rename cancelBody to releaseBody").
const RENAME_TOOL_CALLS = [
  { tool: 'trace_symbols', input: { symbols: ['cancelBody'] }, result_summary: 'Rename touches source/utils/timeout.ts' },
  {
    tool: 'plan_change',
    input: { files: ['source/core/Ky.ts', 'source/utils/body.ts', 'source/utils/timeout.ts'], new_files: [] },
    result_summary: 'Rename the `cancelBody` function to `releaseBody` in source/utils/body.ts.',
  },
  {
    tool: 'validate_patch',
    input: { files: ['source/core/Ky.ts', 'source/utils/body.ts', 'source/utils/timeout.ts'] },
    result_summary: '3 file(s), +15 −15, every hunk matched exactly once; not changed: source/types/hooks.ts, source/types/options.ts',
  },
  { tool: 'code_edit', input: { proposal_id: 'a7728c12' }, result_summary: 'Update source/core/Ky.ts' },
];

const TIMEOUT_HUNKS = [
  {
    old: "import {TimeoutError} from '../errors/TimeoutError.js';\nimport type {InternalOptions} from '../types/options.js';\nimport {cancelBody} from './body.js';\n\nexport type TimeoutOptions = {",
    new: "import {TimeoutError} from '../errors/TimeoutError.js';\nimport type {InternalOptions} from '../types/options.js';\nimport {releaseBody} from './body.js';\n\nexport type TimeoutOptions = {",
  },
  {
    old: '\t\t\t\tif (timedOut) {\n\t\t\t\t\tcancelBody(response);\n\t\t\t\t\treturn;\n\t\t\t\t}',
    new: '\t\t\t\tif (timedOut) {\n\t\t\t\t\treleaseBody(response);\n\t\t\t\t\treturn;\n\t\t\t\t}',
  },
];

test('hunksToToolLines diffs each hunk into context/added/removed lines', () => {
  assert.deepEqual(hunksToToolLines([{ old: 'a\nb\nc', new: 'a\nB\nc' }]), [
    { text: 'a', kind: 'context' },
    { text: 'b', kind: 'removed' },
    { text: 'B', kind: 'added' },
    { text: 'c', kind: 'context' },
  ]);
  // Pure insertion and pure deletion.
  assert.deepEqual(hunksToToolLines([{ old: 'x', new: 'x\ny' }]), [
    { text: 'x', kind: 'context' },
    { text: 'y', kind: 'added' },
  ]);
  assert.deepEqual(hunksToToolLines([{ old: 'x\ny\n', new: 'y\n' }]), [
    { text: 'x', kind: 'removed' },
    { text: 'y', kind: 'context' },
  ]);
});

test('hunksToToolLines joins hunks with a gap line and keeps real code text', () => {
  const lines = hunksToToolLines(TIMEOUT_HUNKS);
  assert.deepEqual(lines, [
    { text: "import {TimeoutError} from '../errors/TimeoutError.js';", kind: 'context' },
    { text: "import type {InternalOptions} from '../types/options.js';", kind: 'context' },
    { text: "import {cancelBody} from './body.js';", kind: 'removed' },
    { text: "import {releaseBody} from './body.js';", kind: 'added' },
    { text: '', kind: 'context' },
    { text: 'export type TimeoutOptions = {', kind: 'context' },
    { text: DIFF_GAP, kind: 'context' },
    { text: '\t\t\t\tif (timedOut) {', kind: 'context' },
    { text: '\t\t\t\t\tcancelBody(response);', kind: 'removed' },
    { text: '\t\t\t\t\treleaseBody(response);', kind: 'added' },
    { text: '\t\t\t\t\treturn;', kind: 'context' },
    { text: '\t\t\t\t}', kind: 'context' },
  ]);
  assert.deepEqual(countToolLines(lines), { added: 2, removed: 2 });
  assert.equal(hunksToToolLines(TIMEOUT_HUNKS, { gap: null }).some((line) => line.text === DIFF_GAP), false);
});

test('hunksToToolLines trims context and splits far-apart changes inside one hunk', () => {
  const old = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10'].join('\n');
  const next = ['1', 'two', '3', '4', '5', '6', '7', '8', 'nine', '10'].join('\n');
  const lines = hunksToToolLines([{ old, new: next }], { context: 1 });
  assert.deepEqual(lines.map((line) => `${line.kind[0]}${line.text}`), [
    'c1', 'r2', 'atwo', 'c3', `c${DIFF_GAP}`, 'c8', 'r9', 'anine', 'c10',
  ]);
  const all = hunksToToolLines([{ old, new: next }], { context: Infinity });
  assert.equal(all.length, 12);
  assert.equal(all.some((line) => line.text === DIFF_GAP), false);
});

test('hunksToToolLines skips unchanged and malformed hunks', () => {
  assert.deepEqual(hunksToToolLines([{ old: 'same', new: 'same' }, null, 'x', { old: 1, new: 2 }]), []);
  assert.deepEqual(hunksToToolLines(undefined), []);
  assert.deepEqual(hunksToToolLines([{ new: 'only new' }]), [{ text: 'only new', kind: 'added' }]);
  // CRLF is treated as LF, as in the review diff.
  assert.deepEqual(hunksToToolLines([{ old: 'a\r\nb', new: 'a\r\nc' }]).map((line) => line.kind), ['context', 'removed', 'added']);
});

test('hunksToToolLines caps long previews with a "more lines" line', () => {
  const after = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`).join('\n');
  const lines = hunksToToolLines([{ old: '', new: after }], { maxLines: 10 });
  assert.equal(lines.length, 11);
  assert.deepEqual(lines[9], { text: 'line 10', kind: 'added' });
  assert.deepEqual(lines[10], { text: '20 more lines', kind: 'context' });
  const custom = hunksToToolLines([{ old: '', new: 'a\nb' }], { maxLines: 1, moreLines: (count) => `+${count}` });
  assert.deepEqual(custom, [{ text: 'a', kind: 'added' }, { text: '+1', kind: 'context' }]);
});

test('fileToToolChange uses the server counts and diffs created files whole', () => {
  const modified = fileToToolChange({ file_path: 'source/utils/timeout.ts', action: 'modify', additions: 2, deletions: 2, changes: TIMEOUT_HUNKS });
  assert.equal(modified.path, 'source/utils/timeout.ts');
  assert.equal(modified.added, 2);
  assert.equal(modified.removed, 2);
  assert.equal(modified.lines.length, 12);

  const created = fileToToolChange({ file_path: 'test/timeout.js', action: 'create', changes: [], before: '', after: 'a\nb\n' });
  assert.deepEqual(created, {
    path: 'test/timeout.js', added: 2, removed: 0,
    lines: [{ text: 'a', kind: 'added' }, { text: 'b', kind: 'added' }],
  });

  // Before/after without hunks (e.g. a rebuilt file), counts derived from the diff.
  const rebuilt = fileToToolChange({ file_path: 'a.ts', action: 'modify', before: 'x\ny\n', after: 'x\nz\n' });
  assert.equal(rebuilt.added, 1);
  assert.equal(rebuilt.removed, 1);

  // Truncated responses drop before/after and hunks: counts only, no popover.
  assert.deepEqual(fileToToolChange({ file_path: 'big.ts', additions: 4, deletions: 1, before: null, after: null, changes: [] }), {
    path: 'big.ts', added: 4, removed: 1,
  });
  assert.equal(fileToToolChange({ action: 'modify' }), null);
  assert.equal(fileToToolChange(null), null);
});

test('proposalToToolChanges keeps one change per path, legacy single-file proposals too', () => {
  const changes = proposalToToolChanges({
    files: [
      { file_path: 'a.ts', additions: 1, deletions: 0, changes: [{ old: 'a', new: 'a\nb' }] },
      { file_path: 'a.ts', additions: 9, deletions: 9, changes: [] },
      { file_path: '', additions: 1 },
      { file_path: 'b.ts', additions: 0, deletions: 3, changes: [] },
    ],
  });
  assert.deepEqual(changes.map((change) => [change.path, change.added, change.removed, Boolean(change.lines)]), [
    ['a.ts', 1, 0, true],
    ['b.ts', 0, 3, false],
  ]);
  const legacy = proposalToToolChanges({ file_path: 'c.ts', changes: [{ old: 'x', new: 'y' }] });
  assert.deepEqual(legacy, [{
    path: 'c.ts', added: 1, removed: 1,
    lines: [{ text: 'x', kind: 'removed' }, { text: 'y', kind: 'added' }],
  }]);
  assert.deepEqual(proposalToToolChanges(null), []);
  assert.deepEqual(proposalToToolChanges({ files: [] }), []);
});

test('toolCallsToCalls maps the editor tools and drops the code_edit pointer', () => {
  const calls = toolCallsToCalls(RENAME_TOOL_CALLS);
  assert.deepEqual(calls.map((call) => call.id), ['0:trace_symbols', '1:plan_change', '2:validate_patch']);
  assert.deepEqual(calls[0], {
    id: '0:trace_symbols', label: 'Trace symbols', status: 'done', detail: 'cancelBody', detailIsCode: true,
    output: [{ text: 'Rename touches source/utils/timeout.ts', kind: 'context' }],
  });
  assert.equal(calls[1].label, 'Plan change');
  assert.equal(calls[1].detail, '3 files');
  assert.equal(calls[1].detailIsCode, undefined);
  assert.deepEqual(calls[1].output.map((line) => line.text), [
    'Rename the `cancelBody` function to `releaseBody` in source/utils/body.ts.',
    'source/core/Ky.ts', 'source/utils/body.ts', 'source/utils/timeout.ts',
  ]);
  assert.deepEqual(calls[2].output.map((line) => line.text), [
    '3 file(s), +15 −15, every hunk matched exactly once',
    'not changed: source/types/hooks.ts, source/types/options.ts',
  ]);
  assert.ok(calls.every((call) => call.output.every((line) => line.kind === 'context')));
});

test('toolCallsToCalls handles single files, new files, issues and unknown tools', () => {
  const calls = toolCallsToCalls([
    { tool: 'plan_change', input: { files: [], new_files: ['test/timeout.js'] }, result_summary: '' },
    { tool: 'read_github_issue', input: { issue: 'sindresorhus/ky#42' }, result_summary: 'Retry ignores Retry-After' },
    { tool: 'rename_codemod', input: { file: 'a.ts' }, result_summary: 'Model hunks did not match; applied the rename directly' },
    { tool: 'lookup' },
    { tool: 'search_code', input: { query: 'retry after' } },
    null, { tool: '' }, { input: {} },
  ]);
  assert.deepEqual(calls.map((call) => [call.label, call.detail, Boolean(call.detailIsCode)]), [
    ['Plan change', 'test/timeout.js', true],
    ['Read issue', 'sindresorhus/ky#42', true],
    ['Apply rename', 'a.ts', true],
    ['Lookup', undefined, false],
    ['Search code', 'retry after', false],
  ]);
  assert.deepEqual(calls[0].output, [{ text: 'test/timeout.js (new)', kind: 'context' }]);
  assert.equal(calls[3].output, undefined);
  assert.equal(new Set(calls.map((call) => call.id)).size, calls.length);
  assert.equal(humanizeToolName('read_files'), 'Read files');
  assert.equal(humanizeToolName(''), 'Tool');
});

test('stageToToolCall and failureToToolCall describe live and failed runs', () => {
  assert.deepEqual(stageToToolCall({ stage: 'reading', detail: 'Reading 5 current files from GitHub' }), {
    id: 'stage:reading', label: 'Reading 5 current files from GitHub', status: 'running',
  });
  assert.equal(stageToToolCall({ stage: 'validating' }).label, 'Validating the patch');
  assert.equal(stageToToolCall({ stage: 'some_new_stage' }).label, 'Some new stage');
  assert.equal(stageToToolCall(null), null);
  assert.equal(stageToToolCall({}), null);

  const failed = failureToToolCall("## I couldn't produce a safe patch\n\nHunk 2 of a.ts was not found.\n\nTry naming the file.");
  assert.equal(failed.status, 'error');
  assert.equal(failed.label, 'Validate patch');
  assert.deepEqual(failed.output.map((line) => line.text), [
    "I couldn't produce a safe patch", 'Hunk 2 of a.ts was not found.', 'Try naming the file.',
  ]);
  // Inline code from the server's markdown is shown without its backticks.
  assert.deepEqual(
    failureToToolCall('## No patch\n\nHunk 1 of `source/core/Ky.ts` did not match.\n\nTry naming the file (for example `src/a.ts`).')
      .output.map((line) => line.text),
    ['No patch', 'Hunk 1 of source/core/Ky.ts did not match.', 'Try naming the file (for example src/a.ts).'],
  );
  assert.equal(failureToToolCall('').output, undefined);
});

test('summarizeToolRun says what matters', () => {
  const done = (id) => ({ id, label: id, status: 'done' });
  assert.equal(summarizeToolRun({ calls: [done('a'), done('b'), done('c'), done('d')], changes: Array(7).fill({}) }), '4 steps · 7 files changed');
  assert.equal(summarizeToolRun({ calls: [done('a')], changes: [{}] }), '1 step · 1 file changed');
  assert.equal(summarizeToolRun({ calls: [done('a'), done('b')] }), '2 steps');
  assert.equal(summarizeToolRun({ changes: [{}, {}] }), '2 files changed');
  assert.equal(summarizeToolRun({}), 'No steps');
  assert.equal(summarizeToolRun({ calls: [done('a'), { id: 's', label: 'Reading files', status: 'running' }] }), 'Working · 2 steps');
  assert.equal(summarizeToolRun({ calls: [done('a'), done('b'), failureToToolCall('x')] }), 'Validation failed · 3 steps');
  assert.equal(summarizeToolRun({ calls: [failureToToolCall('x')] }), 'Validation failed');
  assert.equal(summarizeToolRun({ calls: [{ id: 'x', label: 'Read issue', status: 'error' }] }), 'Read issue failed');
});

test('buildToolChips combines stored calls, the proposal, a live stage or a failure', () => {
  const proposal = { files: [{ file_path: 'source/utils/timeout.ts', additions: 2, deletions: 2, changes: TIMEOUT_HUNKS }] };
  const complete = buildToolChips({ toolCalls: RENAME_TOOL_CALLS, proposal });
  assert.equal(complete.summary, '3 steps · 1 file changed');
  assert.equal(complete.calls.length, 3);
  assert.equal(complete.changes[0].lines.length, 12);

  const live = buildToolChips({ toolCalls: [], stage: { stage: 'planning', detail: 'Planning the change across 4 candidate files' } });
  assert.equal(live.summary, 'Working · 1 step');
  assert.equal(live.calls[0].status, 'running');

  const failed = buildToolChips({ toolCalls: RENAME_TOOL_CALLS.slice(0, 2), failure: 'No hunk matched.' });
  assert.equal(failed.summary, 'Validation failed · 3 steps');
  assert.deepEqual(failed.changes, []);
});

test('defaultToolChipsLabels matches the reference wording', () => {
  assert.equal(defaultToolChipsLabels.running, 'Running');
  assert.equal(defaultToolChipsLabels.failed, 'Failed');
  assert.equal(defaultToolChipsLabels.changes, 'Files changed');
  assert.equal(defaultToolChipsLabels.showMoreChanges(4), 'Show 4 more');
  assert.equal(defaultToolChipsLabels.diffFor('a.ts'), 'Changes to a.ts');
  assert.equal(defaultToolChipsLabels.added(6), '6 added');
  assert.equal(defaultToolChipsLabels.removed(4), '4 removed');
});

test('mappers never mutate their inputs', () => {
  const calls = structuredClone(RENAME_TOOL_CALLS);
  const proposal = { files: [{ file_path: 'a.ts', changes: structuredClone(TIMEOUT_HUNKS) }] };
  const frozenProposal = structuredClone(proposal);
  buildToolChips({ toolCalls: calls, proposal });
  assert.deepEqual(calls, RENAME_TOOL_CALLS);
  assert.deepEqual(proposal, frozenProposal);
});
