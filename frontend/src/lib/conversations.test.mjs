import assert from 'node:assert/strict';
import test from 'node:test';
import {
  appendConversations, cleanTitle, conversationDate, conversationFromLocation, conversationSummary, isConversationId,
  recordExchange, rememberedConversationKey, sortConversations, titleProblem, upsertConversation,
} from './conversations.js';

const ID_A = 'a'.repeat(32);
const ID_B = 'b'.repeat(32);
const ID_C = 'c'.repeat(32);

test('only server-shaped ids count as conversation ids', () => {
  assert.equal(isConversationId(ID_A), true);
  assert.equal(isConversationId('A'.repeat(32)), false);
  assert.equal(isConversationId('a'.repeat(31)), false);
  assert.equal(isConversationId('11111111-2222-3333-4444-555555555555'), false);
  assert.equal(isConversationId(null), false);
});

test('the URL picks a conversation only on that repository’s page', () => {
  const at = (pathname, search) => ({ pathname, search });
  assert.equal(conversationFromLocation(at('/app/r/requests', `?c=${ID_A}`), 'requests'), ID_A);
  assert.equal(conversationFromLocation(at('/app/r/requests/', `?tab=overview&c=${ID_A}`), 'requests'), ID_A);
  assert.equal(conversationFromLocation(at('/app/r/my%20repo', `?c=${ID_A}`), 'my repo'), ID_A);
  // Another repository's link, a malformed id, or no id: nothing.
  assert.equal(conversationFromLocation(at('/app/r/flask', `?c=${ID_A}`), 'requests'), null);
  assert.equal(conversationFromLocation(at('/app/r/requests', '?c=../../x'), 'requests'), null);
  assert.equal(conversationFromLocation(at('/app/r/requests', ''), 'requests'), null);
  assert.equal(conversationFromLocation(at('/app', `?c=${ID_A}`), 'requests'), null);
  assert.equal(conversationFromLocation(at('/app/r/%E0%A4%A', `?c=${ID_A}`), 'requests'), null);
  assert.equal(rememberedConversationKey('requests'), 'ci-conversation:requests');
});

test('the list is ordered by last activity, newest first, like the server', () => {
  const items = [
    { id: ID_A, updated_at: '2026-11-01T20:31:35.631649+00:00' },
    { id: ID_B, updated_at: '2026-11-02T08:00:00+00:00' },
    { id: ID_C, updated_at: '2026-11-01T20:31:35.631649+00:00' },
  ];
  assert.deepEqual(sortConversations(items).map((item) => item.id), [ID_B, ID_C, ID_A]);
  // A new or updated conversation moves into place and keeps known fields.
  const updated = upsertConversation(items, { id: ID_A, updated_at: '2026-11-03T00:00:00+00:00', title: 'Renamed' });
  assert.deepEqual(updated.map((item) => item.id), [ID_A, ID_B, ID_C]);
  assert.equal(updated[0].title, 'Renamed');
  assert.equal(upsertConversation(items, null), items);
  assert.deepEqual(appendConversations(items.slice(0, 2), [items[1], items[2]]).map((item) => item.id), [ID_A, ID_B, ID_C]);
});

test('an answer adds two messages, a proposal when it has one, and moves its conversation up', () => {
  const items = [
    { id: ID_B, title: 'Redirects', updated_at: '2026-11-02T08:00:00Z', message_count: 2, proposal_count: 0 },
    { id: ID_A, title: 'Triage #7634', updated_at: '2026-11-01T08:00:00Z', message_count: 4, proposal_count: 1 },
  ];
  const after = recordExchange(items, { id: ID_A, title: 'Triage #7634', updated_at: '2026-11-03T08:00:00Z' }, { proposal: true });
  assert.deepEqual(after.map((item) => [item.id, item.message_count, item.proposal_count]), [[ID_A, 6, 2], [ID_B, 2, 0]]);
  // A conversation the list has not loaded yet starts at one exchange.
  const fresh = recordExchange([], { id: ID_C, title: 'New', updated_at: '2026-11-03T09:00:00Z' });
  assert.deepEqual(fresh, [{ id: ID_C, title: 'New', updated_at: '2026-11-03T09:00:00Z', message_count: 2, proposal_count: 0 }]);
});

test('the summary names messages and, when there are some, proposals', () => {
  assert.equal(conversationSummary({ message_count: 1 }), '1 message');
  assert.equal(conversationSummary({ message_count: 14, proposal_count: 3 }), '14 messages · 3 proposals');
  assert.equal(conversationSummary({ message_count: 6, proposal_count: 1 }), '6 messages · 1 proposal');
  assert.equal(conversationSummary({}), '0 messages');
});

test('dates read as today, yesterday, or a calendar date', () => {
  const now = new Date(2026, 10, 2, 15, 0);
  const options = { now, locale: 'en-US' };
  assert.match(conversationDate(new Date(2026, 10, 2, 9, 5).toISOString(), options), /^Today, 09:05 AM$/);
  assert.match(conversationDate(new Date(2026, 10, 1, 23, 59).toISOString(), options), /^Yesterday, 11:59 PM$/);
  assert.equal(conversationDate(new Date(2026, 9, 28, 12, 0).toISOString(), options), 'Oct 28');
  assert.equal(conversationDate(new Date(2025, 9, 28, 12, 0).toISOString(), options), 'Oct 28, 2025');
  assert.equal(conversationDate('', options), '');
  assert.equal(conversationDate('not a date', options), '');
});

test('titles are saved on one line and must be 1 to 120 characters', () => {
  assert.equal(cleanTitle('  Triage   #7634\n'), 'Triage #7634');
  assert.equal(titleProblem('   '), 'Enter a title.');
  assert.equal(titleProblem('x'.repeat(121)), 'Titles can be at most 120 characters.');
  assert.equal(titleProblem(` ${'x'.repeat(120)} `), '');
});
