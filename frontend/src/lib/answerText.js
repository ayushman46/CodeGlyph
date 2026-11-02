/**
 * Reading an answer's Markdown: the section under a heading (for "Copy
 * section"), the code names an answer says it did not find (for a one-click
 * "Search code" that checks the claim against the index), which search
 * results match such a name exactly, and the server's note on an answer that
 * stopped partway.
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

const IDENTIFIER_TOKEN = /[A-Za-z_$][\w$]*/g;
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Splits code search results for `term` into exact and partial matches. The
 * search matches prefixes and ignores case, so a search for `send_request`
 * also returns `send_request_headers`. An exact match is the name as a whole
 * identifier in the same case, the rule the server uses before it flags a
 * name as not found in the indexed code. For a dotted name
 * (`Command.invoke`) the last part is matched, since code rarely spells out
 * the dotted form.
 *
 * @param {Array<{file_path: string, start_line?: number, matches?: Array<{line: number, text: string}>}>} results
 * @param {string} term
 * @returns {{ name: string, exact: object[], partial: object[], longerNames: string[] }}
 *   `exact` and `partial` hold `{file_path, line, text}` per matching line;
 *   `longerNames` are up to three identifiers that contain the name.
 */
export const splitSearchMatches = (results, term) => {
  const name = String(term || '').trim().replace(/\(\)$/, '').split(/\.|::/).pop() || '';
  const whole = name ? new RegExp(`(?<![\\w$])${escapeRegExp(name)}(?![\\w$])`) : null;
  const lowered = name.toLowerCase();
  const exact = [];
  const partial = [];
  const longerNames = [];
  (Array.isArray(results) ? results : []).forEach((result) => {
    const lines = Array.isArray(result?.matches) && result.matches.length
      ? result.matches
      : [{ line: result?.start_line || null, text: '' }];
    lines.forEach((match) => {
      const entry = { file_path: result.file_path, line: match.line || null, text: String(match.text || '') };
      if (whole && whole.test(entry.text)) {
        exact.push(entry);
        return;
      }
      partial.push(entry);
      for (const [token] of entry.text.matchAll(IDENTIFIER_TOKEN)) {
        if (token !== name && token.toLowerCase().includes(lowered) && !longerNames.includes(token) && longerNames.length < 3) {
          longerNames.push(token);
        }
      }
    });
  });
  return { name, exact, partial, longerNames };
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
