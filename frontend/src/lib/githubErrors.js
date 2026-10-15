/**
 * Classify a failed GitHub call (push-pr, github/file) into what the review
 * UI should do. Pure, so it is unit-tested without axios.
 *
 * kinds:
 *  - reconnect     401: the GitHub token expired or was revoked.
 *  - permission    403 + X-Error-Code: github_permission: no access, or the
 *                  org has not approved the OAuth app. Never retried with a
 *                  fresh ticket and never disconnects GitHub.
 *  - ticket        any other 403: the edit ticket expired; retry once fresh.
 *  - not_found     404: repository missing or not visible.
 *  - branch_exists 409 "already exists": choose a new branch name.
 *  - stale         other 409: the file changed upstream; refresh.
 *  - too_large     413: show the server's detail; offer the .patch instead.
 *  - rate_limited  429: wait Retry-After, same idempotency key.
 *  - unavailable   503: e.g. fork not ready; retry in a minute, same key.
 *  - failed        anything else (400, network): retry with the same key.
 */

const detailOf = (error) => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return '';
};

const header = (error, name) => {
  const headers = error?.response?.headers;
  if (!headers) return undefined;
  if (typeof headers.get === 'function') return headers.get(name) ?? undefined;
  return headers[name] ?? headers[name.toLowerCase()];
};

/** Seconds from a Retry-After header (delta-seconds or an HTTP date). */
export const retryAfterSeconds = (value, now = Date.now()) => {
  if (value == null || value === '') return null;
  const text = String(value).trim();
  if (/^\d+$/.test(text)) return Number(text);
  const date = Date.parse(text);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - now) / 1000));
};

const waitText = (seconds) => {
  if (seconds == null) return 'a moment';
  if (seconds < 60) return `${seconds} second${seconds === 1 ? '' : 's'}`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} minute${minutes === 1 ? '' : 's'}`;
};

const withPeriod = (text) => (text && !/[.!?]$/.test(text) ? `${text}.` : text);

export const classifyGithubError = (error, { now = Date.now() } = {}) => {
  const status = error?.response?.status;
  const detail = detailOf(error);
  const base = { status, keepKey: true, retryWithFreshTicket: false, disconnect: false, retryAfter: null, action: null };
  if (status === 401) {
    return {
      ...base, kind: 'reconnect', disconnect: true, action: 'connect',
      message: 'Your GitHub connection expired or was revoked. Connect GitHub again to push.',
    };
  }
  if (status === 403) {
    // Cross-origin responses only expose these headers when the API lists
    // them in Access-Control-Expose-Headers; body fields are a fallback.
    const data = error?.response?.data || {};
    const code = String(header(error, 'x-error-code') || data.error_code || data.code || '').toLowerCase();
    if (code === 'github_permission') {
      return {
        ...base, kind: 'permission',
        message: `${withPeriod(detail) || 'GitHub refused this action.'} You need write access to the repository (or permission to fork it). If it belongs to an organization, an owner may need to approve the Codebase Intel OAuth app for that organization in its GitHub settings.`,
      };
    }
    return { ...base, kind: 'ticket', retryWithFreshTicket: true, message: detail || 'The review session expired. Reopen the review and try again.' };
  }
  if (status === 404) {
    return { ...base, kind: 'not_found', message: detail || 'The repository was not found or is not visible to your GitHub account.' };
  }
  if (status === 409) {
    if (/already exists/i.test(detail)) {
      return {
        ...base, kind: 'branch_exists', keepKey: false, action: 'new_branch',
        message: `${withPeriod(detail) || 'That branch already exists.'} Choose a new branch name and create the pull request again.`,
      };
    }
    return {
      ...base, kind: 'stale', keepKey: false, action: 'refresh',
      message: `${withPeriod(detail) || 'The file changed on GitHub.'} Use "Refresh from GitHub" to re-apply the change to the latest files.`,
    };
  }
  if (status === 413) {
    // Nothing to retry; the patch can still be applied locally.
    return { ...base, kind: 'too_large', action: 'download', message: detail || 'This change is too large to push in one pull request.' };
  }
  if (status === 429) {
    const seconds = retryAfterSeconds(header(error, 'retry-after') ?? error?.response?.data?.retry_after, now);
    return {
      ...base, kind: 'rate_limited', retryAfter: seconds,
      message: `GitHub is rate limiting requests. Wait ${waitText(seconds)}, then try again — nothing is duplicated.`,
    };
  }
  if (status === 503) {
    return {
      ...base, kind: 'unavailable',
      message: `${withPeriod(detail) || 'GitHub is not ready yet.'} Try again in a minute; the pull request resumes where it stopped.`,
    };
  }
  return { ...base, kind: 'failed', message: detail || 'GitHub could not create the pull request.' };
};
