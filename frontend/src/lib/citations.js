/**
 * Citation helpers. Answers cite code as `path/to/file.ext:L12-L40`; these
 * helpers recognise those references (and the older `**path (L1-L9)**`
 * form), build exact GitHub links, and group sources by folder.
 */

const KNOWN_FILENAMES = new Set([
  'dockerfile', 'makefile', 'procfile', 'gemfile', 'rakefile', 'jenkinsfile', 'vagrantfile', 'brewfile', 'readme',
  'license', 'licence', 'changelog', 'codeowners', 'package.json', 'readme.md', 'tsconfig.json', 'go.mod', 'go.sum',
  'cargo.toml', 'cargo.lock', 'pyproject.toml', 'yarn.lock', 'gemfile.lock', 'poetry.lock', '.env', '.env.example',
  '.env.local', '.env.sample', '.gitignore', '.gitattributes', '.dockerignore', '.npmrc', '.nvmrc', '.prettierrc',
  '.eslintrc', '.editorconfig', '.babelrc',
]);

/**
 * Real source, config and docs extensions. A slash-less token only counts as
 * a file when its (case-sensitive) extension is listed here, so member
 * accesses such as `prisma.booking.update`, `req.body`, `process.env`,
 * `console.log` or `BookingStatus.ACCEPTED` stay inline code.
 */
const SOURCE_EXTENSIONS = new Set([
  'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'mts', 'cts', 'py', 'pyi', 'ipynb', 'go', 'rs', 'java', 'kt', 'kts', 'scala',
  'groovy', 'gradle', 'rb', 'erb', 'php', 'cs', 'cshtml', 'razor', 'fsx', 'swift', 'c', 'h', 'cc', 'cpp', 'cxx', 'hpp',
  'hh', 'hxx', 'dart', 'lua', 'jl', 'exs', 'erl', 'hrl', 'clj', 'cljs', 'elm', 'hs', 'ml', 'mli', 'zig', 'nim', 'sol',
  'vue', 'svelte', 'astro', 'md', 'mdx', 'markdown', 'rst', 'adoc', 'txt', 'json', 'jsonc', 'json5', 'yml', 'yaml',
  'toml', 'ini', 'cfg', 'conf', 'xml', 'plist', 'html', 'htm', 'css', 'scss', 'sass', 'less', 'styl', 'prisma', 'sql',
  'graphql', 'gql', 'proto', 'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'tf', 'tfvars', 'hcl', 'nix', 'dockerfile',
  'properties', 'lock', 'svg', 'hbs', 'ejs', 'pug', 'njk', 'liquid', 'twig', 'mk', 'cmake', 'csv', 'tsv',
]);

// The path may contain Next.js route groups `(group)` and `[...slug]`
// segments; the legacy `path (L1-L9)` form needs whitespace before `(L`.
const REFERENCE = /^(?<path>[^\s:`'"<>]+?|[^:`'"<>]*\/[^:`'"<>]+?)(?::L?(?<start>\d+)(?:\s*[-–]\s*L?(?<end>\d+))?|\s*\(L(?<pstart>\d+)(?:\s*[-–]\s*L?(?<pend>\d+))?\))?$/;

const extensionOf = (name) => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1) : '';
};

export const looksLikePath = (value) => {
  const raw = String(value || '').trim();
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) || raw.startsWith('//')) return false;
  const text = raw.replace(/^\.?\//, '');
  if (!text || text.length > 300 || text.startsWith('@')) {
    return false;
  }
  const segments = text.split('/');
  if (segments.some((segment) => !segment || segment === '.' || segment === '..' || /^\s|\s$/.test(segment))) return false;
  // Whitespace is only plausible inside a multi-segment path ("docs/User Guide.md").
  if (/\s/.test(text) && (segments.length < 2 || /\s{2}|[&|;$<>=]/.test(text))) return false;
  const name = segments[segments.length - 1];
  if (KNOWN_FILENAMES.has(name.toLowerCase())) return true;
  const extension = extensionOf(name);
  if (!extension) return false;
  if (SOURCE_EXTENSIONS.has(extension)) return /^[\w.()[\]@+\- ]+$/.test(name);
  // `README-OAUTH2.MD` style upper-case extensions only count under a directory.
  if (segments.length > 1 && SOURCE_EXTENSIONS.has(extension.toLowerCase()) && !/\s/.test(text)) return true;
  if (/\s/.test(text)) return false;
  // Under a directory, an unlisted but file-like extension (lowercase, has a
  // letter) is still accepted; a bare token never is.
  return segments.length > 1 && /^[a-z0-9]{1,8}$/.test(extension) && /[a-z]/.test(extension);
};

const TRAILING_PUNCTUATION = /[.,;:!?]+$/;

/** Parse "src/a.ts:L10-L20", "src/a.ts:10", "src/a.ts (L3-L9)", or "src/a.ts". */
export const parseReference = (value) => {
  const text = String(value || '').trim().replace(/^\*\*|\*\*$/g, '').trim().replace(TRAILING_PUNCTUATION, '');
  const match = REFERENCE.exec(text);
  if (!match?.groups?.path || !looksLikePath(match.groups.path)) return null;
  const start = Number(match.groups.start || match.groups.pstart) || null;
  const end = Number(match.groups.end || match.groups.pend) || start;
  return { path: match.groups.path.replace(/^\.?\//, ''), start, end: end && start && end < start ? start : end };
};

/** A reference that can stand on its own: a real directory path, no spaces. */
const isStrongPath = (path) => path.includes('/') && !/\s/.test(path);

const referenceKey = (value) => String(value ?? '').trim().replace(/^`+|`+$/g, '').replace(/^\*\*|\*\*$/g, '').trim()
  .replace(TRAILING_PUNCTUATION, '');

const samePath = (candidate, path) => Boolean(candidate && path) && (candidate === path || candidate.endsWith(`/${path}`));

/**
 * What is wrong with a reference the server could not verify, in the words
 * the answer shows. Only a path missing from the index is "not in the
 * indexed snapshot"; real files outside the evidence say so instead.
 */
export const REFERENCE_REASONS = {
  range_outside_evidence: 'exists in the index but was not part of this answer’s evidence',
  lines_outside_file: 'the file exists but does not have these lines',
  ambiguous: 'matches several files',
  not_in_index: 'not in the indexed snapshot',
  lookup_failed: 'could not be checked: the index lookup failed',
};

/** The label for an unverified reference (`{reason, file_lines}` from the server). */
export const referenceReasonLabel = (item) => {
  if (item?.reason === 'lines_outside_file' && Number(item.file_lines)) {
    return `the file exists but has only ${Number(item.file_lines)} lines`;
  }
  return REFERENCE_REASONS[item?.reason] || 'not found in the indexed code';
};

/**
 * Decide how one inline-code span of an answer renders.
 *
 * With the server's `answer_references` (each `{text, file_path, start_line,
 * end_line, valid, reason}`), references the server verified become chips;
 * an indexed file whose lines were outside the answer's evidence becomes an
 * `outside_evidence` chip (it still opens the file); other invalid ones
 * render as unverified text with their `reason` (and `candidates` for a name
 * that matches several files); anything else is plain code. Without them, a
 * reference becomes a chip only when it resolves to one of the message's
 * citations or is a real directory path (contains `/`); bare file names need
 * a citation.
 *
 * @returns {{status: 'verified'|'cited'|'path'|'outside_evidence'|'unverified', reference: {path: string, start: number|null, end: number|null}, reason?: string, label?: string, candidates?: string[]}|null}
 */
export const resolveReference = (text, { citations = [], answerReferences } = {}) => {
  const reference = parseReference(text);
  if (Array.isArray(answerReferences)) {
    const key = referenceKey(text);
    const candidates = answerReferences.filter(Boolean);
    const match = candidates.find((item) => referenceKey(item.text) === key)
      || (reference && (
        candidates.find((item) => samePath(item.file_path, reference.path) && reference.start && item.start_line === reference.start)
        || candidates.find((item) => samePath(item.file_path, reference.path) && (!reference.start || item.start_line == null))
      ));
    if (!match) return null;
    const start = Number(match.start_line) || reference?.start || null;
    const end = Number(match.end_line) || reference?.end || start;
    const resolved = { path: match.file_path || reference?.path || key, start, end: end && start && end < start ? start : end };
    if (match.valid === true) return { status: 'verified', reference: resolved };
    const detail = { reason: match.reason || null, label: referenceReasonLabel(match) };
    if (Array.isArray(match.candidates) && match.candidates.length) detail.candidates = match.candidates.map(String);
    if (match.reason === 'range_outside_evidence' && match.file_path) return { status: 'outside_evidence', reference: resolved, ...detail };
    return { status: 'unverified', reference: resolved, ...detail };
  }
  if (!reference) return null;
  if (matchCitation(citations, reference) >= 0) return { status: 'cited', reference };
  if (isStrongPath(reference.path)) return { status: 'path', reference };
  return null;
};

// One path segment: word characters plus whole Next.js `[id]`, `[...slug]`,
// `[[...slug]]` and `(group)` parts, so a bracket or parenthesis that only
// wraps the reference is never swallowed into the path.
const SEGMENT = String.raw`(?:\[\[?[\w.-]+\]\]?|\([\w.-]+\)|[\w@.+-])`;
const EXTENSIONS_ALTERNATION = [...SOURCE_EXTENSIONS].sort((a, b) => b.length - a.length).join('|');
const PLAIN_LEAD = String.raw`(^|[\s(\[]|\*\*)`;
const PLAIN_TAIL = String.raw`:L?(\d+)(?:\s*[-–]\s*L?(\d+))?(?=$|[\s),.;:!?\]*])`;
// `dir/file.ext:L1-L2` in prose (the path needs a directory) ...
const PLAIN_PATH = new RegExp(String.raw`${PLAIN_LEAD}((?:${SEGMENT}+\/)+${SEGMENT}+\.[A-Za-z0-9]{1,8})${PLAIN_TAIL}`, 'gm');
// ... or a root file with a listed extension and an explicit `:L` line.
const PLAIN_ROOT_FILE = new RegExp(String.raw`${PLAIN_LEAD}([\w@+-][\w@.+-]*\.(?:${EXTENSIONS_ALTERNATION})):L(\d+)(?:\s*[-–]\s*L?(\d+))?(?=$|[\s),.;:!?\]*])`, 'gm');
// "`path` lines 10-20", "`path` from line 133 to 392", "`path` (line 7)".
const PROSE_RANGE = /`([^`\n]+)`(\s+\(?(?:from\s+)?(?:on\s+)?lines?\s+(\d+)(?:\s*(?:-|–|to|through)\s*(\d+))?)/gi;

// ... or an extension-less well-known file (`Dockerfile:L1-L9`, `apps/api/LICENSE:L3`).
const BARE_NAMES = [...KNOWN_FILENAMES].filter((name) => !name.includes('.')).join('|');
const PLAIN_KNOWN_FILE = new RegExp(String.raw`${PLAIN_LEAD}((?:${SEGMENT}+\/)*(?:${BARE_NAMES}))${PLAIN_TAIL}`, 'gim');

const wrapPlain = (full, lead, path, start, end) => (
  looksLikePath(path) ? `${lead}\`${path}:L${start}${end ? `-L${end}` : ''}\`` : full
);

/**
 * Wrap plain-text references in backticks so the markdown renderer can turn
 * them into interactive chips, and fold a line range written in prose right
 * after a backticked path into it. Fenced blocks and inline code are untouched.
 */
export const normaliseCitationMarkdown = (markdown) => {
  const source = String(markdown || '');
  const fences = source.split(/(```[\s\S]*?```)/g);
  return fences.map((segment) => {
    if (segment.startsWith('```')) return segment;
    const withRanges = segment.replace(PROSE_RANGE, (full, code, prose, start, end) => {
      const reference = parseReference(code);
      if (!reference || reference.start) return full;
      return `\`${reference.path}:L${start}${end && Number(end) >= Number(start) ? `-L${end}` : ''}\`${prose}`;
    });
    return withRanges.split(/(`[^`\n]*`)/g).map((part) => {
      if (part.startsWith('`')) return part;
      return part
        .replace(/\*\*([^*\n`]+?)\s*\(L(\d+)(?:\s*[-–]\s*L?(\d+))?\):?\*\*/g, (full, path, start, end) => (
          looksLikePath(path.trim()) ? `\`${path.trim()}:L${start}${end ? `-L${end}` : ''}\`` : full
        ))
        .replace(PLAIN_PATH, wrapPlain)
        .replace(PLAIN_ROOT_FILE, wrapPlain)
        .replace(PLAIN_KNOWN_FILE, wrapPlain);
    }).join('');
  }).join('');
};

export const parseRepository = (githubUrl) => {
  const match = /github\.com[/:]([^/\s]+)\/([^/\s#?]+?)(?:\.git)?(?:[/#?].*)?$/i.exec(String(githubUrl || ''));
  return match ? { owner: match[1], repo: match[2], fullName: `${match[1]}/${match[2]}` } : null;
};

const encodePath = (path) => String(path).split('/').map(encodeURIComponent).join('/');

export const githubBlobUrl = (githubUrl, ref, path, start, end) => {
  const repo = parseRepository(githubUrl);
  if (!repo || !path) return null;
  const anchor = start ? `#L${start}${end && end !== start ? `-L${end}` : ''}` : '';
  return `https://github.com/${repo.owner}/${repo.repo}/blob/${encodeURIComponent(ref || 'HEAD')}/${encodePath(path)}${anchor}`;
};

export const githubTreeUrl = (githubUrl, ref, directory) => {
  const repo = parseRepository(githubUrl);
  if (!repo) return null;
  return `https://github.com/${repo.owner}/${repo.repo}/tree/${encodeURIComponent(ref || 'HEAD')}/${encodePath(directory || '')}`;
};

export const dirname = (path) => {
  const index = String(path || '').lastIndexOf('/');
  return index === -1 ? '' : path.slice(0, index);
};

export const basename = (path) => String(path || '').split('/').pop();

/**
 * Shorten a long path from the left, keeping the most specific trailing
 * segments (folder + file), e.g. "…/googlecalendar/lib/CalendarService.ts".
 */
export const compactPath = (path, max = 44) => {
  const text = String(path || '');
  if (text.length <= max) return text;
  const parts = text.split('/');
  let label = parts.pop();
  while (parts.length && `${parts[parts.length - 1]}/${label}`.length + 2 <= max) label = `${parts.pop()}/${label}`;
  return `…/${label}`;
};

/**
 * Group citations by directory, keeping the original (relevance) order. An
 * item that already carries an `index` (see partitionCitations) keeps it, so
 * grouped subsets still point at the message's full citation list.
 */
export const groupByDirectory = (citations = []) => {
  const groups = new Map();
  citations.forEach((citation, index) => {
    const directory = citation.directory ?? dirname(citation.file_path);
    if (!groups.has(directory)) groups.set(directory, []);
    groups.get(directory).push({ ...citation, index: Number.isInteger(citation.index) ? citation.index : index });
  });
  return [...groups.entries()].map(([directory, items]) => ({ directory, items }));
};

/**
 * Split a message's citations into the sections the answer cites and the
 * other retrieved evidence. Each item keeps its position in the full list as
 * `index`. Answers from servers that never mark `cited_in_answer` put every
 * section under `cited` so nothing looks unused.
 */
export const partitionCitations = (citations = []) => {
  const list = Array.isArray(citations) ? citations : [];
  const marked = list.some((citation) => typeof citation?.cited_in_answer === 'boolean');
  const cited = [];
  const retrieved = [];
  list.forEach((citation, index) => {
    const item = { ...citation, index };
    if (!marked || citation.cited_in_answer) cited.push(item);
    else retrieved.push(item);
  });
  return { cited, retrieved, marked };
};

/** "L42–118", "L7", or "" when there is no line. */
export const formatLines = (start, end) => {
  const first = Number(start) || 0;
  if (!first) return '';
  const last = Number(end) || first;
  return last > first ? `L${first}–${last}` : `L${first}`;
};

/**
 * The lines of a citation's indexed content that cover `start`–`end`
 * (clamped to the chunk), for hover previews. Returns null when the chunk
 * does not overlap the range or has no content.
 *
 * @returns {{ lines: string[], firstLine: number, more: number } | null}
 */
export const citationExcerpt = (citation, start, end, maxLines = 12) => {
  const content = typeof citation?.content === 'string' ? citation.content.replace(/\r\n/g, '\n') : '';
  const chunkStart = Number(citation?.start_line) || 0;
  if (!content || !chunkStart) return null;
  const all = content.split('\n');
  if (all.length > 1 && all[all.length - 1] === '') all.pop();
  const chunkEnd = chunkStart + all.length - 1;
  const from = Math.max(chunkStart, Number(start) || chunkStart);
  const to = Math.min(chunkEnd, Math.max(from, Number(end) || Number(start) || chunkEnd));
  if (from > chunkEnd || to < chunkStart) return null;
  const wanted = all.slice(from - chunkStart, to - chunkStart + 1);
  // Drop leading blank lines so the preview starts on code.
  let skip = 0;
  while (skip < wanted.length - 1 && !wanted[skip].trim()) skip += 1;
  const lines = wanted.slice(skip, skip + maxLines);
  return { lines, firstLine: from + skip, more: Math.max(0, wanted.length - skip - lines.length) };
};

/**
 * Summarise the server's reference check for one answer. `byReason` counts
 * the unverified references by why they failed (`range_outside_evidence`,
 * `ambiguous`, ...; `unknown` for servers that send no reason), and
 * `inIndex` counts the references whose file exists in the index (verified
 * ones plus indexed files whose lines were outside the evidence).
 *
 * @returns {{ total: number, verified: number, unverified: number, repaired: number, inIndex: number, byReason: Record<string, number> } | null}
 */
export const summariseReferences = (answerReferences) => {
  if (!Array.isArray(answerReferences) || !answerReferences.length) return null;
  const seen = new Set();
  let verified = 0;
  let unverified = 0;
  let repaired = 0;
  let inIndex = 0;
  const byReason = {};
  answerReferences.forEach((item) => {
    if (!item) return;
    const key = `${item.file_path || item.text}:${item.start_line ?? ''}-${item.end_line ?? ''}`;
    if (seen.has(key)) return;
    seen.add(key);
    if (item.valid === true) verified += 1;
    else {
      unverified += 1;
      const reason = item.reason || 'unknown';
      byReason[reason] = (byReason[reason] || 0) + 1;
    }
    if (item.valid === true || (item.reason === 'range_outside_evidence' && item.file_path)) inIndex += 1;
    if (item.repaired) repaired += 1;
  });
  return { total: verified + unverified, verified, unverified, repaired, inIndex, byReason };
};

// ------------------------------------------------------------ permalinks

/** The commit a message's citations were pinned to (from their GitHub permalinks), or null. */
export const commitFromCitations = (citations = []) => {
  for (const citation of Array.isArray(citations) ? citations : []) {
    const match = /^https:\/\/github\.com\/[^/]+\/[^/]+\/blob\/([0-9a-f]{7,40})\//i.exec(String(citation?.github_url || ''));
    if (match) return match[1];
  }
  return null;
};

/**
 * Where an answer's references link to: the repository and the commit the
 * answer was written against (its snapshot, else its citations' permalinks,
 * else the repository's current commit).
 *
 * @returns {{ githubUrl: string, commit: string } | null}
 */
export const answerLinkTarget = (message, repo) => {
  const githubUrl = message?.snapshot?.github_url || repo?.github_url || '';
  const commit = message?.snapshot?.commit_sha || commitFromCitations(message?.citations) || repo?.commit_sha || '';
  return parseRepository(githubUrl) && commit ? { githubUrl, commit } : null;
};

/**
 * The answer as Markdown for pasting elsewhere: every file/line reference
 * that resolves to an indexed file becomes a link to that file at the
 * answer's commit (`https://github.com/<owner>/<repo>/blob/<commit>/<path>#Lx-Ly`),
 * with the reference text kept as its label. References the server could not
 * resolve, code blocks and other code stay as they are.
 *
 * @param {string} markdown
 * @param {{ githubUrl?: string, commit?: string, citations?: object[], answerReferences?: object[] }} target
 */
export const markdownWithPermalinks = (markdown, { githubUrl, commit, citations = [], answerReferences } = {}) => {
  const source = normaliseCitationMarkdown(markdown);
  if (!githubUrl || !commit || !parseRepository(githubUrl)) return source;
  return source.split(/(```[\s\S]*?```)/g).map((segment) => {
    if (segment.startsWith('```')) return segment;
    return segment.replace(/(\[?)`([^`\n]+)`(\]\()?/g, (full, open, code, close) => {
      if (open || close) return full; // already a link label
      const resolved = resolveReference(code, { citations, answerReferences });
      if (!resolved || !['verified', 'cited', 'outside_evidence'].includes(resolved.status)) return full;
      const { start, end } = resolved.reference;
      // A bare name matched to a source links to that source's full path.
      const path = resolved.status === 'cited'
        ? citations[matchCitation(citations, resolved.reference)]?.file_path || resolved.reference.path
        : resolved.reference.path;
      const url = githubBlobUrl(githubUrl, commit, path, start, end);
      return url ? `[\`${code}\`](${url})` : full;
    });
  }).join('');
};

/** Find the citation that best matches a reference in the answer text. */
export const matchCitation = (citations = [], reference) => {
  if (!reference) return -1;
  let best = -1;
  let bestScore = -1;
  citations.forEach((citation, index) => {
    const path = citation.file_path || '';
    if (!(path === reference.path || path.endsWith(`/${reference.path}`))) return;
    let score = 1;
    if (reference.start && citation.start_line <= reference.start && citation.end_line >= reference.start) score += 2;
    if (path === reference.path) score += 1;
    if (score > bestScore) {
      best = index;
      bestScore = score;
    }
  });
  return best;
};
