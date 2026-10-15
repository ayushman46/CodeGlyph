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

/** Pull request description: the server's body plus a testing checklist. */
export const buildPrBody = (proposal) => {
  const body = String(proposal?.pr_body || '').trim() || `## Summary\n\n${proposal?.summary || 'Reviewed change.'}`;
  const validation = (proposal?.validation || []).filter(Boolean);
  const checklist = validation.length && !/##\s*testing/i.test(body)
    ? `\n\n## Testing\n${validation.map((step) => `- [ ] ${step}`).join('\n')}`
    : '';
  return `${body}${checklist}\n\n---\n_Generated with Codebase Intel and reviewed before pushing._`.slice(0, 5000);
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
