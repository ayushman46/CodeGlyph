import { create } from 'zustand';
import {
  ApiError, StreamUnavailableError, cancelIndexing, clearConversation, continueAnswerRequest, deleteRepository,
  errorMessage, getAccountUsage, getConversation, getEditProposal, getGithubStatus, getRepos, getRepositoryFile,
  getRepositoryImpact, getRepositoryOverview, getStatuses, ingestRepo, queryRepo, reindexRepository, renameRepository,
  streamQuery,
} from '../api/client';
import { isSupabaseConfigured, supabase } from '../api/supabase';

let historyController = null;
let queryController = null;
let statusPollInFlight = false;
let reposRevision = 0;
let usageRevision = 0;
const mutationRevisions = new Map();
const TICKET_MAX_AGE_MS = 9 * 60 * 1000;
// Proposal hydrations in flight, by proposal id (one request per proposal).
const proposalLoads = new Map();
export const POST_LOGIN_KEY = 'ci-post-login';
const FILE_CACHE_LIMIT = 40;

const uid = () => (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`);

export const isIndexing = (repo) => Boolean(repo?.is_indexing);

/** Normalize a stored chat row or a live response into one message shape. */
const normalizeMessage = (message) => {
  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
  const proposalCall = toolCalls.find((call) => call?.tool === 'code_edit' && call?.input?.proposal_id);
  return {
    id: message.id || uid(),
    role: message.role,
    content: message.content || '',
    citations: Array.isArray(message.citations) ? message.citations : [],
    // Server-verified inline references ({text, file_path, start_line,
    // end_line, valid}); undefined for answers from older servers, which
    // keeps the client-side heuristics in charge of citation chips.
    answer_references: Array.isArray(message.answer_references) ? message.answer_references : undefined,
    // Answer trust: backticked code names not found in the index ({text,
    // names}), why no model answered, which model answered instead, and the
    // stored id a cut-off answer (mode "truncated") is continued by.
    unverified_identifiers: Array.isArray(message.unverified_identifiers) ? message.unverified_identifiers : undefined,
    answer_failure: message.answer_failure || null,
    answer_notice: message.answer_notice || null,
    message_id: message.message_id !== undefined ? message.message_id : message.id || null,
    tool_calls: toolCalls.filter((call) => call?.tool !== 'code_edit'),
    mode: message.mode,
    latency_ms: message.latency_ms,
    created_at: message.created_at,
    // Stored replies carry no workflow: an Edit & PR run reads as one by its
    // mode or its proposal (so Try again asks in the same workflow).
    workflow: message.workflow
      || (message.role === 'assistant' && (message.mode === 'editing' || message.edit_suggestion || proposalCall) ? 'editing' : undefined),
    model_profile: message.model_profile,
    evidence_plan: message.evidence_plan,
    snapshot: message.snapshot,
    edit_suggestion: message.edit_suggestion || null,
    edit_ticket: message.edit_ticket || null,
    ticket_issued_at: message.edit_ticket ? Date.now() : null,
    proposal_id: message.edit_suggestion?.id || proposalCall?.input?.proposal_id || null,
    proposal_title: proposalCall?.result_summary || message.edit_suggestion?.title || null,
    streaming: false,
    stage: null,
  };
};

/** Messages per history page: enough to fill a tall screen, small enough to load fast. */
const HISTORY_PAGE_SIZE = 30;
// The first page only has to fill the screen (answers are long): rendering 30
// cited answers before anything shows was most of a workspace's open time.
// Older pages load as the reader scrolls up.
const FIRST_HISTORY_PAGE_SIZE = 12;
// A repository link opened directly starts its first history page with the
// app (see prefetchRepo); selectRepo takes it over if it is recent.
const HISTORY_PREFETCH_TTL_MS = 15_000;
let historyPrefetch = null;
// An older page is added a few messages per frame, nearest the reader first:
// one render of 30 long cited answers blocked scrolling for a visible beat.
const OLDER_BATCH_SIZE = 6;
// Resolves just after the next paint (a frame callback, then a task), so each
// batch gets a frame of its own instead of two landing before one paint.
const afterNextPaint = () => new Promise((resolve) => {
  if (typeof window !== 'undefined' && window.requestAnimationFrame) window.requestAnimationFrame(() => setTimeout(resolve, 0));
  else setTimeout(resolve, 0);
});

const useStore = create((set, get) => ({
  user: null,
  repos: [],
  reposLoaded: false,
  reposError: null,
  selectedRepo: null,
  messages: [],
  isHistoryLoading: false,
  historyError: '',
  // History is loaded newest-first in pages; older pages load on demand.
  historyHasMore: false,
  historyCursor: null,
  isLoadingOlder: false,
  isQuerying: false,
  queryEpoch: 0,
  isSigningIn: false,
  authError: null,
  accountUsage: null,
  accountUsageLoading: false,
  accountUsageError: '',
  impactAnalysis: null,
  impactAnalysisLoading: false,
  impactAnalysisError: '',
  sources: { open: false, messageId: null, activeIndex: null, file: null, stack: [] },
  // Text another part of the chat (a follow-up suggestion, "edit question")
  // asks the composer to load; `nonce` makes repeated drafts distinct.
  composerDraft: null,
  overviews: {},
  files: {},
  review: null,
  github: { connected: false, username: '', checked: false },
  serverWaking: false,

  setServerWaking: (serverWaking) => set({ serverWaking }),

  setUser: (user) => set((state) => {
    if (state.user?.id === user?.id) return { user };
    return {
      user,
      repos: [],
      reposLoaded: false,
      selectedRepo: null,
      messages: [],
      accountUsage: null,
      overviews: {},
      files: {},
      review: null,
      github: { connected: false, username: '', checked: false },
    };
  }),

  clearAuthError: () => set({ authError: null }),

  signInWithGoogle: async () => {
    if (!isSupabaseConfigured) {
      set({ authError: 'Google sign-in is unavailable because the Supabase environment variables are missing.' });
      return false;
    }
    set({ isSigningIn: true, authError: null });
    try {
      // Supabase redirect allow-lists usually contain the bare origin, so
      // return there and let the app forward the user to the workspace.
      try {
        window.sessionStorage.setItem(POST_LOGIN_KEY, window.location.pathname === '/' ? '/app' : window.location.pathname);
      } catch {
        // Storage can be unavailable; the user simply lands on the home page.
      }
      const { error } = await supabase.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin },
      });
      if (error) throw error;
      return true;
    } catch (error) {
      set({ authError: error.message || 'Google sign-in could not be started. Please try again.' });
      return false;
    } finally {
      set({ isSigningIn: false });
    }
  },

  signOut: async () => {
    historyController?.abort();
    queryController?.abort();
    try {
      await supabase.auth.signOut();
    } finally {
      usageRevision += 1;
      set((state) => ({
        user: null, selectedRepo: null, messages: [], repos: [], reposLoaded: false, accountUsage: null,
        overviews: {}, files: {}, review: null, isQuerying: false, queryEpoch: state.queryEpoch + 1,
        sources: { open: false, messageId: null, activeIndex: null, file: null, stack: [] },
      }));
    }
  },

  // ---------------------------------------------------------------- repos

  fetchRepos: async () => {
    const revision = ++reposRevision;
    const userId = get().user?.id;
    try {
      const response = await getRepos();
      if (revision !== reposRevision || get().user?.id !== userId) return;
      set({ repos: response.data || [], reposLoaded: true, reposError: null });
      void get().fetchAccountUsage({ silent: true });
    } catch (error) {
      if (revision === reposRevision && get().user?.id === userId) {
        set({ reposLoaded: true, reposError: errorMessage(error, 'Could not load your repositories. Check your connection and try again.') });
      }
    }
  },

  pollStatuses: async () => {
    if (statusPollInFlight) return;
    const userId = get().user?.id;
    if (!userId || !get().repos.some(isIndexing)) return;
    const revisions = new Map(get().repos.map((repo) => [repo.id, mutationRevisions.get(repo.id) || 0]));
    statusPollInFlight = true;
    try {
      const response = await getStatuses();
      if (get().user?.id !== userId) return;
      const byId = new Map((response.data || []).map((repo) => [repo.id, repo]));
      const finished = [];
      set((state) => ({
        repos: state.repos.map((repo) => {
          if (revisions.get(repo.id) !== (mutationRevisions.get(repo.id) || 0)) return repo;
          const next = byId.get(repo.id);
          if (!next) return repo;
          if (repo.is_indexing && !next.is_indexing) finished.push(next.repo_name);
          return next;
        }),
      }));
      if (finished.length) {
        void get().fetchAccountUsage({ silent: true });
        set((state) => {
          const overviews = { ...state.overviews };
          finished.forEach((name) => { delete overviews[name]; });
          return { overviews };
        });
      }
    } catch (error) {
      if (error?.code !== 'ERR_CANCELED') console.warn('Status poll failed', error);
    } finally {
      statusPollInFlight = false;
    }
  },

  ingest: async (url) => {
    const response = await ingestRepo(url);
    await get().fetchRepos();
    return response.data?.repo_name;
  },

  reindexRepo: async (repo) => {
    mutationRevisions.set(repo.id, (mutationRevisions.get(repo.id) || 0) + 1);
    await reindexRepository(repo.repo_name);
    set((state) => ({
      repos: state.repos.map((item) => (item.id === repo.id
        ? { ...item, status: 'queued', is_indexing: true, refreshing: item.queryable, error_message: null, message: null }
        : item)),
    }));
  },

  cancelRepoIndexing: async (repo) => {
    mutationRevisions.set(repo.id, (mutationRevisions.get(repo.id) || 0) + 1);
    const response = await cancelIndexing(repo.repo_name);
    if (response.data?.state === 'stopping') {
      set((state) => ({
        repos: state.repos.map((item) => (item.id === repo.id ? { ...item, message: 'Stopping indexing…' } : item)),
      }));
    }
    // Let the next poll publish the durable state (stopped or restored).
    mutationRevisions.set(repo.id, (mutationRevisions.get(repo.id) || 0) + 1);
    await get().fetchRepos();
  },

  renameRepo: async (repo, nextName) => {
    const response = await renameRepository(repo.repo_name, nextName);
    const repoName = response.data.repo_name;
    set((state) => ({
      repos: state.repos.map((item) => (item.id === repo.id ? { ...item, repo_name: repoName } : item)),
      selectedRepo: state.selectedRepo === repo.repo_name ? repoName : state.selectedRepo,
    }));
    return repoName;
  },

  deleteRepo: async (repo) => {
    await deleteRepository(repo.repo_name);
    set((state) => ({
      repos: state.repos.filter((item) => item.id !== repo.id),
      ...(state.selectedRepo === repo.repo_name ? { selectedRepo: null, messages: [] } : {}),
    }));
    void get().fetchAccountUsage({ silent: true });
  },

  analyzeImpact: async (repo, filePath) => {
    set({ impactAnalysis: null, impactAnalysisLoading: true, impactAnalysisError: '' });
    try {
      const response = await getRepositoryImpact(repo.repo_name, filePath);
      set({ impactAnalysis: response.data, impactAnalysisLoading: false });
      return response.data;
    } catch (error) {
      set({ impactAnalysisLoading: false, impactAnalysisError: errorMessage(error, 'Impact analysis failed. Check the file path and try again.') });
      return null;
    }
  },

  clearImpactAnalysis: () => set({ impactAnalysis: null, impactAnalysisLoading: false, impactAnalysisError: '' }),

  loadOverview: async (repoName, { force = false } = {}) => {
    const existing = get().overviews[repoName];
    if (!force && (existing?.data || existing?.loading)) return;
    set((state) => ({ overviews: { ...state.overviews, [repoName]: { ...(state.overviews[repoName] || {}), loading: true, error: '' } } }));
    try {
      const response = await getRepositoryOverview(repoName);
      set((state) => ({ overviews: { ...state.overviews, [repoName]: { data: response.data, loading: false, error: '' } } }));
    } catch (error) {
      set((state) => ({ overviews: { ...state.overviews, [repoName]: { data: existing?.data || null, loading: false, error: errorMessage(error, 'Overview unavailable.') } } }));
    }
  },

  loadFile: async (repoName, path) => {
    const key = `${repoName}::${path}`;
    const existing = get().files[key];
    if (existing?.data || existing?.loading) return existing;
    set((state) => ({ files: { ...state.files, [key]: { loading: true, error: '', data: null, at: Date.now() } } }));
    try {
      const response = await getRepositoryFile(repoName, path);
      set((state) => {
        const files = { ...state.files, [key]: { loading: false, error: '', data: response.data, at: Date.now() } };
        const keys = Object.keys(files);
        if (keys.length > FILE_CACHE_LIMIT) {
          keys.sort((a, b) => (files[a].at || 0) - (files[b].at || 0)).slice(0, keys.length - FILE_CACHE_LIMIT).forEach((stale) => delete files[stale]);
        }
        return { files };
      });
    } catch (error) {
      set((state) => ({ files: { ...state.files, [key]: { loading: false, error: errorMessage(error, 'Could not load this file.'), data: null, at: Date.now() } } }));
    }
    return get().files[key];
  },

  // ------------------------------------------------------------ conversation

  /** Start a repository's first history page and overview before its workspace code has loaded. */
  prefetchRepo: (repoName) => {
    if (!repoName || historyPrefetch?.repoName === repoName) return;
    const promise = getConversation(repoName, { params: { limit: FIRST_HISTORY_PAGE_SIZE } });
    // A failed prefetch is not an error: selectRepo simply asks again.
    promise.catch(() => {});
    historyPrefetch = { repoName, promise, at: Date.now() };
    void get().loadOverview(repoName);
  },

  selectRepo: async (repoName) => {
    if (get().selectedRepo === repoName && (get().messages.length || get().isHistoryLoading)) return;
    const prefetched = historyPrefetch?.repoName === repoName && Date.now() - historyPrefetch.at < HISTORY_PREFETCH_TTL_MS
      ? historyPrefetch.promise : null;
    historyPrefetch = null;
    historyController?.abort();
    queryController?.abort();
    historyController = null;
    queryController = null;
    const closedSources = { open: false, messageId: null, activeIndex: null, file: null, stack: [] };
    if (!repoName) {
      set((state) => ({ selectedRepo: null, messages: [], isHistoryLoading: false, historyError: '', historyHasMore: false, historyCursor: null, isLoadingOlder: false, isQuerying: false, queryEpoch: state.queryEpoch + 1, sources: closedSources }));
      return;
    }
    set((state) => ({ selectedRepo: repoName, messages: [], isHistoryLoading: true, historyError: '', historyHasMore: false, historyCursor: null, isLoadingOlder: false, isQuerying: false, queryEpoch: state.queryEpoch + 1, sources: closedSources }));
    const controller = new AbortController();
    historyController = controller;
    try {
      const response = (prefetched && await prefetched.catch(() => null))
        || await getConversation(repoName, { signal: controller.signal, params: { limit: FIRST_HISTORY_PAGE_SIZE } });
      if (get().selectedRepo === repoName && historyController === controller) {
        set({
          messages: (response.data.messages || []).map(normalizeMessage),
          isHistoryLoading: false,
          historyError: '',
          historyHasMore: Boolean(response.data.has_more),
          historyCursor: response.data.next_before || null,
        });
      }
    } catch (error) {
      if (error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError') return;
      if (historyController === controller && get().selectedRepo === repoName) {
        // A repository that is still indexing has no conversation yet.
        set({ isHistoryLoading: false, historyError: error?.response?.status === 404 ? '' : errorMessage(error, 'Conversation history could not be loaded.') });
      }
    } finally {
      if (historyController === controller) historyController = null;
    }
  },

  /** Prepend the next older page of the selected repository's history. */
  loadOlderMessages: async () => {
    const { selectedRepo: repoName, historyHasMore, historyCursor, isLoadingOlder, isHistoryLoading } = get();
    if (!repoName || !historyHasMore || !historyCursor || isLoadingOlder || isHistoryLoading) return;
    set({ isLoadingOlder: true });
    try {
      const response = await getConversation(repoName, { params: { limit: HISTORY_PAGE_SIZE, before: historyCursor } });
      const older = (response.data.messages || []).map(normalizeMessage);
      // The cursor moves only once the whole page is in, so a page cut short
      // (another repository, a cleared chat) is never skipped.
      const current = () => get().selectedRepo === repoName && get().historyCursor === historyCursor;
      for (let end = older.length; end > 0; end -= OLDER_BATCH_SIZE) {
        if (!current()) return;
        const batch = older.slice(Math.max(0, end - OLDER_BATCH_SIZE), end);
        set((state) => {
          const known = new Set(state.messages.map((message) => message.id));
          return { messages: [...batch.filter((message) => !known.has(message.id)), ...state.messages] };
        });
        if (end > OLDER_BATCH_SIZE) await afterNextPaint();
      }
      if (!current()) return;
      set({ historyHasMore: Boolean(response.data.has_more), historyCursor: response.data.next_before || null });
    } catch (error) {
      if (get().selectedRepo === repoName) set({ historyError: errorMessage(error, 'Earlier messages could not be loaded.') });
    } finally {
      if (get().selectedRepo === repoName) set({ isLoadingOlder: false });
    }
  },

  retryHistory: () => {
    const repo = get().selectedRepo;
    set({ selectedRepo: null });
    void get().selectRepo(repo);
  },

  clearChat: async () => {
    const repo = get().selectedRepo;
    if (!repo) return;
    queryController?.abort();
    await clearConversation(repo);
    set((state) => ({ messages: [], historyHasMore: false, historyCursor: null, isQuerying: false, queryEpoch: state.queryEpoch + 1, sources: { open: false, messageId: null, activeIndex: null, file: null, stack: [] } }));
  },

  stopQuery: () => {
    queryController?.abort();
  },

  setComposerDraft: (text, mode = null) => set({ composerDraft: { text: String(text || ''), mode, nonce: uid() } }),

  askQuestion: async (question, { modelProfile = 'fast', workflow = 'general' } = {}) => {
    const repo = get().selectedRepo;
    const text = String(question || '').trim();
    if (!repo || !text || get().isQuerying) return;
    queryController?.abort();
    const controller = new AbortController();
    queryController = controller;
    const epoch = get().queryEpoch;
    const assistantId = uid();
    const now = new Date().toISOString();
    const startedAt = Date.now();
    set((state) => {
      // Asking the same question again right after it failed or was stopped
      // before any text replaces that attempt instead of stacking a second
      // copy of the question under an error.
      let previous = state.messages;
      const last = previous[previous.length - 1];
      const before = previous[previous.length - 2];
      const failedAttempt = last?.role === 'assistant' && !last.streaming
        && (last.mode === 'error' || (last.stopped && !last.partial));
      if (failedAttempt && before?.role === 'user' && before.content.trim() === text) previous = previous.slice(0, -2);
      return {
        isQuerying: true,
        messages: [
          ...previous,
          { id: uid(), role: 'user', content: text, created_at: now, citations: [], tool_calls: [], live: true },
          {
            id: assistantId, role: 'assistant', content: '', streaming: true, created_at: now, workflow, model_profile: modelProfile,
            started_at: startedAt,
            stage: { stage: 'retrieving', detail: workflow === 'editing' ? 'Finding the code to change' : 'Searching the indexed code' },
            citations: [], tool_calls: [],
          },
        ],
      };
    });

    const isCurrent = (state) => state.selectedRepo === repo && state.queryEpoch === epoch;
    const update = (patch) => set((state) => (isCurrent(state) ? {
      messages: state.messages.map((message) => (message.id === assistantId
        ? { ...message, ...(typeof patch === 'function' ? patch(message) : patch) }
        : message)),
    } : {}));

    let pending = '';
    let flushTimer = null;
    let firstToken = true;
    const flush = () => {
      flushTimer = null;
      if (!pending) return;
      const chunk = pending;
      pending = '';
      update((message) => ({ content: message.content + chunk, stage: null }));
    };
    let finished = false;
    const finish = (data) => {
      if (flushTimer) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      pending = '';
      finished = true;
      const normalized = normalizeMessage({ ...data, id: assistantId, role: 'assistant', content: data.answer, created_at: now });
      update(() => normalized);
    };
    const payload = { repo_name: repo, question: text, model_profile: modelProfile, workflow };

    try {
      let received = 0;
      try {
        await streamQuery(payload, {
          signal: controller.signal,
          onEvent: (event, data) => {
            received += 1;
            if (event === 'stage') update({ stage: data });
            else if (event === 'citations') update({ citations: data.citations || [], evidence_plan: data.evidence_plan, snapshot: data.snapshot, citations_at: Date.now() });
            else if (event === 'token') {
              pending += data.text || '';
              if (firstToken && pending) {
                // Show the first words at once; later tokens batch every 40 ms.
                firstToken = false;
                update({ first_token_at: Date.now() });
                flush();
              } else if (!flushTimer) flushTimer = setTimeout(flush, 40);
            } else if (event === 'reset') {
              // The text streamed so far was model reasoning; the answer follows.
              if (flushTimer) {
                clearTimeout(flushTimer);
                flushTimer = null;
              }
              pending = '';
              update({ content: '' });
            } else if (event === 'done') finish(data);
            else if (event === 'error') throw new ApiError(data.detail || 'The answer could not be completed.', data.status || 500);
          },
        });
      } catch (error) {
        if (!(error instanceof StreamUnavailableError) || received > 0) throw error;
        // The connection cannot stream (proxy, old browser): use JSON.
        update({ stage: { stage: 'generating', detail: 'Writing the answer — it appears all at once on this connection' } });
        const response = await queryRepo(repo, text, modelProfile, workflow, { signal: controller.signal });
        finish(response.data);
      }
      if (!finished) throw new ApiError('The answer stream ended before the answer was complete.');
    } catch (error) {
      if (flushTimer) clearTimeout(flushTimer);
      flush();
      if (error?.name === 'AbortError' || error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError') {
        update((message) => ({
          streaming: false, stage: null, stopped: true, partial: Boolean(message.content),
          content: message.content || '_Stopped before an answer was written._',
        }));
      } else {
        const detail = errorMessage(error, 'This question could not be completed. Your repository and conversation are unchanged — please try again.');
        const status = Number(error?.status || error?.response?.status) || 0;
        // A shared repository's access may have changed (removed, or now a viewer).
        if (status === 403 || status === 404) void get().fetchRepos();
        // A partial answer stays readable; the bubble shows the failure as a
        // notice with a retry instead of mixing it into the answer text.
        update((message) => ({
          streaming: false, stage: null, mode: message.content ? message.mode : 'error',
          content: message.content || detail,
          partial: Boolean(message.content), interrupted: Boolean(message.content),
          error: detail, error_status: status,
        }));
      }
    } finally {
      if (queryController === controller) queryController = null;
      set((state) => (isCurrent(state) ? { isQuerying: false } : {}));
    }
  },

  /**
   * Writes the rest of a reply that stopped at the output limit (mode
   * "truncated") into the same message; the server stores the joined answer.
   * The reply shows its kept text and then the new text as it streams. A
   * failure or Stop puts the stored answer back and says why
   * (`continue_error`), so the reply is never left half-replaced.
   *
   * @param {string} messageId the reply's id in `messages`
   * @param {{ modelProfile?: 'fast' | 'detailed' }} [options]
   */
  continueAnswer: async (messageId, { modelProfile } = {}) => {
    const repo = get().selectedRepo;
    const index = get().messages.findIndex((item) => item.id === messageId);
    const message = get().messages[index];
    if (!repo || !message || message.mode !== 'truncated' || !message.message_id || get().isQuerying) return;
    const question = get().messages.slice(0, index).reverse().find((item) => item.role === 'user')?.content || 'Continue the answer';
    queryController?.abort();
    const controller = new AbortController();
    queryController = controller;
    const epoch = get().queryEpoch;
    const original = { content: message.content, mode: message.mode };
    const isCurrent = (state) => state.selectedRepo === repo && state.queryEpoch === epoch;
    const update = (patch) => set((state) => (isCurrent(state) ? {
      messages: state.messages.map((item) => (item.id === messageId
        ? { ...item, ...(typeof patch === 'function' ? patch(item) : patch) }
        : item)),
    } : {}));
    set({ isQuerying: true });
    update({ streaming: true, continuing: true, continue_error: null, stage: { stage: 'generating', detail: 'Writing the rest of the answer' } });

    let pending = '';
    let flushTimer = null;
    const flush = () => {
      flushTimer = null;
      if (!pending) return;
      const chunk = pending;
      pending = '';
      update((item) => ({ content: item.content + chunk }));
    };
    let finished = false;
    const finish = (data) => {
      if (flushTimer) clearTimeout(flushTimer);
      flushTimer = null;
      pending = '';
      finished = true;
      update(() => ({
        ...normalizeMessage({ ...data, id: messageId, role: 'assistant', content: data.answer, created_at: message.created_at }),
        continuing: false,
      }));
    };
    const payload = {
      repo_name: repo,
      question,
      model_profile: modelProfile || (message.model_profile === 'detailed' ? 'detailed' : 'fast'),
      workflow: message.workflow && message.workflow !== 'editing' ? message.workflow : 'general',
      continue_from: message.message_id,
    };
    try {
      let received = 0;
      try {
        await streamQuery(payload, {
          signal: controller.signal,
          onEvent: (event, data) => {
            received += 1;
            if (event === 'continue') {
              // The kept text (the unfinished last sentence is rewritten).
              if (flushTimer) clearTimeout(flushTimer);
              flushTimer = null;
              pending = '';
              update({ content: data.base || '', stage: null });
            } else if (event === 'token') {
              pending += data.text || '';
              if (!flushTimer) flushTimer = setTimeout(flush, 40);
            } else if (event === 'stage') update({ stage: data });
            else if (event === 'done') finish(data);
            else if (event === 'error') throw new ApiError(data.detail || 'The rest of the answer could not be written.', data.status || 500);
          },
        });
      } catch (error) {
        if (!(error instanceof StreamUnavailableError) || received > 0) throw error;
        const response = await continueAnswerRequest(payload, { signal: controller.signal });
        finish(response.data);
      }
      if (!finished) throw new ApiError('The connection closed before the rest of the answer was written.');
    } catch (error) {
      if (flushTimer) clearTimeout(flushTimer);
      const stopped = error?.name === 'AbortError' || error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError';
      update({
        ...original, streaming: false, continuing: false, stage: null,
        continue_error: stopped
          ? 'Stopped. The answer is unchanged.'
          : errorMessage(error, 'The rest of the answer could not be written. The answer so far is unchanged.'),
      });
    } finally {
      if (queryController === controller) queryController = null;
      set((state) => (isCurrent(state) ? { isQuerying: false } : {}));
    }
  },

  // ---------------------------------------------------------------- sources

  // Opening a file while the panel is open remembers the current view, so
  // "Back" returns to the previous file or to the source list.
  openFile: (file, messageId = null) => set((state) => {
    const current = state.sources;
    const nextMessageId = messageId ?? current.messageId;
    const same = current.file && file && current.file.path === file.path && current.file.start === file.start && current.file.end === file.end;
    const stack = current.open && !same
      ? [...(current.stack || []), { file: current.file, messageId: current.messageId }].slice(-20)
      : (current.open ? current.stack || [] : []);
    return { sources: { open: true, messageId: nextMessageId, activeIndex: null, file, stack } };
  }),

  closeSources: () => set({ sources: { open: false, messageId: null, activeIndex: null, file: null, stack: [] } }),

  // Lines selected in the open file (a line-number click, Go to line). The
  // workspace URL follows them, so a copied link reopens those lines.
  setSourcesSelection: (selection) => set((state) => {
    if (!state.sources.file) return state;
    const next = selection?.start ? { start: selection.start, end: selection.end || selection.start } : null;
    const current = state.sources.selection || null;
    if (current?.start === next?.start && current?.end === next?.end) return state;
    return { sources: { ...state.sources, selection: next } };
  }),

  backToSources: () => set((state) => {
    const stack = [...(state.sources.stack || [])];
    const previous = stack.pop();
    if (!previous) return { sources: { ...state.sources, file: null, selection: null, activeIndex: null, stack: [] } };
    return { sources: { ...state.sources, file: previous.file, selection: null, messageId: previous.messageId ?? state.sources.messageId, activeIndex: null, stack } };
  }),

  // ------------------------------------------------------------------ review

  openReview: async (messageId) => {
    const message = get().messages.find((item) => item.id === messageId);
    if (!message) return;
    const fresh = message.edit_suggestion && message.edit_ticket && message.ticket_issued_at && Date.now() - message.ticket_issued_at < TICKET_MAX_AGE_MS;
    if (fresh) {
      set({ review: { messageId, proposal: message.edit_suggestion, ticket: message.edit_ticket, repoName: get().selectedRepo, loading: false, error: '' } });
      return;
    }
    const proposalId = message.proposal_id || message.edit_suggestion?.id;
    if (!proposalId) return;
    set({ review: { messageId, proposal: message.edit_suggestion || null, ticket: null, repoName: get().selectedRepo, loading: true, error: '' } });
    try {
      const response = await getEditProposal(proposalId);
      const { proposal, edit_ticket: ticket, repo_name: repoName } = response.data;
      set((state) => ({
        review: state.review?.messageId === messageId ? { messageId, proposal, ticket, repoName, loading: false, error: '' } : state.review,
        messages: state.messages.map((item) => (item.id === messageId
          ? { ...item, edit_suggestion: proposal, edit_ticket: ticket, ticket_issued_at: Date.now(), proposal_id: proposal.id }
          : item)),
      }));
    } catch (error) {
      set((state) => ({ review: state.review ? { ...state.review, loading: false, error: errorMessage(error, 'This proposal could not be loaded.') } : null }));
    }
  },

  closeReview: () => set({ review: null }),

  /**
   * Loads a stored reply's proposal (messages from history carry only its
   * id) onto the message, like openReview does, without opening the review:
   * the proposal's card note, Tool Chips, Artifact Panel diff and patch then
   * fill in. Resolves once loaded; rejects with the request's error.
   *
   * @param {string} messageId
   */
  loadProposal: async (messageId) => {
    const message = get().messages.find((item) => item.id === messageId);
    const proposalId = message?.proposal_id;
    if (!message || message.edit_suggestion || !proposalId) return;
    if (!proposalLoads.has(proposalId)) {
      const request = getEditProposal(proposalId).finally(() => proposalLoads.delete(proposalId));
      proposalLoads.set(proposalId, request);
    }
    const response = await proposalLoads.get(proposalId);
    const { proposal, edit_ticket: ticket } = response.data;
    set((state) => ({
      messages: state.messages.map((item) => (item.id === messageId && !item.edit_suggestion
        ? { ...item, edit_suggestion: proposal, edit_ticket: ticket, ticket_issued_at: Date.now(), proposal_id: proposal.id }
        : item)),
    }));
  },

  // ------------------------------------------------------------- account

  fetchAccountUsage: async ({ silent = false } = {}) => {
    const userId = get().user?.id;
    if (!userId) return;
    const revision = ++usageRevision;
    if (!silent) set({ accountUsageLoading: true, accountUsageError: '' });
    try {
      const response = await getAccountUsage();
      if (revision !== usageRevision || get().user?.id !== userId) return;
      set({ accountUsage: response.data, accountUsageLoading: false, accountUsageError: '' });
    } catch (error) {
      if (revision !== usageRevision || get().user?.id !== userId) return;
      // A background refresh never replaces a known-good value with an error.
      set((state) => ({ accountUsageLoading: false, accountUsageError: state.accountUsage ? '' : errorMessage(error, 'Usage information is temporarily unavailable.') }));
    }
  },

  checkGithub: async () => {
    try {
      const response = await getGithubStatus();
      const github = { connected: Boolean(response.data?.connected), username: response.data?.github_username || '', updatedAt: response.data?.updated_at || null, checked: true };
      set({ github });
      return github;
    } catch {
      set({ github: { connected: false, username: '', checked: true } });
      return null;
    }
  },

  setGithub: (github) => set({ github: { checked: true, ...github } }),
}));

export default useStore;
