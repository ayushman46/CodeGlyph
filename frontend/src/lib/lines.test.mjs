import assert from 'node:assert/strict';
import test from 'node:test';
import { isBeyondEnd, splitHighlightedLines } from './lines.js';

test('splitHighlightedLines has no phantom row after a final newline (FE-9)', () => {
  assert.deepEqual(splitHighlightedLines('a\nb\n', 'a\nb\n'), ['a', 'b']);
  assert.deepEqual(splitHighlightedLines('a\nb', 'a\nb'), ['a', 'b']);
  assert.deepEqual(splitHighlightedLines('a\n\n', 'a\n\n'), ['a', '']);
  assert.deepEqual(splitHighlightedLines('', ''), ['']);
  // CRLF content: Prism keeps the \r on the line, the row count still matches.
  assert.equal(splitHighlightedLines('a\r\nb\r\n', 'a\r\nb\r\n').length, 2);
});

test('splitHighlightedLines closes and reopens tokens spanning lines', () => {
  const html = '<span class="token comment">/* x\ny */</span>\n';
  assert.deepEqual(splitHighlightedLines(html, '/* x\ny */\n'), [
    '<span class="token comment">/* x</span>',
    '<span class="token comment">y */</span>',
  ]);
});

test('isBeyondEnd flags references past the last line (FE-9)', () => {
  assert.equal(isBeyondEnd(12000, 10000), true);
  assert.equal(isBeyondEnd(10000, 10000), false);
  assert.equal(isBeyondEnd(null, 10), false);
});
