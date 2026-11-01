import assert from 'node:assert/strict';
import test from 'node:test';
import { absenceTerms, markdownSection } from './answerText.js';

const ANSWER = [
  '## Direct answer',
  'Click parses options in `src/click/parser.py:L10-L20`.',
  '',
  '### Detail',
  'The parser reads tokens.',
  '```md',
  '## not a heading inside code',
  '```',
  '',
  '## Draft GitHub comment',
  'Thanks for the report: see `src/click/core.py:L5`.',
  '',
  '## Evidence limits',
  'I did not retrieve `Command.invoke`.',
].join('\n');

test('markdownSection copies one section, including its subsections, and ignores headings in code', () => {
  assert.equal(markdownSection(ANSWER, 1), [
    '## Direct answer',
    'Click parses options in `src/click/parser.py:L10-L20`.',
    '',
    '### Detail',
    'The parser reads tokens.',
    '```md',
    '## not a heading inside code',
    '```',
  ].join('\n'));
  assert.equal(markdownSection(ANSWER, 4), '### Detail\nThe parser reads tokens.\n```md\n## not a heading inside code\n```');
  assert.equal(markdownSection(ANSWER, 10), '## Draft GitHub comment\nThanks for the report: see `src/click/core.py:L5`.');
  assert.equal(markdownSection(ANSWER, 13), '## Evidence limits\nI did not retrieve `Command.invoke`.');
  assert.equal(markdownSection(ANSWER, 2), '');
  assert.equal(markdownSection(ANSWER, 7), '');
});

test('absenceTerms finds the code names an answer says it did not retrieve', () => {
  const text = [
    'The retrieved evidence does not include `_encode_files` or `to_key_val_list()`.',
    'I did not retrieve `test/res.redirect.js`, so I cannot list its tests.',
    '`Command.invoke` was not retrieved. `Group.invoke` handles subcommands (`src/click/core.py:L10`).',
    'The repository uses `depd` for deprecations.',
    '```',
    'I did not retrieve `insideFence`',
    '```',
  ].join('\n');
  assert.deepEqual(absenceTerms(text), ['_encode_files', 'to_key_val_list', 'Command.invoke']);
  assert.deepEqual(absenceTerms(text, 5), ['_encode_files', 'to_key_val_list', 'Command.invoke']);
  assert.deepEqual(absenceTerms('`flask` handles it. Everything was retrieved.'), []);
  assert.deepEqual(absenceTerms("I couldn't find `make_response` in the evidence."), ['make_response']);
});
