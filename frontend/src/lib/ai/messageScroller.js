/*
 * Pure logic behind components/ai/MessageScroller.jsx (Honest UI "Message
 * Scroller" port): rail labels and previews built from plain message text,
 * the live-edge maths, the active-message pick and the centred scroll target.
 * Everything here works on strings and numbers so it can be unit tested
 * without a DOM (see messageScroller.test.mjs).
 */

/** Longest preview title before it is cut at a word boundary. */
export const PREVIEW_TITLE_LENGTH = 56;
/** Longest preview description before it is cut at a word boundary. */
export const PREVIEW_DESCRIPTION_LENGTH = 88;
/** Distance from the end (px) that still counts as the live edge. */
export const DEFAULT_FOLLOW_THRESHOLD = 56;
/** How long a smooth programmatic scroll is ignored by the scroll handler. */
export const SMOOTH_SCROLL_SETTLE_MS = 320;
/**
 * After the transcript opens, its content keeps growing for a moment (the
 * code font swapping in, highlighted code) without any new output. Following
 * is instant for that long, so the conversation does not glide down on every
 * open; later growth (a finished reply's actions) still glides.
 */
export const OPENING_SETTLE_MS = 1500;
/** After that, how often a still-moving smooth scroll is checked again. */
export const SMOOTH_SCROLL_POLL_MS = 80;
/** Upper bound on ignoring the scroll events of one programmatic scroll. */
export const SMOOTH_SCROLL_MAX_MS = 1500;
/** Keys on the viewport that mean the reader is moving away from the end. */
export const LEAVE_LIVE_EDGE_KEYS = ['ArrowUp', 'PageUp', 'Home'];

/**
 * Cuts `text` to `limit` characters, preferring the last space when it falls
 * in the final third of the excerpt, and ends it with an ellipsis.
 *
 * @param {string} text
 * @param {number} limit
 * @returns {string}
 */
export function truncateMessageText(text, limit) {
  if (text.length <= limit) return text;
  const excerpt = text.slice(0, limit);
  const boundary = excerpt.lastIndexOf(' ');
  return `${excerpt.slice(0, boundary > limit * 0.65 ? boundary : limit).trim()}…`;
}

/**
 * Collapses every run of whitespace to one space and trims the ends, the way
 * the rail reads a message's `textContent`.
 *
 * @param {string | null | undefined} text
 * @returns {string}
 */
export function normalizeMessageText(text) {
  return (text ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * @typedef {object} MessagePreview
 * @property {string} label Preview title (and the rail item's visible name).
 * @property {string | undefined} description Second line, if any.
 */

/**
 * Builds the preview for one message from its normalised text.
 *
 * - Empty text gives the label "Message" and no description.
 * - Short text (at most 56 characters) is the label; the paired assistant
 *   reply, if any, becomes the description.
 * - Longer text is cut at a word boundary for the label; the description is
 *   the paired reply or, with no reply, the rest of the message.
 *
 * @param {string} text The message's normalised text.
 * @param {string} [responseText] The paired assistant reply's normalised
 *   text. `undefined` means there is no paired reply; an empty string means
 *   the reply exists but has no text yet (no description, no fallback).
 * @returns {MessagePreview}
 */
export function getMessagePreview(text, responseText) {
  if (!text) return { label: 'Message', description: undefined };
  if (text.length <= PREVIEW_TITLE_LENGTH) {
    const reply = responseText ?? '';
    return { label: text, description: reply ? truncateMessageText(reply, PREVIEW_DESCRIPTION_LENGTH) : undefined };
  }
  const titleExcerpt = text.slice(0, PREVIEW_TITLE_LENGTH);
  const titleBoundary = titleExcerpt.lastIndexOf(' ');
  const titleEnd = titleBoundary > PREVIEW_TITLE_LENGTH * 0.65 ? titleBoundary : PREVIEW_TITLE_LENGTH;
  const label = `${text.slice(0, titleEnd).trim()}…`;
  const rest = responseText !== undefined ? responseText : text.slice(titleEnd).trim();
  return { label, description: rest ? truncateMessageText(rest, PREVIEW_DESCRIPTION_LENGTH) : undefined };
}

/**
 * Accessible name of a rail tick.
 *
 * @param {string} sender `data-from` of the message ("user", "assistant", ...).
 * @param {number} index Zero-based position of the message.
 * @param {number} total Number of marked messages.
 * @returns {string}
 */
export function railItemAriaLabel(sender, index, total) {
  return `Go to ${sender} message ${index + 1} of ${total}`;
}

/**
 * The sender name a message is announced with: its `data-from`, or
 * "conversation" when the attribute is missing.
 *
 * @param {string | null | undefined} from
 * @returns {string}
 */
export function messageSender(from) {
  return from ?? 'conversation';
}

/**
 * Index of the assistant reply paired with the user message at `index`: the
 * first later message from "assistant". -1 for non-user messages or when no
 * reply follows.
 *
 * @param {Array<string | null | undefined>} senders `data-from` of each message.
 * @param {number} index
 * @returns {number}
 */
export function findAssistantReply(senders, index) {
  if (senders[index] !== 'user') return -1;
  for (let next = index + 1; next < senders.length; next += 1) {
    if (senders[next] === 'assistant') return next;
  }
  return -1;
}

/**
 * @typedef {object} RailMessage
 * @property {string | null | undefined} from The message's `data-from`.
 * @property {string} text The message's normalised text.
 */

/**
 * @typedef {object} RailDescription
 * @property {string} label
 * @property {string | undefined} description
 * @property {string} ariaLabel
 */

/**
 * Labels, descriptions and tick names for every marked message, in order.
 * A user message is previewed together with the assistant reply after it.
 *
 * @param {RailMessage[]} messages
 * @returns {RailDescription[]}
 */
export function describeRailMessages(messages) {
  const senders = messages.map((message) => message.from);
  return messages.map((message, index) => {
    const sender = messageSender(message.from);
    const replyIndex = sender === 'user' ? findAssistantReply(senders, index) : -1;
    const preview = getMessagePreview(message.text, replyIndex >= 0 ? messages[replyIndex].text : undefined);
    return {
      label: preview.label,
      description: preview.description,
      ariaLabel: railItemAriaLabel(sender, index, messages.length),
    };
  });
}

/**
 * Whether two rail item lists describe the same ticks (so state can keep the
 * current array and skip a render).
 *
 * @param {Array<{ id: string, label: string, description?: unknown, ariaLabel?: string }>} current
 * @param {Array<{ id: string, label: string, description?: unknown, ariaLabel?: string }>} next
 * @returns {boolean}
 */
export function railItemsEqual(current, next) {
  return current.length === next.length && current.every((item, index) => (
    item.id === next[index]?.id
    && item.label === next[index]?.label
    && item.description === next[index]?.description
    && item.ariaLabel === next[index]?.ariaLabel
  ));
}

/**
 * @typedef {object} ScrollMetrics
 * @property {number} scrollHeight
 * @property {number} scrollTop
 * @property {number} clientHeight
 */

/**
 * Pixels left between the bottom of the viewport and the end of the content.
 *
 * @param {ScrollMetrics} metrics
 * @returns {number}
 */
export function distanceFromEnd({ scrollHeight, scrollTop, clientHeight }) {
  return scrollHeight - scrollTop - clientHeight;
}

/**
 * Whether the viewport sits at the live edge (within `threshold` of the end).
 *
 * @param {ScrollMetrics} metrics
 * @param {number} [threshold]
 * @returns {boolean}
 */
export function isAtLiveEdge(metrics, threshold = DEFAULT_FOLLOW_THRESHOLD) {
  return distanceFromEnd(metrics) <= threshold;
}

/**
 * Whether a programmatic smooth scroll has come to rest, checked every
 * SMOOTH_SCROLL_POLL_MS once SMOOTH_SCROLL_SETTLE_MS has passed: its position
 * did not move since the previous check (null on the first one), or it has
 * been ignored for `maxMs`. A long jump outlasts the settle window (Chrome
 * takes about 600ms for 1,400px), and its late scroll events would
 * otherwise read as the reader leaving the live edge.
 *
 * @param {{ scrollTop: number, previousScrollTop: number | null, elapsedMs: number, maxMs?: number }} check
 * @returns {boolean}
 */
export function smoothScrollSettled({ scrollTop, previousScrollTop, elapsedMs, maxMs = SMOOTH_SCROLL_MAX_MS }) {
  return elapsedMs >= maxMs || scrollTop === previousScrollTop;
}

/**
 * Whether the rail should show: the transcript overflows (by more than a
 * pixel of rounding) and holds at least two marked messages.
 *
 * @param {{ scrollHeight: number, clientHeight: number }} metrics
 * @param {number} messageCount
 * @returns {boolean}
 */
export function isRailOverflowing({ scrollHeight, clientHeight }, messageCount) {
  return scrollHeight > clientHeight + 1 && messageCount > 1;
}

/** Row height of a message rail tick (px) while every tick fits. */
export const RAIL_ITEM_SIZE = 14;
/** Smallest row height the rail shrinks its ticks to before it scrolls (px). */
export const MIN_RAIL_ITEM_SIZE = 4;
/** Vertical space the rail leaves free inside the transcript: inset-y-3 + py-1, top and bottom (px). */
export const RAIL_INSET = 32;

/**
 * Row height that fits `count` rail ticks into a transcript `clientHeight`
 * tall: the full 14px while they fit, else as much as fits, never below
 * 4px (a longer rail scrolls).
 *
 * @param {number} clientHeight Height of the transcript viewport (px).
 * @param {number} count Number of ticks.
 * @returns {number}
 */
export function fitRailItemSize(clientHeight, count) {
  if (!(count > 0) || !(clientHeight > 0)) return RAIL_ITEM_SIZE;
  const fit = Math.floor((clientHeight - RAIL_INSET) / count);
  return Math.max(MIN_RAIL_ITEM_SIZE, Math.min(RAIL_ITEM_SIZE, fit));
}

/**
 * Whether a key pressed on the viewport moves the reader away from the end.
 *
 * @param {string} key
 * @returns {boolean}
 */
export function isLeaveLiveEdgeKey(key) {
  return LEAVE_LIVE_EDGE_KEYS.includes(key);
}

/**
 * The rail item that should read as current: the first message near the
 * top, the last one near the end, otherwise the message whose centre is
 * closest to the viewport's centre (the earlier one on a tie). Messages are
 * only measured in that last case.
 *
 * @param {ScrollMetrics & {
 *   threshold: number,
 *   viewportCenter: number,
 *   ids: string[],
 *   measure: (id: string, index: number) => { top: number, height: number },
 * }} input `viewportCenter` and `measure` work in client-rect coordinates.
 * @returns {string | null} `null` when there are no messages (keep the current id).
 */
export function pickActiveRailId({ scrollHeight, scrollTop, clientHeight, threshold, viewportCenter, ids, measure }) {
  if (ids.length === 0) return null;
  if (scrollTop <= threshold) return ids[0];
  if (distanceFromEnd({ scrollHeight, scrollTop, clientHeight }) <= threshold) return ids[ids.length - 1];
  let nearestId = ids[0];
  let nearestDistance = Number.POSITIVE_INFINITY;
  ids.forEach((id, index) => {
    const { top, height } = measure(id, index);
    const distance = Math.abs(top + height / 2 - viewportCenter);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestId = id;
    }
  });
  return nearestId;
}

/**
 * The `scrollTop` that centres a message in the viewport.
 *
 * @param {{ scrollTop: number, clientHeight: number, viewportTop: number, targetTop: number, targetHeight: number }} input
 *   `viewportTop`, `targetTop` and `targetHeight` are client-rect values.
 * @returns {number}
 */
export function centeredScrollTop({ scrollTop, clientHeight, viewportTop, targetTop, targetHeight }) {
  return scrollTop + targetTop - viewportTop - (clientHeight - targetHeight) / 2;
}

/**
 * Scale of a rail tick: full for the highlighted one, then 0.68 and 0.44 for
 * its first and second neighbours, 0.25 for the rest (and for every tick
 * when nothing is highlighted).
 *
 * @param {number} index
 * @param {number} displayedIndex Index of the highlighted tick, or -1.
 * @param {boolean} highlighted
 * @returns {number}
 */
export function tickScale(index, displayedIndex, highlighted) {
  if (highlighted) return 1;
  const distance = displayedIndex < 0 ? Number.POSITIVE_INFINITY : Math.abs(index - displayedIndex);
  if (distance === 1) return 0.68;
  if (distance === 2) return 0.44;
  return 0.25;
}
