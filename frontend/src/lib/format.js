const trimOne = (amount) => (amount >= 100 ? String(Math.round(amount)) : amount.toFixed(1).replace(/\.0$/, ''));
// The value as it will be displayed (whole numbers from 100, one decimal
// below), so a unit is chosen after rounding: 999,950 B is "1 MB", never "1000 KB".
const displayed = (amount) => (amount >= 100 ? Math.round(amount) : Math.round(amount * 10) / 10);

export const formatBytes = (value) => {
  const bytes = Math.round(Math.max(0, Number(value) || 0));
  if (bytes < 1000) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = bytes / 1000;
  let index = 0;
  while (displayed(amount) >= 1000 && index < units.length - 1) {
    amount /= 1000;
    index += 1;
  }
  return `${trimOne(amount)} ${units[index]}`;
};

export const formatCount = (value) => {
  const number = Math.round(Math.max(0, Number(value) || 0));
  if (number < 10000) return number.toLocaleString();
  const thousands = number / 1000;
  const shownThousands = thousands < 100 ? Math.round(thousands * 10) / 10 : Math.round(thousands);
  if (shownThousands < 1000) return `${thousands < 100 ? thousands.toFixed(1).replace(/\.0$/, '') : Math.round(thousands)}k`;
  const millions = number / 1_000_000;
  return `${(Math.round(millions * 10) / 10).toFixed(1).replace(/\.0$/, '')}M`;
};

export const pluralize = (count, singular, plural = `${singular}s`) => `${formatCount(count)} ${Number(count) === 1 ? singular : plural}`;

export const relativeTime = (iso) => {
  if (!iso) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '';
  // Positive for the past, negative for the future ("in 2 days").
  const seconds = Math.round((Date.now() - time) / 1000);
  if (Math.abs(seconds) < 45) return 'just now';
  const units = [['minute', 60], ['hour', 3600], ['day', 86400], ['week', 604800], ['month', 2592000], ['year', 31536000]];
  let chosen = units[0];
  for (const unit of units) {
    if (Math.abs(seconds) >= unit[1]) chosen = unit;
  }
  const value = Math.round(seconds / chosen[1]);
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(-value, chosen[0]);
};

export const shortSha = (sha) => (sha ? String(sha).slice(0, 7) : '');

export const formatDuration = (ms) => {
  const value = Math.round(Math.max(0, Number(ms) || 0));
  if (value < 1000) return `${value} ms`;
  const seconds = value / 1000;
  if (Math.round(seconds * 10) / 10 < 10) return `${seconds.toFixed(1)} s`;
  const whole = Math.round(seconds);
  if (whole < 60) return `${whole} s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return whole % 60 ? `${minutes} min ${whole % 60} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
};

/** "12 of 3,400 shown" when a list is a sample of a larger total, else "12 shown". */
export const shownLabel = (shown, total) => {
  const visible = Math.max(0, Number(shown) || 0);
  const all = Number(total);
  return Number.isFinite(all) && all > visible ? `${formatCount(visible)} of ${formatCount(all)} shown` : `${formatCount(visible)} shown`;
};

// ---------------------------------------------------------------------------
// Repository input. Mirrors the server's URL policy (backend/ingest/cloner.py)
// so typing mistakes are explained instantly; the server stays authoritative
// and every message matches the one it would return.

const GITHUB_HOSTS = new Set(['github.com', 'www.github.com']);
const OWNER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
const REPOSITORY_PATTERN = /^[A-Za-z0-9._-]{1,100}$/;
const ROUTE_SEGMENTS = new Set([
  'tree', 'blob', 'commits', 'commit', 'issues', 'pulls', 'pull', 'releases', 'actions', 'wiki', 'tags', 'branches',
  'compare', 'discussions', 'security', 'pulse', 'graphs', 'network', 'projects', 'milestones', 'labels',
]);
const RESERVED_OWNERS = new Set([
  'settings', 'orgs', 'topics', 'marketplace', 'sponsors', 'login', 'logout', 'features', 'about', 'explore',
  'notifications', 'new', 'organizations', 'enterprise', 'pricing', 'search', 'collections', 'trending',
]);
const REF_ROUTES = new Set(['tree', 'blob', 'commit', 'commits']);

export const REPOSITORY_INPUT_MESSAGES = {
  shape: 'Provide a GitHub repository URL such as https://github.com/owner/repository or owner/repository.',
  characters: 'GitHub URL must not contain spaces, control characters, or backslashes.',
  scheme: 'Only https:// (or http://) GitHub repository URLs are supported.',
  host: 'Only public repositories hosted on github.com are supported.',
  credentials: 'GitHub URL must not include a username, password, or token.',
  port: 'GitHub URL must not include a port number.',
  owner: 'GitHub owner name is not valid.',
  reserved: 'The URL points to a GitHub page, not to a repository owner.',
  repository: 'GitHub repository name is not valid.',
  route: 'GitHub URL must point to a repository, not to another page inside it.',
};

const fail = (reason) => ({ ok: false, reason, error: REPOSITORY_INPUT_MESSAGES[reason] });
const stripQuery = (value) => value.split(/[?#]/, 1)[0];

const urlPath = (candidate) => {
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    return fail('shape');
  }
  if (!['https:', 'http:'].includes(parsed.protocol)) return fail('scheme');
  const authority = candidate.slice(candidate.indexOf('//') + 2).split(/[/?#]/, 1)[0];
  if (authority.includes('@')) return fail('credentials');
  if (!GITHUB_HOSTS.has(parsed.hostname.toLowerCase())) return fail('host');
  if (parsed.port || /:\d*$/.test(authority)) return fail('port');
  return { path: decodeURI(parsed.pathname) };
};

/**
 * Parse what a user typed into the "add repository" field.
 * Returns { ok: true, owner, repo, fullName, ref } (ref: { kind, label } for
 * links to a branch, tag or commit) or { ok: false, reason, error }; an empty
 * value is { ok: false, reason: 'empty', error: '' }.
 */
export const parseRepositoryInput = (value) => {
  const candidate = String(value ?? '').trim();
  if (!candidate) return { ok: false, reason: 'empty', error: '' };
  if (candidate.length > 2048) return fail('shape');
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f\\]/.test(candidate)) return fail('characters');
  let path;
  if (candidate.startsWith('git@')) {
    const rest = candidate.slice(4);
    const separator = rest.indexOf(':');
    if (separator === -1) return fail('shape');
    if (!GITHUB_HOSTS.has(rest.slice(0, separator).toLowerCase())) return fail('host');
    path = stripQuery(rest.slice(separator + 1));
  } else if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(candidate)) {
    const result = urlPath(candidate);
    if (result.ok === false) return result;
    ({ path } = result);
  } else {
    const withoutQuery = stripQuery(candidate);
    const first = withoutQuery.split('/', 1)[0];
    if (/[.:@]/.test(first)) {
      const result = urlPath(`https://${candidate}`);
      if (result.ok === false) return result;
      ({ path } = result);
    } else {
      path = withoutQuery;
    }
  }
  const segments = path.replace(/^\/+|\/+$/g, '').split('/');
  if (segments.length < 2 || !segments[0] || !segments[1]) return fail('shape');
  const owner = segments[0];
  let repo = segments[1];
  if (repo.toLowerCase().endsWith('.git')) repo = repo.slice(0, -4);
  if (!OWNER_PATTERN.test(owner)) return fail('owner');
  if (RESERVED_OWNERS.has(owner.toLowerCase())) return fail('reserved');
  if (repo === '.' || repo === '..' || !REPOSITORY_PATTERN.test(repo)) return fail('repository');
  if (segments.length > 2 && !ROUTE_SEGMENTS.has(segments[2])) return fail('route');
  let ref = null;
  const rest = segments.slice(3).filter(Boolean);
  if (REF_ROUTES.has(segments[2]) && rest.length) {
    ref = segments[2] === 'commit'
      ? { kind: 'commit', label: rest[0].slice(0, 7) }
      : { kind: 'ref', label: rest.join('/') };
  }
  return { ok: true, owner, repo, fullName: `${owner}/${repo}`, ref };
};
