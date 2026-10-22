import axios from 'axios';
import { supabase } from './supabase';

export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? 'http://localhost:8000/api' : '/api')
).replace(/\/$/, '');

const api = axios.create({
  baseURL: API_BASE,
  // Bound stalled requests so the UI can recover. Code edits stream through
  // fetch instead, so this ceiling only applies to ordinary JSON calls.
  timeout: 120000,
});

export const authHeaders = async () => {
  const { data: { session } } = await supabase.auth.getSession();
  return session?.access_token ? { Authorization: `Bearer ${session.access_token}` } : {};
};

api.interceptors.request.use(async (config) => {
  Object.assign(config.headers, await authHeaders());
  return config;
});

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export class StreamUnavailableError extends Error {
  constructor(message = 'Streaming is unavailable.') {
    super(message);
    this.name = 'StreamUnavailableError';
  }
}

/** Extract a safe, human-readable message from any API failure. */
export const errorMessage = (error, fallback = 'Something went wrong. Please try again.') => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  if (error instanceof ApiError && error.message) return error.message;
  if (error?.code === 'ECONNABORTED') return 'The server took too long to respond. Please try again.';
  if (error?.message === 'Network Error') return 'Could not reach the server. It may be waking up — try again in a few seconds.';
  return fallback;
};

const readDetail = async (response) => {
  try {
    const data = await response.json();
    if (typeof data?.detail === 'string') return data.detail;
  } catch {
    // Non-JSON error bodies fall through to the generic message.
  }
  return `Request failed (HTTP ${response.status}).`;
};

const STREAM_IDLE_TIMEOUT_MS = 60000;

/**
 * POST /query/stream and dispatch server-sent events as they arrive.
 * Throws StreamUnavailableError when the transport cannot stream, so the
 * caller can fall back to the JSON endpoint before any event was delivered.
 */
export const streamQuery = async (payload, { signal, onEvent }) => {
  let response;
  try {
    response = await fetch(`${API_BASE}/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...(await authHeaders()) },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new StreamUnavailableError(error?.message);
  }
  if (!response.ok) throw new ApiError(await readDetail(response), response.status);
  if (!response.body?.getReader) throw new StreamUnavailableError();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let delivered = 0;
  // The server writes a keep-alive comment every 15 s, so a minute of
  // silence means the connection is gone even though it never closed.
  let stalled = false;
  let idleTimer = null;
  const armIdleTimer = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      stalled = true;
      reader.cancel().catch(() => {});
    }, STREAM_IDLE_TIMEOUT_MS);
  };
  const dispatch = (frame) => {
    let event = 'message';
    const data = [];
    for (const line of frame.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const separator = line.indexOf(':');
      const field = separator === -1 ? line : line.slice(0, separator);
      const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (!data.length) return;
    let parsed;
    try {
      parsed = JSON.parse(data.join('\n'));
    } catch {
      return;
    }
    delivered += 1;
    onEvent(event, parsed);
  };
  try {
    armIdleTimer();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      armIdleTimer();
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf('\n\n');
      }
    }
    if (stalled) throw new ApiError('The server stopped responding while the answer was being written. Check your connection and try again.', 504);
    if (buffer.trim()) dispatch(buffer);
  } catch (error) {
    // Server-sent "error" events surface as ApiError from onEvent; keep
    // their real message instead of reporting a transport failure.
    if (error?.name === 'AbortError' || error instanceof ApiError) throw error;
    if (!delivered) throw new StreamUnavailableError(error?.message);
    throw new ApiError('The connection was interrupted while the answer was streaming. Check your connection and try again.');
  } finally {
    if (idleTimer) clearTimeout(idleTimer);
  }
  return delivered;
};

export const health = () => api.get('/health', { timeout: 70000 });
export const ingestRepo = (github_url) => api.post('/ingest', { github_url });
export const queryRepo = (repo_name, question, model_profile = 'fast', workflow = 'general', config = {}) => api.post(
  '/query', { repo_name, question, model_profile, workflow }, { timeout: 300000, ...config },
);
export const getConversation = (repo_name, config = {}) => api.get(`/conversations/${encodeURIComponent(repo_name)}`, config);
export const clearConversation = (repo_name) => api.delete(`/conversations/${encodeURIComponent(repo_name)}`);
export const getRepos = () => api.get('/repos');
export const renameRepository = (repo_name, new_repo_name) => api.patch(`/repos/${encodeURIComponent(repo_name)}`, { repo_name: new_repo_name });
export const deleteRepository = (repo_name) => api.delete(`/repos/${encodeURIComponent(repo_name)}`);
export const getStatus = (repo_name) => api.get(`/status/${encodeURIComponent(repo_name)}`);
export const getStatuses = (config = {}) => api.get('/repos/statuses', config);
export const cancelIndexing = (repo_name) => api.post(`/repos/${encodeURIComponent(repo_name)}/cancel-indexing`);
export const reindexRepository = (repo_name) => api.post(`/repos/${encodeURIComponent(repo_name)}/reindex`);
export const getRepositoryImpact = (repo_name, file_path, config = {}) => api.get(
  `/repos/${encodeURIComponent(repo_name)}/impact`,
  { params: { file_path, limit: 30 }, ...config },
);
export const getRepositoryCoverage = (repo_name) => api.get(`/repos/${encodeURIComponent(repo_name)}/coverage`);
export const getRepositoryOverview = (repo_name, config = {}) => api.get(`/repos/${encodeURIComponent(repo_name)}/overview`, config);
export const searchRepository = (repo_name, q, config = {}) => api.get(
  `/repos/${encodeURIComponent(repo_name)}/search`, { params: { q, limit: 25 }, ...config },
);
export const getRepositoryFile = (repo_name, path, config = {}) => api.get(
  `/repos/${encodeURIComponent(repo_name)}/file`, { params: { path }, ...config },
);
export const getEditProposal = (proposalId) => api.get(`/edits/${encodeURIComponent(proposalId)}`);
export const getAccountUsage = () => api.get('/account/usage');
export const createTeamOrder = () => api.post('/create-order', { plan: 'team' });
export const verifyTeamPayment = (payment) => api.post('/verify-payment', payment);
export const getGithubStatus = () => api.get('/github/status');
export const getGithubLoginUrl = (redirectTo = '/app') => api.get('/github/login', { params: { redirect_to: redirectTo } });
export const disconnectGithub = () => api.post('/github/disconnect');
/**
 * Finish GitHub OAuth for the signed-in user. The API's /github/callback
 * redirects the popup to /github/callback#code=…&state=… on this origin;
 * the server links the token only when this bearer's user started the flow.
 */
export const completeGithubConnection = (code, state) => api.post('/github/complete', { code, state });
export const getGithubFile = (repoName, filePath, editTicket = '', config = {}) => {
  const { headers = {}, ...rest } = config;
  return api.get('/github/file', {
    ...rest,
    params: { repo_name: repoName, file_path: filePath },
    headers: { ...headers, 'X-Editing-Ticket': editTicket },
  });
};
export const pushGithubPR = (payload) => api.post('/github/push-pr', payload, { timeout: 180000 });
/** CI checks and merge readiness of a pull request opened from a proposal. */
export const getPullRequestStatus = (repoName, prNumber, config = {}) => api.get(
  '/github/pr-status', { params: { repo_name: repoName, pr_number: prNumber }, ...config },
);

export default api;
