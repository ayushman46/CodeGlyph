import assert from 'node:assert/strict';
import test from 'node:test';
import { absenceTerms, markdownSection, splitSearchMatches, withoutInterruptionNote } from './answerText.js';

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

test('splitSearchMatches counts only the whole name in the same case as found', () => {
  // The search matches prefixes and ignores case: httpx's docs only mention
  // http11.send_request_headers, so `send_request` is not found there.
  const results = [
    { file_path: 'docs/advanced/extensions.md', start_line: 1, matches: [{ line: 1, text: 'Calls `http11.send_request_headers` during a request.' }] },
    { file_path: 'httpx/_client.py', start_line: 580, matches: [
      { line: 581, text: '    def Send_Request(self):' },
      { line: 590, text: '    response = self.send_request(request)' },
    ] },
  ];
  const split = splitSearchMatches(results, 'send_request');
  assert.equal(split.name, 'send_request');
  assert.deepEqual(split.exact, [{ file_path: 'httpx/_client.py', line: 590, text: '    response = self.send_request(request)' }]);
  assert.deepEqual(split.partial.map((match) => match.line), [1, 581]);
  assert.deepEqual(split.longerNames, ['send_request_headers', 'Send_Request']);

  const partialOnly = splitSearchMatches(results.slice(0, 1), 'send_request');
  assert.deepEqual([partialOnly.exact.length, partialOnly.partial.length], [0, 1]);
  assert.deepEqual(splitSearchMatches([], 'send_request'), { name: 'send_request', exact: [], partial: [], longerNames: [] });
});

test('splitSearchMatches matches the last part of a dotted name', () => {
  const results = [{ file_path: 'src/click/core.py', start_line: 900, matches: [
    { line: 912, text: '    def invoke(self, ctx: Context) -> t.Any:' },
    { line: 930, text: '        return self.invoke_without_command' },
  ] }];
  const split = splitSearchMatches(results, 'Command.invoke');
  assert.equal(split.name, 'invoke');
  assert.deepEqual(split.exact.map((match) => match.line), [912]);
  assert.deepEqual(split.longerNames, ['invoke_without_command']);
});

test('withoutInterruptionNote removes only the server note that ends a stopped answer', () => {
  const answer = 'Sessions are checked in `src/session.py:L1-L2`. Then';
  const note = '> The answer was interrupted. The Fast model stopped responding partway through the answer. Ask again to regenerate it.';
  assert.equal(withoutInterruptionNote(`${answer}\n\n${note}`), answer);
  assert.equal(withoutInterruptionNote(`${answer}\n\n> The answer was interrupted. Ask again to regenerate it.\n`), answer);
  const quoted = `> The answer was interrupted. Quoted earlier.\n\n${answer}`;
  assert.equal(withoutInterruptionNote(quoted), quoted);
  assert.equal(withoutInterruptionNote(undefined), '');
});
