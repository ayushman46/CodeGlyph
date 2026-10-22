import {
  AlertTriangle, Check, CheckCircle2, Copy, Download, ExternalLink, FileDiff, FilePlus2, GitBranch, GitCommitHorizontal, GitFork,
  GitPullRequest, Info, LayoutDashboard, MessageSquareText, MoreHorizontal, Pencil, PieChart, RefreshCw, Square, SquarePen, Tag,
  Trash2, X,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import useMediaQuery from '../../hooks/useMediaQuery';
import { errorMessage } from '../../api/client';
import { parseRepository } from '../../lib/citations';
import { formatCount, shortSha } from '../../lib/format';
import { isKeywordOnly, phaseLabel, statusTone } from '../../lib/phase';
import { downloadText, proposalPatch, slugify } from '../../lib/patch';
import { approximatePaths } from '../../lib/proposal';
import useArtifacts, {
  PROPOSAL_PANEL_ID, artifactCardId, proposalArtifactStatus, proposalArtifactSummary,
} from '../../store/useArtifacts';
import useStore from '../../store/useStore';
import { ArtifactPanel } from '../ai/ArtifactPanel';
import { LoadingState } from '../ai/LoadingState';
import { MessageScroller } from '../ai/MessageScroller';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Menu from '../ui/Menu';
import Composer from './Composer';
import DiffView from './DiffView';
import { REVIEW_LABEL, isEditRun } from './EditProposalCard';
import IngestionProgress, { IndexingLoadingState, useIndexingEta } from './IngestionProgress';
import MessageBubble from './MessageBubble';
import ProposalNotices from './ProposalNotices';
import RepoOverview from './RepoOverview';
import SourcesPanel from './SourcesPanel';

const TABS = [['chat', 'Chat', MessageSquareText], ['overview', 'Overview', LayoutDashboard]];

const exportMarkdown = (repo, messages) => {
  const lines = [`# ${repo.full_name || repo.repo_name}`, '', `Exported from Codebase Intel on ${new Date().toLocaleString()}.`, ''];
  messages.forEach((message) => {
    lines.push(message.role === 'user' ? `## Q: ${message.content}` : message.content, '');
    if (message.role === 'assistant' && message.citations?.length) {
      lines.push('Sources:');
      message.citations.forEach((citation) => lines.push(`- ${citation.file_path}:L${citation.start_line}-L${citation.end_line}${citation.github_url ? ` (${citation.github_url})` : ''}`));
      lines.push('');
    }
  });
  return lines.join('\n');
};

/** Likely cause and fix for a failed index, from the worker's message. */
const failureAdvice = (message = '') => {
  if (/not found|404|private|not visible/i.test(message)) return 'Check that the repository exists and is public. Private repositories are not supported.';
  if (/quota|indexed-source|is full|limit/i.test(message)) return 'Delete a repository you no longer need to free space, or see the plans for a larger limit.';
  if (/no supported|no readable/i.test(message)) return 'The repository has no text source files that can be indexed (only binaries or unsupported formats).';
  if (/rate limit|timed out|timeout|unavailable|try again/i.test(message)) return 'This is usually temporary. Wait a moment and try again.';
  return 'This is often temporary. Try again; if it keeps failing, check the repository on GitHub.';
};

/** Dismissed notices survive tab switches and reloads for this session. */
const NOTICE_KEY = 'ci-dismissed-notices';
const readDismissed = () => {
  try {
    return new Set(JSON.parse(window.sessionStorage.getItem(NOTICE_KEY) || '[]'));
  } catch {
    return new Set();
  }
};
const rememberDismissed = (key) => {
  try {
    const all = readDismissed();
    all.add(key);
    window.sessionStorage.setItem(NOTICE_KEY, JSON.stringify([...all].slice(-50)));
  } catch {
    // Storage may be unavailable; the notice simply returns next time.
  }
};

const Notice = ({ tone = 'info', icon: Icon = Info, children, action, onDismiss, live = false }) => {
  const tones = {
    info: 'border-line bg-surface-2/70 text-ink-2',
    success: 'border-success/20 bg-success-soft text-ink',
    warning: 'border-warning/25 bg-warning-soft text-ink',
    danger: 'border-danger/20 bg-danger-soft text-danger',
  };
  const iconTone = { info: 'text-ink-3', success: 'text-success', warning: 'text-warning', danger: 'text-danger' };
  return (
    <div className={`flex shrink-0 items-start gap-2.5 border-b px-4 py-2 text-[12.5px] leading-relaxed sm:px-6 ${tones[tone]}`} role={live ? 'status' : undefined}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${iconTone[tone]}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">{children}</div>
      {action}
      {onDismiss && (
        <button type="button" onClick={onDismiss} className="-mr-1 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink" aria-label="Dismiss">
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
};

/** Failed or stopped first index: what happened and what to do next. */
const IndexingProblem = ({ repo, onRetry, retrying, onDelete }) => {
  const failed = repo.status === 'failed';
  const repository = parseRepository(repo.github_url);
  const detail = failed
    ? repo.message || 'The indexing worker could not finish this repository.'
    : 'Nothing was made searchable. Start again whenever you are ready; it fetches the latest commit.';
  return (
    <section className={`rounded-2xl border p-5 sm:p-6 ${failed ? 'border-danger/30 bg-surface' : 'border-line bg-surface'}`} aria-labelledby="indexing-problem-title">
      <div className="flex items-start gap-3">
        <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${failed ? 'bg-danger-soft text-danger' : 'bg-surface-2 text-ink-3'}`}>
          {failed ? <AlertTriangle className="h-4.5 w-4.5" aria-hidden="true" /> : <Square className="h-4 w-4" aria-hidden="true" />}
        </span>
        <div className="min-w-0">
          <h2 id="indexing-problem-title" className="text-[15px] font-semibold text-ink">{failed ? 'Indexing failed' : 'Indexing was stopped'}</h2>
          <p className="mt-1 text-sm leading-relaxed text-ink-2">{detail}</p>
          {failed && <p className="mt-2 text-[13px] leading-relaxed text-ink-3">{failureAdvice(repo.message)}</p>}
        </div>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2 sm:pl-12">
        <Button variant="primary" size="sm" icon={RefreshCw} loading={retrying} onClick={onRetry}>{failed ? 'Try again' : 'Start indexing again'}</Button>
        {repository && (
          <Button variant="secondary" size="sm" href={`https://github.com/${repository.fullName}`} target="_blank" rel="noreferrer" iconRight={ExternalLink}>Open on GitHub</Button>
        )}
        <Button variant="danger-ghost" size="sm" icon={Trash2} onClick={onDelete}>Delete</Button>
      </div>
    </section>
  );
};

/**
 * An Edit & PR reply's proposal as an Artifact Panel: the multi-file diff,
 * with "Review & open PR" (the existing review dialog, opened exactly as the
 * inline proposal card opens it) and Copy patch in the header. Status
 * follows the reply: streaming while it is written, error when it finished
 * without a proposal.
 */
const ProposalArtifact = ({ message, openedBy, onClose, onRetry }) => {
  const openReview = useStore((state) => state.openReview);
  const loadProposal = useStore((state) => state.loadProposal);
  const status = proposalArtifactStatus(message);
  const { title, description } = proposalArtifactSummary(message);
  const proposal = message.edit_suggestion;
  const files = proposal?.files || [];
  const approximate = approximatePaths(proposal);
  const canCopy = status === 'complete' && files.length > 0 && files.every((file) => file.after != null);
  const [copied, setCopied] = useState(false);
  // A reply from history carries only its proposal's id: load the proposal
  // (its diff, counts and patch) when the panel shows it.
  const needsProposal = status === 'complete' && !proposal && Boolean(message.proposal_id);
  const [loadError, setLoadError] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);
  const panelRef = useRef(null);
  // Try again leaves with the error row (the Loading State takes its place):
  // keep keyboard focus in the panel instead of letting it fall to <body>.
  const retryLoad = () => {
    panelRef.current?.focus({ preventScroll: true });
    setLoadAttempt((count) => count + 1);
  };
  useEffect(() => {
    if (!needsProposal) return undefined;
    let current = true;
    setLoadError('');
    loadProposal(message.id).catch((error) => {
      if (current) setLoadError(errorMessage(error, 'The diff could not be loaded.'));
    });
    return () => { current = false; };
  }, [needsProposal, message.id, loadProposal, loadAttempt]);
  useEffect(() => {
    if (!copied) return undefined;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const copyPatch = async () => {
    try {
      await navigator.clipboard.writeText(proposalPatch(proposal));
      setCopied(true);
    } catch {
      // Clipboard can be unavailable; the review dialog can still download it.
    }
  };

  // A failed run never gets a proposal, so it has no review to open; a
  // streaming one shows it disabled until the proposal arrives.
  const actions = (
    <>
      {status !== 'error' && (
        <Button variant="link" size="sm" disabled={status !== 'complete'} title={REVIEW_LABEL} onClick={() => openReview(message.id)}>
          <GitPullRequest aria-hidden="true" />
          <span className="max-sm:sr-only">{REVIEW_LABEL}</span>
        </Button>
      )}
      {canCopy && (
        <Button variant="link" size="icon-sm" aria-label={copied ? 'Copied patch' : 'Copy patch'} title="Copy patch" onClick={copyPatch}>
          {copied ? <Check className="text-success" aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </Button>
      )}
    </>
  );

  // The error row names why there is no change, like the reply's card.
  let errorLabel;
  if (status === 'error' && !message.stopped && !message.error) {
    if (message.mode === 'editing') errorLabel = 'No safe patch was produced.';
    else if (message.mode === 'retrieval_fallback') errorLabel = 'The model was unavailable, so no change was written.';
  }

  let body;
  if (status === 'streaming') {
    // The description carries the live stage.
    body = <p className="m-0 text-ink-2">The diff appears here once every hunk is validated.</p>;
  } else if (status === 'error') {
    body = (
      <p className="m-0 text-ink-2">
        {message.stopped
          ? 'Stopped before a change was written.'
          : message.error || 'The reply in the conversation explains why.'}
      </p>
    );
  } else if (needsProposal) {
    body = loadError ? (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1" role="alert">
        <p className="m-0 min-w-0 flex-1 basis-48 text-ink-2">{loadError}</p>
        <Button variant="link" size="sm" className="shrink-0" onClick={retryLoad}>Try again</Button>
      </div>
    ) : (
      <LoadingState label="Loading the diff" pattern="dots" showElapsed={false} />
    );
  } else if (!files.length) {
    body = <p className="m-0 text-ink-2">The diff loads with the review. Choose {REVIEW_LABEL} to see it.</p>;
  } else {
    body = (
      <div className="flex flex-col gap-4">
        <ProposalNotices proposal={proposal} />
        {files.map((file) => {
          const created = file.action === 'create';
          const Icon = created ? FilePlus2 : FileDiff;
          return (
            <div key={file.file_path} className="flex min-w-0 flex-col gap-1.5">
              <div className="flex min-w-0 items-center gap-2">
                <Icon className={`size-3.5 shrink-0 ${created ? 'text-success' : 'text-ink-3'}`} aria-hidden="true" />
                <p className="m-0 min-w-0 flex-1 truncate font-mono text-[11px] leading-4 text-ink-2" title={file.file_path}>{file.file_path}</p>
                {created && <Badge tone="success">New</Badge>}
                {approximate.has(file.file_path) && <span title="Reconstructed from the index; refresh from GitHub before pushing."><Badge tone="info">Approximate</Badge></span>}
                {/* The diff's own stats bar shows the counts; a file too large to diff here shows them in its row. */}
                {file.after == null && (
                  <span className="shrink-0 font-mono text-[11px] leading-4">
                    <span className="text-success">+{file.additions ?? 0}</span>{' '}
                    <span className="text-danger-strong">−{file.deletions ?? 0}</span>
                  </span>
                )}
              </div>
              {file.summary && <p className="m-0 text-ink-2">{file.summary}</p>}
              {file.after != null ? (
                <DiffView before={created ? '' : file.before} after={file.after} />
              ) : (
                <p className="m-0 rounded-xl border border-line bg-surface-2 p-3 text-ink-2">This file is large; open the review to load the full diff.</p>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <ArtifactPanel
      ref={panelRef}
      id={PROPOSAL_PANEL_ID}
      title={title}
      description={description}
      status={status}
      actions={actions}
      onClose={onClose}
      onRetry={status === 'error' ? onRetry : undefined}
      autoFocus={openedBy !== 'reply'}
      labels={errorLabel ? { error: errorLabel } : undefined}
      className="[&>[data-slot=artifact-panel-body]]:[scrollbar-width:thin] [&>[data-slot=artifact-panel-body]]:[scrollbar-color:var(--color-line-strong)_transparent]"
    >
      {body}
    </ArtifactPanel>
  );
};

/** Start loading the previous history page this far from the top. */
const OLDER_HISTORY_THRESHOLD_PX = 600;

const RepoWorkspace = ({ repo, onRequestDialog }) => {
  const messages = useStore((state) => state.messages);
  const isHistoryLoading = useStore((state) => state.isHistoryLoading);
  const historyError = useStore((state) => state.historyError);
  const retryHistory = useStore((state) => state.retryHistory);
  const historyHasMore = useStore((state) => state.historyHasMore);
  const isLoadingOlder = useStore((state) => state.isLoadingOlder);
  const loadOlderMessages = useStore((state) => state.loadOlderMessages);
  const isQuerying = useStore((state) => state.isQuerying);
  const askQuestion = useStore((state) => state.askQuestion);
  const stopQuery = useStore((state) => state.stopQuery);
  const clearChat = useStore((state) => state.clearChat);
  const sources = useStore((state) => state.sources);
  const sourcesOpen = sources.open;
  const closeSources = useStore((state) => state.closeSources);
  const artifact = useArtifacts((state) => state.artifact);
  const openProposal = useArtifacts((state) => state.openProposal);
  const closeArtifact = useArtifacts((state) => state.close);
  const reindexRepo = useStore((state) => state.reindexRepo);
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get('tab') === 'overview' ? 'overview' : 'chat';
  const [actionError, setActionError] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [finished, setFinished] = useState(false);
  const [dismissed, setDismissed] = useState(readDismissed);
  const scrollRef = useRef(null);
  const viewportRef = useRef(null);
  const composerRef = useRef(null);
  const tabRefs = useRef({});
  const [pinRequest, setPinRequest] = useState(0);
  // The last reply's paced text is still appearing (it can outlast the
  // request): the log stays busy and following stays instant until it is final.
  const [revealing, setRevealing] = useState(false);
  const repository = parseRepository(repo.github_url);
  const eta = useIndexingEta(repo);
  const notice = location.state?.notice || '';

  // The tab lives in the URL (?tab=overview) so it survives reloads and can be linked.
  const setTab = (next) => {
    if (next === tab) return;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      if (next === 'overview') params.set('tab', 'overview');
      else params.delete('tab');
      return params;
    }, { replace: true, state: location.state });
  };

  useEffect(() => {
    setActionError('');
    setFinished(false);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [repo.id]);

  // The Message Scroller follows the conversation while the reader is at
  // its end. Asking a question returns there from anywhere, so the new
  // question and its answer are in view (and following resumes).
  useEffect(() => {
    const node = viewportRef.current;
    if (pinRequest && node) node.scrollTop = node.scrollHeight;
  }, [pinRequest]);
  // Whether the transcript follows new output (reported by the scroller,
  // which starts following each time it mounts).
  const following = useRef(true);
  const onFollowChange = useCallback((next) => { following.current = next; }, []);
  const showTranscript = tab === 'chat' && repo.queryable && !isHistoryLoading && messages.length > 0;

  // Older history loads a page at a time when the reader nears the top. The
  // scroll position is kept on the message the reader was looking at, so the
  // prepended page appears above without moving the view.
  // The transcript's height before the next batch of older messages lands.
  const olderAnchor = useRef(null);
  const requestOlder = useCallback(() => {
    const node = viewportRef.current;
    if (!node || !historyHasMore || isLoadingOlder) return;
    olderAnchor.current = node.scrollHeight;
    void loadOlderMessages();
  }, [historyHasMore, isLoadingOlder, loadOlderMessages]);
  const onTranscriptScroll = useCallback((event) => {
    if (event.currentTarget.scrollTop < OLDER_HISTORY_THRESHOLD_PX) requestOlder();
  }, [requestOlder]);
  const firstMessageId = messages[0]?.id;
  // A page arrives in batches above the reader. Content added above leaves
  // scrollTop as it was (scroll anchoring is off), so scrollTop is wherever
  // the reader is now, even mid-scroll: moving it down by the height added
  // keeps the same lines in view without undoing the reader's own scrolling.
  useLayoutEffect(() => {
    const height = olderAnchor.current;
    const node = viewportRef.current;
    if (height === null || !node) return;
    node.scrollTop += node.scrollHeight - height;
    olderAnchor.current = isLoadingOlder ? node.scrollHeight : null;
  }, [firstMessageId, isLoadingOlder]);
  // A short first page may not fill the screen, so there is nothing to
  // scroll: load the next page straight away.
  useEffect(() => {
    const node = viewportRef.current;
    if (showTranscript && node && historyHasMore && !isLoadingOlder && node.scrollHeight <= node.clientHeight + 8) requestOlder();
  }, [showTranscript, historyHasMore, isLoadingOlder, firstMessageId, requestOlder]);
  useEffect(() => {
    if (showTranscript) following.current = true;
  }, [showTranscript]);
  // Progress, empty and overview states always open at the top.
  useEffect(() => {
    const node = scrollRef.current;
    if (node && tab === 'overview') node.scrollTop = 0;
  }, [tab]);

  // Screen readers hear when an answer finishes or fails (the stage
  // indicator's live region unmounts as soon as text starts streaming), and
  // when indexing finishes, fails, stops or makes keyword search ready (each
  // step's progress is announced by the indexing Loading State's status).
  const [announcement, setAnnouncement] = useState('');
  const lastAssistant = [...messages].reverse().find((message) => message.role === 'assistant');
  const wasStreaming = useRef(false);
  useEffect(() => {
    const streaming = Boolean(lastAssistant?.streaming);
    if (wasStreaming.current && !streaming && lastAssistant) {
      if (lastAssistant.mode === 'error' || lastAssistant.error) setAnnouncement(`The answer failed: ${lastAssistant.error || lastAssistant.content}`);
      else if (lastAssistant.stopped) setAnnouncement('Stopped.');
      else setAnnouncement(lastAssistant.proposal_id || lastAssistant.edit_suggestion ? 'The proposed change is ready to review.' : 'Answer ready.');
    }
    wasStreaming.current = streaming;
  }, [lastAssistant?.streaming, lastAssistant?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const previous = useRef(null);
  useEffect(() => {
    const before = previous.current;
    previous.current = { id: repo.id, status: repo.status, queryable: repo.queryable, indexing: repo.is_indexing };
    if (!before || before.id !== repo.id) return;
    if (before.indexing && !repo.is_indexing) {
      if (repo.status === 'failed') setAnnouncement(`Indexing ${repo.repo_name} failed. ${repo.message || ''}`);
      else if (repo.status === 'cancelled') setAnnouncement(`Indexing ${repo.repo_name} stopped.`);
      else {
        setAnnouncement(`${repo.repo_name} is fully indexed.`);
        setFinished(true);
      }
    } else if (!before.queryable && repo.queryable && repo.is_indexing) {
      setAnnouncement('Keyword search is ready. You can ask questions now while the semantic index finishes.');
    }
  }, [repo.id, repo.status, repo.queryable, repo.is_indexing]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!finished) return undefined;
    const timer = window.setTimeout(() => setFinished(false), 15000);
    return () => window.clearTimeout(timer);
  }, [finished]);

  // One artifact slot beside the conversation: the sources panel (a file
  // opened from a citation, or the source list) or a reply's proposal.
  // Docked where the conversation keeps a readable width beside it and the
  // sidebar (from 1280px); on narrower screens it covers the conversation,
  // which is then inert and hidden from assistive technology.
  const isLarge = useMediaQuery('(min-width: 1280px)');
  const proposalMessage = artifact?.kind === 'proposal' ? messages.find((message) => message.id === artifact.messageId) || null : null;
  const slot = proposalMessage ? 'proposal' : sourcesOpen ? 'sources' : null;
  const covered = Boolean(slot) && !isLarge;
  const slotRef = useRef(null);

  // Remember the control outside the slot that last opened or changed the
  // sources panel (a citation, a source chip), so closing returns focus to
  // it. Clicks count too: Safari does not focus a clicked button.
  const lastActivated = useRef(null);
  const sourcesOpener = useRef(null);
  useEffect(() => {
    const remember = (event) => {
      const target = event.target instanceof Element
        ? event.target.closest('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])')
        : null;
      if (target && !slotRef.current?.contains(target)) lastActivated.current = target;
    };
    document.addEventListener('focusin', remember, true);
    document.addEventListener('click', remember, true);
    return () => {
      document.removeEventListener('focusin', remember, true);
      document.removeEventListener('click', remember, true);
    };
  }, []);
  useLayoutEffect(() => {
    if (sources.open && lastActivated.current) sourcesOpener.current = lastActivated.current;
  }, [sources]);

  // Docking or undocking the panel changes the transcript's width (and its
  // height a lot): a reader following the conversation stays at its end at
  // once, instead of a long smooth scroll that new output could outrun.
  const docked = Boolean(slot) && isLarge;
  useLayoutEffect(() => {
    const node = viewportRef.current;
    if (node && following.current) node.scrollTop = node.scrollHeight;
  }, [docked]);

  // Closing the panel (Close, or anything else that leaves focus
  // nowhere) returns focus to what opened it, else to the transcript.
  const previousSlot = useRef(slot);
  useEffect(() => {
    const before = previousSlot.current;
    previousSlot.current = slot;
    if (!before || slot) return;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const returnId = useArtifacts.getState().returnFocusId;
    const target = before === 'proposal' ? (returnId ? document.getElementById(returnId) : null) : sourcesOpener.current;
    (target?.isConnected ? target : viewportRef.current)?.focus();
  }, [slot]);

  // Close renders first (so a narrow layout un-inerts the conversation),
  // then returns focus to the card that opened the proposal.
  const closeProposal = () => {
    const returnId = useArtifacts.getState().returnFocusId;
    flushSync(() => closeArtifact());
    const card = returnId ? document.getElementById(returnId) : null;
    if (card) card.focus();
  };

  const ask = (question, options) => {
    setPinRequest((count) => count + 1);
    setTab('chat');
    void askQuestion(question, options);
  };
  // A stable `ask` for the (memoized) replies, e.g. their follow-ups.
  const askRef = useRef(ask);
  askRef.current = ask;
  const askFromReply = useCallback((question, options) => askRef.current(question, options), []);

  const lastUser = [...messages].reverse().find((message) => message.role === 'user');
  const lastMessage = messages[messages.length - 1];
  const regenerate = lastUser && lastMessage?.role === 'assistant' && !isQuerying
    // A stored reply carries only its mode (or proposal), not the workflow.
    ? () => {
      ask(lastUser.content, {
        workflow: lastMessage.workflow || (isEditRun(lastMessage) ? 'editing' : 'general'),
        modelProfile: lastMessage.model_profile || 'fast',
      });
      // The reply's Try again leaves with it (it is no longer the last
      // reply): keep keyboard focus in the conversation, not on <body>.
      window.requestAnimationFrame(() => {
        const focused = document.activeElement;
        if (!focused || focused === document.body) viewportRef.current?.focus({ preventScroll: true });
      });
    }
    : null;
  // Try again on a failed proposal asks again and shows the new attempt in
  // the panel (the retry may replace the failed reply).
  const retryProposal = regenerate && proposalMessage && proposalMessage.id === lastMessage?.id
    ? () => {
      regenerate();
      const next = useStore.getState().messages.at(-1);
      if (next?.role === 'assistant') openProposal(next.id, artifactCardId(next.id), { openedBy: 'person' });
    }
    : undefined;

  const runAction = async (action, fallback) => {
    setActionError('');
    try {
      await action();
    } catch (error) {
      setActionError(errorMessage(error, fallback));
    }
  };

  const retry = async () => {
    setRetrying(true);
    await runAction(() => reindexRepo(repo), 'Could not queue a re-index.');
    setRetrying(false);
  };

  const dismissNotice = (key) => {
    rememberDismissed(key);
    setDismissed(readDismissed());
  };
  const clearNavigationNotice = () => navigate(`${location.pathname}${location.search}`, { replace: true, state: null });

  const onTabKeyDown = (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = TABS.findIndex(([value]) => value === tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
    setTab(TABS[next][0]);
    tabRefs.current[TABS[next][0]]?.focus();
  };

  const notQueryable = !repo.queryable;
  const problem = notQueryable && (repo.status === 'failed' || repo.status === 'cancelled');
  const stopping = Boolean(repo.is_indexing && repo.message?.startsWith('Stopping'));
  const semantic = Math.max(0, Math.min(100, Number(repo.semantic_progress) || 0));
  const keywordOnly = isKeywordOnly(repo);
  // A finished repository can carry a note from its last run (a stopped or
  // failed refresh, a budget summary, a semantic-search warning).
  const readyNote = !repo.is_indexing && repo.queryable && repo.message ? repo.message : '';
  const readyNoteKey = `${repo.id}:${readyNote}`;
  const readyNoteWarning = /unavailable|fail|timed out|warning|stopped|could not/i.test(readyNote);

  const problemPanel = (
    <IndexingProblem repo={repo} onRetry={retry} retrying={retrying} onDelete={() => onRequestDialog('delete', repo)} />
  );
  const progressPanel = (
    <IngestionProgress repo={repo} onStop={repo.is_indexing ? () => onRequestDialog('stop', repo) : null} stopping={stopping} />
  );

  // A conversation renders in the Message Scroller; progress, loading,
  // empty and overview states keep the plain scroll container.
  const tabPanelProps = { id: 'workspace-panel', role: 'tabpanel', 'aria-labelledby': `workspace-tab-${tab}` };
  const historyAlert = historyError ? (
    <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[13px] text-danger" role="alert">
      <span>{historyError}</span>
      <button type="button" onClick={retryHistory} className="shrink-0 font-semibold underline underline-offset-2">Retry</button>
    </div>
  ) : null;

  return (
    <div className="relative flex h-full min-h-0">
      <div
        className="flex min-w-0 flex-1 flex-col"
        aria-hidden={covered ? 'true' : undefined}
        inert={covered ? '' : undefined}
      >
        {/* Header: one row of controls; status and links underneath. */}
        <div className="shrink-0 border-b border-line bg-canvas px-4 py-2.5 sm:px-6 sm:py-3">
          <div className="flex items-center gap-2 sm:gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <h1 className="truncate text-[15px] font-semibold tracking-tight text-ink" title={repo.repo_name}>{repo.repo_name}</h1>
                <span className="hidden sm:inline-flex"><Badge tone={statusTone(repo)} dot>{phaseLabel(repo)}</Badge></span>
              </div>
              <div className="mt-0.5 flex min-w-0 items-center gap-x-3 text-[12px] text-ink-3">
                <Badge tone={statusTone(repo)} dot className="sm:hidden">{phaseLabel(repo)}</Badge>
                {repository && (
                  <a href={`https://github.com/${repository.fullName}`} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-1 hover:text-ink" title={`Open ${repository.fullName} on GitHub`}>
                    <span className="truncate">{repository.fullName}</span> <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" /><span className="sr-only">(opens GitHub)</span>
                  </a>
                )}
                {repo.ref && (
                  <span className="hidden min-w-0 items-center gap-1 font-mono sm:inline-flex" title={`Indexed ${repo.ref_kind || 'ref'} ${repo.ref}`}>
                    {repo.ref_kind === 'tag' ? <Tag className="h-3.5 w-3.5 shrink-0" aria-hidden="true" /> : <GitBranch className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
                    <span className="truncate">{repo.ref}</span>
                  </span>
                )}
                {repo.commit_sha && (
                  <span className="hidden shrink-0 items-center gap-1 font-mono md:inline-flex" title={`Indexed commit ${repo.commit_sha}`}>
                    <GitCommitHorizontal className="h-3.5 w-3.5" aria-hidden="true" /> {shortSha(repo.commit_sha)}
                  </span>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1 sm:gap-1.5">
              <div className="flex rounded-xl border border-line bg-surface-2 p-0.5" role="tablist" aria-label="Workspace view" onKeyDown={onTabKeyDown}>
                {TABS.map(([value, label, Icon]) => (
                  <button
                    key={value}
                    ref={(node) => { tabRefs.current[value] = node; }}
                    id={`workspace-tab-${value}`}
                    type="button"
                    role="tab"
                    aria-selected={tab === value}
                    aria-controls="workspace-panel"
                    tabIndex={tab === value ? 0 : -1}
                    onClick={() => setTab(value)}
                    className={`inline-flex h-7 items-center gap-1.5 rounded-[10px] px-2.5 text-[12px] font-medium transition-colors ${tab === value ? 'bg-surface text-ink shadow-sm' : 'text-ink-3 hover:text-ink'}`}
                  >
                    <Icon className="hidden h-3.5 w-3.5 sm:block" aria-hidden="true" /> {label}
                  </button>
                ))}
              </div>
              <Button size="icon" variant="ghost" icon={SquarePen} aria-label="New chat" title="New chat" disabled={!messages.length || isQuerying} onClick={() => runAction(clearChat, 'Could not clear the conversation.')} />
              <Menu
                label="Repository actions"
                trigger={({ toggle, props }) => (
                  <button type="button" onClick={toggle} {...props} className="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink">
                    <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
                items={[
                  { label: 'Export conversation', icon: Download, disabled: !messages.length, onSelect: () => downloadText(`${slugify(repo.repo_name)}-conversation.md`, exportMarkdown(repo, messages), 'text/markdown') },
                  repo.is_indexing
                    ? { label: 'Stop indexing…', icon: Square, disabled: stopping, onSelect: () => onRequestDialog('stop', repo) }
                    : { label: 'Re-index…', icon: RefreshCw, onSelect: () => onRequestDialog('reindex', repo) },
                  { separator: true },
                  { label: 'Coverage report', icon: PieChart, onSelect: () => onRequestDialog('coverage', repo) },
                  { label: 'Change impact', icon: GitFork, disabled: notQueryable, onSelect: () => onRequestDialog('impact', repo) },
                  { label: 'Rename', icon: Pencil, onSelect: () => onRequestDialog('rename', repo) },
                  { label: 'Delete repository', icon: Trash2, danger: true, onSelect: () => onRequestDialog('delete', repo) },
                ]}
              />
            </div>
          </div>
        </div>

        {repo.is_indexing && repo.queryable && (
          <div className="flex shrink-0 items-start gap-3 border-b border-line bg-surface-2/70 px-4 py-2 text-ink-2 sm:px-6">
            {/* Indexing continues after keyword search is ready: the same Loading State as the indexing card, one detail line. */}
            <IndexingLoadingState repo={repo} eta={eta} brief className="min-w-0 flex-1" />
            <button type="button" onClick={() => onRequestDialog('stop', repo)} disabled={stopping} className="-my-0.5 shrink-0 rounded-md px-2 py-1 text-[12.5px] font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50" aria-label={`Stop indexing ${repo.repo_name}`}>
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
          </div>
        )}
        {finished && repo.queryable && !repo.is_indexing && (
          <Notice tone="success" icon={CheckCircle2} onDismiss={() => setFinished(false)}>
            <span className="font-medium">{repo.repo_name} is fully indexed.</span> {keywordOnly ? 'Keyword search is on; semantic search is unavailable for this snapshot.' : `${formatCount(repo.indexed_files)} files are searchable by keyword and by meaning.`}
          </Notice>
        )}
        {notice && (
          <Notice icon={Info} onDismiss={clearNavigationNotice} live>{notice}</Notice>
        )}
        {readyNote && !dismissed.has(readyNoteKey) && (
          <Notice
            tone={readyNoteWarning ? 'warning' : 'info'}
            icon={readyNoteWarning ? AlertTriangle : Info}
            onDismiss={() => dismissNotice(readyNoteKey)}
            action={readyNoteWarning ? <Button size="xs" variant="secondary" icon={RefreshCw} onClick={() => onRequestDialog('reindex', repo)}>Re-index</Button> : null}
          >
            {readyNote}
          </Notice>
        )}
        {actionError && (
          <Notice tone="danger" icon={AlertTriangle} onDismiss={() => setActionError('')}>
            <span role="alert">{actionError}</span>
          </Notice>
        )}

        {/* Body */}
        {showTranscript ? (
          <MessageScroller
            {...tabPanelProps}
            className="min-h-0 flex-1"
            label={`Conversation about ${repo.repo_name}`}
            busy={isQuerying || revealing}
            navigation="rail"
            onFollowChange={onFollowChange}
            viewportRef={viewportRef}
            viewportProps={{ onScroll: onTranscriptScroll }}
            contentClassName="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6"
          >
            {historyAlert}
            {historyHasMore && (
              // One height for the button and the loading line: the swap
              // happens above the reader and must not nudge the view.
              <div className="mb-6 flex h-8 items-center justify-center">
                {isLoadingOlder ? (
                  <LoadingState label="Loading earlier messages" showElapsed={false} />
                ) : (
                  <Button variant="ghost" size="sm" onClick={requestOlder}>Load earlier messages</Button>
                )}
              </div>
            )}
            <div className="space-y-7">
              {messages.map((message, index) => (
                <MessageBubble key={message.id} message={message} repo={repo} isLast={index === messages.length - 1} onRegenerate={index === messages.length - 1 ? regenerate : null} onAsk={askFromReply} onRevealChange={index === messages.length - 1 ? setRevealing : undefined} />
              ))}
            </div>
          </MessageScroller>
        ) : (
          <div
            ref={scrollRef}
            {...tabPanelProps}
            className="scroll-thin min-h-0 flex-1 overflow-y-auto"
          >
            <div className={`mx-auto w-full px-4 py-6 sm:px-6 ${tab === 'overview' ? 'max-w-4xl' : 'max-w-3xl'}`}>
              {problem ? problemPanel : tab === 'overview' ? (
                notQueryable ? progressPanel : (
                  <RepoOverview
                    repo={repo}
                    onAsk={(question) => ask(question, { workflow: 'general', modelProfile: 'fast' })}
                    onOpenCoverage={() => onRequestDialog('coverage', repo)}
                  />
                )
              ) : notQueryable ? progressPanel : isHistoryLoading ? (
                <div className="space-y-6 py-4" aria-busy="true">
                  <span className="sr-only" role="status">Loading conversation</span>
                  {[0, 1].map((index) => (
                    <div key={index} className="space-y-2.5" aria-hidden="true">
                      <div className="skeleton ml-auto h-9 w-2/5 rounded-2xl" />
                      <div className="skeleton h-4 w-11/12" />
                      <div className="skeleton h-4 w-4/5" />
                      <div className="skeleton h-4 w-3/5" />
                    </div>
                  ))}
                </div>
              ) : (
                <>
                  {historyAlert}
                  {messages.length === 0 && (
                    <div className="animate-rise py-2 sm:py-4">
                      <p className="eyebrow">Ask anything</p>
                      <h2 className="mt-2 text-xl font-semibold tracking-tight text-ink sm:text-2xl">What do you want to know about {repo.repo_name}?</h2>
                      <p className="mt-2 text-sm leading-relaxed text-ink-3">
                        {repo.is_indexing && !repo.refreshing
                          ? <>Keyword search is ready, so you can ask now. The semantic index is {semantic}% built; when it finishes, answers get better at finding code by meaning.</>
                          : <>Answers cite exact files and lines. Switch to <span className="font-medium text-ink-2">Edit &amp; PR</span> to have a change written, validated and turned into a pull request.</>}
                      </p>
                      <div className="mt-6">
                        <RepoOverview
                          repo={repo}
                          compact
                          onAsk={(question) => ask(question, { workflow: 'general', modelProfile: 'fast' })}
                          onShowOverview={() => setTab('overview')}
                        />
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}

        <div className="shrink-0 border-t border-line bg-canvas px-3 pb-3 pt-3 sm:px-6">
          <Composer
            ref={composerRef}
            busy={isQuerying}
            disabled={notQueryable || isHistoryLoading}
            disabledReason={repo.status === 'failed' ? 'Indexing failed. Try again above to ask questions.' : repo.status === 'cancelled' && notQueryable ? 'Indexing was stopped. Start it again to ask questions.' : undefined}
            onSubmit={ask}
            onStop={stopQuery}
          />
        </div>
      </div>

      {/* Outside the conversation column, so it is heard while a panel covers it. */}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>

      {/* Artifact slot: docked beside the conversation on large screens, over it on narrow ones (Close leads back). */}
      {slot && (
        <div
          ref={slotRef}
          className={isLarge
            ? 'flex w-[min(46vw,42rem)] shrink-0 flex-col py-3 pe-3'
            : 'absolute inset-0 z-20 flex flex-col bg-canvas p-2 sm:p-3'}
        >
          {slot === 'proposal' ? (
            <ProposalArtifact
              key={proposalMessage.id}
              message={proposalMessage}
              openedBy={artifact.openedBy}
              onClose={closeProposal}
              onRetry={retryProposal}
            />
          ) : (
            <SourcesPanel repo={repo} onClose={closeSources} autoFocus />
          )}
        </div>
      )}
    </div>
  );
};

export default RepoWorkspace;
