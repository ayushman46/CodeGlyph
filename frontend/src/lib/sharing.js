/*
 * Shared repositories: roles, what each role may do, and the words the UI
 * uses for them. The server enforces every rule here (api/repository_state);
 * the UI only explains it and hides controls that would be refused.
 */

export const ROLE_LABELS = { owner: 'Owner', editor: 'Editor', viewer: 'Viewer' };

export const ROLE_DESCRIPTIONS = {
  viewer: 'Can ask questions, search the code and browse files.',
  editor: 'Can also use Edit & PR to propose changes and open pull requests with their own GitHub account.',
};

/** The selectable roles for an invite or a member, least access first. */
export const MEMBER_ROLES = ['viewer', 'editor'];

/** How long a new invite link works, in days. */
export const EXPIRY_OPTIONS = [
  { days: 1, label: '1 day' },
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
];

/** The caller's role for a repository from the list API (older servers: owner). */
export const roleOf = (repo) => (repo?.access_role && ROLE_LABELS[repo.access_role] ? repo.access_role : 'owner');

export const isShared = (repo) => roleOf(repo) !== 'owner';

export const canEdit = (repo) => roleOf(repo) !== 'viewer';

/** Rename, re-index, stop indexing, delete and share are the owner's alone. */
export const canManage = (repo) => roleOf(repo) === 'owner';

/** Who shared the repository, for a member; '' for the owner. */
export const ownerName = (repo) => (isShared(repo) ? String(repo?.owner_label || 'its owner') : '');

/** Why Edit & PR is unavailable (a viewer), or '' when it is available. */
export const editBlockedReason = (repo) => (canEdit(repo)
  ? ''
  : `You have view access to this shared repository, so Edit & PR is off. Ask ${ownerName(repo)} for editor access to propose changes.`);

/** Repositories you own (in list order) and the ones shared with you. */
export const splitRepos = (repos = []) => {
  const owned = [];
  const shared = [];
  repos.forEach((repo) => (isShared(repo) ? shared : owned).push(repo));
  return { owned, shared };
};

/** One line describing a member's access, e.g. "Viewer · shared by ann@example.com". */
export const sharedLine = (repo) => `${ROLE_LABELS[roleOf(repo)]} · shared by ${ownerName(repo)}`;

/** "Shared with 2 people" for an owner's repository, '' when it isn't shared. */
export const membersLine = (repo) => {
  const count = Math.max(0, Number(repo?.member_count) || 0);
  if (!count) return '';
  return `Shared with ${count} ${count === 1 ? 'person' : 'people'}`;
};

/** The full invite URL for a server path such as "/invite/<token>". */
export const inviteUrl = (path, origin = '') => `${String(origin).replace(/\/$/, '')}${path?.startsWith('/') ? path : `/${path || ''}`}`;

const DAY = 86_400_000;
const HOUR = 3_600_000;

/** "Expires in 7 days", "Expires in 5 hours", "Expires in under an hour" or "Expired". */
export const expiryLabel = (expiresAt, now = Date.now()) => {
  const time = new Date(expiresAt).getTime();
  if (Number.isNaN(time)) return '';
  const left = time - now;
  if (left <= 0) return 'Expired';
  if (left < HOUR) return 'Expires in under an hour';
  if (left < DAY) {
    const hours = Math.round(left / HOUR);
    return `Expires in ${hours} hour${hours === 1 ? '' : 's'}`;
  }
  const days = Math.round(left / DAY);
  return `Expires in ${days} day${days === 1 ? '' : 's'}`;
};

/** "9 Nov 2026" in the reader's locale. */
export const formatDate = (iso, locale = undefined) => {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return '';
  return time.toLocaleDateString(locale, { day: 'numeric', month: 'short', year: 'numeric' });
};

/** What an invite link lets someone do, in one sentence. */
export const inviteSentence = (role, expiresAt, locale = undefined) => {
  const date = formatDate(expiresAt, locale);
  const who = role === 'editor' ? 'an editor' : 'a viewer';
  return `Anyone signed in with this link can join as ${who}${date ? ` until ${date}` : ''}.`;
};

/**
 * The title and next step for an invite link the server refused, from the
 * response status and its X-Error-Code header.
 */
export const inviteProblem = (status, code, detail = '') => {
  if (code === 'invite_expired') return { title: 'This invite link has expired', detail: detail || 'Ask the person who shared it for a new link.' };
  if (code === 'invite_revoked') return { title: 'This invite link was revoked', detail: detail || 'Ask the person who shared it for a new link.' };
  if (code === 'share_removed') return { title: 'Your access was removed', detail: detail || 'Ask the owner for a new invite link if you still need this repository.' };
  if (status === 404 || code === 'invite_invalid') {
    return { title: 'This invite link isn’t valid', detail: detail || 'Check that you copied the whole link, or ask for a new one.' };
  }
  return { title: 'This invite couldn’t be opened', detail: detail || 'Check your connection and try again.' };
};
