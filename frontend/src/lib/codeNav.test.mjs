import assert from 'node:assert/strict';
import test from 'node:test';
import {
  commandScore, enclosingSymbols, fileLinkKey, highlightParts, isMarkdownPath, pathScore, readFileLink,
  resolveRepositoryLink, searchSummary, symbolLabel, symbolScore, viewerLink, writeFileLink,
} from './codeNav.js';

test('a workspace link names a file and a line or range (FEAT-10)', () => {
  assert.deepEqual(readFileLink('?file=httpx/_client.py&line=1036'), { path: 'httpx/_client.py', start: 1036, end: 1036 });
  assert.deepEqual(readFileLink('?tab=overview&file=httpx%2F_client.py&line=L40-L12'), { path: 'httpx/_client.py', start: 12, end: 40 });
  assert.deepEqual(readFileLink('?file=/README.md'), { path: 'README.md', start: null, end: null });
  assert.deepEqual(readFileLink('?file=a.py&line=zero'), { path: 'a.py', start: null, end: null });
  assert.equal(readFileLink('?line=10'), null);
  assert.equal(readFileLink('?file=../../etc/passwd&line=1'), null);
  assert.equal(readFileLink(''), null);
});

test('writing a link keeps other parameters and a readable path', () => {
  assert.equal(writeFileLink('', { path: 'httpx/_client.py', start: 1036, end: 1036 }), '?file=httpx/_client.py&line=1036');
  assert.equal(writeFileLink('?tab=overview', { path: 'src/a b.ts', start: 10, end: 20 }), '?tab=overview&file=src/a%20b.ts&line=10-20');
  assert.equal(writeFileLink('?tab=overview&file=x.py&line=3', null), '?tab=overview');
  assert.equal(writeFileLink('?file=x.py&line=3', null), '');
  assert.equal(writeFileLink('', { path: 'README.md' }), '?file=README.md');
  // A written link reads back as the same file and lines.
  const file = { path: 'lib/response.js', start: 5, end: 9 };
  assert.equal(fileLinkKey(readFileLink(writeFileLink('?tab=overview', file))), fileLinkKey(file));
  assert.equal(fileLinkKey(null), '');
});

test('the viewer link follows the selected lines, else the opened ones', () => {
  const file = { path: 'httpx/_client.py', start: 584, end: 584 };
  assert.deepEqual(viewerLink({ open: true, file }), file);
  assert.deepEqual(viewerLink({ open: true, file, selection: { start: 1036, end: 1040 } }), { path: file.path, start: 1036, end: 1040 });
  assert.deepEqual(viewerLink({ open: true, file: { path: 'README.md' } }), { path: 'README.md', start: null, end: null });
  assert.equal(viewerLink({ open: true, file: null }), null);
  assert.equal(viewerLink({ open: false, file }), null);
});

test('the enclosing symbols of a line, outermost first (FEAT-02)', () => {
  const outline = [
    { name: 'Context', kind: 'class', line: 238, end_line: 986, depth: 0 },
    { name: 'Command', kind: 'class', line: 1200, end_line: 1500, depth: 0 },
    { name: 'parse_args', kind: 'def', line: 1396, end_line: 1420, depth: 1 },
    { name: 'invoke', kind: 'def', line: 1421, end_line: 1450, depth: 1 },
  ];
  assert.deepEqual(enclosingSymbols(outline, 1400).map(symbolLabel), ['class Command', 'def parse_args']);
  assert.deepEqual(enclosingSymbols(outline, 1396).map((entry) => entry.name), ['Command', 'parse_args']);
  assert.deepEqual(enclosingSymbols(outline, 100), []);
  assert.deepEqual(enclosingSymbols(outline, null), []);
  assert.equal(symbolLabel({ name: 'Install', kind: 'h2' }), 'Install');
});

test('Markdown files and the repository links inside them (UX-04)', () => {
  assert.ok(isMarkdownPath('docs/Readme.MD'));
  assert.ok(!isMarkdownPath('src/markdown.py'));
  assert.equal(resolveRepositoryLink('docs/api.md#usage', 'README.md'), 'docs/api.md');
  assert.equal(resolveRepositoryLink('../src/app.py', 'docs/guide/intro.md'), 'docs/src/app.py');
  assert.equal(resolveRepositoryLink('./a%20b.md', 'docs/x.md'), 'docs/a b.md');
  assert.equal(resolveRepositoryLink('/LICENSE', 'docs/x.md'), 'LICENSE');
  assert.equal(resolveRepositoryLink('https://example.com/a.md', 'README.md'), null);
  assert.equal(resolveRepositoryLink('mailto:a@b.c', 'README.md'), null);
  assert.equal(resolveRepositoryLink('#install', 'README.md'), null);
  assert.equal(resolveRepositoryLink('../../x.md', 'README.md'), null);
});

test('search lines split into plain and highlighted parts (BUG-03)', () => {
  assert.deepEqual(highlightParts('self._set_timeout(request)', [[5, 17]]), [
    { text: 'self.', match: false }, { text: '_set_timeout', match: true }, { text: '(request)', match: false },
  ]);
  assert.deepEqual(highlightParts('abc', [[0, 3]]), [{ text: 'abc', match: true }]);
  assert.deepEqual(highlightParts('abc', [[2, 9], [0, 1]]), [
    { text: 'a', match: true }, { text: 'b', match: false }, { text: 'c', match: true },
  ]);
  assert.deepEqual(highlightParts('abc', undefined), [{ text: 'abc', match: false }]);
  assert.deepEqual(highlightParts('', []), [{ text: '', match: false }]);
  assert.equal(searchSummary({ files: 1, lines: 3, more: false }), '1 file · 3 matching lines');
  assert.equal(searchSummary({ files: 25, lines: 80, more: true }), 'First 25 files · 80 matching lines');
});

test('palette scores put a file named like the query, and an exact symbol, first (FEAT-01)', () => {
  assert.equal(pathScore('httpx/_client.py', '_client'), 98);
  assert.equal(pathScore('httpx/_client.py', 'client'), 98);
  assert.equal(pathScore('src/click/types.py', 'types.py'), 98);
  assert.equal(pathScore('httpx/_client.py', 'httpx/_cl'), 68);
  assert.ok(pathScore('tests/client/test_client.py', '_client') < pathScore('httpx/_client.py', '_client'));
  assert.equal(pathScore('lib/response.js', 'rspns'), 30);
  assert.equal(pathScore('tests/test_types/__init__.py', 'types.py'), 0);
  assert.equal(symbolScore('ParamType', 'ParamType'), 100);
  assert.equal(symbolScore('ParamType', 'paramtype'), 95);
  assert.equal(symbolScore('ParamTypeInfoDict', 'ParamType'), 85);
  assert.equal(symbolScore('FuncParamType', 'ParamType'), 75);
  assert.equal(symbolScore('_get_client_nonce', '_client'), 62);
  assert.equal(symbolScore('parse', 'convert'), 0);
  // "_client": the file outranks every symbol that merely contains it.
  assert.ok(pathScore('httpx/_client.py', '_client') > symbolScore('test_client_timeout', '_client'));
  // "ParamType": the class outranks the test file named after it.
  assert.ok(symbolScore('ParamType', 'ParamType') > pathScore('tests/test_types/test_ParamType.py', 'ParamType'));
  assert.equal(commandScore('cal.com', 'cal'), 90);
  assert.ok(commandScore('Toggle dark mode', 'dark') > 60);
  assert.equal(commandScore('Pricing', 'zz'), 0);
});
