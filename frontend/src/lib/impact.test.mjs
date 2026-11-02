import assert from 'node:assert/strict';
import test from 'node:test';
import {
  depthLabel, examplePath, groupDependents, groupTestFunctions, impactMarkdown, testMatchLabel,
} from './impact.js';

const SHA = '0123456789abcdef0123456789abcdef01234567';

const result = {
  repository: 'flask',
  github_url: 'https://github.com/pallets/flask.git',
  commit_sha: SHA,
  target_file: 'src/flask/app.py',
  summary: '6 indexed files import src/flask/app.py directly.',
  method: 'Static analysis of resolved imports.',
  depth_counts: { 1: 2, 2: 3 },
  dependents: [
    { file_path: 'src/flask/__init__.py', depth: 1, via: 'src/flask/app.py', line_number: 2, is_test: false },
    { file_path: 'src/flask/testing.py', depth: 1, via: 'src/flask/app.py', line_number: 24, is_test: false },
    { file_path: 'tests/test_basic.py', depth: 2, via: 'src/flask/__init__.py', line_number: 24, is_test: true },
  ],
  affected_tests: {
    count: 1,
    files: [{ file_path: 'tests/test_basic.py', depth: 2, via: 'src/flask/__init__.py', line_number: 24 }],
    functions: [
      { file_path: 'tests/test_basic.py', name: 'test_make_response', line: 1287, kind: 'reference', reference_line: 1288, references: 4 },
      { file_path: 'tests/test_basic.py', name: 'test_response_types', line: 1144, kind: 'name', matched: 'response', reference_line: null, references: 0 },
    ],
  },
  symbol: { name: 'make_response', defined_at: 1238, mentions: 9 },
  imports: [{ file_path: 'src/flask/ctx.py', line_number: 32 }],
};

test('dependents are grouped by depth with the server totals', () => {
  const groups = groupDependents(result);
  assert.deepEqual(groups.map((group) => [group.depth, group.items.length, group.total]), [[1, 2, 2], [2, 1, 3]]);
  assert.equal(depthLabel(1), 'Direct');
  assert.equal(depthLabel(3), '3 levels away');
});

test('test functions are grouped per file and ordered by line', () => {
  const [file] = groupTestFunctions(result.affected_tests.functions);
  assert.equal(file.filePath, 'tests/test_basic.py');
  assert.deepEqual(file.items.map((item) => item.line), [1144, 1287]);
  assert.equal(testMatchLabel(file.items[1], 'make_response'), 'uses make_response at line 1288 (4 times)');
  assert.equal(testMatchLabel(file.items[0], 'make_response'), 'name contains “response”; does not mention make_response');
  assert.equal(testMatchLabel({ kind: 'name', matched: 'make_response' }, 'make_response'), 'name contains make_response');
});

test('the Markdown report links every file at the analysed commit', () => {
  const markdown = impactMarkdown(result, { symbol: 'make_response' });
  assert.match(markdown, /^## Change impact: `src\/flask\/app.py`/);
  assert.match(markdown, /flask @ `0123456`/);
  assert.ok(markdown.includes(`[src/flask/testing.py:24](https://github.com/pallets/flask/blob/${SHA}/src/flask/testing.py#L24)`));
  assert.ok(markdown.includes('**2 levels away (3)**'));
  assert.ok(markdown.includes('via `src/flask/__init__.py` (test)'));
  assert.ok(markdown.includes('…and 2 more files'));
  assert.ok(markdown.includes(`[tests/test_basic.py:1144](https://github.com/pallets/flask/blob/${SHA}/tests/test_basic.py#L1144) \`test_response_types\`: name contains “response”; does not mention make_response`));
  assert.ok(markdown.includes('(imported at line 32)'));
  assert.ok(markdown.trim().endsWith('_Static analysis of resolved imports._'));
});

test('without a GitHub URL paths stay plain code, and an ambiguous result has no report', () => {
  const markdown = impactMarkdown({ ...result, github_url: null });
  assert.ok(markdown.includes('- `src/flask/testing.py:24`'));
  assert.ok(!markdown.includes('Test functions'));
  assert.equal(impactMarkdown({ ambiguous: true, target_file: null }), '');
});

test('the example path comes from the repository when known', () => {
  assert.equal(examplePath([{ file_path: 'lib/utils.js' }]), 'lib/utils.js');
  assert.equal(examplePath([], 'src/main.py'), 'src/main.py');
  assert.equal(examplePath(undefined), 'path/to/file');
});
