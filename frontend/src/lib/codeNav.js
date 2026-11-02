/**
 * Pure helpers for code navigation: links to a file and line in the
 * workspace URL, the symbols enclosing a line, highlighted search text, and
 * the scores that mix files and symbols with commands in the ⌘K palette.
 */

// ------------------------------------------------------------- deep links

/**
 * The file and lines a workspace URL points at (`?file=src/a.py&line=10`,
 * `line=10-20`, `line=L10`), or null. Paths that climb out of the
 * repository (`..`) are ignored.
 *
 * @param {string} search `location.search`
 * @returns {{ path: string, start: number | null, end: number | null } | null}
 */
export const readFileLink = (search) => {
  const params = new URLSearchParams(search || '');
  const path = (params.get('file') || '').trim().replace(/^\/+/, '');
  if (!path || path.split('/').includes('..')) return null;
  const match = /^L?(\d+)(?:-L?(\d+))?$/i.exec((params.get('line') || '').trim());
  let start = match ? Number(match[1]) : null;
  let end = match?.[2] ? Number(match[2]) : start;
  if (!start || start < 1) return { path, start: null, end: null };
  if (end < start) [start, end] = [end, start];
  return { path, start, end };
};

const encodePath = (value) => encodeURIComponent(value).replace(/%2F/gi, '/');

/**
 * `search` pointing at `file` (or at no file when it is null). Other
 * parameters (`tab=overview`) are kept; the path stays readable (`/` is not
 * escaped).
 *
 * @param {string} search
 * @param {{ path: string, start?: number | null, end?: number | null } | null} file
 * @returns {string} '' or a string starting with '?'
 */
export const writeFileLink = (search, file) => {
  const params = new URLSearchParams(search || '');
  params.delete('file');
  params.delete('line');
  const parts = [params.toString()];
  if (file?.path) {
    parts.push(`file=${encodePath(file.path)}`);
    if (file.start) parts.push(`line=${file.start}${file.end && file.end !== file.start ? `-${file.end}` : ''}`);
  }
  const query = parts.filter(Boolean).join('&');
  return query ? `?${query}` : '';
};

/** One string per file and range, for comparing links. */
export const fileLinkKey = (file) => (file?.path ? `${file.path}:${file.start || ''}-${file.end || file.start || ''}` : '');

/**
 * What the sources panel shows, as a link: its file at the selected lines,
 * else at the lines it was opened on; null when no file is open.
 *
 * @param {{ open?: boolean, file?: { path: string, start?: number | null, end?: number | null } | null,
 *   selection?: { start: number, end: number } | null }} sources
 */
export const viewerLink = (sources) => (sources?.open && sources.file?.path
  ? {
    path: sources.file.path,
    start: sources.selection?.start ?? sources.file.start ?? null,
    end: sources.selection?.end ?? sources.file.end ?? null,
  }
  : null);

// ----------------------------------------------------- outline and symbols

/**
 * The outline entries that contain `line`, outermost first
 * (`class Group` › `def parse_args`).
 *
 * @param {{ name: string, kind: string, line: number, end_line?: number, depth?: number }[]} outline
 * @param {number | null} line
 */
export const enclosingSymbols = (outline, line) => {
  if (!line || !outline?.length) return [];
  return outline
    .filter((entry) => entry.line <= line && line <= (entry.end_line || entry.line))
    .sort((a, b) => (a.depth || 0) - (b.depth || 0) || a.line - b.line);
};

/** `class ParamType`, `def convert`; Markdown headings are just their text. */
export const symbolLabel = (entry) => (/^h\d$/.test(entry.kind || '') || !entry.kind || entry.kind === 'symbol'
  ? entry.name
  : `${entry.kind} ${entry.name}`);

const MARKDOWN = /\.(md|mdx|markdown)$/i;
export const isMarkdownPath = (path) => MARKDOWN.test(path || '');

/**
 * A link inside a Markdown file, resolved against that file's folder:
 * a repository path to open in the viewer, or null for web links, anchors
 * and paths that leave the repository.
 *
 * @param {string} href
 * @param {string} fromPath the Markdown file's path
 * @returns {string | null}
 */
export const resolveRepositoryLink = (href, fromPath) => {
  const value = String(href || '').trim();
  if (!value || /^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('//') || value.startsWith('#')) return null;
  const target = value.split('#')[0].split('?')[0];
  if (!target) return null;
  let decoded = target;
  try {
    decoded = decodeURIComponent(target);
  } catch {
    // Keep the raw text.
  }
  const parts = decoded.startsWith('/') ? [] : (fromPath || '').split('/').slice(0, -1);
  for (const part of decoded.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(part);
  }
  return parts.length ? parts.join('/') : null;
};

// ---------------------------------------------------------- search results

/**
 * Splits `text` into plain and matched parts from `[start, end)` spans,
 * counted in UTF-16 code units like JavaScript string indexes (the search
 * API sends them that way).
 *
 * @param {string} text
 * @param {number[][]} [spans]
 * @returns {{ text: string, match: boolean }[]}
 */
export const highlightParts = (text, spans = []) => {
  const value = String(text ?? '');
  const parts = [];
  let cursor = 0;
  [...(spans || [])].sort((a, b) => a[0] - b[0]).forEach(([start, end]) => {
    const from = Math.max(cursor, Math.min(Number(start) || 0, value.length));
    const to = Math.min(Number(end) || 0, value.length);
    if (to <= from) return;
    if (from > cursor) parts.push({ text: value.slice(cursor, from), match: false });
    parts.push({ text: value.slice(from, to), match: true });
    cursor = to;
  });
  if (cursor < value.length || !parts.length) parts.push({ text: value.slice(cursor), match: false });
  return parts;
};

/** "3 files · 17 lines", with "first" when more files matched than were listed. */
export const searchSummary = ({ files, lines, more }) => {
  const fileText = `${more ? 'First ' : ''}${files} ${files === 1 ? 'file' : 'files'}`;
  return `${fileText} · ${lines} matching ${lines === 1 ? 'line' : 'lines'}`;
};

// -------------------------------------------------------------- palette

/**
 * How well a file path matches a typed name or path, 0–100. Mirrors the
 * API's file ranking: the file's own name (or its stem, with or without a
 * leading `_`) scores highest, then a name prefix, a name substring, a
 * folder or path prefix, anywhere in the path, and letters in order.
 */
export const pathScore = (path, query) => {
  const needle = String(query || '').trim().toLowerCase().replace(/^[./]+/, '');
  if (!needle) return 0;
  const low = String(path || '').toLowerCase();
  const name = low.slice(low.lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  if (needle === name || needle === stem || needle === stem.replace(/^_+/, '')) return 98;
  if (name.startsWith(needle) || name.replace(/^_+/, '').startsWith(needle)) return 88;
  if (name.includes(needle)) return 70;
  if (low.startsWith(needle) || low.includes(`/${needle}`)) return 68;
  if (low.includes(needle)) return 55;
  if (needle.length >= 3 && !/[./]/.test(needle)) {
    let position = 0;
    for (const character of needle) {
      position = name.indexOf(character, position);
      if (position === -1) return 0;
      position += 1;
    }
    return 30;
  }
  return 0;
};

/**
 * A declaration's score for the palette: its own name, or its qualified
 * name when the query names the class too (`Client.get`, `Choice::convert`);
 * a member of another class still matches its name, lower.
 */
export const qualifiedSymbolScore = (symbol, query) => {
  const plain = symbolScore(symbol?.name, query);
  const needle = String(query || '').trim().replace(/::|#/g, '.');
  if (!needle.includes('.')) return plain;
  const qualified = symbol?.container ? symbolScore(`${symbol.container}.${symbol.name}`, needle) : 0;
  // A class the index does not know: its members by name, ranked lower.
  const member = Math.round(symbolScore(symbol?.name, needle.split('.').pop()) * 0.6);
  return Math.max(plain, qualified, member);
};

/**
 * How well a declared name matches the query, 0–100: the exact name, the
 * same name in another case, a prefix, a word inside the name
 * (`timeout` in `_set_timeout`, `Type` in `ParamType`), anywhere.
 */
export const symbolScore = (name, query) => {
  const needle = String(query || '').trim();
  const value = String(name || '');
  if (!needle || !value) return 0;
  if (value === needle) return 100;
  const low = value.toLowerCase();
  const lowNeedle = needle.toLowerCase();
  if (low === lowNeedle) return 95;
  if (low.startsWith(lowNeedle)) return 85;
  let position = low.indexOf(lowNeedle);
  if (position < 0) return 0;
  const isUpper = (character) => character !== character.toLowerCase();
  const isLower = (character) => character !== character.toUpperCase();
  while (position > 0) {
    const before = value[position - 1];
    if (before === '_' || before === '$' || (isUpper(value[position]) && isLower(before))) return 75;
    position = low.indexOf(lowNeedle, position + 1);
  }
  return 62;
};

/**
 * Commands and repositories on the same 0–100 scale: the typed text at the
 * start of the label, inside it, or its letters in order.
 */
export const commandScore = (text, query) => {
  const haystack = String(text || '').toLowerCase();
  const needle = String(query || '').toLowerCase().trim();
  if (!needle) return 50;
  const index = haystack.indexOf(needle);
  if (index === 0) return 90;
  if (index > 0) return 80 - Math.min(20, (index / Math.max(1, haystack.length)) * 20);
  let position = 0;
  for (const character of needle) {
    position = haystack.indexOf(character, position);
    if (position === -1) return 0;
    position += 1;
  }
  return 25;
};
