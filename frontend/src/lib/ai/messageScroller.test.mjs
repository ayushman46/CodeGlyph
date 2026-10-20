import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_FOLLOW_THRESHOLD, PREVIEW_DESCRIPTION_LENGTH, PREVIEW_TITLE_LENGTH, SMOOTH_SCROLL_MAX_MS,
  SMOOTH_SCROLL_POLL_MS, SMOOTH_SCROLL_SETTLE_MS, smoothScrollSettled,
  centeredScrollTop, describeRailMessages, distanceFromEnd, findAssistantReply, getMessagePreview,
  isAtLiveEdge, isLeaveLiveEdgeKey, isRailOverflowing, messageSender, normalizeMessageText,
  pickActiveRailId, railItemAriaLabel, railItemsEqual, tickScale, truncateMessageText,
  MIN_RAIL_ITEM_SIZE, RAIL_INSET, RAIL_ITEM_SIZE, fitRailItemSize,
} from './messageScroller.js';

test('fitRailItemSize keeps 14px ticks while they fit, then shrinks them to fit, never below 4px', () => {
  assert.equal(RAIL_ITEM_SIZE, 14);
  assert.equal(MIN_RAIL_ITEM_SIZE, 4);
  assert.equal(RAIL_INSET, 32);
  // 10 ticks in a 600px transcript: 14px rows fit.
  assert.equal(fitRailItemSize(600, 10), 14);
  // 56 ticks in a 447px transcript: (447 - 32) / 56 = 7.4 → 7px rows.
  assert.equal(fitRailItemSize(447, 56), 7);
  assert.ok(fitRailItemSize(447, 56) * 56 <= 447 - RAIL_INSET);
  // Exactly fitting.
  assert.equal(fitRailItemSize(14 * 20 + RAIL_INSET, 20), 14);
  // Too many to fit even at the minimum: the rail scrolls.
  assert.equal(fitRailItemSize(300, 400), 4);
  // Unmeasured or empty: the default size.
  assert.equal(fitRailItemSize(0, 10), 14);
  assert.equal(fitRailItemSize(500, 0), 14);
});

test('reference constants', () => {
  assert.equal(PREVIEW_TITLE_LENGTH, 56);
  assert.equal(PREVIEW_DESCRIPTION_LENGTH, 88);
  assert.equal(DEFAULT_FOLLOW_THRESHOLD, 56);
  assert.equal(SMOOTH_SCROLL_SETTLE_MS, 320);
});

test('smoothScrollSettled waits while a long smooth scroll is still moving', () => {
  // First check after the settle window: nothing to compare with yet.
  assert.equal(smoothScrollSettled({ scrollTop: 900, previousScrollTop: null, elapsedMs: SMOOTH_SCROLL_SETTLE_MS }), false);
  assert.equal(smoothScrollSettled({ scrollTop: 1200, previousScrollTop: 900, elapsedMs: 400 }), false);
  assert.equal(smoothScrollSettled({ scrollTop: 1200, previousScrollTop: 1200, elapsedMs: 480 }), true);
  // Never ignored for longer than the upper bound.
  assert.equal(smoothScrollSettled({ scrollTop: 1300, previousScrollTop: 1250, elapsedMs: SMOOTH_SCROLL_MAX_MS }), true);
  assert.ok(SMOOTH_SCROLL_POLL_MS > 0 && SMOOTH_SCROLL_POLL_MS < SMOOTH_SCROLL_SETTLE_MS);
});

test('truncateMessageText keeps text that fits', () => {
  assert.equal(truncateMessageText('short', 10), 'short');
  assert.equal(truncateMessageText('exactly10!', 10), 'exactly10!');
  assert.equal(truncateMessageText('', 10), '');
});

test('truncateMessageText cuts at a late word boundary', () => {
  // Space at index 15 of a 20-char excerpt: 15 > 13 (0.65 * 20), so cut there.
  assert.equal(truncateMessageText('abcdefghij abcd efghijklmnop', 20), 'abcdefghij abcd…');
});

test('truncateMessageText cuts mid-word when the only space is early', () => {
  // Space at index 3: 3 <= 13, so it cuts hard at the limit.
  assert.equal(truncateMessageText('abc defghijklmnopqrstuvwxyz', 20), 'abc defghijklmnopqrs…');
  // No space at all.
  assert.equal(truncateMessageText('a'.repeat(30), 20), `${'a'.repeat(20)}…`);
});

test('truncateMessageText trims whitespace before the ellipsis', () => {
  // Un-normalised double space: the cut at the last space leaves another
  // space at the end of the excerpt, which is trimmed.
  assert.equal(truncateMessageText('abcdefghijklmno  pqrstuvwxyz', 20), 'abcdefghijklmno…');
});

test('normalizeMessageText collapses whitespace like textContent reading', () => {
  assert.equal(normalizeMessageText('  Hello\n\n  world\t again  '), 'Hello world again');
  assert.equal(normalizeMessageText(''), '');
  assert.equal(normalizeMessageText(null), '');
  assert.equal(normalizeMessageText(undefined), '');
});

test('getMessagePreview: empty text is "Message"', () => {
  assert.deepEqual(getMessagePreview(''), { label: 'Message', description: undefined });
  assert.deepEqual(getMessagePreview('', 'a reply'), { label: 'Message', description: undefined });
});

test('getMessagePreview: short text with and without a reply', () => {
  assert.deepEqual(getMessagePreview('How do retries work?'), { label: 'How do retries work?', description: undefined });
  assert.deepEqual(
    getMessagePreview('How do retries work?', 'ky retries failed requests twice by default.'),
    { label: 'How do retries work?', description: 'ky retries failed requests twice by default.' },
  );
  // A reply that exists but is still empty gives no description.
  assert.deepEqual(getMessagePreview('How do retries work?', ''), { label: 'How do retries work?', description: undefined });
});

test('getMessagePreview: short text truncates a long reply to 88 characters', () => {
  const reply = 'word '.repeat(40).trim();
  const { description } = getMessagePreview('Question?', reply);
  assert.equal(description, truncateMessageText(reply, 88));
  assert.ok(description.endsWith('…'));
  assert.ok(description.length <= 89);
});

test('getMessagePreview: exactly 56 characters is still a short title', () => {
  const text = 'x'.repeat(56);
  assert.deepEqual(getMessagePreview(text), { label: text, description: undefined });
});

test('getMessagePreview: long text splits into title and the rest', () => {
  const text = 'Explain how the hooks option lets me retry a request after refreshing an expired token in ky';
  const { label, description } = getMessagePreview(text);
  // The last space inside the first 56 characters (index 51 > 36.4).
  assert.equal(label, 'Explain how the hooks option lets me retry a request…');
  assert.equal(description, 'after refreshing an expired token in ky');
});

test('getMessagePreview: long text prefers the paired reply as description', () => {
  const text = 'Explain how the hooks option lets me retry a request after refreshing an expired token in ky';
  assert.equal(getMessagePreview(text, 'Use beforeRetry.').description, 'Use beforeRetry.');
  // An empty reply does not fall back to the rest of the message.
  assert.equal(getMessagePreview(text, '').description, undefined);
});

test('getMessagePreview: long text without spaces cuts the title at 56', () => {
  const text = 'a'.repeat(70);
  assert.deepEqual(getMessagePreview(text), { label: `${'a'.repeat(56)}…`, description: 'a'.repeat(14) });
});

test('railItemAriaLabel and messageSender', () => {
  assert.equal(railItemAriaLabel('user', 0, 4), 'Go to user message 1 of 4');
  assert.equal(railItemAriaLabel('assistant', 3, 4), 'Go to assistant message 4 of 4');
  assert.equal(messageSender('user'), 'user');
  assert.equal(messageSender(undefined), 'conversation');
  assert.equal(messageSender(null), 'conversation');
  // Only a missing attribute falls back (?? semantics).
  assert.equal(messageSender(''), '');
});

test('findAssistantReply pairs a user message with the next assistant one', () => {
  const senders = ['user', 'assistant', 'user', 'user', 'system', 'assistant', 'user'];
  assert.equal(findAssistantReply(senders, 0), 1);
  assert.equal(findAssistantReply(senders, 2), 5);
  assert.equal(findAssistantReply(senders, 3), 5);
  assert.equal(findAssistantReply(senders, 6), -1);
  assert.equal(findAssistantReply(senders, 1), -1);
  assert.equal(findAssistantReply(senders, 4), -1);
});

test('describeRailMessages builds labels, pairs replies and names ticks', () => {
  const items = describeRailMessages([
    { from: 'user', text: 'How do retries work?' },
    { from: 'assistant', text: 'ky retries failed requests twice by default.' },
    { from: undefined, text: 'Context compacted' },
    { from: 'user', text: 'And timeouts?' },
  ]);
  assert.deepEqual(items, [
    { label: 'How do retries work?', description: 'ky retries failed requests twice by default.', ariaLabel: 'Go to user message 1 of 4' },
    { label: 'ky retries failed requests twice by default.', description: undefined, ariaLabel: 'Go to assistant message 2 of 4' },
    { label: 'Context compacted', description: undefined, ariaLabel: 'Go to conversation message 3 of 4' },
    { label: 'And timeouts?', description: undefined, ariaLabel: 'Go to user message 4 of 4' },
  ]);
  assert.deepEqual(describeRailMessages([]), []);
});

test('railItemsEqual compares id, label, description and ariaLabel', () => {
  const a = [{ id: 'm1', label: 'A', description: 'd', ariaLabel: 'x' }];
  assert.equal(railItemsEqual(a, [{ id: 'm1', label: 'A', description: 'd', ariaLabel: 'x' }]), true);
  assert.equal(railItemsEqual(a, [{ id: 'm2', label: 'A', description: 'd', ariaLabel: 'x' }]), false);
  assert.equal(railItemsEqual(a, [{ id: 'm1', label: 'B', description: 'd', ariaLabel: 'x' }]), false);
  assert.equal(railItemsEqual(a, [{ id: 'm1', label: 'A', description: undefined, ariaLabel: 'x' }]), false);
  assert.equal(railItemsEqual(a, [{ id: 'm1', label: 'A', description: 'd', ariaLabel: 'y' }]), false);
  assert.equal(railItemsEqual(a, []), false);
  assert.equal(railItemsEqual([], []), true);
});

test('distanceFromEnd and isAtLiveEdge use a 56px threshold by default', () => {
  assert.equal(distanceFromEnd({ scrollHeight: 1000, scrollTop: 600, clientHeight: 300 }), 100);
  assert.equal(isAtLiveEdge({ scrollHeight: 1000, scrollTop: 644, clientHeight: 300 }), true); // 56
  assert.equal(isAtLiveEdge({ scrollHeight: 1000, scrollTop: 643, clientHeight: 300 }), false); // 57
  assert.equal(isAtLiveEdge({ scrollHeight: 1000, scrollTop: 600, clientHeight: 300 }, 100), true);
  // Content shorter than the viewport is always at the edge.
  assert.equal(isAtLiveEdge({ scrollHeight: 200, scrollTop: 0, clientHeight: 300 }), true);
});

test('isRailOverflowing needs real overflow and two messages', () => {
  assert.equal(isRailOverflowing({ scrollHeight: 400, clientHeight: 300 }, 2), true);
  assert.equal(isRailOverflowing({ scrollHeight: 301, clientHeight: 300 }, 5), false); // rounding pixel
  assert.equal(isRailOverflowing({ scrollHeight: 302, clientHeight: 300 }, 5), true);
  assert.equal(isRailOverflowing({ scrollHeight: 900, clientHeight: 300 }, 1), false);
  assert.equal(isRailOverflowing({ scrollHeight: 900, clientHeight: 300 }, 0), false);
});

test('isLeaveLiveEdgeKey: ArrowUp, PageUp and Home only', () => {
  for (const key of ['ArrowUp', 'PageUp', 'Home']) assert.equal(isLeaveLiveEdgeKey(key), true, key);
  for (const key of ['ArrowDown', 'PageDown', 'End', ' ', 'Enter', 'Escape', 'Tab']) assert.equal(isLeaveLiveEdgeKey(key), false, key);
});

test('pickActiveRailId: first near the top, last near the end, else nearest centre', () => {
  const rects = {
    a: { top: -500, height: 100 },
    b: { top: 80, height: 100 }, // centre 130
    c: { top: 220, height: 100 }, // centre 270
    d: { top: 800, height: 100 },
  };
  const measured = [];
  const measure = (id, index) => { measured.push([id, index]); return rects[id]; };
  const base = { scrollHeight: 2000, clientHeight: 400, threshold: 56, viewportCenter: 200, ids: ['a', 'b', 'c', 'd'], measure };
  assert.equal(pickActiveRailId({ ...base, scrollTop: 0 }), 'a');
  assert.equal(pickActiveRailId({ ...base, scrollTop: 56 }), 'a');
  assert.equal(pickActiveRailId({ ...base, scrollTop: 1600 }), 'd');
  assert.equal(pickActiveRailId({ ...base, scrollTop: 1544 }), 'd'); // 56 from the end
  // The edge cases never measure a message.
  assert.deepEqual(measured, []);
  assert.equal(pickActiveRailId({ ...base, scrollTop: 700 }), 'b'); // |130-200| 70 vs |270-200| 70: earlier wins
  assert.deepEqual(measured, [['a', 0], ['b', 1], ['c', 2], ['d', 3]]);
  assert.equal(pickActiveRailId({ ...base, scrollTop: 700, viewportCenter: 230 }), 'c');
  assert.equal(pickActiveRailId({ ...base, scrollTop: 700, ids: [] }), null);
});

test('centeredScrollTop centres the target in the viewport', () => {
  // Viewport at client top 100, 400 tall, scrolled 300. Target at client top 700, 100 tall.
  // Target's content offset = 300 + 700 - 100 = 900; centred = 900 - (400 - 100) / 2 = 750.
  assert.equal(centeredScrollTop({ scrollTop: 300, clientHeight: 400, viewportTop: 100, targetTop: 700, targetHeight: 100 }), 750);
  // A target taller than the viewport aligns its centre too (negative half-gap).
  assert.equal(centeredScrollTop({ scrollTop: 0, clientHeight: 200, viewportTop: 0, targetTop: 300, targetHeight: 600 }), 500);
});

test('tickScale steps down with distance from the highlighted tick', () => {
  assert.equal(tickScale(4, 4, true), 1);
  assert.equal(tickScale(3, 4, false), 0.68);
  assert.equal(tickScale(5, 4, false), 0.68);
  assert.equal(tickScale(2, 4, false), 0.44);
  assert.equal(tickScale(6, 4, false), 0.44);
  assert.equal(tickScale(0, 4, false), 0.25);
  assert.equal(tickScale(0, -1, false), 0.25);
});
