/**
 * Pure logic behind components/ai/StreamingText.jsx (the Honest UI
 * "Streaming Text" port). Everything here is framework-free so it runs
 * under `node --test`.
 */

import { formatLines, partitionCitations } from '../citations.js';

/** Default reveal rate of `useSmoothStream`, in characters per second. */
export const DEFAULT_CHARACTERS_PER_SECOND = 110;

/** How long the copy button shows its check (and the status text stays). */
export const COPIED_MILLISECONDS = 1600;

/**
 * Pacing thresholds (the user's constraint: smooth streaming "only where
 * necessary for small conversations and small texts only").
 *
 * - PACE_MAX_CHARACTERS: a reply is paced while its text (received so far,
 *   and therefore also its final text) is at most 1,200 characters. At 110
 *   characters per second that is at most ~11 seconds of reveal, i.e. a
 *   handful of short paragraphs. Anything longer renders as it arrives.
 * - PACE_MAX_MESSAGES: the conversation, counting the reply itself, has at
 *   most 6 messages (three question/answer exchanges). Longer threads render
 *   replies as they arrive.
 * - Replies with code (a fenced block, or `hasCode: true` from the caller,
 *   e.g. an edit proposal or a diff) are never paced.
 *
 * Every input only grows during a reply, so the decision can switch from
 * "pace" to "do not pace" mid-stream (the hook then shows the full text at
 * once, like its own catch-up) but never back.
 */
export const PACE_MAX_CHARACTERS = 1200;
export const PACE_MAX_MESSAGES = 6;

/**
 * Across a re-run of the reveal loop (a new chunk arrived while the reveal was
 * still behind), the time since the previous run's last frame is carried over
 * when it is at most this long, so frequent chunks do not stall the reveal.
 */
export const MAX_FRAME_CARRY_MS = 100;

/**
 * @typedef {{
 *   copy: string, copied: string, copyFailed: string, retry: string,
 *   helpful: string, notHelpful: string, sources: (count: number) => string, error: string,
 * }} StreamingTextLabels
 */

/** @type {StreamingTextLabels} */
export const defaultStreamingTextLabels = {
  copy: 'Copy reply',
  copied: 'Copied',
  copyFailed: 'Copy failed',
  retry: 'Try again',
  helpful: 'Helpful',
  notHelpful: 'Not helpful',
  sources: (count) => (count === 1 ? '1 source' : `${count} sources`),
  error: 'This reply stopped before it finished.',
};

/**
 * Merge caller labels over the defaults (a missing or undefined key keeps
 * its default).
 *
 * @param {Partial<StreamingTextLabels> | undefined} labels
 * @returns {StreamingTextLabels}
 */
export function resolveLabels(labels) {
  const merged = { ...defaultStreamingTextLabels };
  if (labels && typeof labels === 'object') {
    for (const key of Object.keys(labels)) {
      if (labels[key] !== undefined) merged[key] = labels[key];
    }
  }
  return merged;
}

/**
 * The length the reveal shows for `text`: never more than was received.
 *
 * @param {number} shownLength
 * @param {number} textLength
 */
export function visibleLength(shownLength, textLength) {
  return Math.max(0, Math.min(shownLength, textLength));
}

/**
 * Milliseconds to advance on this animation frame. Inside one run of the
 * loop it is the time since the previous frame; the first frame of a run
 * carries over from the previous run's last frame only when that frame was
 * at most `MAX_FRAME_CARRY_MS` ago (otherwise it starts at 0, like a fresh
 * reveal).
 *
 * @param {number | undefined} previousTime frame time of the previous frame
 * @param {number} time this frame's time
 * @param {{ firstOfRun?: boolean }} [options]
 */
export function frameElapsed(previousTime, time, { firstOfRun = false } = {}) {
  if (previousTime === undefined || !Number.isFinite(previousTime)) return 0;
  const elapsed = time - previousTime;
  if (!(elapsed > 0)) return 0;
  if (firstOfRun && elapsed > MAX_FRAME_CARRY_MS) return 0;
  return elapsed;
}

/**
 * One reveal step: advance `position` by `elapsedMs` at `charactersPerSecond`,
 * never past `length` (the received text).
 *
 * @param {number} position fractional characters revealed so far
 * @param {number} elapsedMs
 * @param {number} charactersPerSecond
 * @param {number} length
 */
export function advanceReveal(position, elapsedMs, charactersPerSecond, length) {
  const rate = Number.isFinite(charactersPerSecond) && charactersPerSecond > 0
    ? charactersPerSecond
    : DEFAULT_CHARACTERS_PER_SECOND;
  const step = elapsedMs > 0 ? (elapsedMs / 1000) * rate : 0;
  return Math.min(position + step, length);
}

/**
 * @typedef {{ time: number | undefined, fraction: number }} RevealClock
 *   Carried across runs of the reveal loop: the last frame's time and the
 *   sub-character progress the floored `shownLength` dropped.
 */

/** A clock with no previous frame (a fresh reveal). */
export const IDLE_REVEAL_CLOCK = Object.freeze({ time: undefined, fraction: 0 });

/**
 * One animation frame of `useSmoothStream`'s reveal loop.
 *
 * @param {{
 *   position: number, firstOfRun: boolean, clock: RevealClock, time: number,
 *   length: number, charactersPerSecond: number,
 * }} frame `position` is where this run stands (a run starts at the visible
 * length), `length` the received text's length.
 * @returns {{ position: number, shown: number, done: boolean, clock: RevealClock }}
 */
export function revealFrame({ position, firstOfRun, clock, time, length, charactersPerSecond }) {
  const elapsed = frameElapsed(clock?.time, time, { firstOfRun });
  // A run that carries the previous run's time also carries its fraction.
  const start = firstOfRun && elapsed > 0 ? position + (clock?.fraction || 0) : position;
  const next = advanceReveal(start, elapsed, charactersPerSecond, length);
  const shown = Math.floor(next);
  const done = next >= length;
  return {
    position: next,
    shown,
    done,
    clock: done ? IDLE_REVEAL_CLOCK : { time, fraction: next - shown },
  };
}

/**
 * Next feedback value: pressing the chosen rating again clears it.
 *
 * @param {'up' | 'down' | null} current
 * @param {'up' | 'down'} value
 * @returns {'up' | 'down' | null}
 */
export function nextFeedback(current, value) {
  return current === value ? null : value;
}

const FENCE = /(^|\n)[ \t]{0,3}(```|~~~)/;

/** True when the Markdown text contains a fenced code block (open or closed). */
export function containsCode(text) {
  return typeof text === 'string' && FENCE.test(text);
}

/**
 * Whether a reply should be paced with `useSmoothStream`.
 *
 * @param {{ text?: string, messageCount?: number, hasCode?: boolean }} reply
 *   `text` is the reply received so far, `messageCount` the number of
 *   messages in the conversation including this reply, `hasCode` true for a
 *   reply that carries code outside its text (edit proposals, diffs). When
 *   `hasCode` is not given, fenced code in `text` counts.
 * @returns {boolean}
 */
export function shouldPaceReply({ text = '', messageCount = 0, hasCode } = {}) {
  const body = typeof text === 'string' ? text : '';
  if (body.length > PACE_MAX_CHARACTERS) return false;
  const count = Number.isFinite(messageCount) ? messageCount : 0;
  if (count > PACE_MAX_MESSAGES) return false;
  if (hasCode === true) return false;
  if (hasCode === undefined && containsCode(body)) return false;
  return true;
}

/**
 * A plain primary-button click with no modifier: the case a source with both
 * `href` and `onSelect` handles in the app. Modified or middle clicks keep
 * the link's default (open in a new tab or window).
 *
 * @param {{ button?: number, metaKey?: boolean, ctrlKey?: boolean, shiftKey?: boolean, altKey?: boolean, defaultPrevented?: boolean }} event
 */
export function isPlainClick(event) {
  if (!event || event.defaultPrevented) return false;
  if ((event.button ?? 0) !== 0) return false;
  return !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
}

/**
 * Stable, unique React keys for a list of sources: `id`, else `href`, else
 * `title`; a repeated key gets a `#n` suffix.
 *
 * @param {Array<{ id?: string, href?: string, title?: string }>} sources
 * @returns {string[]}
 */
export function sourceKeys(sources) {
  const seen = new Map();
  return (Array.isArray(sources) ? sources : []).map((source, index) => {
    const base = String(source?.id ?? source?.href ?? source?.title ?? `source-${index}`);
    const count = seen.get(base) || 0;
    seen.set(base, count + 1);
    return count ? `${base}#${count}` : base;
  });
}

/**
 * The app's code citations as StreamingText sources: title `path:L12–40`,
 * the domain slot "cited" or "retrieved" (cited sections first, in relevance
 * order), each opening in the app via `onSelect(citation, event)`. `hrefFor`
 * may return a GitHub permalink, kept reachable with a modified or middle
 * click.
 *
 * @param {Array<{ file_path: string, start_line?: number, end_line?: number, cited_in_answer?: boolean }>} citations
 * @param {{ onSelect?: (citation: object, event: unknown) => void, hrefFor?: (citation: object) => string | null | undefined }} [options]
 */
export function citationsToSources(citations, { onSelect, hrefFor } = {}) {
  const { cited, retrieved } = partitionCitations(citations);
  const toSource = (citation, domain) => {
    const range = formatLines(citation.start_line, citation.end_line);
    const href = hrefFor ? hrefFor(citation) || undefined : undefined;
    return {
      id: `${citation.file_path}:${citation.start_line || 0}-${citation.end_line || 0}:${citation.index}`,
      title: range ? `${citation.file_path}:${range}` : String(citation.file_path || ''),
      domain,
      href,
      onSelect: onSelect ? (event) => onSelect(citation, event) : undefined,
    };
  };
  return [
    ...cited.map((citation) => toSource(citation, 'cited')),
    ...retrieved.map((citation) => toSource(citation, 'retrieved')),
  ];
}
