import { createUnifiedPatch } from './diff.js';

/** Build a `git apply`-able patch from a proposal (optionally with edits). */
export const proposalPatch = (proposal, contentByPath = {}) => {
  const files = (proposal?.files || [])
    .filter((file) => file.after != null || contentByPath[file.file_path] != null)
    .map((file) => ({
      path: file.file_path,
      before: file.action === 'create' ? '' : (file.before ?? ''),
      after: contentByPath[file.file_path] ?? file.after ?? '',
      isNew: file.action === 'create',
    }));
  return files.length ? createUnifiedPatch(files) : '';
};

export const downloadText = (filename, text, type = 'text/x-diff') => {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
};

export const slugify = (value, fallback = 'change') => {
  const slug = String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
  return slug || fallback;
};

// The same character set Python's ``str.isspace()`` / ``re`` ``\s`` uses,
// minus the line feed, so trailing-whitespace tolerance matches
// backend/agent/code_edit.py byte for byte.
const WS_CHARS = '\\t\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const TRAILING_WS = `[${WS_CHARS}]`;
const TRAILING_WS_RUN = new RegExp(`${TRAILING_WS}+$`);
const OVERLAP_SCAN_LIMIT = 1000;

const rstrip = (value) => value.replace(TRAILING_WS_RUN, '');
const BLANK = new RegExp(`^[\\n${WS_CHARS}]*$`);
const isBlank = (value) => BLANK.test(value);
const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

/** Count every start offset of `old`, overlapping ones included. */
const occurrenceCount = (content, old) => {
  let count = 0;
  let index = content.indexOf(old);
  while (index !== -1) {
    count += 1;
    if (count >= OVERLAP_SCAN_LIMIT) return count;
    index = content.indexOf(old, index + 1);
  }
  return count;
};

/** LF-normalised text plus the LF-view offset of every former CRLF. */
const lfView = (content) => {
  if (!content.includes('\r\n')) return { text: content, crlf: [] };
  const parts = content.split('\r\n');
  const crlf = [];
  let offset = 0;
  for (let index = 0; index < parts.length - 1; index += 1) {
    offset += parts[index].length;
    crlf.push(offset);
    offset += 1;
  }
  return { text: parts.join('\n'), crlf };
};

/** Map an LF-view offset back to the original string (bisect_left). */
const originalOffset = (index, crlf) => {
  let low = 0;
  let high = crlf.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (crlf[mid] < index) low = mid + 1;
    else high = mid;
  }
  return index + low;
};

/** Regex that ignores trailing whitespace per line (code_edit._tolerant_pattern). */
const tolerantPattern = (old) => {
  if (isBlank(old)) return null;
  const ws = TRAILING_WS;
  const segments = old.split('\n');
  const last = segments.length - 1;
  const parts = segments.map((segment, index) => {
    const stripped = rstrip(segment);
    if (index < last) {
      if (index === 0 && !segment) return '\\n';
      if (index === 0 && !stripped) return `(?<!${ws})${ws}*\\n`;
      return `${escapeRegExp(stripped)}${ws}*\\n`;
    }
    if (segment && stripped === segment) return escapeRegExp(segment);
    if (segment) return `(?:${escapeRegExp(stripped)}${ws}*(?=\\n|$)|${escapeRegExp(segment)})`;
    return '';
  });
  return new RegExp(parts.join(''), 'g');
};

const tolerantSpans = (content, old) => {
  const pattern = tolerantPattern(old);
  if (!pattern) return [];
  const spans = [];
  pattern.lastIndex = 0;
  let match = pattern.exec(content);
  while (match) {
    spans.push([match.index, match.index + match[0].length]);
    if (spans.length >= OVERLAP_SCAN_LIMIT) break;
    // Restart one character later so overlapping locations count too.
    pattern.lastIndex = match.index + 1;
    match = pattern.exec(content);
  }
  return spans;
};

const ambiguous = (hunk, count) => new Error(`Hunk ${hunk} now matches ${count} locations; include more surrounding lines.`);

const uniqueHunkSpan = (content, old, hunk) => {
  const first = content.indexOf(old);
  if (first >= 0) {
    const count = occurrenceCount(content, old);
    if (count === 1) return [first, first + old.length];
    throw ambiguous(hunk, count);
  }
  const tolerant = tolerantSpans(content, old);
  if (tolerant.length === 1) return tolerant[0];
  if (tolerant.length) throw ambiguous(hunk, tolerant.length);
  throw new Error(`Hunk ${hunk} no longer matches the current file.`);
};

/**
 * Re-apply search/replace hunks to a newer file revision with exactly the
 * rules of the server's backend/agent/code_edit.py `apply_hunks`:
 * - matching happens on an LF view, splicing on the original string, so
 *   untouched lines keep their bytes (mixed endings stay mixed) and only the
 *   replacement adopts CRLF when the file contains CRLF;
 * - an exact match must be unique counting overlapping occurrences; only
 *   when there is none, a unique trailing-whitespace-tolerant match is used;
 * - empty or no-op hunks and overlapping hunks are rejected.
 * Throws an Error with a user-safe message on any mismatch.
 */
export const applyHunks = (content, changes = []) => {
  const source = String(content ?? '');
  if (!Array.isArray(changes) || !changes.length) throw new Error('The change has no hunks to apply.');
  const usesCrLf = source.includes('\r\n');
  const { text, crlf } = lfView(source);
  const located = changes.map((change, index) => {
    const hunk = index + 1;
    if (typeof change?.old !== 'string' || !change.old || typeof change?.new !== 'string') {
      throw new Error(`Hunk ${hunk} is empty.`);
    }
    const oldText = change.old.replace(/\r\n/g, '\n');
    const newText = change.new.replace(/\r\n/g, '\n');
    if (oldText === newText) throw new Error(`Hunk ${hunk} does not change the matched text.`);
    const [start, end] = uniqueHunkSpan(text, oldText, hunk);
    return { start, end, newText, hunk };
  }).sort((left, right) => left.start - right.start || left.end - right.end);
  for (let index = 1; index < located.length; index += 1) {
    if (located[index].start < located[index - 1].end) {
      const [first, second] = [located[index - 1].hunk, located[index].hunk].sort((a, b) => a - b);
      throw new Error(`Hunks ${first} and ${second} overlap; combine them into one change.`);
    }
  }
  const pieces = [];
  let cursor = source.length;
  for (let index = located.length - 1; index >= 0; index -= 1) {
    const { start, end, newText } = located[index];
    const originalStart = originalOffset(start, crlf);
    const originalEnd = originalOffset(end, crlf);
    pieces.push(source.slice(originalEnd, cursor));
    pieces.push(usesCrLf ? newText.replace(/\n/g, '\r\n') : newText);
    cursor = originalStart;
  }
  pieces.push(source.slice(0, cursor));
  return pieces.reverse().join('');
};
