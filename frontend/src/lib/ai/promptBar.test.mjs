import assert from 'node:assert/strict';
import test from 'node:test';
import {
  MAX_INPUT_HEIGHT, defaultPromptBarLabels, findSuggestion, insertSuggestion, matchSuggestions, stepActiveIndex,
} from './promptBar.js';

const at = (text) => findSuggestion(text, text.length);

test('findSuggestion opens on a trigger at the start of the text or of a word', () => {
  assert.deepEqual(at('@'), { kind: 'mention', start: 0, query: '' });
  assert.deepEqual(at('/'), { kind: 'command', start: 0, query: '' });
  assert.deepEqual(at('explain @ky'), { kind: 'mention', start: 8, query: 'ky' });
  assert.deepEqual(at('please /sum'), { kind: 'command', start: 7, query: 'sum' });
  // Any whitespace starts a word: newline and tab included.
  assert.deepEqual(at('first line\n@ret'), { kind: 'mention', start: 11, query: 'ret' });
  assert.deepEqual(at('a\t/ed'), { kind: 'command', start: 2, query: 'ed' });
});

test('findSuggestion tells @ (mentions) from / (commands)', () => {
  assert.equal(at('@x').kind, 'mention');
  assert.equal(at('/x').kind, 'command');
  assert.equal(at('@a /b').kind, 'command');
  assert.equal(at('/a @b').kind, 'mention');
});

test('findSuggestion does not trigger in the middle of a word', () => {
  assert.equal(at('email@x'), null);
  assert.equal(at('me@example'), null);
  assert.equal(at('src/lib'), null);
  assert.equal(at('and/or'), null);
  assert.equal(at('https://'), null);
  assert.equal(at('(@ky'), null);
  assert.equal(at('"/edit'), null);
});

test('findSuggestion closes once the word ends or holds other characters', () => {
  assert.equal(at('@ky '), null);
  assert.equal(at('/edit now'), null);
  assert.equal(at('@Ky:ts'), null);
  assert.equal(at('/edit.md'), null);
  assert.equal(at('/src/lib'), null);
  assert.equal(at('@a@b'), null);
  assert.equal(at('//'), null);
  assert.equal(at(''), null);
  assert.equal(at('plain text'), null);
});

test('findSuggestion accepts hyphen, underscore and digits in the word', () => {
  assert.deepEqual(at('@ky-core'), { kind: 'mention', start: 0, query: 'ky-core' });
  assert.deepEqual(at('@options_types'), { kind: 'mention', start: 0, query: 'options_types' });
  assert.deepEqual(at('/fix-123'), { kind: 'command', start: 0, query: 'fix-123' });
  assert.deepEqual(at('/-'), { kind: 'command', start: 0, query: '-' });
});

test('findSuggestion lets an @ mention run through a file path (dots and slashes); commands stay one word', () => {
  assert.deepEqual(at('@source/utils/normalize.ts'), { kind: 'mention', start: 0, query: 'source/utils/normalize.ts' });
  assert.deepEqual(at('explain @src/'), { kind: 'mention', start: 8, query: 'src/' });
  assert.deepEqual(at('see @.github/workflows/ci.yml'), { kind: 'mention', start: 4, query: '.github/workflows/ci.yml' });
  assert.deepEqual(at('@README.md'), { kind: 'mention', start: 0, query: 'readme.md' });
  // The trigger still has to start a word.
  assert.equal(at('a/@b'), null);
  assert.equal(at('x.com/@user'), null);
  // A path typed after the mention ends it.
  assert.equal(at('@src/a.ts more'), null);
  // A slash command ends at a slash or a dot.
  assert.equal(at('/explain/more'), null);
  assert.deepEqual(at('@src/a.ts /exp'), { kind: 'command', start: 10, query: 'exp' });
});

test('findSuggestion lower-cases the query', () => {
  assert.equal(at('@ReadMe').query, 'readme');
  assert.equal(at('/EXPLAIN').query, 'explain');
});

test('findSuggestion only looks at the text before the caret', () => {
  const text = 'ask @ret about this';
  // Caret right after "@ret".
  assert.deepEqual(findSuggestion(text, 8), { kind: 'mention', start: 4, query: 'ret' });
  // Caret right after "@re": the query stops at the caret.
  assert.deepEqual(findSuggestion(text, 7), { kind: 'mention', start: 4, query: 're' });
  // Caret right after the trigger.
  assert.deepEqual(findSuggestion(text, 5), { kind: 'mention', start: 4, query: '' });
  // Caret before the trigger, or after the word has ended.
  assert.equal(findSuggestion(text, 4), null);
  assert.equal(findSuggestion(text, 9), null);
  assert.equal(findSuggestion(text, text.length), null);
  // Caret at 0.
  assert.equal(findSuggestion('@ky', 0), null);
});

const SOURCES = [{ label: 'ky' }, { label: 'ky-core' }, { label: 'retry' }, { label: 'README' }];
const COMMANDS = [{ label: 'explain' }, { label: 'edit' }, { label: 'review' }, { label: 'summarize' }];

test('matchSuggestions: mentions match anywhere in the label, ignoring case', () => {
  const labels = (query) => matchSuggestions(SOURCES, { kind: 'mention', start: 0, query }).map((item) => item.label);
  assert.deepEqual(labels(''), ['ky', 'ky-core', 'retry', 'README']);
  assert.deepEqual(labels('re'), ['ky-core', 'retry', 'README']);
  assert.deepEqual(labels('readme'), ['README']);
  assert.deepEqual(labels('zzz'), []);
});

test('matchSuggestions: commands match from the start of the label', () => {
  const labels = (query) => matchSuggestions(COMMANDS, { kind: 'command', start: 0, query }).map((item) => item.label);
  assert.deepEqual(labels(''), ['explain', 'edit', 'review', 'summarize']);
  assert.deepEqual(labels('e'), ['explain', 'edit']);
  assert.deepEqual(labels('view'), []);
  assert.deepEqual(labels('re'), ['review']);
});

test('matchSuggestions returns nothing without a suggestion or items', () => {
  assert.deepEqual(matchSuggestions(SOURCES, null), []);
  assert.deepEqual(matchSuggestions(undefined, { kind: 'mention', start: 0, query: '' }), []);
  assert.deepEqual(matchSuggestions([], { kind: 'command', start: 0, query: '' }), []);
});

test('stepActiveIndex wraps both ways and recovers from a stale index', () => {
  assert.equal(stepActiveIndex(0, 3, 'ArrowDown'), 1);
  assert.equal(stepActiveIndex(2, 3, 'ArrowDown'), 0);
  assert.equal(stepActiveIndex(0, 3, 'ArrowUp'), 2);
  assert.equal(stepActiveIndex(2, 3, 'ArrowUp'), 1);
  assert.equal(stepActiveIndex(0, 1, 'ArrowDown'), 0);
  assert.equal(stepActiveIndex(0, 1, 'ArrowUp'), 0);
  // The list shrank under an index of 5: count from the last option.
  assert.equal(stepActiveIndex(5, 2, 'ArrowDown'), 0);
  assert.equal(stepActiveIndex(5, 2, 'ArrowUp'), 0);
  assert.equal(stepActiveIndex(5, 3, 'ArrowUp'), 1);
});

test('insertSuggestion replaces the typed word and adds a trailing space', () => {
  const text = 'explain @re';
  assert.deepEqual(insertSuggestion(text, text.length, at(text), 'retry'), { value: 'explain @retry ', caret: 15 });
  assert.deepEqual(insertSuggestion('/su', 3, at('/su'), 'summarize'), { value: '/summarize ', caret: 11 });
});

test('insertSuggestion keeps the text after the caret', () => {
  const text = 'ask @re about it';
  const suggestion = findSuggestion(text, 7);
  assert.deepEqual(insertSuggestion(text, 7, suggestion, 'retry'), { value: 'ask @retry  about it', caret: 11 });
  const command = findSuggestion('/ then more', 1);
  assert.deepEqual(insertSuggestion('/ then more', 1, command, 'edit'), { value: '/edit  then more', caret: 6 });
});

test('defaultPromptBarLabels holds every string of the reference', () => {
  assert.equal(defaultPromptBarLabels.prompt, 'Prompt');
  assert.equal(defaultPromptBarLabels.send, 'Send');
  assert.equal(defaultPromptBarLabels.stop, 'Stop');
  assert.equal(defaultPromptBarLabels.attach, 'Attach files');
  assert.equal(defaultPromptBarLabels.attachments, 'Attachments');
  assert.equal(defaultPromptBarLabels.removeAttachment('notes.md'), 'Remove notes.md');
  assert.equal(defaultPromptBarLabels.model, 'Model');
  assert.equal(defaultPromptBarLabels.mentions, 'Sources');
  assert.equal(defaultPromptBarLabels.commands, 'Commands');
  assert.equal(defaultPromptBarLabels.noMatches('zzz'), 'No matches for “zzz”.');
  assert.equal(defaultPromptBarLabels.suggestionCount(1), '1 suggestion');
  assert.equal(defaultPromptBarLabels.suggestionCount(0), '0 suggestions');
  assert.equal(defaultPromptBarLabels.suggestionCount(4), '4 suggestions');
  assert.equal(MAX_INPUT_HEIGHT, 160);
});
