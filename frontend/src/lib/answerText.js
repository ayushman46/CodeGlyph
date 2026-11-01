/**
 * Reading an answer's Markdown: the section under a heading (for "Copy
 * section"), the code names an answer says it did not find (for a one-click
 * "Search code" that checks the claim against the index), and the server's
 * note on an answer that stopped partway.
 */

import { looksLikePath } from './citations.js';

const FENCE = /^\s{0,3}(```|~~~)/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;

/** Every Markdown line of `markdown` with whether it is inside a code fence. */
const linesWithFences = (markdown) => {
  let fence = null;
  return String(markdown || '').split('\n').map((text) => {
    const marker = FENCE.exec(text)?.[1];
    const inFence = Boolean(fence);
    if (marker && (!fence || marker === fence)) fence = fence ? null : marker;
    return { text, inFence: inFence || Boolean(marker) };
  });
};

/**
 * The section that starts at the heading on 1-based `line`: the heading and
 * everything up to the next heading of the same or a higher level (headings
 * inside code fences do not count). Returns '' when `line` is not a heading.
 *
 * @param {string} markdown
 * @param {number} line
 */
export const markdownSection = (markdown, line) => {
  const lines = linesWithFences(markdown);
  const index = Number(line) - 1;
  const heading = lines[index] && !lines[index].inFence ? HEADING.exec(lines[index].text) : null;
  if (!heading) return '';
  const level = heading[1].length;
  let end = index + 1;
  while (end < lines.length) {
    const next = lines[end].inFence ? null : HEADING.exec(lines[end].text);
    if (next && next[1].length <= level) break;
    end += 1;
  }
  return lines.slice(index, end).map((item) => item.text).join('\n').replace(/\s+$/, '');
};

// "I did not retrieve `X`", "the retrieved evidence does not include `X`",
// "`X` was not retrieved", "not found in the evidence", ...
const ABSENCE = /\b(?:did\s+not|didn't|could\s+not|couldn't)\s+(?:retrieve|find|see)\b|\b(?:was|were|is|are)\s+not\s+(?:retrieved|in\s+the\s+(?:retrieved\s+)?evidence|shown\s+in\s+the\s+evidence)\b|\bnot\s+(?:part\s+of|included\s+in)\s+the\s+(?:retrieved\s+)?evidence\b|\b(?:evidence|retrieved\s+(?:code|lines|files))\s+(?:does|do)\s+not\s+(?:include|contain|show)\b|\bno\s+(?:evidence|retrieved\s+code)\s+(?:of|for|shows)\b|\bnot\s+found\s+(?:in|anywhere\s+in)\s+the\s+(?:retrieved\s+evidence|evidence|indexed\s+code)\b/i;
const CODE_SPAN = /`([^`\n]+)`/g;
const CODE_NAME = /^@?[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)*(?:\(\))?$/;

/**
 * Code names that an answer says were missing from its evidence, so the
 * reader can search the whole index for them in one click. Only backticked
 * names in a sentence that states the absence count; file references,
 * commands and fenced code are ignored. At most `limit` names.
 *
 * @param {string} markdown
 * @param {number} [limit]
 * @returns {string[]}
 */
export const absenceTerms = (markdown, limit = 3) => {
  const terms = [];
  linesWithFences(markdown).forEach(({ text, inFence }) => {
    if (inFence) return;
    text.split(/(?<=[.!?])\s+(?=[A-Z`*_(])/).forEach((sentence) => {
      if (!ABSENCE.test(sentence)) return;
      for (const match of sentence.matchAll(CODE_SPAN)) {
        const name = match[1].trim().replace(/\(\)$/, '').replace(/^@/, '');
        if (name.length < 3 || !CODE_NAME.test(match[1].trim()) || looksLikePath(name)) continue;
        if (!terms.includes(name)) terms.push(name);
      }
    });
  });
  return terms.slice(0, limit);
};

// The note the server appends to an answer whose model failed partway
// through ("> The answer was interrupted. <why> Ask again to regenerate it.").
const INTERRUPTION_NOTE = /\n+> The answer was interrupted\.[^\n]*\s*$/;

/**
 * An answer without the server's trailing interruption note, for when the
 * chat shows that failure as a notice with its own actions.
 *
 * @param {string} markdown
 */
export const withoutInterruptionNote = (markdown) => String(markdown || '').replace(INTERRUPTION_NOTE, '');
