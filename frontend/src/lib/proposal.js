import { slugify } from './patch.js';

/** Paths the server rebuilt from the index instead of reading from GitHub. */
export const approximatePaths = (proposal) => new Set(Array.isArray(proposal?.approximate_files) ? proposal.approximate_files.filter(Boolean) : []);

/** "Fixes #N" closes the issue on merge, so it is only used for a confirmed issue. */
export const issueLabel = (proposal) => {
  if (!proposal?.issue_reference) return '';
  return `${proposal.issue_confirmed === true ? 'Fixes' : 'Refs'} #${proposal.issue_reference}`;
};

/** Planned-but-unchanged files, and whether the proposal may be incomplete. */
export const incompleteness = (proposal) => {
  const unchanged = Array.isArray(proposal?.unchanged_files) ? proposal.unchanged_files.filter((item) => item?.file_path) : [];
  return { unchanged, incomplete: proposal?.incomplete === true || unchanged.length > 0 };
};

// ------------------------------------------------------------ PR form defaults

export const BRANCH_PREFIX = 'codebase-intel/';

/**
 * Default branch for a proposal: the title as a slug cut at a word boundary
 * (never mid-word), plus a short random suffix so retries never collide.
 */
export const branchNameFor = (title, suffix) => {
  const slug = slugify(title, 'change');
  let base = slug.slice(0, 40);
  if (slug.length > 40) {
    const cut = base.lastIndexOf('-');
    if (cut >= 16) base = base.slice(0, cut);
  }
  return `${BRANCH_PREFIX}${base.replace(/-+$/, '')}${suffix ? `-${suffix}` : ''}`;
};

/** Why a branch name would be refused, or '' when it is valid. */
export const branchProblem = (branch) => {
  const value = String(branch ?? '').trim();
  if (!value.startsWith(BRANCH_PREFIX)) return `Branch names must start with ${BRANCH_PREFIX}`;
  const rest = value.slice(BRANCH_PREFIX.length);
  if (!rest) return `Add a name after ${BRANCH_PREFIX}`;
  if (!/^[A-Za-z0-9._\-/]+$/.test(rest)) return 'Use only letters, numbers, dots, dashes and slashes — no spaces.';
  if (value.includes('..') || value.includes('//') || /\/\./.test(value) || value.endsWith('/') || value.endsWith('.') || /\.lock(\/|$)/.test(value)) {
    return 'Branch names cannot contain "..", "//" or "/.", or end with "/", "." or ".lock".';
  }
  return '';
};

/** Longest pull request description the API accepts (PushPRRequest.pr_body). */
export const PR_BODY_LIMIT = 20000;
/** Longest commit message the API accepts. */
export const COMMIT_MESSAGE_LIMIT = 500;
const PR_FOOTER = '\n\n---\n_Generated with Codebase Intel and reviewed before pushing._';

// A heading (Markdown "## …" or a bold line used as one) that already names a
// testing section: "Testing", "How Has This Been Tested?", "Test plan",
// "How to verify", "Verification", "Validation", "QA".
const TESTING_WORDS = String.raw`\b(?:test(?:s|ing|ed)?|verif(?:y|ied|ication)|validat(?:e|ed|ion)|qa)\b`;
const TESTING_HEADING = new RegExp(String.raw`^\s{0,3}(?:#{1,6}\s+.*${TESTING_WORDS}|\*\*[^*\n]*${TESTING_WORDS}[^*\n]*\*\*\s*:?\s*$)`, 'im');

/** Whether a PR body already has a testing / how-to-verify section. */
export const hasTestingSection = (body) => TESTING_HEADING.test(String(body ?? ''));

/**
 * Pull request description: the server's body (which follows the
 * repository's PR template when it has one) plus a testing checklist only
 * when the body has no testing section of its own, then a footer. Checklist
 * boxes are never ticked. At most PR_BODY_LIMIT characters, footer included.
 */
export const buildPrBody = (proposal) => {
  const body = String(proposal?.pr_body || '').trim() || `## Summary\n\n${proposal?.summary || 'Reviewed change.'}`;
  const validation = (Array.isArray(proposal?.validation) ? proposal.validation : [])
    .filter((step) => typeof step === 'string' && step.trim())
    .map((step) => step.trim());
  // A step the body already lists as a bullet or checkbox is not repeated.
  const listed = new Set(body.split(/\r?\n/).map((line) => line.replace(/^\s*[-*+]\s+(?:\[[ xX]\]\s+)?/, '').trim()));
  const missing = validation.filter((step) => !listed.has(step) && !listed.has(`\`${step}\``));
  const checklist = missing.length && !hasTestingSection(body)
    ? `\n\n## Testing\n${missing.map((step) => `- [ ] ${step}`).join('\n')}`
    : '';
  return `${body}${checklist}`.slice(0, PR_BODY_LIMIT - PR_FOOTER.length) + PR_FOOTER;
};

/** The commit message to prefill: the server's (conventions-aware) message, else the title. */
export const commitMessageFor = (proposal) => {
  const message = typeof proposal?.commit_message === 'string' ? proposal.commit_message.trim() : '';
  return (message || String(proposal?.title || '').trim() || 'Apply reviewed changes').slice(0, COMMIT_MESSAGE_LIMIT);
};

/** The Conventional Commits subject rule the server checks titles against. */
export const CONVENTIONAL_TITLE = /^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: \S/;

/**
 * Live hint for the PR title when the repository uses Conventional Commits:
 * `{ ok, message }`, or null when the repository has no such convention (or
 * the proposal predates convention detection).
 */
export const titleConventionHint = (title, conventions) => {
  if (conventions?.commit_style !== 'conventional') return null;
  const source = typeof conventions.commit_style_source === 'string' && conventions.commit_style_source.trim()
    ? ` (${conventions.commit_style_source.trim()})`
    : '';
  if (CONVENTIONAL_TITLE.test(String(title ?? '').trim())) {
    return { ok: true, message: `Follows Conventional Commits, which this repository uses${source}.` };
  }
  return {
    ok: false,
    message: `This repository uses Conventional Commits${source}. Start the title with a type, for example "fix: …" or "feat(scope): …".`,
  };
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The proposal id to send with a push (the API accepts only a UUID), or ''. */
export const proposalIdFor = (proposal) => (typeof proposal?.id === 'string' && UUID.test(proposal.id) ? proposal.id : '');

// ------------------------------------------------------------ quality gates

const QUALITY_RANK = { pass: 0, warn: 1, fail: 2 };
const CHECK_STATUSES = new Set(['pass', 'warn', 'fail', 'skipped']);
const MAX_FINDINGS = 20;

/** Default names of the server's quality checks (a check's own `label` wins). */
export const CHECK_LABELS = Object.freeze({
  syntax: 'Syntax',
  style: 'Style',
  imports: 'Imports',
  completeness: 'Related code',
  tests: 'Tests touched',
  review: 'Self-review',
  conventions: 'Conventions',
  scope: 'Scope',
});

const isRecord = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const text = (value) => (typeof value === 'string' ? value.trim() : '');
const position = (value) => (Number.isInteger(value) && value > 0 ? value : null);
const worse = (left, right) => ((QUALITY_RANK[right] ?? -1) > (QUALITY_RANK[left] ?? -1) ? right : left);

/** `{ file_path, line, column, message }` findings, at most 20, malformed ones dropped. */
export const normalizeFindings = (files) => (Array.isArray(files) ? files : [])
  .filter((entry) => isRecord(entry) && (text(entry.file_path) || text(entry.message)))
  .slice(0, MAX_FINDINGS)
  .map((entry) => ({
    file_path: text(entry.file_path),
    line: position(entry.line),
    column: position(entry.column),
    message: text(entry.message),
  }));

/** "test/is.ts:2:1 message", "test/is.ts message" or the message alone. */
export const findingText = (finding) => {
  const where = finding?.file_path
    ? `${finding.file_path}${finding.line ? `:${finding.line}${finding.column ? `:${finding.column}` : ''}` : ''}`
    : '';
  return [where, text(finding?.message)].filter(Boolean).join(' ');
};

const qualityOf = (proposal) => (isRecord(proposal?.quality) ? proposal.quality : null);

const normalizeCheck = (check) => {
  const id = text(check.id);
  return {
    id,
    label: text(check.label) || CHECK_LABELS[id] || id.replace(/[_-]+/g, ' ').replace(/^./, (first) => first.toUpperCase()) || 'Check',
    // An unknown status is reported, never counted as a pass or a failure.
    status: CHECK_STATUSES.has(check.status) ? check.status : 'skipped',
    detail: text(check.detail),
    files: normalizeFindings(check.files),
    // A check skipped on purpose (the self-review of a small change that
    // every deterministic check passed) is not a warning.
    intentional: check.intentional === true,
  };
};

const checksOf = (quality) => (Array.isArray(quality?.checks) ? quality.checks : [])
  .filter((check) => isRecord(check) && text(check.id))
  .map(normalizeCheck);

/**
 * The proposal's quality gates in one place:
 * `{ status, failing, warnings, skipped, passed, reviewed, reviewSummary,
 * reviewIssues, autofixed }`.
 * - status: 'pass' | 'warn' | 'fail', the worst of the server's status and
 *   its checks (a skipped self-review counts as a warning); null for an older
 *   proposal without `quality`, which then renders exactly as before.
 * - failing / warnings: checks that failed / warn (the skipped self-review is
 *   a warning); skipped: other checks that could not run; passed: the rest.
 */
export const qualitySummary = (proposal) => {
  const quality = qualityOf(proposal);
  if (!quality) {
    return {
      status: null, failing: [], warnings: [], skipped: [], passed: [], reviewed: false, reviewSummary: '',
      reviewSkippedOnPurpose: false, reviewIssues: [], autofixed: [],
    };
  }
  const checks = checksOf(quality);
  const review = checks.find((check) => check.id === 'review');
  const reviewed = quality.reviewed === true;
  const reviewSkipped = review ? review.status === 'skipped' && !review.intentional : quality.reviewed === false;
  let derived = 'pass';
  checks.forEach((check) => {
    derived = worse(derived, check.status === 'skipped' ? (check.id === 'review' && !check.intentional ? 'warn' : 'pass') : check.status);
  });
  if (reviewSkipped) derived = worse(derived, 'warn');
  const status = QUALITY_RANK[quality.status] !== undefined ? worse(quality.status, derived) : derived;
  const reviewSummary = text(quality.review_summary) || (review && !reviewed ? review.detail : '');
  return {
    status,
    failing: checks.filter((check) => check.status === 'fail'),
    warnings: checks.filter((check) => check.status === 'warn' || (check.id === 'review' && check.status === 'skipped' && !check.intentional)),
    skipped: checks.filter((check) => check.status === 'skipped' && check.id !== 'review'),
    passed: checks.filter((check) => check.status === 'pass'),
    reviewed,
    reviewSummary,
    reviewSkippedOnPurpose: Boolean(review && review.status === 'skipped' && review.intentional),
    reviewIssues: (Array.isArray(quality.review_issues) ? quality.review_issues : [])
      .filter((issue) => isRecord(issue) && text(issue.problem))
      .map((issue) => ({
        severity: ['high', 'medium', 'low'].includes(issue.severity) ? issue.severity : 'medium',
        file_path: text(issue.file_path),
        line: position(issue.line),
        problem: text(issue.problem),
        fixed: issue.fixed === true,
        // An issue that cites no automatic finding and quotes no wrong code
        // is a suggestion the server did not apply; older proposals lack the field.
        verified: issue.verified !== false,
        note: text(issue.note),
      })),
    autofixed: (Array.isArray(quality.autofixed) ? quality.autofixed : [])
      .filter((fix) => isRecord(fix) && text(fix.file_path) && text(fix.rule))
      .map((fix) => ({ file_path: text(fix.file_path), rule: text(fix.rule), lines: Number.isInteger(fix.lines) && fix.lines > 0 ? fix.lines : 0 })),
  };
};

/**
 * One file's quality: 'fail' / 'warn' when a check (or an unfixed review
 * issue) names it, 'pass' only when its syntax was actually checked and
 * nothing names it, else null (not checked, or an older proposal).
 */
export const fileQuality = (proposal, path) => {
  if (!path) return null;
  const file = (Array.isArray(proposal?.files) ? proposal.files : []).find((item) => item?.file_path === path);
  const syntax = file?.syntax?.status;
  let found = null;
  const quality = qualityOf(proposal);
  checksOf(quality).forEach((check) => {
    if ((check.status === 'fail' || check.status === 'warn') && check.files.some((entry) => entry.file_path === path)) {
      found = worse(found, check.status);
    }
  });
  if ((Array.isArray(quality?.review_issues) ? quality.review_issues : [])
    .some((issue) => isRecord(issue) && issue.fixed !== true && issue.verified !== false && issue.file_path === path)) {
    found = worse(found, 'warn');
  }
  if (QUALITY_RANK[syntax] !== undefined) return worse(syntax, found);
  return found;
};

/** The card's quality note: "checks passed", "2 warnings", "syntax errors"; '' without quality. */
export const qualityNote = (summary) => {
  if (!summary?.status) return '';
  if (summary.status === 'fail') {
    const ids = new Set(summary.failing.map((check) => check.id));
    if (ids.has('syntax')) return 'syntax errors';
    if (ids.has('imports')) return 'import errors';
    return summary.failing.length ? `${summary.failing.length} failed check${summary.failing.length === 1 ? '' : 's'}` : 'checks failed';
  }
  if (summary.status === 'warn') {
    const count = summary.warnings.length;
    return count ? `${count} warning${count === 1 ? '' : 's'}` : 'warnings';
  }
  return 'checks passed';
};

/** A response header by case-insensitive name (axios headers, or a plain object). */
const responseHeader = (error, name) => {
  const headers = error?.response?.headers;
  if (!headers) return undefined;
  if (typeof headers.get === 'function') return headers.get(name) ?? undefined;
  const key = Object.keys(headers).find((item) => item.toLowerCase() === name.toLowerCase());
  return key === undefined ? undefined : headers[key];
};

/**
 * A push refused by the server's push-time syntax check (422,
 * X-Error-Code: quality_failed): `{ message, files }`, or null for any other
 * failure (FastAPI's own 422s carry a list detail, not this object).
 */
export const qualityFailure = (error) => {
  if (error?.response?.status !== 422) return null;
  const detail = error.response.data?.detail;
  const code = String(responseHeader(error, 'x-error-code') || '').toLowerCase();
  const structured = isRecord(detail) && Array.isArray(detail.files);
  if (code !== 'quality_failed' && !structured) return null;
  const message = (isRecord(detail) ? text(detail.message) : text(detail)) || 'The changed files have syntax errors.';
  return { message, files: normalizeFindings(isRecord(detail) ? detail.files : []) };
};

// -------------------------------------------------------------- CI status

const CI_LABELS = Object.freeze({
  success: 'Checks passed',
  failure: 'Checks failed',
  pending: 'Checks running',
  action_required: 'Waiting for approval',
  none: 'No checks yet',
});
const CI_TONES = Object.freeze({ success: 'success', failure: 'danger', pending: 'info', action_required: 'warning', none: 'neutral' });

/** "Checks passed", "Checks failed", … for a pr-status `state`. */
export const ciStateLabel = (state) => CI_LABELS[state] || 'Checks unknown';

/** Badge tone for a pr-status `state`. */
export const ciStateTone = (state) => CI_TONES[state] || 'neutral';

const COUNT_WORDS = [
  ['success', 'passed'], ['failure', 'failed'], ['pending', 'running'], ['action_required', 'awaiting approval'],
  ['neutral', 'neutral'], ['skipped', 'skipped'],
];

/** "3 passed · 1 failed" from pr-status `counts` (zero counts left out). */
export const ciCountsText = (counts) => COUNT_WORDS
  .filter(([key]) => Number.isInteger(counts?.[key]) && counts[key] > 0)
  .map(([key, word]) => `${counts[key]} ${word}`)
  .join(' · ');

/** What a check's status / conclusion means, for its row. */
export const ciCheckOutcome = (check) => {
  const conclusion = check?.conclusion;
  if (conclusion === 'action_required') return { label: 'Needs approval', tone: 'warning' };
  if (['failure', 'timed_out', 'cancelled', 'startup_failure', 'error'].includes(conclusion) || check?.status === 'failure' || check?.status === 'error') {
    return { label: conclusion === 'timed_out' ? 'Timed out' : conclusion === 'cancelled' ? 'Cancelled' : 'Failed', tone: 'danger' };
  }
  if (conclusion === 'success' || check?.status === 'success') return { label: 'Passed', tone: 'success' };
  if (conclusion === 'skipped') return { label: 'Skipped', tone: 'neutral' };
  if (conclusion === 'neutral' || conclusion === 'stale') return { label: conclusion === 'stale' ? 'Stale' : 'Neutral', tone: 'neutral' };
  return { label: check?.status === 'queued' ? 'Queued' : 'Running', tone: 'info' };
};

/** Why GitHub would or would not merge the pull request now ('' when unknown). */
export const mergeStateMessage = (pr) => {
  if (!isRecord(pr)) return '';
  if (pr.merged) return 'Merged.';
  if (pr.state === 'closed') return 'Closed without merging.';
  switch (pr.mergeable_state) {
    case 'dirty': return 'Merge conflicts with the base branch: update the branch before it can be merged.';
    case 'behind': return 'The branch is behind the base branch: update it before merging.';
    case 'blocked': return 'Merging is blocked until the required reviews or checks pass.';
    case 'unstable': return 'No conflicts, but some checks are failing or still running.';
    case 'clean': return 'No conflicts with the base branch; ready for a maintainer\'s review.';
    case 'has_hooks': return 'No conflicts; the repository runs merge hooks.';
    case 'draft': return 'Draft pull request: mark it ready for review before it can be merged.';
    case 'unknown': return 'GitHub is still checking whether the branch can be merged.';
    default: return pr.mergeable == null ? 'GitHub is still checking whether the branch can be merged.' : '';
  }
};

/** Re-check CI every 20 s while it is pending, for at most 10 minutes. */
export const CI_POLL_MS = 20000;
export const CI_POLL_LIMIT_MS = 10 * 60 * 1000;

/**
 * Milliseconds until the next automatic CI refresh, or null to stop: only
 * while `state` is pending and within 10 minutes of `startedAt`; a rate
 * limit's Retry-After (seconds) stretches the wait.
 */
export const ciPollDelay = ({ state, startedAt, now = Date.now(), retryAfter = null } = {}) => {
  if (state !== 'pending' || !Number.isFinite(startedAt)) return null;
  const delay = Math.max(CI_POLL_MS, Number.isFinite(retryAfter) ? retryAfter * 1000 : 0);
  if (now + delay - startedAt > CI_POLL_LIMIT_MS) return null;
  return delay;
};

/** A failed pr-status request as `{ message, retryAfter, disconnect }`. */
export const ciError = (error) => {
  const status = error?.response?.status;
  const detail = error?.response?.data?.detail;
  const message = text(typeof detail === 'string' ? detail : '');
  const code = String(responseHeader(error, 'x-error-code') || '').toLowerCase();
  const base = { retryAfter: null, disconnect: false };
  if (status === 401) return { ...base, disconnect: true, message: 'Your GitHub connection expired. Connect GitHub again to see the checks.' };
  if (status === 403) return { ...base, message: message || 'GitHub denied access to the checks of this pull request.' };
  if (status === 404) {
    return { ...base, message: message || (code === 'github_pr_not_found' ? 'GitHub could not find this pull request.' : 'Repository not found.') };
  }
  if (status === 429) {
    const raw = String(responseHeader(error, 'retry-after') ?? '').trim();
    const seconds = /^\d+$/.test(raw) ? Number(raw) : null;
    return { ...base, retryAfter: seconds, message: `GitHub is rate limiting requests. Try again in ${seconds ?? 'a few'} seconds.` };
  }
  if (!error?.response) return { ...base, message: 'Could not reach the server to load the checks.' };
  return { ...base, message: message || 'Could not load the checks from GitHub.' };
};

/** Only https links are rendered for a CI check (any CI host). */
export const safeHttpsUrl = (value) => {
  try {
    const url = new URL(String(value || ''));
    return url.protocol === 'https:' ? url.href : '';
  } catch {
    return '';
  }
};

// ---------------------------------------------------------------- file names

const BOUNDARY = /[._\-/A-Z]/;

/**
 * Split a file name so the start can truncate with an ellipsis while the end
 * stays visible: "BookingAttendeesRemoveService.integration-test.ts" and
 * "BookingAttendeesRemoveService.ts" stay distinguishable in a narrow list.
 * The tail starts at the word boundary nearest the cut.
 */
export const truncationParts = (name, tail = 14) => {
  const text = String(name ?? '');
  if (text.length <= tail + 6) return { head: text, tail: '' };
  const cut = text.length - tail;
  let best = cut;
  for (let distance = 0; distance <= 6; distance += 1) {
    const after = cut + distance;
    const before = cut - distance;
    if (before > 4 && BOUNDARY.test(text[before])) { best = before; break; }
    if (after < text.length - 3 && BOUNDARY.test(text[after])) { best = after; break; }
  }
  return { head: text.slice(0, best), tail: text.slice(best) };
};

// ------------------------------------------------------------ persisted state

const browserStorage = () => {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
};

const LAYOUT_KEY = 'ci-diff-layout';

/** The reviewer's last diff layout ('unified' | 'split'). */
export const readDiffLayout = () => {
  try {
    return browserStorage()?.getItem(LAYOUT_KEY) === 'split' ? 'split' : 'unified';
  } catch {
    return 'unified';
  }
};

export const saveDiffLayout = (layout) => {
  try {
    browserStorage()?.setItem(LAYOUT_KEY, layout === 'split' ? 'split' : 'unified');
  } catch {
    // Storage can be unavailable; the layout still applies to this review.
  }
};

const PR_KEY = 'ci-pull-requests';
const PR_LIMIT = 50;
let pullRequests = null;
const prListeners = new Set();

const loadPullRequests = () => {
  if (pullRequests) return pullRequests;
  pullRequests = {};
  try {
    const parsed = JSON.parse(browserStorage()?.getItem(PR_KEY) || '{}');
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) pullRequests = parsed;
  } catch {
    // Corrupt or unavailable storage starts empty.
  }
  return pullRequests;
};

/** Only GitHub https links are rendered as links. */
export const safeGithubUrl = (value) => {
  try {
    const url = new URL(String(value || ''));
    return url.protocol === 'https:' && (url.hostname === 'github.com' || url.hostname.endsWith('.github.com')) ? url.href : '';
  } catch {
    return '';
  }
};

/** The pull request already opened from a proposal (stable reference), or null. */
export const pullRequestFor = (proposalId) => (proposalId ? loadPullRequests()[proposalId] || null : null);

/** Remember the pull request opened from a proposal so the chat card shows it. */
export const rememberPullRequest = (proposalId, result, now = Date.now()) => {
  const url = safeGithubUrl(result?.pr_url);
  if (!proposalId || !url) return;
  const entry = {
    pr_url: url,
    pr_number: result.pr_number ?? null,
    // The repository the proposal belongs to, for its CI status.
    repo_name: typeof result.repo_name === 'string' ? result.repo_name : '',
    branch_name: result.branch_name || '',
    target_repo: result.target_repo || '',
    is_fork: Boolean(result.is_fork),
    created_at: now,
  };
  const entries = Object.entries({ ...loadPullRequests(), [proposalId]: entry })
    .sort((left, right) => (right[1]?.created_at || 0) - (left[1]?.created_at || 0))
    .slice(0, PR_LIMIT);
  pullRequests = Object.fromEntries(entries);
  try {
    browserStorage()?.setItem(PR_KEY, JSON.stringify(pullRequests));
  } catch {
    // Kept in memory for this session.
  }
  prListeners.forEach((listener) => listener());
};

export const subscribePullRequests = (listener) => {
  prListeners.add(listener);
  return () => prListeners.delete(listener);
};

// A file the reviewer asked to open from the chat card; read once by the
// review when it mounts for that proposal.
const reviewFileRequests = new Map();

export const requestReviewFile = (proposalId, path) => {
  if (proposalId && path) reviewFileRequests.set(proposalId, path);
};

export const peekReviewFile = (proposalId) => (proposalId ? reviewFileRequests.get(proposalId) || '' : '');

export const clearReviewFile = (proposalId) => {
  reviewFileRequests.delete(proposalId);
};
