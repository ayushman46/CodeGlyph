import assert from 'node:assert/strict';
import test from 'node:test';
import { applyHunks, proposalPatch, slugify } from './patch.js';

test('applyHunks applies non-overlapping hunks and preserves CRLF', () => {
  const crlf = 'a\r\nb\r\nc\r\n';
  assert.equal(applyHunks(crlf, [{ old: 'b', new: 'B' }]), 'a\r\nB\r\nc\r\n');
  assert.equal(applyHunks('one two three', [{ old: 'one', new: '1' }, { old: 'three', new: '3' }]), '1 two 3');
});

test('applyHunks rejects missing, ambiguous and overlapping hunks', () => {
  assert.throws(() => applyHunks('abc', [{ old: 'zzz', new: 'y' }]), /no longer matches/);
  assert.throws(() => applyHunks('x x', [{ old: 'x', new: 'y' }]), /matches 2 locations/);
  assert.throws(() => applyHunks('abcdef', [{ old: 'abcd', new: '1' }, { old: 'cdef', new: '2' }]), /overlap/);
  assert.throws(() => applyHunks('abc', [{ old: '', new: 'y' }]), /empty/);
  // Overlapping occurrences count as ambiguous, exactly like the server's
  // code_edit._occurrence_count (EP-11 / FE-4).
  assert.throws(() => applyHunks('aaa', [{ old: 'aa', new: 'b' }]), /matches 2 locations/);
  assert.throws(() => applyHunks('}\n}\n}\n', [{ old: '}\n}\n', new: '}\n' }]), /matches 2 locations/);
});

// Expected values below were produced by backend/agent/code_edit.py apply_hunks.
test('applyHunks keeps the original bytes of untouched lines in mixed-ending files (EP-10 / PR-8)', () => {
  assert.equal(applyHunks('a\r\nb\nc\nd\n', [{ old: 'b', new: 'B' }]), 'a\r\nB\nc\nd\n');
  // The replacement adopts CRLF because the file contains CRLF.
  assert.equal(applyHunks('a\r\nb\nc\n', [{ old: 'b\nc', new: 'B\nC' }]), 'a\r\nB\r\nC\n');
  // A CRLF hunk still matches an LF file and is written with LF.
  assert.equal(applyHunks('a\nb\nc\n', [{ old: 'a\r\nb', new: 'A\r\nB' }]), 'A\nB\nc\n');
  // A span boundary never splits a CRLF pair.
  assert.equal(applyHunks('x\r\ny\r\n', [{ old: 'x\n', new: 'z\n' }]), 'z\r\ny\r\n');
});

test('applyHunks tolerates trailing-whitespace drift exactly like the server (EP-06 / FE-3)', () => {
  assert.equal(
    applyHunks('const a = 1;  \nconst b = 2;\n', [{ old: 'const a = 1;\nconst b = 2;', new: 'const a = 10;\nconst b = 20;' }]),
    'const a = 10;\nconst b = 20;\n',
  );
  assert.equal(applyHunks('x = 1;\t\r\ny\r\n', [{ old: 'x = 1;\ny', new: 'x = 2;\ny' }]), 'x = 2;\r\ny\r\n');
  // Only trailing whitespace is tolerated, never leading or interior.
  assert.equal(applyHunks('  a\nb\n', [{ old: 'a \nb', new: 'c' }]), '  c\n');
  assert.throws(() => applyHunks('a b\nc\n', [{ old: 'a  b\nc', new: 'x' }]), /no longer matches/);
  // Exact matches win before tolerance; ambiguous tolerant matches are rejected.
  assert.throws(() => applyHunks('a \nb\na\t\nb\n', [{ old: 'a\nb', new: 'c' }]), /matches 2 locations/);
  // Whitespace-only hunks are never applied tolerantly.
  assert.throws(() => applyHunks('a\t\nb\n', [{ old: ' \n', new: '\n' }]), /no longer matches/);
});

test('applyHunks rejects no-op hunks and empty change lists like the server', () => {
  assert.throws(() => applyHunks('abc', [{ old: 'b', new: 'b' }]), /does not change/);
  assert.throws(() => applyHunks('abc', [{ old: 'b\r\n', new: 'b\n' }]), /no longer matches|does not change/);
  assert.throws(() => applyHunks('abc', []), /no hunks/);
  assert.throws(() => applyHunks('abc', [{ old: 'b' }]), /empty/);
});

test('proposalPatch builds a patch for modified and created files and honours edits', () => {
  const proposal = {
    files: [
      { file_path: 'src/a.js', action: 'modify', before: 'const a = 1;\n', after: 'const a = 2;\n' },
      { file_path: 'src/new.js', action: 'create', before: '', after: 'export {};\n' },
    ],
  };
  const patch = proposalPatch(proposal, { 'src/a.js': 'const a = 3;\n' });
  assert.match(patch, /diff --git a\/src\/a\.js b\/src\/a\.js/);
  assert.match(patch, /\+const a = 3;/);
  assert.match(patch, /new file mode 100644/);
  assert.match(patch, /\+\+\+ b\/src\/new\.js/);
  assert.equal(proposalPatch({ files: [] }), '');
});

test('slugify produces safe branch and file names', () => {
  assert.equal(slugify('Fix: Booking emails (timezone)!'), 'fix-booking-emails-timezone');
  assert.equal(slugify('   '), 'change');
});
