import assert from 'node:assert/strict';
import test from 'node:test';
import { describeEolChange, eolStyle, exactStats, restoreLineEndings } from './eol.js';

test('eolStyle classifies files', () => {
  assert.equal(eolStyle('a'), 'none');
  assert.equal(eolStyle('a\nb\n'), 'lf');
  assert.equal(eolStyle('a\r\nb\r\n'), 'crlf');
  assert.equal(eolStyle('a\r\nb\n'), 'mixed');
});

test('restoreLineEndings keeps a CRLF file CRLF after textarea editing (FE-5 / EP-04)', () => {
  const base = 'one\r\ntwo\r\nthree\r\n';
  // The textarea hands back LF only; an untouched round trip is byte-exact.
  assert.equal(restoreLineEndings('one\ntwo\nthree\n', base), base);
  assert.equal(restoreLineEndings('one\ntwo\nthree\nfour\n', base), 'one\r\ntwo\r\nthree\r\nfour\r\n');
  assert.equal(restoreLineEndings('one\nTWO\nthree', base), 'one\r\nTWO\r\nthree');
  // LF files are untouched.
  assert.equal(restoreLineEndings('a\nb\n', 'a\nb\n'), 'a\nb\n');
});

test('restoreLineEndings keeps each line ending of a mixed file', () => {
  const base = '// header\r\nline1\nline2\nline3\n';
  assert.equal(restoreLineEndings('// header\nline1\nline2\nline3\n', base), base);
  assert.equal(restoreLineEndings('// header\nline1\nNEW\nline2\nline3\n', base), '// header\r\nline1\nNEW\nline2\nline3\n');
  assert.equal(restoreLineEndings('// header\nINSERTED\nline1\nline2\nline3\n', base), '// header\r\nINSERTED\r\nline1\nline2\nline3\n');
});

test('exactStats counts final-newline and line-ending changes (EP-12)', () => {
  assert.deepEqual(exactStats('a\nb', 'a\nb\n'), { additions: 1, deletions: 1 });
  assert.deepEqual(exactStats('a\r\nb\r\n', 'a\nb\n'), { additions: 2, deletions: 2 });
  assert.deepEqual(exactStats('a\nb\n', 'a\nB\n'), { additions: 1, deletions: 1 });
  assert.deepEqual(exactStats('a\r\nb\r\n', 'a\r\nB\r\n'), { additions: 1, deletions: 1 });
  assert.deepEqual(exactStats('', 'x\n'), { additions: 1, deletions: 0 });
});

test('describeEolChange reports what the line diff hides (EP-12)', () => {
  assert.equal(describeEolChange('a\nb\n', 'a\nB\n'), null);
  assert.deepEqual(describeEolChange('a\nb', 'a\nb\n'), {
    styleBefore: 'lf', styleAfter: 'lf', changedLines: 0, finalNewlineBefore: false, finalNewlineAfter: true,
  });
  const crlfToLf = describeEolChange('a\r\nb\r\nc\r\n', 'a\nb\nC\n');
  assert.equal(crlfToLf.styleBefore, 'crlf');
  assert.equal(crlfToLf.styleAfter, 'lf');
  assert.equal(crlfToLf.changedLines, 2);
  assert.equal(describeEolChange('a\r\nb\r\n', 'a\r\nB\r\n'), null);
});

test('detectIndent finds tabs, 2 or 4 spaces and ignores alignment', async () => {
  const { detectIndent } = await import('./eol.js');
  assert.equal(detectIndent('a {\n\tb;\n\tif (x) {\n\t\tc;\n\t}\n}\n'), '\t');
  assert.equal(detectIndent('a {\n  b;\n  if (x) {\n    c;\n  }\n}\n'), '  ');
  assert.equal(detectIndent('def a():\n    b = 1\n    if x:\n        c = 2\n'), '    ');
  assert.equal(detectIndent('one line'), '  ');
  assert.equal(detectIndent(''), '  ');
});
